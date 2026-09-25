from __future__ import annotations

import sqlite3

import pytest

from src.agent.task_controller import TaskController
from src.db.session_store import SessionStore
from src.services.authorization import (
    AuthorizationRequest,
    RiskTier,
    ToolPolicy,
    build_approval_request,
    evaluate_policy,
)
from src.services.tool_receipts import ReceiptStore


@pytest.fixture
def task_env():
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=conn)
    receipts = ReceiptStore(conn)
    receipts.ensure_schema()
    conn.execute(
        "INSERT INTO monitor_rules(id,user_id,name,kind,config) VALUES (1,'owner','x','large_deposit','{}')"
    )
    controller = TaskController(store)
    store.begin_turn("owner", "session", turn_id="turn-1")
    controller.create_turn("owner", "session", "turn-1", "Create a monitor")
    call = store.record_tool_call(
        "owner", "session", tool_name="monitor_add_rule", arguments={"name": "x"},
        call_id="call-1", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="receipt-1", call_id="call-1", user_id="owner", turn_id="turn-1",
        round_id=1, tool_name="monitor_add_rule", origin="native", arguments={"name": "x"},
    )
    return store, receipts, controller, call


def test_task_step_is_linked_before_dispatch_and_confirmed_completion(task_env):
    store, receipts, controller, call = task_env
    claimed = controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="monitor_add_rule",
    )
    assert claimed["task"]["status"] == "running"
    assert claimed["step"]["status"] == "running"
    assert claimed["step"]["receipt_id"] == "receipt-1"
    receipts.start("receipt-1")
    receipts.finish(
        "receipt-1", status="confirmed", ok=True, complete=True,
        result_summary="Created monitor rule #1: x.",
    )
    task = controller.finish_step(
        "owner", "task_turn-1", claimed["step"]["step_id"], outcome="confirmed"
    )
    assert task["steps"][0]["status"] == "succeeded"
    assert store.task_requires_monitor_readback("owner", "task_turn-1")
    assert store.has_confirmed_task_call(
        "owner", "task_turn-1", tool_name="monitor_add_rule", arguments={"name": "x"}
    )
    queued = controller.finish_turn("owner", "task_turn-1")
    assert queued["status"] == "queued"
    assert queued["wait_reason"] == "monitor_readback_required"
    list_call = store.record_tool_call(
        "owner", "session", tool_name="monitor_list_rules", arguments={},
        call_id="list-call", turn_id="turn-1", status="running",
    )
    step = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_list_rules")
    controller.link_call_to_step(
        "owner", "task_turn-1", step["step_id"], tool_call_id=list_call["id"]
    )
    receipts.prepare(
        receipt_id="list-receipt", call_id="list-call", user_id="owner", turn_id="turn-1",
        round_id=2, tool_name="monitor_list_rules", origin="native", arguments={},
    )
    controller.claim_step("owner", "task_turn-1", step["step_id"], receipt_id="list-receipt")
    receipts.start("list-receipt")
    receipts.finish(
        "list-receipt", status="confirmed", ok=True, complete=True,
        result_summary="Monitor rules (1):\n- #1 [on] x (large_deposit) config={}",
    )
    controller.finish_step("owner", "task_turn-1", step["step_id"], outcome="confirmed")
    assert not store.task_requires_monitor_readback("owner", "task_turn-1")
    assert controller.finish_turn("owner", "task_turn-1")["status"] == "succeeded"


def test_turn_phases_are_durable_ordered_and_record_next_action(task_env):
    store, _receipts, controller, _call = task_env
    task_id = "task_turn-1"
    assert store.get_task("owner", task_id)["phase"] == "received"
    planned = controller.transition_phase(
        "owner", task_id, "planning", next_action="request_model_plan"
    )
    executing = controller.transition_phase(
        "owner", task_id, "executing", next_action="execute_tool_batch",
        event_payload={"call_count": 1, "tool_names": ["monitor_add_rule"],
                       "provider_call_ids": ["call-1"]},
    )
    assert planned["phase"] == "planning"
    assert executing["phase"] == "executing"
    assert executing["phase_next_action"] == "execute_tool_batch"
    event = store.get_task("owner", task_id)["events"][-1]
    assert event["event_type"] == "task.phase_transitioned"
    assert event["payload"]["tool_names"] == ["monitor_add_rule"]
    with pytest.raises(ValueError, match="invalid task phase transition"):
        controller.transition_phase("owner", task_id, "completed", next_action=None)


def test_phase_transition_uses_task_version_compare_and_swap(task_env):
    store, _receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    store.transition_task_phase(
        "owner", task_id, expected_phase="received", expected_version=task["version"],
        new_phase="planning", next_action="request_model_plan",
    )
    with pytest.raises(Exception, match="phase/version changed"):
        store.transition_task_phase(
            "owner", task_id, expected_phase="received", expected_version=task["version"],
            new_phase="planning", next_action="stale_writer",
        )


def test_phase_transition_retries_only_when_concurrent_update_kept_same_phase(task_env, monkeypatch):
    store, _receipts, controller, _call = task_env
    original = store.transition_task_phase
    raced = False

    def concurrent_task_update(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            task = store.get_task("owner", "task_turn-1", include_steps=False, include_events=False)
            store.transition_task_run(
                "owner", "task_turn-1", expected_status="queued",
                expected_version=int(task["version"]), new_status="running",
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "transition_task_phase", concurrent_task_update)
    updated = controller.transition_phase(
        "owner", "task_turn-1", "planning", next_action="request_model_plan"
    )
    assert updated["phase"] == "planning"
    assert updated["status"] == "running"


def test_restart_resume_uses_phase_and_stops_on_ambiguous_delivery(task_env):
    store, _receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="queued", wait_reason="restart_before_dispatch",
    )
    controller.transition_phase(
        "owner", task_id, "planning", next_action="request_model_plan"
    )
    controller.transition_phase(
        "owner", task_id, "executing", next_action="dispatch_provider_tool_batch"
    )
    resumed = controller.resume_ready_task("owner", task_id)
    assert resumed["phase"] == "planning"
    assert resumed["phase_next_action"] == "resume_recovered_task"

    store.transition_task_phase(
        "owner", task_id, expected_phase="planning", expected_version=resumed["version"],
        new_phase="verifying", next_action="verify_final_claims",
    )
    delivering = store.get_task("owner", task_id)
    store.transition_task_phase(
        "owner", task_id, expected_phase="verifying",
        expected_version=delivering["version"], new_phase="delivering",
        next_action="deliver_final_response",
    )
    with pytest.raises(ValueError, match="delivery outcome is ambiguous"):
        controller.resume_ready_task("owner", task_id)


def test_restart_during_delivery_becomes_partial_without_retry(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "verifying", next_action="verify_final_claims")
    controller.transition_phase("owner", task_id, "delivering", next_action="deliver_final_response")
    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "partial"}]
    persisted = store.get_task("owner", task_id)
    assert persisted["status"] == "partial"
    assert persisted["phase"] == "partial"
    assert persisted["wait_reason"] == "delivery_outcome_unknown"
    assert receipts.get("receipt-1").status == "prepared"


def test_restart_repairs_partial_delivery_phase_after_split_write(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="partial", wait_reason="delivery_outcome_unknown",
    )
    store.transition_task_phase(
        "owner", task_id, expected_phase="received", expected_version=int(task["version"]),
        new_phase="delivering", next_action="deliver_final_response",
    )
    assert store.get_task("owner", task_id)["phase"] == "delivering"
    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "partial"}]
    assert store.get_task("owner", task_id)["phase"] == "partial"


