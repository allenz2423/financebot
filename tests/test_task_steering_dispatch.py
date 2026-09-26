from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

import src.services.llm as llm
from src.agent.task_controller import TaskController
from src.db.session_store import SessionStore
from src.services.tool_receipts import ReceiptStore


class _SentMessage:
    async def edit(self, **_kwargs):
        return self

    async def delete(self):
        return None


class _Channel:
    id = "channel-1"

    async def send(self, *_args, **_kwargs):
        return _SentMessage()


class _Reply:
    channel = _Channel()


class _StreamResponse:
    status_code = 200

    def __init__(self, model_responses):
        self.model_responses = model_responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_lines(self):
        response = self.model_responses.pop(0)
        callback = response.get("before_emit")
        if callable(callback):
            callback()
        if "tool_call" in response:
            call = response["tool_call"]
            event = {
                "choices": [{
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"]),
                        },
                    }]},
                    "finish_reason": "tool_calls",
                }]
            }
        else:
            event = {
                "choices": [{
                    "delta": {"content": response.get("content", "Done.")},
                    "finish_reason": "stop",
                }]
            }
        yield "data: " + json.dumps(event)
        yield "data: [DONE]"

    async def aread(self):
        return b""

    def raise_for_status(self):
        raise AssertionError("mock provider unexpectedly returned an error")


class _MockAsyncClient:
    model_responses = []
    response_count = 0
    requests = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    @asynccontextmanager
    async def stream(self, *_args, **kwargs):
        type(self).response_count += 1
        type(self).requests.append(kwargs.get("json", {}))
        yield _StreamResponse(self.model_responses)


def _seed_open_step(store: SessionStore):
    task_id = "task-current"
    task = store.create_task_run(
        "owner", "session-1", "Apply the requested correction", task_id=task_id,
        channel_id="channel-1", thread_id=None,
    )
    TaskController(store).transition_phase(
        "owner", task_id, "planning", next_action="test_dispatch"
    )
    step = store.add_task_step(
        "owner", task_id, 0, step_id="step-1", status="ready",
        next_action="dispatch:query_spending", description="Review spending",
        completion_criteria="Summarize the requested period",
    )
    return task_id, step


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "correction", "should_steer"),
    [
        ("Can you tell me what's going on?", "Use only the past week.", False),
        (
            "Correct step step-1: Use only the past week.",
            "Use only the past week.",
            True,
        ),
    ],
)
async def test_steer_task_dispatch_requires_exact_user_correction_before_grant_or_receipt(
    monkeypatch, prompt, correction, should_steer,
):
    connection = sqlite3.connect(
        ":memory:", check_same_thread=False, isolation_level=None
    )
    store = SessionStore(connection=connection)
    receipts = ReceiptStore(connection)
    receipts.ensure_schema()
    task_id, step = _seed_open_step(store)
    turn_id = "turn-steering-dispatch"
    store.begin_turn(
        "owner", "session-1", turn_id=turn_id,
        channel_id="channel-1", thread_id=None,
    )

    tool_arguments = {
        "task_id": task_id,
        "step_id": step["step_id"],
        "correction": correction,
    }
    _MockAsyncClient.model_responses = [
        {"tool_call": {
            "id": "call-steer-1", "name": "steer_task",
            "arguments": tool_arguments,
        }},
        {"content": "The task guidance request was processed."},
    ]
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _MockAsyncClient)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENROUTER_PAID_MODEL", "openai/test-model")
    monkeypatch.setenv("ADVISOR_LIVE_PREVIEW", "0")
    monkeypatch.setattr(llm, "build_advisor_context", lambda **_kwargs: "")

    async def empty_world_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(llm, "build_semantic_world_model_context", empty_world_context)
    monkeypatch.setattr(llm, "_auto_session_recall_evidence", lambda *_args, **_kwargs: "")

    calls = {
        "steering_handler": 0, "visible_scope_check": 0,
        "issue_grant": 0, "consume_grant": 0,
    }
    visible_scope = []
    original_steer = store.steer_task_step
    original_issue_grant = llm.issue_grant
    original_consume_grant = llm.consume_grant
    original_visible_task = llm._visible_conversation_task

    def counted_steer(*args, **kwargs):
        calls["steering_handler"] += 1
        return original_steer(*args, **kwargs)

    def counted_issue_grant(*args, **kwargs):
        calls["issue_grant"] += 1
        return original_issue_grant(*args, **kwargs)

    def counted_consume_grant(*args, **kwargs):
        calls["consume_grant"] += 1
        return original_consume_grant(*args, **kwargs)

    def counted_visible_task(*args, **kwargs):
        calls["visible_scope_check"] += 1
        visible_scope.append((args, kwargs))
        return original_visible_task(*args, **kwargs)

    monkeypatch.setattr(store, "steer_task_step", counted_steer)
    monkeypatch.setattr(llm, "issue_grant", counted_issue_grant)
    monkeypatch.setattr(llm, "consume_grant", counted_consume_grant)
    monkeypatch.setattr(llm, "_visible_conversation_task", counted_visible_task)

    context_tokens = [
        (llm.CURRENT_TASK_ID, llm.CURRENT_TASK_ID.set(task_id)),
        (llm.CURRENT_SESSION_KEY, llm.CURRENT_SESSION_KEY.set("session-1")),
        (llm.CURRENT_CHANNEL_ID, llm.CURRENT_CHANNEL_ID.set("channel-1")),
        (llm.CURRENT_THREAD_ID, llm.CURRENT_THREAD_ID.set(None)),
        (llm.CURRENT_TURN_ID, llm.CURRENT_TURN_ID.set(turn_id)),
    ]
    try:
        await llm._chat_with_delilah_impl(prompt, "owner", _Reply())
    finally:
        for context_var, token in reversed(context_tokens):
            context_var.reset(token)

    task = store.get_task("owner", task_id)
    steering_events = [
        event for event in task["events"]
        if event["event_type"] == "task.step_steered"
    ]
    receipt_rows = connection.execute(
        "SELECT receipt_id, status FROM tool_receipts WHERE turn_id = ?",
        (turn_id,),
    ).fetchall()

    assert calls["steering_handler"] == (1 if should_steer else 0)
    assert calls["visible_scope_check"] == (1 if should_steer else 0)
    assert len(steering_events) == (1 if should_steer else 0)
    # steer_task is a task-control operation, not an external action receipt.
    # In particular, denied calls must not create a receipt before authorization.
    assert receipt_rows == []
    assert calls["issue_grant"] == (1 if should_steer else 0)
    assert calls["consume_grant"] == (1 if should_steer else 0)
    if should_steer:
        _scope_args, scope_kwargs = visible_scope[0]
        assert scope_kwargs == {
            "session_id": "session-1",
            "channel_id": "channel-1",
            "thread_id": None,
        }
        assert steering_events[0]["payload"]["correction"] == correction
        assert task["steps"][0]["status"] == "ready"
    else:
        assert task["steps"][0]["status"] == "ready"


