"""Deterministic parsing primitives for OpenAI-compatible and Ollama streams."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class ProviderStreamEvent:
    delta: dict[str, Any]
    finish_reason: str | None = None
    done: bool = False


def parse_stream_line(line: str, provider: str) -> ProviderStreamEvent | None:
    """Parse one provider stream line without interpreting ordinary prose.

    Native tool calls are returned only from the provider's structured
    tool-call fields. Text that happens to say it called a tool remains plain
    content and must go through the claim gate.
    """

    raw_line = str(line or "").strip()
    provider = str(provider or "").casefold()
    if not raw_line:
        return None

    if provider == "openai":
        if not raw_line.startswith("data:"):
            return None
        raw = raw_line[5:].strip()
        if raw == "[DONE]":
            return ProviderStreamEvent(delta={}, done=True)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        choices = data.get("choices") or []
        if not choices:
            return ProviderStreamEvent(delta={})
        choice = choices[0] or {}
        return ProviderStreamEvent(
            delta=choice.get("delta") or {},
            finish_reason=choice.get("finish_reason"),
        )

    try:
        data = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    message = data.get("message") or {}
    return ProviderStreamEvent(
        delta={
            "content": message.get("content") or "",
            "reasoning": message.get("reasoning") or message.get("reasoning_content") or "",
            "tool_calls": message.get("tool_calls") or [],
        },
        finish_reason=data.get("done_reason") or data.get("finish_reason"),
        done=bool(data.get("done")),
    )


def normalize_native_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """Normalize a provider-native call while preserving structured fields."""

    function = call.get("function") or {}
    return {
        "id": str(call.get("id") or "").strip() or None,
        "type": call.get("type") or "function",
        "function": {
            "name": str(function.get("name") or ""),
            "arguments": function.get("arguments", ""),
        },
    }


__all__ = ["ProviderStreamEvent", "normalize_native_tool_call", "parse_stream_line"]