def test_restart_after_persisted_but_unstarted_tool_block_becomes_explicitly_resumable(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_only_lookup", "arguments": {"query": "x"}, "call_id": "read-call"}],
    )

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "queued"}]
    persisted = store.get_task("owner", task_id)
    assert persisted["wait_reason"] == "restart_before_dispatch"
    assert persisted["phase"] == "planning"
    assert controller.resume_ready_task("owner", task_id)["status"] == "queued"
    block = store.list_assistant_tool_call_blocks("owner", task_id)[0]
    assert block["calls"][0]["call_id"] == "read-call"
    assert block["calls"][0]["status"] == "pending"


def test_restart_with_unlinked_running_call_requires_manual_reconciliation(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "unclassified_tool", "arguments": {}, "call_id": "running-call"}],
    )
    store.record_tool_call(
        "owner", "session", tool_name="unclassified_tool", arguments={},
        call_id="running-call", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="running-receipt", call_id="running-call", user_id="owner",
        turn_id="turn-1", round_id=2, tool_name="unclassified_tool",
        origin="native", arguments={},
    )
    receipts.start("running-receipt")

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "needs_reconciliation"}]
    persisted = store.get_task("owner", task_id)
    assert persisted["wait_reason"] == "unlinked_tool_call_outcome_unknown"
    assert persisted["phase"] == "partial"


def test_recovery_checks_earlier_blocks_for_ambiguous_call_receipts(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_one", "arguments": {}, "call_id": "old-call"}],
    )
    store.record_tool_call(
        "owner", "session", tool_name="read_one", arguments={},
        call_id="old-call", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="old-receipt", call_id="old-call", user_id="owner",
        turn_id="turn-1", round_id=2, tool_name="read_one", origin="native", arguments={},
    )
    receipts.start("old-receipt")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_two", "arguments": {}, "call_id": "new-pending-call"}],
    )
    step = controller.prepare_step("owner", task_id, tool_name="monitor_add_rule")
    controller.link_call_to_step(
        "owner", task_id, step["step_id"], tool_call_id=_call["id"]
    )
    controller.transition_phase(
        "owner", task_id, "planning", next_action="request_next_model_step"
    )

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "needs_reconciliation"}]
    assert store.get_task("owner", task_id)["phase"] == "partial"


def test_recovery_ignores_confirmed_historical_block_before_active_pending_block(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_one", "arguments": {}, "call_id": "completed-old-call"}],
    )
    store.record_tool_call(
        "owner", "session", tool_name="read_one", arguments={},
        call_id="completed-old-call", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="completed-old-receipt", call_id="completed-old-call", user_id="owner",
        turn_id="turn-1", round_id=2, tool_name="read_one", origin="native", arguments={},
    )
    receipts.start("completed-old-receipt")
    receipts.finish(
        "completed-old-receipt", status="confirmed", ok=True, complete=True,
        result_summary="read complete",
    )
    store.finish_tool_call(
        "owner", "session", "completed-old-call", status="succeeded", result={"ok": True}
    )
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_two", "arguments": {}, "call_id": "active-pending-call"}],
    )

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "queued"}]
    persisted = store.get_task("owner", task_id)
    assert persisted["wait_reason"] == "restart_before_dispatch"


def test_recovery_allows_consistently_failed_historical_block(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_one", "arguments": {}, "call_id": "failed-old-call"}],
    )
    store.record_tool_call(
        "owner", "session", tool_name="read_one", arguments={},
        call_id="failed-old-call", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="failed-old-receipt", call_id="failed-old-call", user_id="owner",
        turn_id="turn-1", round_id=2, tool_name="read_one", origin="native", arguments={},
    )
    receipts.start("failed-old-receipt")
    receipts.finish(
        "failed-old-receipt", status="failed", ok=False, complete=False,
        error="read failed",
    )
    store.finish_tool_call(
        "owner", "session", "failed-old-call", status="failed", error="read failed"
    )
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_two", "arguments": {}, "call_id": "active-pending-call"}],
    )

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "queued"}]


def test_active_terminal_unlinked_block_is_not_resumed(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "read_one", "arguments": {}, "call_id": "active-completed-call"}],
    )
    store.record_tool_call(
        "owner", "session", tool_name="read_one", arguments={},
        call_id="active-completed-call", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="active-completed-receipt", call_id="active-completed-call", user_id="owner",
        turn_id="turn-1", round_id=2, tool_name="read_one", origin="native", arguments={},
    )
    receipts.start("active-completed-receipt")
    receipts.finish(
        "active-completed-receipt", status="confirmed", ok=True, complete=True,
        result_summary="done",
    )
    store.finish_tool_call(
        "owner", "session", "active-completed-call", status="succeeded", result={"ok": True}
    )

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "needs_reconciliation"}]


def test_unlinked_receipt_from_another_turn_is_not_accepted(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "unlinked_tool", "arguments": {}, "call_id": "foreign-receipt-call"}],
    )
    store.record_tool_call(
        "owner", "session", tool_name="unlinked_tool", arguments={},
        call_id="foreign-receipt-call", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="foreign-unlinked-receipt", call_id="foreign-receipt-call", user_id="owner",
        turn_id="other-turn", round_id=2, tool_name="unlinked_tool",
        origin="native", arguments={},
    )
    receipts.start("foreign-unlinked-receipt")

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "needs_reconciliation"}]


def test_recovery_repairs_needs_reconciliation_phase_after_split_write(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="needs_reconciliation", wait_reason="unlinked_tool_call_outcome_unknown",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "needs_reconciliation"}]
    persisted = store.get_task("owner", task_id)
    assert persisted["status"] == "needs_reconciliation"
    assert persisted["phase"] == "partial"


def test_recovery_repairs_successful_run_phase_after_split_write(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="succeeded",
    )
    store.transition_task_phase(
        "owner", task_id, expected_phase="received", expected_version=int(task["version"]),
        new_phase="delivering", next_action="deliver_final_response",
    )
    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "succeeded"}]
    assert store.get_task("owner", task_id)["phase"] == "completed"


def test_terminal_call_with_prepared_receipt_is_not_resumed(task_env):
    store, receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "executing", next_action="dispatch_provider_tool_batch")
    store.persist_assistant_tool_call_block(
        "owner", "session", task_id, "turn-1",
        [{"tool_name": "tool_x", "arguments": {}, "call_id": "terminal-call"}],
    )
    store.record_tool_call(
        "owner", "session", tool_name="tool_x", arguments={},
        call_id="terminal-call", turn_id="turn-1", status="running",
    )
    store.finish_tool_call("owner", "session", "terminal-call", status="failed")
    receipts.prepare(
        receipt_id="prepared-terminal-receipt", call_id="terminal-call", user_id="owner",
        turn_id="turn-1", round_id=2, tool_name="tool_x", origin="native", arguments={},
    )

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": task_id, "status": "needs_reconciliation"}]
    assert store.get_task("owner", task_id)["phase"] == "partial"


def test_turn_finalization_preserves_ambiguous_delivery_as_partial(task_env):
    store, _receipts, controller, _call = task_env
    task_id = "task_turn-1"
    task = store.get_task("owner", task_id)
    task = store.transition_task_run(
        "owner", task_id, expected_status="queued", expected_version=int(task["version"]),
        new_status="running",
    )
    controller.transition_phase("owner", task_id, "planning", next_action="request_model_plan")
    controller.transition_phase("owner", task_id, "verifying", next_action="verify_final_claims")
    controller.transition_phase("owner", task_id, "delivering", next_action="deliver_final_response")
    partial = controller.mark_delivery_unknown("owner", task_id)
    assert partial["phase"] == "partial"
    assert partial["wait_reason"] == "delivery_outcome_unknown"
    finalized = controller.finish_turn("owner", task_id)
    assert finalized["status"] == partial["status"] == "partial"
    assert finalized["wait_reason"] == "delivery_outcome_unknown"