@pytest.mark.asyncio
async def test_stale_task_tool_call_is_discarded_if_guidance_changes_during_generation(
    monkeypatch,
):
    connection = sqlite3.connect(
        ":memory:", check_same_thread=False, isolation_level=None
    )
    store = SessionStore(connection=connection)
    receipts = ReceiptStore(connection)
    receipts.ensure_schema()
    task_id, step = _seed_open_step(store)
    turn_id = "turn-stale-task-state"
    store.begin_turn("owner", "session-1", turn_id=turn_id,
                     channel_id="channel-1", thread_id=None)

    def concurrent_steering():
        current_task = store.get_task(
            "owner", task_id, include_steps=False, include_events=False
        )
        current_step = store.get_task_step("owner", task_id, step["step_id"])
        store.steer_task_step(
            "owner", task_id, step["step_id"], "Use only the past week.",
            expected_task_version=current_task["version"],
            expected_step_version=current_step["version"],
        )

    _MockAsyncClient.model_responses = [
        {
            "tool_call": {
                "id": "stale-query-call", "name": "query_spending",
                "arguments": {"start_date": "2026-01-01", "end_date": "2026-09-26"},
            },
            "before_emit": concurrent_steering,
        },
        {"content": "I rechecked the task state and am waiting for a safe next step."},
    ]
    _MockAsyncClient.response_count = 0
    _MockAsyncClient.requests = []
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _MockAsyncClient)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENROUTER_PAID_MODEL", "openai/test-model")
    monkeypatch.setenv("ADVISOR_LIVE_PREVIEW", "0")
    monkeypatch.setattr(llm, "build_advisor_context", lambda **_kwargs: "")

    async def empty_world_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(llm, "build_semantic_world_model_context", empty_world_context)
    monkeypatch.setattr(llm, "_auto_session_recall_evidence", lambda *_args, **_kwargs: "")
    counters = {"grant": 0, "dispatch": 0}

    def count_grant(*_args, **_kwargs):
        counters["grant"] += 1
        raise AssertionError("stale task tool call reached grant issuance")

    def count_dispatch(*_args, **_kwargs):
        counters["dispatch"] += 1
        raise AssertionError("stale task tool call reached handler dispatch")

    monkeypatch.setattr(llm, "issue_grant", count_grant)
    monkeypatch.setitem(llm.ADVISOR_TOOLS_DISPATCH, "query_spending", count_dispatch)
    context_tokens = [
        (llm.CURRENT_TASK_ID, llm.CURRENT_TASK_ID.set(task_id)),
        (llm.CURRENT_SESSION_KEY, llm.CURRENT_SESSION_KEY.set("session-1")),
        (llm.CURRENT_CHANNEL_ID, llm.CURRENT_CHANNEL_ID.set("channel-1")),
        (llm.CURRENT_THREAD_ID, llm.CURRENT_THREAD_ID.set(None)),
        (llm.CURRENT_TURN_ID, llm.CURRENT_TURN_ID.set(turn_id)),
    ]
    try:
        await llm._chat_with_delilah_impl("Summarize my recent spending", "owner", _Reply())
    finally:
        for context_var, token in reversed(context_tokens):
            context_var.reset(token)

    current = store.get_task("owner", task_id)
    assert _MockAsyncClient.response_count == 2
    assert counters == {"grant": 0, "dispatch": 0}
    assert current["steps"][0]["status"] == "ready"
    assert any(event["event_type"] == "task.step_steered" for event in current["events"])
    assert connection.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE call_key='stale-query-call'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM tool_receipts WHERE turn_id=?", (turn_id,)
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_guidance_committed_before_inference_snapshot_is_in_the_provider_prompt(
    monkeypatch,
):
    connection = sqlite3.connect(
        ":memory:", check_same_thread=False, isolation_level=None
    )
    store = SessionStore(connection=connection)
    receipts = ReceiptStore(connection)
    receipts.ensure_schema()
    task_id, step = _seed_open_step(store)
    turn_id = "turn-guidance-before-snapshot"
    store.begin_turn("owner", "session-1", turn_id=turn_id,
                     channel_id="channel-1", thread_id=None)
    original_snapshot = store.task_prompt_snapshot
    snapshot_calls = 0

    def steering_before_snapshot(user_id, requested_task_id):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            task = store.get_task(user_id, requested_task_id,
                                  include_steps=False, include_events=False)
            current_step = store.get_task_step(user_id, requested_task_id, step["step_id"])
            store.steer_task_step(
                user_id, requested_task_id, step["step_id"],
                "Limit the review to the past week.",
                expected_task_version=task["version"],
                expected_step_version=current_step["version"],
            )
        return original_snapshot(user_id, requested_task_id)

    _MockAsyncClient.model_responses = [{"content": "I will follow that scope."}]
    _MockAsyncClient.response_count = 0
    _MockAsyncClient.requests = []
    monkeypatch.setattr(store, "task_prompt_snapshot", steering_before_snapshot)
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _MockAsyncClient)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENROUTER_PAID_MODEL", "openai/test-model")
    monkeypatch.setenv("ADVISOR_LIVE_PREVIEW", "0")
    monkeypatch.setattr(llm, "build_advisor_context", lambda **_kwargs: "")

    async def empty_world_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(llm, "build_semantic_world_model_context", empty_world_context)
    monkeypatch.setattr(llm, "_auto_session_recall_evidence", lambda *_args, **_kwargs: "")
    context_tokens = [
        (llm.CURRENT_TASK_ID, llm.CURRENT_TASK_ID.set(task_id)),
        (llm.CURRENT_SESSION_KEY, llm.CURRENT_SESSION_KEY.set("session-1")),
        (llm.CURRENT_CHANNEL_ID, llm.CURRENT_CHANNEL_ID.set("channel-1")),
        (llm.CURRENT_THREAD_ID, llm.CURRENT_THREAD_ID.set(None)),
        (llm.CURRENT_TURN_ID, llm.CURRENT_TURN_ID.set(turn_id)),
    ]
    try:
        await llm._chat_with_delilah_impl("Summarize my spending", "owner", _Reply())
    finally:
        for context_var, token in reversed(context_tokens):
            context_var.reset(token)

    provider_messages = _MockAsyncClient.requests[0]["messages"]
    snapshot_messages = [
        item.get("content", "") for item in provider_messages
        if item.get("role") == "system" and "CURRENT UNTRUSTED USER GUIDANCE" in item.get("content", "")
    ]
    assert len(snapshot_messages) == 1
    assert "Limit the review to the past week." in snapshot_messages[0]
    assert snapshot_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_arguments"),
    [
        ("query_spending", {"start_date": "2026-09-19", "end_date": "2026-09-26"}),
        ("search_gmail", {"query": "newer_than:1d"}),
    ],
)
async def test_steering_after_generation_check_wins_before_dispatch_persistence(
    monkeypatch, tool_name, tool_arguments,
):
    connection = sqlite3.connect(
        ":memory:", check_same_thread=False, isolation_level=None
    )
    store = SessionStore(connection=connection)
    receipts = ReceiptStore(connection)
    receipts.ensure_schema()
    task_id, step = _seed_open_step(store)
    turn_id = "turn-guidance-after-generation"
    store.begin_turn("owner", "session-1", turn_id=turn_id,
                     channel_id="channel-1", thread_id=None)
    _MockAsyncClient.model_responses = [{
        "tool_call": {
            "id": "raced-tool-call", "name": tool_name,
            "arguments": tool_arguments,
        },
    }, {"content": "I will use the updated task scope."}]
    _MockAsyncClient.response_count = 0
    _MockAsyncClient.requests = []
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _MockAsyncClient)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENROUTER_PAID_MODEL", "openai/test-model")
    monkeypatch.setenv("ADVISOR_LIVE_PREVIEW", "0")
    monkeypatch.setattr(llm, "build_advisor_context", lambda **_kwargs: "")

    async def empty_world_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(llm, "build_semantic_world_model_context", empty_world_context)
    monkeypatch.setattr(llm, "_auto_session_recall_evidence", lambda *_args, **_kwargs: "")
    original_persist_phase = llm._persist_task_phase
    injection_count = 0

    def steer_before_dispatch_phase(user_id, requested_task_id, phase, *, next_action=None):
        nonlocal injection_count
        if requested_task_id == task_id and phase == "executing" and injection_count == 0:
            injection_count += 1
            task = store.get_task(user_id, task_id, include_steps=False, include_events=False)
            current_step = store.get_task_step(user_id, task_id, step["step_id"])
            store.steer_task_step(
                user_id, task_id, step["step_id"], "Use only the past week.",
                expected_task_version=task["version"],
                expected_step_version=current_step["version"],
            )
        return original_persist_phase(
            user_id, requested_task_id, phase, next_action=next_action
        )

    monkeypatch.setattr(llm, "_persist_task_phase", steer_before_dispatch_phase)
    counters = {"grant": 0, "dispatch": 0, "prefetch": 0}

    def count_grant(*_args, **_kwargs):
        counters["grant"] += 1
        raise AssertionError("stale tool call reached grant issuance")

    def count_dispatch(*_args, **_kwargs):
        counters["dispatch"] += 1
        raise AssertionError("stale tool call reached handler dispatch")

    async def count_prefetch(*_args, **_kwargs):
        counters["prefetch"] += 1
        return {}

    monkeypatch.setattr(llm, "issue_grant", count_grant)
    monkeypatch.setitem(llm.ADVISOR_TOOLS_DISPATCH, tool_name, count_dispatch)
    monkeypatch.setattr(llm, "_prefetch_gmail_batch", count_prefetch)
    context_tokens = [
        (llm.CURRENT_TASK_ID, llm.CURRENT_TASK_ID.set(task_id)),
        (llm.CURRENT_SESSION_KEY, llm.CURRENT_SESSION_KEY.set("session-1")),
        (llm.CURRENT_CHANNEL_ID, llm.CURRENT_CHANNEL_ID.set("channel-1")),
        (llm.CURRENT_THREAD_ID, llm.CURRENT_THREAD_ID.set(None)),
        (llm.CURRENT_TURN_ID, llm.CURRENT_TURN_ID.set(turn_id)),
    ]
    try:
        await llm._chat_with_delilah_impl("Summarize my spending", "owner", _Reply())
    finally:
        for context_var, token in reversed(context_tokens):
            context_var.reset(token)

    current = store.get_task("owner", task_id)
    assert injection_count == 1
    assert _MockAsyncClient.response_count == 2
    assert current["steps"][0]["status"] == "ready"
    assert counters == {"grant": 0, "dispatch": 0, "prefetch": 0}
    assert connection.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE call_key='raced-tool-call'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM tool_receipts WHERE turn_id=?", (turn_id,)
    ).fetchone()[0] == 0
