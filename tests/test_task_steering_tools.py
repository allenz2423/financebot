from __future__ import annotations

import json
import sqlite3

import pytest

import src.services.llm as llm
from src.db.session_store import SessionStore
from src.services.tool_receipts import ReceiptStore


def _trace_store(*, result_summary="[UNTRUSTED] Spending total: $42.00"):
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=connection)
    receipts = ReceiptStore(connection)
    receipts.ensure_schema()
    task = store.create_task_run(
        "owner", "session-a", "Review recent transactions", task_id="task-a",
        channel_id="channel-a", thread_id="thread-a",
    )
    store.begin_turn("owner", "session-a", turn_id="turn-a")
    call = store.record_tool_call(
        "owner", "session-a", tool_name="query_spending",
        arguments={"account_id": "private-account", "limit": 5},
        call_id="call-a", turn_id="turn-a", status="running",
    )
    receipts.prepare(
        receipt_id="receipt-a", call_id="call-a", user_id="owner", turn_id="turn-a",
        round_id=1, tool_name="query_spending", origin="native",
        arguments={"account_id": "private-account", "limit": 5},
    )
    receipts.start("receipt-a")
    receipts.finish(
        "receipt-a", status="confirmed", ok=True, complete=True,
        result_summary=result_summary,
    )
    step = store.add_task_step(
        "owner", task["task_id"], 0, step_id="step-a", status="succeeded",
        next_action="dispatch:query_spending", description="Inspect recent spending",
        completion_criteria="Return a confirmed spending total", tool_call_id=call["id"],
        receipt_id="receipt-a",
    )
    return store, task, step


def test_inspect_task_is_conversation_scoped_and_trace_redacts_argument_values(monkeypatch):
    store, _task, _step = _trace_store()
    store.create_task_run(
        "owner", "session-b", "Other conversation", task_id="task-b",
        channel_id="channel-a", thread_id="thread-a",
    )
    store.create_task_run(
        "other-owner", "session-a", "Other owner", task_id="task-c",
        channel_id="channel-a", thread_id="thread-a",
    )
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)

    summary = llm._inspect_durable_task(
        "owner", "task-a", session_id="session-a", channel_id="channel-a",
        thread_id="thread-a", detail_level="summary",
    )
    assert summary["steps"][0]["step_id"] == "step-a"
    assert summary["steps"][0]["status"] == "succeeded"
    assert summary["steps"][0]["evidence"] == {
        "tool_call_id": 1, "receipt_id": "receipt-a",
    }

    trace = llm._inspect_durable_task(
        "owner", "task-a", session_id="session-a", channel_id="channel-a",
        thread_id="thread-a", detail_level="trace",
    )
    step = trace["steps"][0]
    assert step["inputs"] == {
        "tool_name": "query_spending", "argument_names": ["account_id", "limit"],
    }
    assert step["outputs"]["receipt_status"] == "confirmed"
    assert "$42.00" in step["outputs"]["summary"]
    encoded = json.dumps(trace)
    assert "private-account" not in encoded
    assert "Raw tool arguments" in trace["trace_notice"]
    assert "may contain sensitive context" in trace["trace_notice"]

    for invisible in ("task-b", "task-c", "missing"):
        with pytest.raises(PermissionError, match="TASK_SCOPE_DENIED"):
            llm._inspect_durable_task(
                "owner", invisible, session_id="session-a", channel_id="channel-a",
                thread_id="thread-a", detail_level="trace",
            )


def test_task_trace_caps_sensitive_untrusted_receipt_summary(monkeypatch):
    store, _task, _step = _trace_store(result_summary="sensitive-result-" * 100)
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)

    trace = llm._inspect_durable_task(
        "owner", "task-a", session_id="session-a", channel_id="channel-a",
        thread_id="thread-a", detail_level="trace",
    )

    summary = trace["steps"][0]["outputs"]["summary"]
    assert len(summary) <= 600
    assert summary.startswith("[UNTRUSTED TOOL SUMMARY]")
    assert "sensitive-result-" in summary


def test_steering_guidance_is_projected_only_for_open_steps(monkeypatch):
    store = SessionStore(connection=sqlite3.connect(":memory:", isolation_level=None))
    task = store.create_task_run(
        "owner", "session-a", "Review recent transactions", task_id="task-a",
        channel_id="channel-a", thread_id="thread-a",
    )
    step = store.add_task_step(
        "owner", task["task_id"], 0, step_id="step-open", status="ready",
        next_action="dispatch:query_spending", description="Inspect spending",
        completion_criteria="Return a total",
    )
    store.steer_task_step(
        "owner", task["task_id"], step["step_id"], "Use the prior 30 days only.",
        expected_task_version=task["version"] + 1,
        expected_step_version=step["version"],
    )
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)

    context = llm._render_task_steering_context(
        store.task_prompt_snapshot("owner", task["task_id"]), task["task_id"]
    )
    assert "CURRENT UNTRUSTED USER GUIDANCE" in context
    assert "Use the prior 30 days only." in context
    assert "do not change the planned tool" in context
    assert "grants" in context and "approval requirements" in context