def test_durable_plan_claims_only_the_next_exact_tool_and_reuses_task_steps(task_env):
    store, receipts, controller, call = task_env
    plan = controller.create_plan(
        "owner", "task_turn-1", [
            {
                "tool_name": "monitor_add_rule",
                "description": "Create the requested monitor rule.",
                "completion_criteria": "A confirmed receipt records the rule ID.",
            },
            {
                "tool_name": "monitor_list_rules",
                "description": "Read the new rule back.",
                "completion_criteria": "The owner-scoped rule ID and configuration match.",
            },
        ],
    )
    assert len(plan) == 2
    assert controller.has_plan("owner", "task_turn-1")
    task = store.get_task("owner", "task_turn-1")
    assert task["steps"][0]["status"] == "ready"
    assert task["steps"][0]["completion_criteria"].startswith("A confirmed receipt")

    with pytest.raises(ValueError, match="requires a different tool"):
        controller.prepare_step("owner", "task_turn-1", tool_name="monitor_list_rules")
    first = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_add_rule")
    controller.link_call_to_step(
        "owner", "task_turn-1", first["step_id"], tool_call_id=call["id"]
    )
    controller.claim_step(
        "owner", "task_turn-1", first["step_id"], receipt_id="receipt-1"
    )
    receipts.start("receipt-1")
    receipts.finish("receipt-1", status="confirmed", ok=True, complete=True)
    controller.finish_step("owner", "task_turn-1", first["step_id"], outcome="confirmed")

    second = controller.prepare_step(
        "owner", "task_turn-1", tool_name="monitor_list_rules"
    )
    assert second["step_id"] == plan[1]["step_id"]
    assert second["status"] == "pending"
    with pytest.raises(ValueError, match="different undispatched task step"):
        # A planned task cannot skip the next saved action.
        controller.prepare_step("owner", "task_turn-1", tool_name="monitor_add_rule")


def test_restart_marks_an_unfinished_saved_plan_as_explicitly_resumable(task_env):
    store, receipts, controller, _call = task_env
    controller.create_plan(
        "owner", "task_turn-1", [
            {
                "tool_name": "monitor_add_rule",
                "description": "Create a rule.",
                "completion_criteria": "A confirmed receipt exists.",
            },
            {
                "tool_name": "monitor_list_rules",
                "description": "Read back the rule.",
                "completion_criteria": "The new rule matches the intended configuration.",
            },
        ],
    )
    restarted = TaskController(SessionStore(connection=store.connection))
    assert restarted.recover_incomplete("owner", receipts) == [
        {"task_id": "task_turn-1", "status": "queued"}
    ]
    recovered = store.get_task("owner", "task_turn-1")
    assert recovered["wait_reason"] == "plan_pending"
    assert restarted.resume_ready_task("owner", "task_turn-1")["status"] == "queued"


def test_failed_planned_step_stops_later_steps(task_env):
    store, receipts, controller, call = task_env
    controller.create_plan(
        "owner", "task_turn-1", [
            {
                "tool_name": "monitor_add_rule",
                "description": "Create a rule.",
                "completion_criteria": "The matching rule receipt is confirmed.",
            },
            {
                "tool_name": "monitor_list_rules",
                "description": "Verify the rule.",
                "completion_criteria": "The owner-scoped rule exists with the requested fields.",
            },
        ],
    )
    step = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_add_rule")
    controller.link_call_to_step(
        "owner", "task_turn-1", step["step_id"], tool_call_id=call["id"]
    )
    controller.claim_step("owner", "task_turn-1", step["step_id"], receipt_id="receipt-1")
    receipts.start("receipt-1")
    receipts.finish("receipt-1", status="failed", ok=False, complete=False)
    failed = controller.finish_step(
        "owner", "task_turn-1", step["step_id"], outcome="failed"
    )
    assert failed["status"] == "failed"
    assert controller.finish_turn("owner", "task_turn-1")["status"] == "failed"
    with pytest.raises(ValueError, match="not dispatchable"):
        controller.prepare_step("owner", "task_turn-1", tool_name="monitor_list_rules")


def test_monitor_readback_must_match_owner_scoped_rule_details(task_env):
    store, receipts, controller, call = task_env
    created = controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="monitor_add_rule",
    )
    receipts.start("receipt-1")
    receipts.finish(
        "receipt-1", status="confirmed", ok=True, complete=True,
        result_summary="Created monitor rule #1: x.",
    )
    controller.finish_step("owner", "task_turn-1", created["step"]["step_id"], outcome="confirmed")
    listed = store.record_tool_call(
        "owner", "session", tool_name="monitor_list_rules", arguments={},
        call_id="bad-list-call", turn_id="turn-1", status="running",
    )
    list_step = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_list_rules")
    controller.link_call_to_step("owner", "task_turn-1", list_step["step_id"], tool_call_id=listed["id"])
    receipts.prepare(
        receipt_id="bad-list-receipt", call_id="bad-list-call", user_id="owner", turn_id="turn-1",
        round_id=2, tool_name="monitor_list_rules", origin="native", arguments={},
    )
    controller.claim_step("owner", "task_turn-1", list_step["step_id"], receipt_id="bad-list-receipt")
    receipts.start("bad-list-receipt")
    receipts.finish(
        "bad-list-receipt", status="confirmed", ok=True, complete=True,
        result_summary="Monitor rules (1):\n- #1 [on] different-name (large_deposit) config={}",
    )
    controller.finish_step("owner", "task_turn-1", list_step["step_id"], outcome="confirmed")
    assert store.task_requires_monitor_readback("owner", "task_turn-1")
    assert controller.finish_turn("owner", "task_turn-1")["status"] == "queued"


def test_unknown_outcome_is_not_retried_and_is_surfaced_once(task_env):
    store, receipts, controller, call = task_env
    claimed = controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="monitor_add_rule",
    )
    receipts.start("receipt-1")
    receipts.finish("receipt-1", status="unknown", ok=False, complete=False)
    task = controller.finish_step(
        "owner", "task_turn-1", claimed["step"]["step_id"], outcome="unknown"
    )
    assert task["status"] == "needs_reconciliation"
    assert task["steps"][0]["status"] == "needs_reconciliation"
    assert controller.surface_reconciliation_once("owner", task["task_id"])
    assert not controller.surface_reconciliation_once("owner", task["task_id"])
    with pytest.raises(ValueError, match="not dispatchable"):
        controller.prepare_step("owner", task["task_id"], tool_name="monitor_add_rule")
    with pytest.raises(ValueError, match="not dispatchable"):
        controller.prepare_step("owner", task["task_id"], tool_name="monitor_add_rule")
    with pytest.raises(ValueError, match="not dispatchable"):
        controller.prepare_step("owner", task["task_id"], tool_name="monitor_add_rule")
    assert controller.finish_turn("owner", task["task_id"])["status"] == "needs_reconciliation"


def test_restart_projects_confirmed_receipt_and_only_allows_explicit_resume(task_env):
    store, receipts, controller, call = task_env
    claimed = controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="search_gmail",
    )
    receipts.start("receipt-1")
    receipts.finish("receipt-1", status="confirmed", ok=True, complete=True)
    restarted = TaskController(SessionStore(connection=store.connection))
    result = restarted.recover_incomplete("owner", receipts)
    assert result == [{"task_id": "task_turn-1", "status": "queued"}]
    assert restarted.resume_ready_task("owner", "task_turn-1")["wait_reason"] == "restart_after_confirmed_step"
    assert store.get_task("owner", "task_turn-1")["steps"][0]["status"] == "succeeded"


