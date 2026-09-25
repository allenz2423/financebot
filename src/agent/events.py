"""Transport-neutral events exchanged by agent adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
import uuid


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MessageEvent:
    """A normalized inbound message, independent of Discord or another gateway."""

    user_id: str
    text: str = ""
    platform: str = "discord"
    channel_id: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    attachments: tuple[Mapping[str, Any], ...] = ()
    received_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not str(self.user_id or "").strip():
            raise ValueError("MessageEvent.user_id is required")


@dataclass(frozen=True)
class TurnRequest:
    """A request entering the agent runtime."""

    message: MessageEvent
    session_key: str | None = None
    required_tools: frozenset[str] = frozenset()
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex}")

    def resolved_session_key(self) -> str:
        if self.session_key:
            return str(self.session_key)
        message = self.message
        # Include channel/thread when available. This prevents unrelated Discord
        # conversations from sharing one implicit user-wide transcript.
        channel = message.channel_id or "channel:default"
        thread = message.thread_id or "thread:default"
        return f"{message.platform}:{message.user_id}:{channel}:{thread}"


@dataclass(frozen=True)
class AgentEvent:
    """A bounded runtime event suitable for a delivery adapter or trace sink."""

    kind: str
    request_id: str
    turn_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)


__all__ = ["AgentEvent", "MessageEvent", "TurnRequest"]
