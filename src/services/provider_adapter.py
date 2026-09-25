"""Normalized provider request/event helpers.

The rest of Delilah should not need to know whether a provider emitted an
OpenAI SSE line or an Ollama JSON line.  This module is intentionally a thin
adapter over ``provider_protocol``: existing callers can keep using
``parse_stream_line`` while new runtime code can consume stable request and
event objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Mapping, Protocol

from src.services.provider_protocol import normalize_native_tool_call, parse_stream_line


class ProviderEventKind(str, Enum):
    TEXT = "text"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    FINISH = "finish"
    USAGE = "usage"
    EMPTY = "empty"
    ERROR = "error"


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class ProviderRequest:
    """Provider-neutral completion request.

    Tuples and copied mappings make request objects safe to pass between
    retries/observers without an adapter mutating the caller's prompt.
    """

    provider: str
    model: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    request_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.provider).strip():
            raise ValueError("provider request requires a provider")
        if not str(self.model).strip():
            raise ValueError("provider request requires a model")
        if not self.messages:
            raise ValueError("provider request requires at least one message")
        if self.max_tokens is not None and int(self.max_tokens) <= 0:
            raise ValueError("max_tokens must be positive")
        if self.temperature is not None and float(self.temperature) < 0:
            raise ValueError("temperature cannot be negative")

    @classmethod
    def from_values(
        cls,
        *,
        provider: str,
        model: str,
        messages: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        tools: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
        **kwargs: Any,
    ) -> "ProviderRequest":
        return cls(
            provider=str(provider).strip().casefold(),
            model=str(model).strip(),
            messages=tuple(dict(message) for message in messages),
            tools=tuple(dict(tool) for tool in tools),
            metadata=_copy_mapping(kwargs.pop("metadata", None)),
            **kwargs,
        )

    def as_payload(self) -> dict[str, Any]:
        """Build the common OpenAI-compatible payload without provider I/O."""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in self.messages],
            "stream": self.stream,
        }
        if self.tools:
            payload["tools"] = [dict(tool) for tool in self.tools]
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        return payload


@dataclass(frozen=True)
class ProviderEvent:
    """One normalized provider event, suitable for logging or turn reducers."""

    provider: str
    kind: ProviderEventKind
    request_id: str | None = None
    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    finish_reason: str | None = None
    done: bool = False
    usage: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


class ProviderAdapter(Protocol):
    """Minimal interface a concrete HTTP/provider transport can implement."""

    def build_request(self, request: ProviderRequest) -> Mapping[str, Any]:
        ...

    def parse_line(self, line: str, *, request_id: str | None = None) -> tuple[ProviderEvent, ...]:
        ...


def _event_from_delta(
    *,
    provider: str,
    delta: Mapping[str, Any],
    finish_reason: str | None,
    done: bool,
    request_id: str | None,
) -> tuple[ProviderEvent, ...]:
    events: list[ProviderEvent] = []
    text = str(delta.get("content") or "")
    reasoning = str(delta.get("reasoning") or delta.get("reasoning_content") or "")
    raw_calls = delta.get("tool_calls") or []
    calls = tuple(
        normalize_native_tool_call(call)
        for call in raw_calls
        if isinstance(call, Mapping)
    )
    common = {
        "provider": provider,
        "request_id": request_id,
        "done": done,
        "raw": dict(delta),
    }
    if text:
        events.append(ProviderEvent(kind=ProviderEventKind.TEXT, text=text, **common))
    if reasoning:
        events.append(ProviderEvent(kind=ProviderEventKind.REASONING, reasoning=reasoning, **common))
    if calls:
        events.append(ProviderEvent(kind=ProviderEventKind.TOOL_CALL, tool_calls=calls, **common))
    if finish_reason or done:
        events.append(
            ProviderEvent(
                kind=ProviderEventKind.FINISH,
                finish_reason=finish_reason,
                **common,
            )
        )
    if not events:
        events.append(
            ProviderEvent(
                kind=ProviderEventKind.EMPTY,
                finish_reason=finish_reason,
                **common,
            )
        )
    return tuple(events)


def events_from_stream_line(
    line: str,
    provider: str,
    *,
    request_id: str | None = None,
) -> tuple[ProviderEvent, ...]:
    """Normalize one OpenAI SSE or Ollama JSON line into zero or more events."""

    normalized_provider = str(provider or "").strip().casefold()
    raw_line = str(line or "").strip()
    # Hermes' runtime turns a standalone provider error frame into a typed
    # stream error instead of letting it look like an empty assistant delta.
    # Keep this adapter dependency-free and preserve the existing parser for
    # ordinary frames.
    candidate = raw_line[5:].strip() if raw_line.startswith("data:") else raw_line
    if candidate and candidate != "[DONE]":
        try:
            error_frame = json.loads(candidate)
        except json.JSONDecodeError:
            error_frame = None
        if isinstance(error_frame, Mapping) and error_frame.get("type") == "error":
            detail = error_frame.get("error")
            if isinstance(detail, Mapping):
                message = detail.get("message") or detail.get("code") or "provider stream error"
            else:
                message = detail or "provider stream error"
            return (
                ProviderEvent(
                    provider=normalized_provider,
                    kind=ProviderEventKind.ERROR,
                    request_id=request_id,
                    error=str(message),
                    raw=dict(error_frame),
                ),
            )
    parsed = parse_stream_line(line, normalized_provider)
    if parsed is None:
        return ()
    return _event_from_delta(
        provider=normalized_provider,
        delta=parsed.delta,
        finish_reason=parsed.finish_reason,
        done=parsed.done,
        request_id=request_id,
    )


class CompatibleProviderAdapter:
    """Adapter for the two stream formats already supported by Delilah."""

    def __init__(self, provider: str):
        self.provider = str(provider or "").strip().casefold()
        if self.provider not in {"openai", "ollama"}:
            raise ValueError("compatible adapter supports openai or ollama")

    def build_request(self, request: ProviderRequest) -> Mapping[str, Any]:
        if request.provider != self.provider:
            raise ValueError("request provider does not match adapter")
        return request.as_payload()

    def parse_line(self, line: str, *, request_id: str | None = None) -> tuple[ProviderEvent, ...]:
        return events_from_stream_line(line, self.provider, request_id=request_id)


__all__ = [
    "CompatibleProviderAdapter",
    "ProviderAdapter",
    "ProviderEvent",
    "ProviderEventKind",
    "ProviderRequest",
    "events_from_stream_line",
]