def test_prepared_receipt_is_resumable_but_started_receipt_is_not(task_env):
    store, receipts, controller, call = task_env
    claimed = controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="monitor_add_rule",
    )
    result = controller.recover_incomplete("owner", receipts)
    assert result == [{"task_id": "task_turn-1", "status": "queued"}]
    assert store.get_task("owner", "task_turn-1")["steps"][0]["status"] == "pending"
    assert controller.resume_ready_task("owner", "task_turn-1")["wait_reason"] == "restart_before_dispatch"


def test_restart_recovers_crash_before_step_receipt_link(task_env):
    store, receipts, controller, call = task_env
    step = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_add_rule")
    controller.link_call_to_step(
        "owner", "task_turn-1", step["step_id"], tool_call_id=call["id"]
    )
    recovered = TaskController(SessionStore(connection=store.connection)).recover_incomplete(
        "owner", receipts
    )
    assert recovered == [{"task_id": "task_turn-1", "status": "queued"}]
    task = store.get_task("owner", "task_turn-1")
    assert task["steps"][0]["status"] == "pending"
    assert receipts.get("receipt-1").status == "failed"


def test_prepared_receipt_cleanup_failure_cannot_fail_resumable_task(task_env, monkeypatch):
    store, receipts, controller, call = task_env
    controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="monitor_add_rule",
    )
    original_finish = receipts.finish

    def fail_cleanup(receipt_id, **kwargs):
        if kwargs.get("error") == "dispatch_not_started_recovered":
            raise OSError("simulated crash-window cleanup failure")
        return original_finish(receipt_id, **kwargs)

    monkeypatch.setattr(receipts, "finish", fail_cleanup)
    recovered = TaskController(SessionStore(connection=store.connection)).recover_incomplete(
        "owner", receipts
    )
    assert recovered == [{"task_id": "task_turn-1", "status": "queued"}]
    task = store.get_task("owner", "task_turn-1")
    assert task["status"] == "queued"
    assert task["steps"][0]["status"] == "pending"
    assert receipts.get("receipt-1").status == "prepared"
    assert TaskController(store).resume_ready_task("owner", "task_turn-1")["status"] == "queued"


def test_resume_reuses_pending_step_and_finishes_it_before_success(task_env):
    store, receipts, controller, old_call = task_env
    pending = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_add_rule")
    controller.link_call_to_step(
        "owner", "task_turn-1", pending["step_id"], tool_call_id=old_call["id"]
    )
    TaskController(store).recover_incomplete("owner", receipts)
    assert controller.resume_ready_task("owner", "task_turn-1")["status"] == "queued"

    new_call = store.record_tool_call(
        "owner", "session", tool_name="monitor_add_rule", arguments={"name": "x"},
        call_id="call-2", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="receipt-2", call_id="call-2", user_id="owner", turn_id="turn-1",
        round_id=3, tool_name="monitor_add_rule", origin="native", arguments={"name": "x"},
    )
    reused = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_add_rule")
    assert reused["step_id"] == pending["step_id"]
    controller.link_call_to_step(
        "owner", "task_turn-1", reused["step_id"], tool_call_id=new_call["id"]
    )
    controller.claim_step(
        "owner", "task_turn-1", reused["step_id"], receipt_id="receipt-2"
    )
    receipts.start("receipt-2")
    receipts.finish(
        "receipt-2", status="confirmed", ok=True, complete=True,
        result_summary="Created monitor rule #2: x.",
    )
    store.connection.execute(
        "INSERT INTO monitor_rules(id,user_id,name,kind,config) VALUES (2,'owner','x','large_deposit','{}')"
    )
    controller.finish_step(
        "owner", "task_turn-1", reused["step_id"], outcome="confirmed"
    )
    assert controller.finish_turn("owner", "task_turn-1")["status"] == "queued"
    list_call = store.record_tool_call(
        "owner", "session", tool_name="monitor_list_rules", arguments={},
        call_id="list-call-resume", turn_id="turn-1", status="running",
    )
    list_step = controller.prepare_step("owner", "task_turn-1", tool_name="monitor_list_rules")
    controller.link_call_to_step(
        "owner", "task_turn-1", list_step["step_id"], tool_call_id=list_call["id"]
    )
    receipts.prepare(
        receipt_id="list-receipt-resume", call_id="list-call-resume", user_id="owner",
        turn_id="turn-1", round_id=4, tool_name="monitor_list_rules", origin="native", arguments={},
    )
    controller.claim_step(
        "owner", "task_turn-1", list_step["step_id"], receipt_id="list-receipt-resume"
    )
    receipts.start("list-receipt-resume")
    receipts.finish(
        "list-receipt-resume", status="confirmed", ok=True, complete=True,
        result_summary="Monitor rules (1):\n- #2 [on] x (large_deposit) config={}",
    )
    controller.finish_step("owner", "task_turn-1", list_step["step_id"], outcome="confirmed")
    assert controller.finish_turn("owner", "task_turn-1")["status"] == "succeeded"


def test_started_receipt_after_restart_requires_reconciliation(task_env):
    _store, receipts, controller, call = task_env
    controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="monitor_add_rule",
    )
    receipts.start("receipt-1")
    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": "task_turn-1", "status": "needs_reconciliation"}]


def test_linked_prepared_receipt_cannot_hide_second_started_receipt(task_env):
    store, receipts, controller, call = task_env
    controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="monitor_add_rule",
    )
    receipts.prepare(
        receipt_id="receipt-duplicate", call_id="call-1", user_id="owner",
        turn_id="turn-1", round_id=2, tool_name="monitor_add_rule",
        origin="native", arguments={"name": "x"},
    )
    receipts.start("receipt-duplicate")

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": "task_turn-1", "status": "needs_reconciliation"}]
    persisted = store.get_task("owner", "task_turn-1")
    assert persisted["status"] == "needs_reconciliation"
    assert persisted["steps"][0]["status"] == "needs_reconciliation"
    assert receipts.get("receipt-1").status == "prepared"
    assert receipts.get("receipt-duplicate").status == "started"


def test_linked_receipt_from_another_turn_is_not_accepted(task_env):
    store, receipts, controller, _call = task_env
    foreign_call = store.record_tool_call(
        "owner", "session", tool_name="monitor_add_rule", arguments={"name": "y"},
        call_id="foreign-turn-call", turn_id="turn-1", status="running",
    )
    receipts.prepare(
        receipt_id="foreign-turn-receipt", call_id="foreign-turn-call", user_id="owner",
        turn_id="other-turn", round_id=2, tool_name="monitor_add_rule",
        origin="native", arguments={"name": "y"},
    )
    controller.start_step(
        "owner", "task_turn-1", tool_call_id=foreign_call["id"],
        receipt_id="foreign-turn-receipt", tool_name="monitor_add_rule",
    )

    recovered = controller.recover_incomplete("owner", receipts)
    assert recovered == [{"task_id": "task_turn-1", "status": "needs_reconciliation"}]
    persisted = store.get_task("owner", "task_turn-1")
    assert persisted["status"] == "needs_reconciliation"
    assert persisted["steps"][0]["status"] == "needs_reconciliation"
    task = controller.store.get_task("owner", "task_turn-1")
    assert task["status"] == "needs_reconciliation"
    assert task["steps"][0]["status"] == "needs_reconciliation"
    assert controller.surface_reconciliation_once("owner", task["task_id"])
    assert not controller.surface_reconciliation_once("owner", task["task_id"])


