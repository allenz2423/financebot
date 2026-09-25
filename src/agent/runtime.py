"""Small platform-neutral runtime facade for the existing Delilah loop.

This is intentionally an adapter boundary first. The legacy advisor remains
the execution engine while callers migrate to normalized requests/events. The
boundary makes cancellation, persistence, and delivery hooks replaceable
without importing Discord into the future core runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
import asyncio
import contextvars
import time
import uuid

from .events import AgentEvent, TurnRequest


LegacyRunner = Callable[..., Awaitable[str]]
EventSink = Callable[[AgentEvent], Awaitable[None] | None]

CURRENT_TURN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "delilah_current_turn_id", default=None
)
CURRENT_TASK_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "delilah_current_task_id", default=None
)
CURRENT_SESSION_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "delilah_current_session_key", default=None
)
CURRENT_CHANNEL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "delilah_current_channel_id", default=None
)
CURRENT_THREAD_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "delilah_current_thread_id", default=None
)


@dataclass
class RuntimeHooks:
    """Optional integration hooks; failures in observability never break a turn."""

    emit: EventSink | None = None
    before_run: Callable[[TurnRequest, str], Awaitable[None] | None] | None = None
    after_run: Callable[[TurnRequest, str, str, float], Awaitable[None] | None] | None = None
    on_error: Callable[[TurnRequest, str, BaseException, float], Awaitable[None] | None] | None = None


async def _maybe_call(callback, *args) -> Any:
    if callback is None:
        return None
    result = callback(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result


@dataclass
class AgentRuntime:
    """Run a turn through a platform-neutral facade.

    ``runner`` is injected to preserve the current behavior while the large
    legacy loop is decomposed. It receives the same ``reply_msg`` and image
    arguments used by the existing advisor implementation.
    """

    runner: LegacyRunner
    hooks: RuntimeHooks = field(default_factory=RuntimeHooks)

    async def run(self, request: TurnRequest, *, reply_msg: Any = None, image_b64_list=None) -> str:
        turn_id = f"turn_{uuid.uuid4().hex}"
        started = time.monotonic()
        turn_token = CURRENT_TURN_ID.set(turn_id)
        task_token = CURRENT_TASK_ID.set(None)
        session_token = CURRENT_SESSION_KEY.set(request.resolved_session_key())
        channel_token = CURRENT_CHANNEL_ID.set(request.message.channel_id)
        thread_token = CURRENT_THREAD_ID.set(request.message.thread_id)
        try:
            task_id = await _maybe_call(self.hooks.before_run, request, turn_id)
            if task_id:
                CURRENT_TASK_ID.set(str(task_id))
            await _maybe_call(
                self.hooks.emit,
                AgentEvent("turn_started", request.request_id, turn_id, {"session_key": request.resolved_session_key()}),
            )
            result = await self.runner(
                request.message.text,
                request.message.user_id,
                reply_msg,
                image_b64_list=image_b64_list,
                required_tools=set(request.required_tools) or None,
            )
        except asyncio.CancelledError:
            try:
                await _maybe_call(
                    self.hooks.on_error,
                    request,
                    turn_id,
                    asyncio.CancelledError(),
                    time.monotonic() - started,
                )
                await _maybe_call(
                    self.hooks.emit,
                    AgentEvent("turn_cancelled", request.request_id, turn_id),
                )
            finally:
                CURRENT_TURN_ID.reset(turn_token)
                CURRENT_TASK_ID.reset(task_token)
                CURRENT_SESSION_KEY.reset(session_token)
                CURRENT_CHANNEL_ID.reset(channel_token)
                CURRENT_THREAD_ID.reset(thread_token)
            raise
        except Exception as exc:
            try:
                await _maybe_call(
                    self.hooks.on_error,
                    request,
                    turn_id,
                    exc,
                    time.monotonic() - started,
                )
                await _maybe_call(
                    self.hooks.emit,
                    AgentEvent(
                        "turn_failed",
                        request.request_id,
                        turn_id,
                        {"error_type": type(exc).__name__},
                    ),
                )
            finally:
                CURRENT_TURN_ID.reset(turn_token)
                CURRENT_TASK_ID.reset(task_token)
                CURRENT_SESSION_KEY.reset(session_token)
                CURRENT_CHANNEL_ID.reset(channel_token)
                CURRENT_THREAD_ID.reset(thread_token)
            raise
        try:
            duration = time.monotonic() - started
            await _maybe_call(self.hooks.after_run, request, turn_id, result or "", duration)
            await _maybe_call(
                self.hooks.emit,
                AgentEvent(
                    "turn_completed",
                    request.request_id,
                    turn_id,
                    {"duration_seconds": round(duration, 3), "output_chars": len(result or "")},
                ),
            )
            return result
        finally:
            CURRENT_TURN_ID.reset(turn_token)
            CURRENT_TASK_ID.reset(task_token)
            CURRENT_SESSION_KEY.reset(session_token)
            CURRENT_CHANNEL_ID.reset(channel_token)
            CURRENT_THREAD_ID.reset(thread_token)


__all__ = [
    "AgentRuntime",
    "CURRENT_CHANNEL_ID",
    "CURRENT_SESSION_KEY",
    "CURRENT_TURN_ID",
    "CURRENT_THREAD_ID",
    "CURRENT_TASK_ID",
    "RuntimeHooks",
]
