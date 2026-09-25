import asyncio

import pytest

from src.agent.context import ContextBlock, ContextBundle
from src.agent.events import MessageEvent, TurnRequest
from src.agent.runtime import AgentRuntime, RuntimeHooks


def test_turn_request_default_key_is_channel_scoped():
    request = TurnRequest(
        MessageEvent(user_id="u1", text="hello", channel_id="c1", thread_id="t1")
    )
    assert request.resolved_session_key() == "discord:u1:c1:t1"


def test_context_bundle_orders_tiers_and_bounds_output():
    bundle = ContextBundle.from_blocks(
        [
            ContextBlock("volatile", "live", tier="volatile"),
            ContextBlock("stable", "rules", tier="stable"),
            ContextBlock("context", "memory", tier="context"),
        ],
        max_chars=40,
    )
    rendered = bundle.render()
    assert rendered.index("[STABLE:stable]") < rendered.index("[CONTEXT:context]")
    assert len(rendered) <= 60  # includes the bounded truncation marker.


@pytest.mark.asyncio
async def test_runtime_emits_terminal_event_and_preserves_runner_contract():
    events = []

    async def runner(prompt, user_id, reply_msg, **kwargs):
        assert prompt == "hello"
        assert user_id == "u1"
        return "done"

    async def emit(event):
        events.append(event.kind)

    request = TurnRequest(MessageEvent(user_id="u1", text="hello"))
    result = await AgentRuntime(runner, RuntimeHooks(emit=emit)).run(request)
    assert result == "done"
    assert events == ["turn_started", "turn_completed"]