def test_reply_requires_owner_matching_question_and_single_cas(task_env):
    store, receipts, controller, _call = task_env
    waiting = controller.request_user(
        "owner", "task_turn-1", question_id="q-1", question="Create this rule?",
        allowed_answers={"type": "enum", "enum": ["yes", "no"]},
    )
    assert waiting["status"] == "waiting_user"
    with pytest.raises(ValueError, match="does not match"):
        controller.accept_reply(
            "owner", "task_turn-1", question_id="other", answer="yes", source_message_id="m1"
        )
    with pytest.raises(ValueError, match="outside"):
        controller.accept_reply(
            "owner", "task_turn-1", question_id="q-1", answer="maybe", source_message_id="m1"
        )
    resumed = controller.accept_reply(
        "owner", "task_turn-1", question_id="q-1", answer="yes", source_message_id="m2"
    )
    assert resumed["task"]["status"] == "queued"
    assert resumed["answer"] == "yes"
    with pytest.raises(ValueError, match="does not match"):
        controller.accept_reply(
            "owner", "task_turn-1", question_id="q-1", answer="no", source_message_id="m3"
        )
    assert store.get_task("owner", "task_turn-1")["events"][-1]["event_type"] == "task.reply_matched"


def test_sequential_replies_are_question_scoped_and_persisted(task_env):
    store, _receipts, controller, _call = task_env
    controller.request_user(
        "owner", "task_turn-1", question_id="q-first", question="Choose a category",
        allowed_answers={"type": "enum", "enum": ["food", "travel"]},
    )
    first = controller.accept_reply(
        "owner", "task_turn-1", question_id="q-first", answer="food",
        source_message_id="reply-first",
    )
    assert first["task"]["status"] == "queued"
    store.add_message(
        "owner", "session", role="user", content="food", message_id="reply-first",
    )
    controller.request_user(
        "owner", "task_turn-1", question_id="q-second", question="Confirm the date",
        allowed_answers={"type": "enum", "enum": ["today", "tomorrow"]},
    )
    second = controller.accept_reply(
        "owner", "task_turn-1", question_id="q-second", answer="tomorrow",
        source_message_id="reply-second",
    )
    assert second["task"]["status"] == "queued"
    store.add_message(
        "owner", "session", role="user", content="tomorrow", message_id="reply-second",
    )
    persisted = controller.get_persisted_reply("owner", "task_turn-1")
    assert persisted["answer"] == "tomorrow"
    assert persisted["question"]["question"] == "Confirm the date"


def test_ordinary_reply_matching_is_exact_and_conversation_scoped(task_env):
    _store, _receipts, controller, _call = task_env
    controller.request_user(
        "owner", "task_turn-1", question_id="q-match", question="Proceed?",
        allowed_answers={"type": "enum", "enum": ["yes", "no"]},
    )
    match = controller.match_waiting_reply("owner", "session", "YES")
    assert match == {
        "task_id": "task_turn-1", "question_id": "q-match",
        "answer": "yes", "question": "Proceed?",
        "objective": "Create a monitor", "step_states": [],
    }
    assert controller.match_waiting_reply("owner", "session", "what's my balance?") is None
    assert controller.match_waiting_reply("owner", "another-channel", "yes") is None


def test_request_user_choice_is_durable_and_resumes_same_task(task_env):
    store, _receipts, controller, _call = task_env
    issued = controller.request_user_choice(
        "owner", "task_turn-1", question="Which report should I prepare?",
        choices=["monthly", "quarterly"], expires_in_minutes=60,
    )
    assert issued["task"]["status"] == "waiting_user"
    assert issued["question_id"]
    assert issued["expires_at"]
    assert controller.reply_is_input_only("owner", "task_turn-1") is False
    match = controller.match_waiting_reply("owner", "session", "QUARTERLY")
    assert match["question_id"] == issued["question_id"]
    assert match["answer"] == "quarterly"
    store.add_message(
        "owner", "session", role="user", content="quarterly", message_id="answer-1",
    )
    resumed = controller.accept_reply(
        "owner", "task_turn-1", question_id=issued["question_id"],
        answer=match["answer"], source_message_id="answer-1",
    )
    assert resumed["task"]["status"] == "queued"
    assert controller.reply_is_input_only("owner", "task_turn-1") is True
    assert controller.get_persisted_reply("owner", "task_turn-1")["answer"] == "quarterly"
    from src.services.llm import _input_only_reply_blocks_tool
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "get_financial_dashboard", {}
    ) is False
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "monitor_add_rule", {"name": "x"}
    ) is True
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "run_shell", {"command": "touch file"}
    ) is True
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "fetch_webpage", {"save_only": True}
    ) is True
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "fetch_webpage", {"save_only": False}
    ) is False
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "set_savings_goal", {"name": "x"}
    ) is True
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "monitor_ack_alert", {"alert_id": 1}
    ) is True
    assert _input_only_reply_blocks_tool(
        controller, "owner", "task_turn-1", "future_unclassified_tool", {}
    ) is True


def test_expired_user_choice_is_cancelled_and_never_matched(task_env):
    store, _receipts, controller, _call = task_env
    waiting = controller.request_user(
        "owner", "task_turn-1", question_id="expired-q", question="Continue?",
        allowed_answers={"type": "enum", "enum": ["yes", "no"]},
        expires_at="2000-01-01T00:00:00Z",
    )
    assert waiting["status"] == "waiting_user"
    assert controller.match_waiting_reply("owner", "session", "yes") is None
    expired = store.get_task("owner", "task_turn-1")
    assert expired["status"] == "cancelled"
    assert expired["events"][-1]["event_type"] == "task.question_expired"


def test_pending_approval_link_is_fingerprint_bound(task_env):
    _store, _receipts, controller, _call = task_env
    action = AuthorizationRequest(
        turn_id="turn-1", user_id="owner", tool_name="monitor_add_rule",
        arguments={"name": "x"}, target={"name": "x"},
        policy=ToolPolicy(
            tool_name="monitor_add_rule", risk=RiskTier.HIGH,
            operation="create_monitor", approval_required=True,
        ),
    )
    approval_request = build_approval_request(
        action, evaluate_policy(action), approval_id="approval-1",
        summary="Create monitor x", expires_at=9999999999,
    )
    controller.request_user(
        "owner", "task_turn-1", question_id="approval-q",
        question="Create monitor x?", allowed_answers={"type": "enum", "enum": ["yes", "no"]},
        approval_request=approval_request, authorization_request=action,
    )
    resumed = controller.accept_reply(
        "owner", "task_turn-1", question_id="approval-q", answer="yes",
        source_message_id="m4",
    )
    assert resumed["approval"].approved
    assert resumed["approval"].request_fingerprint == action.fingerprint
    assert resumed["task"]["status"] == "waiting_approval"
    approval_event = controller.store.get_task(
        "owner", "task_turn-1"
    )["events"][-1]
    assert approval_event["event_type"] == "task.approval_reply_received"
    assert approval_event["payload"]["approval_status"] == "approved"
    assert approval_event["payload"]["action_fingerprint"] == action.fingerprint


def test_mismatched_typed_approval_is_rejected(task_env):
    _store, _receipts, controller, _call = task_env
    action = AuthorizationRequest(
        turn_id="turn-1", user_id="owner", tool_name="monitor_add_rule",
        arguments={"name": "x"}, target={"name": "x"},
        policy=ToolPolicy(tool_name="monitor_add_rule", risk=RiskTier.HIGH, approval_required=True),
    )
    other = AuthorizationRequest(
        turn_id="turn-1", user_id="owner", tool_name="monitor_add_rule",
        arguments={"name": "y"}, target={"name": "y"},
        policy=action.policy,
    )
    approval_request = build_approval_request(
        other, evaluate_policy(other), approval_id="approval-other",
        summary="Create monitor y", expires_at=9999999999,
    )
    with pytest.raises(ValueError, match="exact task action"):
        controller.request_user(
            "owner", "task_turn-1", question_id="q-mismatch", question="Create?",
            allowed_answers={"type": "enum", "enum": ["yes", "no"]},
            approval_request=approval_request, authorization_request=action,
        )


def test_denied_approval_cancels_task_without_queueing_dispatch(task_env):
    _store, _receipts, controller, _call = task_env
    action = AuthorizationRequest(
        turn_id="turn-1", user_id="owner", tool_name="monitor_add_rule",
        arguments={"name": "x"}, target={"name": "x"},
        policy=ToolPolicy(tool_name="monitor_add_rule", risk=RiskTier.HIGH, approval_required=True),
    )
    approval_request = build_approval_request(
        action, evaluate_policy(action), approval_id="approval-deny",
        summary="Create monitor x", expires_at=9999999999,
    )
    controller.request_user(
        "owner", "task_turn-1", question_id="approval-q", question="Create?",
        allowed_answers={"type": "enum", "enum": ["yes", "no"]},
        approval_request=approval_request, authorization_request=action,
    )
    denied = controller.accept_reply(
        "owner", "task_turn-1", question_id="approval-q", answer="no",
        source_message_id="m-deny",
    )
    assert not denied["approval"].approved
    assert denied["task"]["status"] == "cancelled"


def test_task_and_step_reads_remain_owner_scoped(task_env):
    store, _receipts, controller, call = task_env
    controller.start_step(
        "owner", "task_turn-1", tool_call_id=call["id"],
        receipt_id="receipt-1", tool_name="search_gmail",
    )
    with pytest.raises(Exception):
        store.get_task("another-owner", "task_turn-1")


def test_step2_resumable_execution_full_gate(task_env):
    """End-to-end acceptance gate verification for Step 2.

    Criteria verified:
    1. Restart at each transition resumes the right step without repeating confirmed work:
       - Read-only workflow: search_gmail -> restart -> read_gmail_message -> success.
       - Monitor workflow: monitor_add_rule -> restart -> monitor_list_rules readback -> success.
       - Pre-dispatch restart (pending/prepared step) recovers cleanly without repeating or losing state.
    2. An unknown side effect is visibly needs_reconciliation, proactively surfaced once (with atomic unique-index race prevention), and not replayed:
       - Task is marked needs_reconciliation on restart.
       - surface_reconciliation_once claims notice once; duplicate claim returns False; DB enforces unique constraint.
       - Cross-turn replay prevention: unresolved receipt blocks future monitor mutations for that owner.
    3. A matching user reply resumes a waiting task; an unrelated reply does not:
       - Unrelated messages return None and do not affect the waiting task.
       - Matching reply calls accept_reply, updates status/event, resumes via resume_ready_task into planning phase.
    """
    store, receipts, controller, _call = task_env

    # 1a. Read-only workflow: search_gmail -> restart -> read_gmail_message
    store.begin_turn("owner", "session", turn_id="turn-gmail")
    controller.create_turn("owner", "session", "turn-gmail", "Search and read Gmail")
    gmail_task_id = "task_turn-gmail"

    call1 = store.record_tool_call(
        "owner", "session", tool_name="search_gmail", arguments={"query": "invoice"},
        call_id="call-g1", turn_id="turn-gmail", status="running",
    )
    receipts.prepare(
        receipt_id="rcpt-g1", call_id="call-g1", user_id="owner", turn_id="turn-gmail",
        round_id=1, tool_name="search_gmail", origin="native", arguments={"query": "invoice"},
    )
    step1 = controller.prepare_step("owner", gmail_task_id, tool_name="search_gmail")
    controller.link_call_to_step("owner", gmail_task_id, step1["step_id"], tool_call_id=call1["id"])
    controller.claim_step("owner", gmail_task_id, step1["step_id"], receipt_id="rcpt-g1")
    receipts.start("rcpt-g1")
    receipts.finish("rcpt-g1", status="confirmed", ok=True, complete=True, result_summary="Found msg-101")
    controller.finish_step("owner", gmail_task_id, step1["step_id"], outcome="confirmed")

    # Restart after step 1 confirmation
    restarted_controller = TaskController(SessionStore(connection=store.connection))
    recovered = restarted_controller.recover_incomplete("owner", receipts)
    assert any(item["task_id"] == gmail_task_id and item["status"] == "queued" for item in recovered)
    task_after_restart = store.get_task("owner", gmail_task_id)
    assert task_after_restart["steps"][0]["status"] == "succeeded"
    assert task_after_restart["wait_reason"] == "restart_after_confirmed_step"

    # Confirmed work cannot be repeated:
    assert store.has_confirmed_task_call("owner", gmail_task_id, tool_name="search_gmail", arguments={"query": "invoice"}) is True
    with pytest.raises(ValueError, match="TASK_STEP_ALREADY_CONFIRMED"):
        restarted_controller.validate_tool_dispatch(
            "owner", gmail_task_id, tool_name="search_gmail", arguments={"query": "invoice"}
        )

    # Resume ready task explicitly
    resumed_task = restarted_controller.resume_ready_task("owner", gmail_task_id)
    assert resumed_task["phase"] == "planning"

    # Step 2: read_gmail_message (resumes next step without repeating step 1)
    call2 = store.record_tool_call(
        "owner", "session", tool_name="read_gmail_message", arguments={"message_id": "msg-101"},
        call_id="call-g2", turn_id="turn-gmail", status="running",
    )
    receipts.prepare(
        receipt_id="rcpt-g2", call_id="call-g2", user_id="owner", turn_id="turn-gmail",
        round_id=2, tool_name="read_gmail_message", origin="native", arguments={"message_id": "msg-101"},
    )
    step2 = restarted_controller.prepare_step("owner", gmail_task_id, tool_name="read_gmail_message")
    restarted_controller.link_call_to_step("owner", gmail_task_id, step2["step_id"], tool_call_id=call2["id"])
    restarted_controller.claim_step("owner", gmail_task_id, step2["step_id"], receipt_id="rcpt-g2")
    receipts.start("rcpt-g2")
    receipts.finish("rcpt-g2", status="confirmed", ok=True, complete=True, result_summary="Invoice body")
    restarted_controller.finish_step("owner", gmail_task_id, step2["step_id"], outcome="confirmed")
    final_gmail_task = restarted_controller.finish_turn("owner", gmail_task_id)
    assert final_gmail_task["status"] == "succeeded"
    assert len(final_gmail_task["steps"]) == 2
    assert final_gmail_task["steps"][0]["status"] == "succeeded" and final_gmail_task["steps"][0]["receipt_id"] == "rcpt-g1"
    assert final_gmail_task["steps"][1]["status"] == "succeeded" and final_gmail_task["steps"][1]["receipt_id"] == "rcpt-g2"

    # 1b. Monitor workflow: monitor_add_rule -> restart -> monitor_list_rules readback
    store.begin_turn("owner", "session", turn_id="turn-mon")
    controller.create_turn("owner", "session", "turn-mon", "Create and verify monitor")
    mon_task_id = "task_turn-mon"

    call_mon = store.record_tool_call(
        "owner", "session", tool_name="monitor_add_rule", arguments={"name": "x"},
        call_id="call-m1", turn_id="turn-mon", status="running",
    )
    receipts.prepare(
        receipt_id="rcpt-m1", call_id="call-m1", user_id="owner", turn_id="turn-mon",
        round_id=1, tool_name="monitor_add_rule", origin="native", arguments={"name": "x"},
    )
    step_m1 = controller.prepare_step("owner", mon_task_id, tool_name="monitor_add_rule")
    controller.link_call_to_step("owner", mon_task_id, step_m1["step_id"], tool_call_id=call_mon["id"])
    controller.claim_step("owner", mon_task_id, step_m1["step_id"], receipt_id="rcpt-m1")
    receipts.start("rcpt-m1")
    receipts.finish("rcpt-m1", status="confirmed", ok=True, complete=True, result_summary="Created monitor rule #1: x.")
    controller.finish_step("owner", mon_task_id, step_m1["step_id"], outcome="confirmed")
    assert store.task_requires_monitor_readback("owner", mon_task_id) is True
    assert store.has_confirmed_task_call("owner", mon_task_id, tool_name="monitor_add_rule", arguments={"name": "x"}) is True

    # Restart occurs with monitor readback pending
    restarted_mon_controller = TaskController(SessionStore(connection=store.connection))
    restarted_mon_controller.recover_incomplete("owner", receipts)
    assert store.task_requires_monitor_readback("owner", mon_task_id) is True

    # Exact expected wait reason after restart is monitor_readback_required:
    resumed_mon_task = restarted_mon_controller.resume_ready_task("owner", mon_task_id)
    assert resumed_mon_task["wait_reason"] == "monitor_readback_required"

    # Confirmed tool replay is blocked and monitor readback is required:
    with pytest.raises(PermissionError, match="MONITOR_READBACK_REQUIRED"):
        restarted_mon_controller.validate_tool_dispatch(
            "owner", mon_task_id, tool_name="search_gmail", arguments={}
        )
    with pytest.raises(PermissionError, match="MONITOR_READBACK_REQUIRED"):
        restarted_mon_controller.validate_tool_dispatch(
            "owner", mon_task_id, tool_name="monitor_add_rule", arguments={"name": "x"}
        )

    call_list = store.record_tool_call(
        "owner", "session", tool_name="monitor_list_rules", arguments={},
        call_id="call-list-1", turn_id="turn-mon", status="running",
    )
    receipts.prepare(
        receipt_id="rcpt-list-1", call_id="call-list-1", user_id="owner", turn_id="turn-mon",
        round_id=2, tool_name="monitor_list_rules", origin="native", arguments={},
    )
    step_list = restarted_mon_controller.prepare_step("owner", mon_task_id, tool_name="monitor_list_rules")
    restarted_mon_controller.link_call_to_step("owner", mon_task_id, step_list["step_id"], tool_call_id=call_list["id"])
    restarted_mon_controller.claim_step("owner", mon_task_id, step_list["step_id"], receipt_id="rcpt-list-1")
    receipts.start("rcpt-list-1")
    receipts.finish(
        "rcpt-list-1", status="confirmed", ok=True, complete=True,
        result_summary="Monitor rules (1):\n- #1 [on] x (large_deposit) config={}",
    )
    restarted_mon_controller.finish_step("owner", mon_task_id, step_list["step_id"], outcome="confirmed")
    assert store.task_requires_monitor_readback("owner", mon_task_id) is False
    with pytest.raises(ValueError, match="TASK_STEP_ALREADY_CONFIRMED"):
        restarted_mon_controller.validate_tool_dispatch(
            "owner", mon_task_id, tool_name="monitor_add_rule", arguments={"name": "x"}
        )
    final_mon_task = restarted_mon_controller.finish_turn("owner", mon_task_id)
    assert final_mon_task["status"] == "succeeded"

    # 1c. Restart at pending/prepared step transition (before start)
    store.begin_turn("owner", "session", turn_id="turn-prestart")
    controller.create_turn("owner", "session", "turn-prestart", "Pending step crash")
    prestart_task_id = "task_turn-prestart"
    call_pre = store.record_tool_call(
        "owner", "session", tool_name="search_gmail", arguments={"query": "pending"},
        call_id="call-pre", turn_id="turn-prestart", status="running",
    )
    receipts.prepare(
        receipt_id="rcpt-pre", call_id="call-pre", user_id="owner", turn_id="turn-prestart",
        round_id=1, tool_name="search_gmail", origin="native", arguments={"query": "pending"},
    )
    step_pre = controller.prepare_step("owner", prestart_task_id, tool_name="search_gmail")
    controller.link_call_to_step("owner", prestart_task_id, step_pre["step_id"], tool_call_id=call_pre["id"])
    controller.claim_step("owner", prestart_task_id, step_pre["step_id"], receipt_id="rcpt-pre")
    # Crash before receipts.start
    restarted_pre = TaskController(SessionStore(connection=store.connection))
    recovered_pre = restarted_pre.recover_incomplete("owner", receipts)
    assert any(item["task_id"] == prestart_task_id and item["status"] == "queued" for item in recovered_pre)
    task_pre = store.get_task("owner", prestart_task_id)
    assert task_pre["wait_reason"] == "restart_before_dispatch"
    # Stale prepared receipt was cleaned up
    assert receipts.get("rcpt-pre").status == "failed"

    # Resumes cleanly: prepare new receipt for the undispatched call, claim, and complete without dropping state
    resumed_pre = restarted_pre.resume_ready_task("owner", prestart_task_id)
    assert resumed_pre["phase"] == "planning"
    receipts.prepare(
        receipt_id="rcpt-pre-resumed", call_id="call-pre", user_id="owner", turn_id="turn-prestart",
        round_id=2, tool_name="search_gmail", origin="native", arguments={"query": "pending"},
    )
    restarted_pre.claim_step("owner", prestart_task_id, step_pre["step_id"], receipt_id="rcpt-pre-resumed")
    receipts.start("rcpt-pre-resumed")
    receipts.finish("rcpt-pre-resumed", status="confirmed", ok=True, complete=True, result_summary="Found pending email")
    restarted_pre.finish_step("owner", prestart_task_id, step_pre["step_id"], outcome="confirmed")
    final_pre = restarted_pre.finish_turn("owner", prestart_task_id)
    assert final_pre["status"] == "succeeded"

    # 2. Side effect unknown outcome: visibly needs_reconciliation, surfaced once, not replayed
    store.begin_turn("owner", "session", turn_id="turn-ambig")
    controller.create_turn("owner", "session", "turn-ambig", "Create ambiguous monitor")
    ambig_task_id = "task_turn-ambig"
    call_ambig = store.record_tool_call(
        "owner", "session", tool_name="monitor_add_rule", arguments={"name": "ambig_rule"},
        call_id="call-ambig", turn_id="turn-ambig", status="running",
    )
    receipts.prepare(
        receipt_id="rcpt-ambig", call_id="call-ambig", user_id="owner", turn_id="turn-ambig",
        round_id=1, tool_name="monitor_add_rule", origin="native", arguments={"name": "ambig_rule"},
    )
    step_ambig = controller.prepare_step("owner", ambig_task_id, tool_name="monitor_add_rule")
    controller.link_call_to_step("owner", ambig_task_id, step_ambig["step_id"], tool_call_id=call_ambig["id"])
    controller.claim_step("owner", ambig_task_id, step_ambig["step_id"], receipt_id="rcpt-ambig")
    receipts.start("rcpt-ambig")
    # Simulate crash/unknown outcome while started
    restarted_controller2 = TaskController(SessionStore(connection=store.connection))
    recovered2 = restarted_controller2.recover_incomplete("owner", receipts)
    assert any(item["task_id"] == ambig_task_id and item["status"] == "needs_reconciliation" for item in recovered2)
    ambig_task = store.get_task("owner", ambig_task_id)
    assert ambig_task["status"] == "needs_reconciliation"
    assert "started" in ambig_task["wait_reason"] or "unknown" in ambig_task["wait_reason"]

    # Proactively surfaced once:
    assert restarted_controller2.surface_reconciliation_once("owner", ambig_task_id) is True
    assert restarted_controller2.surface_reconciliation_once("owner", ambig_task_id) is False

    # Unique index migration 008 race protection: direct insert throws IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        store.append_task_event(
            "owner", ambig_task_id, event_type="task.reconciliation_notice_claimed",
            payload={"reason_code": "duplicate_race"},
        )

    # Not replayed on the same task:
    with pytest.raises(ValueError, match="not dispatchable"):
        restarted_controller2.prepare_step("owner", ambig_task_id, tool_name="monitor_add_rule")
    with pytest.raises(PermissionError, match="TASK_NOT_DISPATCHABLE"):
        restarted_controller2.validate_tool_dispatch(
            "owner", ambig_task_id, tool_name="monitor_add_rule", arguments={"name": "ambig_rule"}
        )

    # Cross-turn / cross-task replay prevention for ambiguous side effects:
    # An unresolved started/unknown receipt for monitor_add_rule blocks both monitor-insert tools
    store.begin_turn("owner", "session", turn_id="turn-next-attempt")
    receipts.prepare(
        receipt_id="rcpt-blocked-1", call_id="call-blocked-1", user_id="owner",
        turn_id="turn-next-attempt", round_id=1, tool_name="monitor_add_rule",
        origin="native", arguments={"name": "another_rule"},
    )
    with pytest.raises(Exception, match="Blocked monitor creation"):
        receipts.start("rcpt-blocked-1")

    receipts.prepare(
        receipt_id="rcpt-blocked-2", call_id="call-blocked-2", user_id="owner",
        turn_id="turn-next-attempt", round_id=2, tool_name="monitor_create_natural_rule",
        origin="native", arguments={"prompt": "alert when balance < 100"},
    )
    with pytest.raises(Exception, match="Blocked monitor creation"):
        receipts.start("rcpt-blocked-2")

    # 3. Matching user reply resumes waiting task; unrelated reply does not
    store.begin_turn("owner", "session", turn_id="turn-reply")
    controller.create_turn("owner", "session", "turn-reply", "Wait for user confirmation")
    reply_task_id = "task_turn-reply"
    controller.request_user_choice(
        "owner", reply_task_id, question="Which account?", choices=["checking", "savings"]
    )

    # Restart during waiting_user transition survives cleanly:
    restarted_waiting = TaskController(SessionStore(connection=store.connection))
    restarted_waiting.recover_incomplete("owner", receipts)
    waiting_task_after_restart = store.get_task("owner", reply_task_id)
    assert waiting_task_after_restart["status"] == "waiting_user"
    assert waiting_task_after_restart["phase"] == "awaiting_user"

    # Unrelated message does NOT match
    assert restarted_waiting.match_waiting_reply("owner", "session", "how much is in my checking?") is None
    assert restarted_waiting.match_waiting_reply("owner", "session", "hello delilah") is None

    # Unrelated message starting a turn does not affect the waiting task
    store.begin_turn("owner", "session", turn_id="turn-unrelated-msg")
    assert store.get_task("owner", reply_task_id)["status"] == "waiting_user"

    # Matching exact choice DOES match
    match = restarted_waiting.match_waiting_reply("owner", "session", "checking")
    assert match is not None
    assert match["task_id"] == reply_task_id
    assert match["answer"] == "checking"

    # Invalid / out-of-range choice is rejected by accept_reply
    with pytest.raises(ValueError, match="outside the allowed answer choices"):
        restarted_waiting.accept_reply(
            "owner", reply_task_id, question_id=match["question_id"], answer="bitcoin",
            source_message_id="msg-invalid-choice",
        )

    # Complete resumption path: accept reply, verify transition and resume_ready_task
    store.add_message("owner", "session", role="user", content="checking", message_id="msg-choice-1")
    accepted = restarted_waiting.accept_reply(
        "owner", reply_task_id, question_id=match["question_id"], answer="checking",
        source_message_id="msg-choice-1",
    )
    assert accepted["task"]["status"] == "queued"
    assert accepted["task"]["wait_reason"] == f"resume_after_reply:{match['question_id']}"
    assert restarted_waiting.reply_is_input_only("owner", reply_task_id) is True

    # Resuming task advances to planning phase
    resumed_reply_task = restarted_waiting.resume_ready_task("owner", reply_task_id)
    assert resumed_reply_task["phase"] == "planning"
    persisted_reply = restarted_waiting.get_persisted_reply("owner", reply_task_id)
    assert persisted_reply["answer"] == "checking"
    assert persisted_reply["question"]["question"] == "Which account?"

    # 4. Confirmed step failure survives restart as failed (never erroneously queued or replayed)
    store.begin_turn("owner", "session", turn_id="turn-fail")
    controller.create_turn("owner", "session", "turn-fail", "Failing task")
    fail_task_id = "task_turn-fail"
    call_f = store.record_tool_call(
        "owner", "session", tool_name="search_gmail", arguments={"query": "broken"},
        call_id="call-f", turn_id="turn-fail", status="running",
    )
    receipts.prepare(
        receipt_id="rcpt-f", call_id="call-f", user_id="owner", turn_id="turn-fail",
        round_id=1, tool_name="search_gmail", origin="native", arguments={"query": "broken"},
    )
    step_f = controller.prepare_step("owner", fail_task_id, tool_name="search_gmail")
    controller.link_call_to_step("owner", fail_task_id, step_f["step_id"], tool_call_id=call_f["id"])
    controller.claim_step("owner", fail_task_id, step_f["step_id"], receipt_id="rcpt-f")
    receipts.start("rcpt-f")
    receipts.finish("rcpt-f", status="failed", ok=False, complete=False, error="Auth expired")
    controller.finish_step("owner", fail_task_id, step_f["step_id"], outcome="failed", reason="Auth expired")

    restarted_fail = TaskController(SessionStore(connection=store.connection))
    recovered_fail = restarted_fail.recover_incomplete("owner", receipts)
    assert any(item["task_id"] == fail_task_id and item["status"] == "failed" for item in recovered_fail)
    task_fail = store.get_task("owner", fail_task_id)
    assert task_fail["status"] == "failed"
    assert task_fail["wait_reason"] == "restart_after_confirmed_failure"
    assert task_fail["phase"] == "failed"

    # 5. Planned task with ready steps recovers with plan_pending and preserves it across subsequent restart
    store.begin_turn("owner", "session", turn_id="turn-plan")
    controller.create_turn("owner", "session", "turn-plan", "Multi-step plan")
    plan_task_id = "task_turn-plan"
    controller.create_plan(
        "owner", plan_task_id, [
            {"description": "Step 1", "tool_name": "search_gmail", "completion_criteria": "found emails"},
            {"description": "Step 2", "tool_name": "read_gmail_message", "completion_criteria": "read email body"},
        ],
    )
    restarted_plan = TaskController(SessionStore(connection=store.connection))
    recovered_plan = restarted_plan.recover_incomplete("owner", receipts)
    task_planned = store.get_task("owner", plan_task_id)
    assert task_planned["wait_reason"] == "plan_pending"
    assert task_planned["status"] == "queued"

    # Second restart does NOT corrupt or overwrite plan_pending with restart_before_dispatch:
    restarted_plan2 = TaskController(SessionStore(connection=store.connection))
    restarted_plan2.recover_incomplete("owner", receipts)
    task_planned_after = store.get_task("owner", plan_task_id)
    assert task_planned_after["wait_reason"] == "plan_pending"
    assert task_planned_after["status"] == "queued"



