from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest

from src.agent.delegation import (
    MAX_CHILD_STEPS,
    MAX_CHILD_TIMEOUT_SECONDS,
    MAX_CHILD_TOKENS,
    DelegationBudget,
    DelegationController,
    DelegationResult,
)
from src.agent.runtime import CURRENT_TURN_ID
from src.agent.scheduler import (
    CapacityAwareScheduler,
    reset_global_scheduler,
    schedule_work,
    set_global_scheduler,
)
from src.agent.task_controller import TaskController
from src.db.session_store import SessionStore
from src.services.tool_receipts import ReceiptStore


def _make_env():
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=conn)
    receipts = ReceiptStore(conn)
    receipts.ensure_schema()
    tasks = TaskController(store)
    parent = store.create_task_run(
        "owner", "parent-session", "delegate work", task_id="parent-task",
        channel_id="channel", thread_id="thread",
    )
    tasks.transition_phase("owner", parent["task_id"], "planning", next_action="plan")
    controller = DelegationController(store, tasks, receipt_store=receipts)
    return store, receipts, tasks, controller


@pytest.fixture
def delegation_env():
    return _make_env()


def _budget(**overrides):
    values = {"max_steps": 3, "timeout_seconds": 5, "max_tokens": 512}
    values.update(overrides)
    return DelegationBudget(**values)


@pytest.mark.asyncio
async def test_delegate_creates_inspectable_child_and_resumes_parent(delegation_env):
    store, _receipts, _tasks, controller = delegation_env

    async def runner(child_id, user_id, goal, tools, context, budget):
        assert user_id == "owner"
        assert goal == "summarize recent messages"
        assert tools == ["search_gmail"]
        assert budget.max_steps == 3
        return DelegationResult(child_id, "parent-task", "succeeded", "Found two messages.")

    result = await controller.delegate(
        "owner", "parent-task", "summarize recent messages",
        allowed_tools=["search_gmail"], parent_allowed_tools={"search_gmail", "delegate_task"},
        budget=_budget(), runner_fn=runner,
    )
    parent = store.get_task("owner", "parent-task")
    children = store.get_child_tasks("owner", "parent-task")
    assert result.status == "succeeded"
    assert len(children) == 1
    child = store.get_task("owner", result.task_id)
    assert child["parent_task_id"] == "parent-task"
    assert child["user_id"] == "owner"
    assert child["session_key"] == f"child:{result.task_id}"
    assert child["status"] == "succeeded"
    assert child["phase"] == "completed"
    grant_event = next(event for event in parent["events"] if event["event_type"] == "task.delegated")
    assert grant_event["payload"]["delegation_grant_id"].startswith("delegation_grant_")
    assert grant_event["payload"]["allowed_tools"] == ["search_gmail"]
    assert parent["phase"] == "planning"
    assert parent["wait_reason"] == f"resume_after_child:{result.task_id}"


@pytest.mark.asyncio
async def test_nonterminal_child_keeps_parent_open_and_is_not_reported_successful(delegation_env):
    store, _receipts, tasks, controller = delegation_env

    async def runner(child_id, _user_id, _goal, _tools, _context, _budget):
        return DelegationResult(child_id, "parent-task", "queued", "Child remains queued.")

    result = await controller.delegate(
        "owner", "parent-task", "long running work",
        allowed_tools=["search_gmail"], parent_allowed_tools=["search_gmail"],
        budget=_budget(), runner_fn=runner,
    )
    assert result.status == "queued"
    assert store.get_task("owner", result.task_id)["status"] == "queued"
    parent = tasks.finish_turn("owner", "parent-task")
    assert parent["status"] == "queued"
    assert parent["phase"] == "awaiting_child"
    with pytest.raises(PermissionError, match="TASK_WAITING_FOR_CHILD"):
        tasks.validate_tool_dispatch("owner", "parent-task", "search_gmail")


@pytest.mark.asyncio
async def test_cancel_racing_with_child_completion_cannot_resurrect_work(delegation_env):
    store, receipts, tasks, controller = delegation_env
    _record_parent_delegation_receipt(
        store, receipts, tasks, goal="slow delegated read",
        budget={"max_steps": 3, "timeout_seconds": 5.0, "max_tokens": 512},
    )
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def runner(child_id, _user_id, _goal, _tools, _context, _budget):
        entered.set()
        await finish.wait()
        return DelegationResult(child_id, "parent-task", "succeeded", "late completion")

    invocation = asyncio.create_task(controller.delegate(
        "owner", "parent-task", "slow delegated read",
        allowed_tools=["search_gmail"], parent_allowed_tools=["search_gmail"],
        budget=_budget(), runner_fn=runner,
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    tasks.cancel("owner", "parent-task")
    finish.set()
    result = await asyncio.wait_for(invocation, timeout=1)
    parent = store.get_task("owner", "parent-task")
    child = store.get_task("owner", result.task_id)
    assert result.status == "cancelled"
    assert parent["status"] == "cancelled"
    assert child["status"] == "cancelled"


@pytest.mark.asyncio
async def test_child_waiting_for_capacity_times_out_without_stranding_parent(delegation_env):
    store, receipts, tasks, controller = delegation_env
    scheduler = CapacityAwareScheduler(max_workers=1, max_active_tasks_per_owner=1)
    set_global_scheduler(scheduler)
    entered = asyncio.Event()
    release = asyncio.Event()
    try:
        store.create_task_run(
            "owner", "second-parent-session", "delegate", task_id="second-parent",
            channel_id="channel", thread_id="second-thread",
        )
        tasks.transition_phase("owner", "second-parent", "planning", next_action="delegate")
        _record_parent_delegation_receipt(
            store, receipts, tasks, goal="first child",
            budget={"max_steps": 3, "timeout_seconds": 2.0, "max_tokens": 512},
        )
        _record_parent_delegation_receipt(
            store, receipts, tasks, parent_task_id="second-parent",
            session_id="second-parent-session", thread_id="second-thread",
            goal="queued child",
            budget={"max_steps": 3, "timeout_seconds": 0.05, "max_tokens": 512},
        )

        async def first_runner(child_id, _user_id, _goal, _tools, _context, _budget):
            entered.set()
            await release.wait()
            return DelegationResult(child_id, "parent-task", "succeeded", "done")

        first = asyncio.create_task(controller.delegate(
            "owner", "parent-task", "first child",
            allowed_tools=["search_gmail"], parent_allowed_tools=["search_gmail"],
            budget=_budget(timeout_seconds=2), runner_fn=first_runner,
        ))
        await asyncio.wait_for(entered.wait(), timeout=1)

        async def should_not_run(*_args):
            raise AssertionError("timed-out child must not dispatch")

        second = await controller.delegate(
            "owner", "second-parent", "queued child",
            allowed_tools=["search_gmail"], parent_allowed_tools=["search_gmail"],
            budget=_budget(timeout_seconds=0.05), runner_fn=should_not_run,
        )
        assert second.status == "cancelled"
        child = store.get_task("owner", second.task_id)
        parent = store.get_task("owner", "second-parent")
        assert child["status"] == "cancelled"
        assert parent["status"] == "queued" and parent["phase"] == "planning"

        release.set()
        assert (await asyncio.wait_for(first, timeout=1)).status == "succeeded"
    finally:
        release.set()
        reset_global_scheduler()


@pytest.mark.asyncio
async def test_permission_narrowing_and_recursive_delegation_are_fail_closed(delegation_env):
    _store, _receipts, _tasks, controller = delegation_env
    with pytest.raises(PermissionError, match="cannot exceed"):
        await controller.delegate(
            "owner", "parent-task", "do work", allowed_tools=["delete_transaction"],
            parent_allowed_tools=["search_gmail"], budget=_budget(), runner_fn=lambda *a: None,
        )
    with pytest.raises(PermissionError, match="required"):
        controller.validate_delegation_permissions("owner", "parent-task", ["search_gmail"])

    store = _store
    child = store.create_child_task_and_await_parent(
        "owner", "parent-task", child_task_id="child-recursion",
        child_session_id="child:child-recursion", objective="nested",
        expected_parent_status="queued", expected_parent_version=int(
            store.get_task("owner", "parent-task")["version"]
        ), expected_parent_phase="planning", next_action="await",
        allowed_tools=["search_gmail"], budget={"max_steps": 1, "timeout_seconds": 1, "max_tokens": 1},
        delegation_grant_id="delegation_grant_test-recursion",
    )
    assert child["child"]["parent_task_id"] == "parent-task"
    with pytest.raises(ValueError, match="Recursive delegation is disabled"):
        controller.validate_delegation_permissions(
            "owner", "child-recursion", ["search_gmail"], ["search_gmail"]
        )


def test_budget_requires_finite_positive_explicit_bounds():
    with pytest.raises(ValueError):
        DelegationBudget(max_steps=True, timeout_seconds=1, max_tokens=10)
    with pytest.raises(ValueError):
        DelegationBudget(max_steps=1, timeout_seconds=float("inf"), max_tokens=10)
    with pytest.raises(ValueError):
        DelegationBudget(max_steps=1, timeout_seconds=1, max_tokens="10")
    with pytest.raises(ValueError, match="configured cap"):
        DelegationBudget(max_steps=MAX_CHILD_STEPS + 1, timeout_seconds=1, max_tokens=10)
    with pytest.raises(ValueError, match="configured cap"):
        DelegationBudget(
            max_steps=1, timeout_seconds=MAX_CHILD_TIMEOUT_SECONDS + 1,
            max_tokens=10,
        )
    with pytest.raises(ValueError, match="configured cap"):
        DelegationBudget(max_steps=1, timeout_seconds=1, max_tokens=MAX_CHILD_TOKENS + 1)


def test_delegation_capability_grant_is_bound_to_owner_parent_child_and_tool():
    from src.services.tool_grants import (
        issue_delegation_capability_grant,
        validate_delegation_capability_grant,
    )

    grant = issue_delegation_capability_grant(
        user_id="owner", parent_task_id="parent", child_task_id="child",
        allowed_tools=["search_gmail"],
    )
    validate_delegation_capability_grant(
        grant, user_id="owner", parent_task_id="parent", task_id="child",
        tool_name="search_gmail",
    )
    for identity in (
        {"user_id": "other", "parent_task_id": "parent", "task_id": "child", "tool_name": "search_gmail"},
        {"user_id": "owner", "parent_task_id": "other", "task_id": "child", "tool_name": "search_gmail"},
        {"user_id": "owner", "parent_task_id": "parent", "task_id": "other", "tool_name": "search_gmail"},
        {"user_id": "owner", "parent_task_id": "parent", "task_id": "child", "tool_name": "delete_transaction"},
    ):
        with pytest.raises(PermissionError):
            validate_delegation_capability_grant(grant, **identity)


@pytest.mark.asyncio
async def test_child_side_effect_links_existing_call_and_receipt(delegation_env):
    store, receipts, tasks, controller = delegation_env

    async def runner(child_id, user_id, _goal, _tools, _context, _budget):
        child = store.get_task(user_id, child_id)
        turn_id = CURRENT_TURN_ID.get()
        call = store.record_tool_call(
            user_id, child["session_key"], tool_name="monitor_add_rule",
            arguments={"name": "delegated"}, call_id="child-call", turn_id=turn_id,
            status="running", channel_id=child.get("channel_id"),
            thread_id=child.get("thread_id"),
        )
        receipts.prepare(
            receipt_id="child-receipt", call_id="child-call", user_id=user_id,
            turn_id=turn_id, round_id=1, tool_name="monitor_add_rule",
            origin="native", arguments={"name": "delegated"},
        )
        step = tasks.prepare_step(user_id, child_id, tool_name="monitor_add_rule")
        tasks.link_call_to_step(user_id, child_id, step["step_id"], tool_call_id=call["id"])
        tasks.claim_step(user_id, child_id, step["step_id"], receipt_id="child-receipt")
        receipts.start("child-receipt")
        receipts.finish(
            "child-receipt", status="confirmed", ok=True, complete=True,
            result_summary="Created delegated monitor.",
        )
        tasks.finish_step(user_id, child_id, step["step_id"], outcome="confirmed")
        return DelegationResult(
            child_id, "parent-task", "succeeded", "Created delegated monitor.",
            steps_executed=1, receipt_ids=["child-receipt"],
        )

    result = await controller.delegate(
        "owner", "parent-task", "create one monitor rule",
        allowed_tools=["monitor_add_rule"], parent_allowed_tools=["monitor_add_rule"],
        budget=_budget(), runner_fn=runner,
    )
    step = store.get_task("owner", result.task_id, include_steps=True)["steps"][0]
    receipt = receipts.list_for_call("child-call", user_id="owner")
    assert step["tool_call_id"] is not None
    assert step["receipt_id"] == "child-receipt"
    assert len(receipt) == 1 and receipt[0].status == "confirmed"
    assert result.receipt_ids == ["child-receipt"]


@pytest.mark.asyncio
async def test_ambiguous_child_receipt_is_reconciliation_and_never_retried(delegation_env):
    store, receipts, tasks, controller = delegation_env
    dispatch_count = 0

    async def runner(child_id, user_id, _goal, _tools, _context, _budget):
        nonlocal dispatch_count
        dispatch_count += 1
        child = store.get_task(user_id, child_id)
        turn_id = CURRENT_TURN_ID.get()
        call = store.record_tool_call(
            user_id, child["session_key"], tool_name="monitor_add_rule",
            arguments={"name": "possibly-created"}, call_id="ambiguous-child-call",
            turn_id=turn_id, status="running", channel_id=child.get("channel_id"),
            thread_id=child.get("thread_id"),
        )
        receipts.prepare(
            receipt_id="ambiguous-child-receipt", call_id="ambiguous-child-call",
            user_id=user_id, turn_id=turn_id, round_id=1,
            tool_name="monitor_add_rule", origin="native",
            arguments={"name": "possibly-created"},
        )
        step = tasks.prepare_step(user_id, child_id, tool_name="monitor_add_rule")
        tasks.link_call_to_step(user_id, child_id, step["step_id"], tool_call_id=call["id"])
        tasks.claim_step(
            user_id, child_id, step["step_id"], receipt_id="ambiguous-child-receipt"
        )
        receipts.start("ambiguous-child-receipt")
        raise TimeoutError("provider response lost after dispatch")

    result = await controller.delegate(
        "owner", "parent-task", "create one monitor rule",
        allowed_tools=["monitor_add_rule"], parent_allowed_tools=["monitor_add_rule"],
        budget=_budget(), runner_fn=runner,
    )
    child = store.get_task("owner", result.task_id, include_steps=True)
    parent = store.get_task("owner", "parent-task")
    assert dispatch_count == 1
    assert child["status"] == "needs_reconciliation"
    assert child["steps"][0]["status"] == "needs_reconciliation"
    assert receipts.list_for_call("ambiguous-child-call", user_id="owner")[0].status == "started"
    assert result.status == "needs_reconciliation"
    assert parent["status"] == "needs_reconciliation"


def test_model_tool_catalog_exposes_delegate_dispatch_contract():
    from src.services import llm

    assert "delegate_task" in llm.SCHEMA_TOOL_NAMES
    assert "delegate_task" in llm.EXPECTED_TOOL_NAMES
    assert "delegate_task" in llm.MUTATION_TOOLS
    assert "task_cancel" in llm.SCHEMA_TOOL_NAMES
    assert "task_cancel" in llm.MUTATION_TOOLS
    assert not llm._tool_requires_durable_plan("task_cancel")
    schema = next(
        item["function"] for item in llm.BOT_TOOLS_SCHEMA
        if item["function"]["name"] == "delegate_task"
    )
    assert set(schema["parameters"]["required"]) == {"goal", "allowed_tools", "budget"}
    assert set(schema["parameters"]["properties"]["budget"]["required"]) == {
        "max_steps", "timeout_seconds", "max_tokens"
    }


def test_over_budget_child_batch_is_rejected_before_any_tool_is_dispatched():
    from src.services.llm import _delegated_batch_budget_error

    calls = [
        {"function": {"name": "task_plan"}},
        {"function": {"name": "monitor_add_rule"}},
        {"function": {"name": "monitor_list_rules"}},
    ]
    assert _delegated_batch_budget_error(calls, max_steps=1, used_steps=0)
    assert _delegated_batch_budget_error(calls, max_steps=2, used_steps=1)
    assert _delegated_batch_budget_error(calls[:2], max_steps=1, used_steps=0) is None


def test_delegated_actions_always_get_task_step_receipt_linkage():
    from src.services.llm import _should_track_task_step

    # A regular side-effecting capability outside the legacy migrated set is
    # still durably linked when dispatched from an isolated child task.
    assert _should_track_task_step(
        "child-id", "sync_plaid_accounting", has_plan=False, delegated=True
    )
    assert _should_track_task_step(
        "child-id", "monitor_add_rule", has_plan=False, delegated=True
    )
    assert _should_track_task_step(
        "parent-id", "delegate_task", has_plan=False, delegated=False
    )
    assert not _should_track_task_step(
        "child-id", "task_list", has_plan=False, delegated=True
    )
    assert not _should_track_task_step(
        None, "sync_plaid_accounting", has_plan=False, delegated=True
    )


def test_parent_task_inspection_includes_owner_scoped_children(delegation_env, monkeypatch):
    store, _receipts, _tasks, _controller = delegation_env
    _create_waiting_child(store)
    from src.services import llm

    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)
    rows = llm._list_durable_tasks(
        "owner", session_id="parent-session", channel_id="channel",
        thread_id="thread", limit=10,
    )
    parent = next(row for row in rows if row["task_id"] == "parent-task")
    assert parent["children"][0]["task_id"] == "child-restart"
    assert parent["children"][0]["status"] == "queued"


def test_dispatch_grant_must_match_durable_parent_child_and_tool_scope(delegation_env):
    store, _receipts, _tasks, _controller = delegation_env
    _create_waiting_child(store)
    from src.services.llm import _validate_delegated_task_grant
    from src.services.tool_grants import issue_delegation_capability_grant

    spec = store.get_child_delegation_spec("owner", "child-restart")
    grant = issue_delegation_capability_grant(
        user_id="owner", parent_task_id="parent-task", child_task_id="child-restart",
        allowed_tools=spec["allowed_tools"], grant_id=spec["delegation_grant_id"],
    )
    _validate_delegated_task_grant(store, "owner", "child-restart", "search_gmail", grant)
    with pytest.raises(PermissionError, match="differs from durable scope"):
        broader = issue_delegation_capability_grant(
            user_id="owner", parent_task_id="parent-task", child_task_id="child-restart",
            allowed_tools=["search_gmail", "delete_transaction"],
            grant_id=spec["delegation_grant_id"],
        )
        _validate_delegated_task_grant(
            store, "owner", "child-restart", "delete_transaction", broader
        )


def _create_waiting_child(store):
    parent = store.get_task("owner", "parent-task")
    return store.create_child_task_and_await_parent(
        "owner", "parent-task", child_task_id="child-restart",
        child_session_id="child:child-restart", objective="resume after restart",
        expected_parent_status=parent["status"], expected_parent_version=int(parent["version"]),
        expected_parent_phase=parent["phase"], next_action="await_child:child-restart",
        allowed_tools=["search_gmail"], budget={"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
        delegation_grant_id="delegation_grant_test-restart",
    )


def _record_parent_delegation_receipt(
    store, receipts, tasks, *, parent_task_id="parent-task",
    session_id="parent-session", channel_id="channel", thread_id="thread",
    goal="resume after restart", allowed_tools=("search_gmail",), budget=None,
):
    budget = budget or {"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100}
    arguments = {"goal": goal, "allowed_tools": list(allowed_tools), "budget": budget}
    turn_id = f"parent-resume-turn-{uuid.uuid4().hex}"
    store.begin_turn(
        "owner", session_id, turn_id=turn_id,
        channel_id=channel_id, thread_id=thread_id,
    )
    call = store.record_tool_call(
        "owner", session_id, tool_name="delegate_task",
        arguments=arguments, call_id=f"parent-resume-{uuid.uuid4().hex}",
        turn_id=turn_id, status="running", channel_id=channel_id, thread_id=thread_id,
    )
    receipt_id = f"parent-resume-receipt-{uuid.uuid4().hex}"
    receipts.prepare(
        receipt_id=receipt_id, call_id=call["call_key"], user_id="owner",
        turn_id=turn_id, round_id=1, tool_name="delegate_task",
        origin="native", arguments=arguments,
    )
    step = tasks.prepare_step("owner", parent_task_id, tool_name="delegate_task")
    tasks.link_call_to_step("owner", parent_task_id, step["step_id"], tool_call_id=call["id"])
    tasks.claim_step("owner", parent_task_id, step["step_id"], receipt_id=receipt_id)
    receipts.start(receipt_id)


def test_recovery_retains_parent_wait_when_child_is_queued(delegation_env):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks, goal="resume after restart")
    _create_waiting_child(store)
    recovered = tasks.recover_incomplete("owner", receipts)
    parent = store.get_task("owner", "parent-task")
    assert parent["phase"] == "awaiting_child"
    assert parent["status"] == "queued"
    assert any(item["task_id"] == "parent-task" and item.get("phase") == "awaiting_child" for item in recovered)


def test_recovery_rejects_parent_receipt_with_mismatched_child_manifest(delegation_env):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(
        store, receipts, tasks, goal="resume after restart",
        budget={"max_steps": 1, "timeout_seconds": 5, "max_tokens": 100},
    )
    _create_waiting_child(store)
    tasks.recover_incomplete("owner", receipts)
    parent = store.get_task("owner", "parent-task")
    child = store.get_task("owner", "child-restart")
    assert parent["status"] == "needs_reconciliation"
    assert parent["phase"] == "partial"
    assert child["status"] == "queued"


def test_child_and_parent_wait_state_survive_database_reopen(tmp_path):
    db_path = tmp_path / "delegation-restart.db"
    first_connection = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    first_store = SessionStore(connection=first_connection)
    first_receipts = ReceiptStore(first_connection)
    first_receipts.ensure_schema()
    first_tasks = TaskController(first_store)
    parent = first_store.create_task_run(
        "owner", "session", "parent objective", task_id="parent-disk",
        channel_id="channel", thread_id="thread",
    )
    first_tasks.transition_phase("owner", parent["task_id"], "planning", next_action="delegate")
    _record_parent_delegation_receipt(
        first_store, first_receipts, first_tasks,
        parent_task_id="parent-disk", session_id="session",
        goal="persisted child goal",
    )
    first_store.create_child_task_and_await_parent(
        "owner", parent["task_id"], child_task_id="child-disk",
        child_session_id="child:child-disk", objective="persisted child goal",
        expected_parent_status=first_store.get_task("owner", parent["task_id"])["status"],
        expected_parent_version=int(first_store.get_task("owner", parent["task_id"])["version"]),
        expected_parent_phase="planning", next_action="await_child:child-disk",
        allowed_tools=["search_gmail"],
        budget={"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
        delegation_grant_id="delegation_grant_test-disk",
    )
    first_connection.close()

    reopened_connection = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    reopened_store = SessionStore(connection=reopened_connection)
    reopened_receipts = ReceiptStore(reopened_connection)
    reopened_receipts.ensure_schema()
    reopened_tasks = TaskController(reopened_store)
    children = reopened_store.get_child_tasks("owner", "parent-disk")
    assert len(children) == 1
    assert children[0]["task_id"] == "child-disk"
    assert children[0]["objective"] == "persisted child goal"
    assert children[0]["status"] == "queued"
    recovered = reopened_tasks.recover_incomplete("owner", reopened_receipts)
    parent_after_restart = reopened_store.get_task("owner", "parent-disk")
    assert parent_after_restart["phase"] == "awaiting_child"
    assert parent_after_restart["status"] == "queued"
    assert any(item["task_id"] == "parent-disk" for item in recovered)
    reopened_connection.close()


@pytest.mark.asyncio
async def test_recovered_queued_child_resumes_from_durable_scope_without_new_child(delegation_env):
    store, receipts, tasks, controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)
    calls = []

    async def runner(child_id, user_id, goal, allowed_tools, context, budget):
        from src.agent.runtime import CURRENT_DELEGATION_GRANT, CURRENT_MAX_TOOL_STEPS

        calls.append(child_id)
        assert user_id == "owner"
        assert goal == "resume after restart"
        assert allowed_tools == ["search_gmail"]
        assert context["recovered"] is True
        assert budget.max_steps == 2
        assert CURRENT_MAX_TOOL_STEPS.get() == 2
        assert CURRENT_DELEGATION_GRANT.get().child_task_id == child_id
        return DelegationResult(child_id, "parent-task", "succeeded", "Recovered result.")

    result = await controller.resume_queued_child(
        "owner", "child-restart", runner_fn=runner,
    )
    assert result.status == "succeeded"
    assert calls == ["child-restart"]
    assert len(store.get_child_tasks("owner", "parent-task")) == 1
    assert store.get_task("owner", "child-restart")["status"] == "succeeded"
    assert store.get_task("owner", "parent-task")["phase"] == "planning"


@pytest.mark.asyncio
async def test_recovery_scheduler_requeues_only_original_conversation_children(
    delegation_env, monkeypatch
):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)
    from src.services import llm
    resumed = []

    async def runner(child_id, owner, _goal, tools, context, budget):
        resumed.append((child_id, owner, tools))
        return DelegationResult(child_id, "parent-task", "succeeded", "done")

    monkeypatch.setattr(llm, "_durable_session_store", lambda: store)
    monkeypatch.setattr(llm, "_run_delegated_child_runtime", runner)
    scheduled = llm._schedule_recovered_delegated_children(
        store, "owner", session_id="different-session",
        channel_id="channel", thread_id="thread",
    )
    assert scheduled == []
    scheduled = llm._schedule_recovered_delegated_children(
        store, "owner", session_id="parent-session",
        channel_id="channel", thread_id="thread",
    )
    assert len(scheduled) == 1
    await asyncio.gather(*scheduled)
    assert resumed == [("child-restart", "owner", ["search_gmail"])]
    assert store.get_task("owner", "parent-task")["phase"] == "planning"


@pytest.mark.asyncio
async def test_recovered_child_with_ambiguous_receipt_is_never_resumed(delegation_env):
    store, receipts, tasks, controller = delegation_env
    _create_waiting_child(store)
    child = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    store.transition_task_run(
        "owner", "child-restart", expected_status="queued",
        expected_version=int(child["version"]), new_status="running",
    )
    tasks.transition_phase("owner", "child-restart", "planning", next_action="execute")
    tasks.transition_phase("owner", "child-restart", "executing", next_action="side_effect")
    child = store.get_task("owner", "child-restart")
    store.begin_turn("owner", child["session_key"], turn_id="ambiguous-recovery-turn")
    call = store.record_tool_call(
        "owner", child["session_key"], tool_name="monitor_add_rule",
        arguments={"name": "maybe-created"}, call_id="ambiguous-recovery-call",
        turn_id="ambiguous-recovery-turn", status="running",
    )
    receipts.prepare(
        receipt_id="ambiguous-recovery-receipt", call_id="ambiguous-recovery-call",
        user_id="owner", turn_id="ambiguous-recovery-turn", round_id=1,
        tool_name="monitor_add_rule", origin="native", arguments={"name": "maybe-created"},
    )
    step = tasks.prepare_step("owner", "child-restart", tool_name="monitor_add_rule")
    tasks.link_call_to_step(
        "owner", "child-restart", step["step_id"], tool_call_id=call["id"]
    )
    tasks.claim_step(
        "owner", "child-restart", step["step_id"], receipt_id="ambiguous-recovery-receipt"
    )
    receipts.start("ambiguous-recovery-receipt")

    async def should_not_run(*_args):
        raise AssertionError("an ambiguous child must never be dispatched again")

    with pytest.raises(ValueError, match="safely queued"):
        await controller.resume_queued_child(
            "owner", "child-restart", runner_fn=should_not_run,
        )
    child_after = store.get_task("owner", "child-restart")
    assert child_after["status"] == "needs_reconciliation"
    assert receipts.get("ambiguous-recovery-receipt").status == "started"


@pytest.mark.asyncio
async def test_recovered_child_does_not_get_a_fresh_timeout_budget(delegation_env):
    store, receipts, tasks, controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks, goal="resume after restart")
    _create_waiting_child(store)
    store.connection.execute(
        "UPDATE task_runs SET created_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
        ("child-restart",),
    )

    async def should_not_run(*_args):
        raise AssertionError("expired child time budget must prevent redispatch")

    result = await controller.resume_queued_child(
        "owner", "child-restart", runner_fn=should_not_run,
    )
    assert result.status == "failed"
    assert "time budget" in result.summary
    assert store.get_task("owner", "child-restart")["status"] == "failed"
    assert store.get_task("owner", "parent-task")["phase"] == "planning"


def test_recovery_parks_terminal_child_without_parent_receipt_or_missing_child(delegation_env):
    store, receipts, tasks, _controller = delegation_env
    _create_waiting_child(store)
    child = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    store.transition_task_run(
        "owner", "child-restart", expected_status="queued", expected_version=int(child["version"]),
        new_status="succeeded", event_type="task.completed",
    )
    tasks.recover_incomplete("owner", receipts)
    parent = store.get_task("owner", "parent-task")
    assert parent["status"] == "needs_reconciliation" and parent["phase"] == "partial"

    store2, receipts2, tasks2, _controller2 = _make_env()
    tasks2.transition_phase("owner", "parent-task", "awaiting_child", next_action="await_child:lost")
    tasks2.recover_incomplete("owner", receipts2)
    parent2 = store2.get_task("owner", "parent-task")
    assert parent2["status"] == "needs_reconciliation" and parent2["phase"] == "partial"


def test_recovery_confirms_parent_delegate_receipt_from_terminal_child(delegation_env):
    store, receipts, tasks, _controller = delegation_env
    turn_id = "parent-delegate-turn"
    store.begin_turn(
        "owner", "parent-session", turn_id=turn_id,
        channel_id="channel", thread_id="thread",
    )
    call = store.record_tool_call(
        "owner", "parent-session", tool_name="delegate_task",
        arguments={
            "goal": "resume after restart", "allowed_tools": ["search_gmail"],
            "budget": {"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
        }, call_id="parent-delegate-call",
        turn_id=turn_id, status="running", channel_id="channel", thread_id="thread",
    )
    receipts.prepare(
        receipt_id="parent-delegate-receipt", call_id="parent-delegate-call",
        user_id="owner", turn_id=turn_id, round_id=1, tool_name="delegate_task",
        origin="native", arguments={
            "goal": "resume after restart", "allowed_tools": ["search_gmail"],
            "budget": {"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
        },
    )
    step = tasks.prepare_step("owner", "parent-task", tool_name="delegate_task")
    tasks.link_call_to_step("owner", "parent-task", step["step_id"], tool_call_id=call["id"])
    tasks.claim_step("owner", "parent-task", step["step_id"], receipt_id="parent-delegate-receipt")
    receipts.start("parent-delegate-receipt")
    _create_waiting_child(store)
    child = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    store.transition_task_run(
        "owner", "child-restart", expected_status="queued", expected_version=int(child["version"]),
        new_status="succeeded",
    )

    tasks.recover_incomplete("owner", receipts)
    parent = store.get_task("owner", "parent-task", include_steps=True)
    assert parent["phase"] == "planning" and parent["status"] == "queued"
    assert parent["steps"][0]["status"] == "succeeded"
    assert receipts.get("parent-delegate-receipt").status == "confirmed"


def test_parent_cancel_cascades_to_queued_child(delegation_env):
    store, _receipts, tasks, _controller = delegation_env
    _create_waiting_child(store)
    tasks.cancel("owner", "parent-task")
    assert store.get_task("owner", "parent-task")["status"] == "needs_reconciliation"
    assert store.get_task("owner", "child-restart")["status"] == "cancelled"


def test_child_cancellation_resumes_its_waiting_parent(delegation_env):
    store, _receipts, tasks, _controller = delegation_env
    _create_waiting_child(store)
    tasks.cancel("owner", "child-restart")
    parent = store.get_task("owner", "parent-task")
    assert store.get_task("owner", "child-restart")["status"] == "cancelled"
    # Without a linked parent delegation receipt, recovery fails closed rather
    # than allowing the parent model to dispatch a duplicate child.
    assert parent["phase"] == "partial"
    assert parent["status"] == "needs_reconciliation"
    with pytest.raises(PermissionError, match="TASK_NOT_DISPATCHABLE"):
        tasks.validate_tool_dispatch("owner", "child-restart", "search_gmail")


def test_cancel_with_started_child_receipt_stays_reconciliation(delegation_env):
    store, receipts, tasks, _controller = delegation_env
    _create_waiting_child(store)
    child = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    store.transition_task_run(
        "owner", "child-restart", expected_status="queued", expected_version=int(child["version"]),
        new_status="running",
    )
    tasks.transition_phase("owner", "child-restart", "planning", next_action="execute")
    tasks.transition_phase("owner", "child-restart", "executing", next_action="side_effect")
    child = store.get_task("owner", "child-restart")
    turn_id = "child-cancel-turn"
    store.begin_turn(
        "owner", child["session_key"], turn_id=turn_id,
        channel_id=child["channel_id"], thread_id=child["thread_id"],
    )
    call = store.record_tool_call(
        "owner", child["session_key"], tool_name="monitor_add_rule",
        arguments={"name": "unknown"}, call_id="cancel-call", turn_id=turn_id,
        status="running", channel_id=child["channel_id"], thread_id=child["thread_id"],
    )
    receipts.prepare(
        receipt_id="cancel-receipt", call_id="cancel-call", user_id="owner",
        turn_id=turn_id, round_id=1, tool_name="monitor_add_rule", origin="native",
        arguments={"name": "unknown"},
    )
    step = tasks.prepare_step("owner", "child-restart", tool_name="monitor_add_rule")
    tasks.link_call_to_step("owner", "child-restart", step["step_id"], tool_call_id=call["id"])
    tasks.claim_step("owner", "child-restart", step["step_id"], receipt_id="cancel-receipt")
    receipts.start("cancel-receipt")

    parent = tasks.cancel("owner", "parent-task")
    child = store.get_task("owner", "child-restart", include_steps=True)
    assert child["status"] == "needs_reconciliation"
    assert child["steps"][0]["status"] == "needs_reconciliation"
    assert receipts.get("cancel-receipt").status == "started"
    assert parent["status"] == "needs_reconciliation"
    with pytest.raises(PermissionError, match="TASK_NOT_DISPATCHABLE"):
        tasks.validate_tool_dispatch("owner", "child-restart", "monitor_add_rule", {"name": "unknown"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workers, provider_limit, expected_max",
    [(1, 1, 1), (3, 3, 3), (3, 1, 1)],
)
async def test_delegated_children_use_scheduler_provider_capacity(
    delegation_env, monkeypatch, workers, provider_limit, expected_max
):
    store, _receipts, tasks, controller = delegation_env
    provider = f"delegation_test_{uuid.uuid4().hex}"
    monkeypatch.setenv(f"{provider.upper()}_CONCURRENCY", str(provider_limit))
    scheduler = CapacityAwareScheduler(
        provider_name=provider, max_workers=workers,
        max_active_tasks_per_owner=workers,
    )
    set_global_scheduler(scheduler)
    active = 0
    observed_max = 0
    active_tool_steps = 0
    observed_tool_max = 0
    lock = asyncio.Lock()

    async def inference():
        nonlocal active, observed_max
        async with lock:
            active += 1
            observed_max = max(observed_max, active)
        await asyncio.sleep(0.02)
        async with lock:
            active -= 1
        return "ok"

    async def tool_step():
        nonlocal active_tool_steps, observed_tool_max
        async with lock:
            active_tool_steps += 1
            observed_tool_max = max(observed_tool_max, active_tool_steps)
        await asyncio.sleep(0.02)
        async with lock:
            active_tool_steps -= 1

    try:
        parent_ids = ["parent-task"]
        for index in range(1, 6):
            parent_id = f"parent-{index}"
            store.create_task_run(
                "owner", f"session-{index}", "delegate", task_id=parent_id,
                channel_id="channel", thread_id=f"thread-{index}",
            )
            tasks.transition_phase("owner", parent_id, "planning", next_action="delegate")
            parent_ids.append(parent_id)

        async def delegate_one(parent_id):
            async def runner(child_id, user_id, goal, allowed, context, budget):
                await schedule_work(
                    child_id, user_id, "background", inference,
                    unit_type="inference", provider=provider,
                )
                await tool_step()
                return DelegationResult(child_id, parent_id, "succeeded", "done")

            return await controller.delegate(
                "owner", parent_id, "bounded child inference",
                allowed_tools=["search_gmail"], parent_allowed_tools=["search_gmail"],
                budget=_budget(), runner_fn=runner,
            )

        results = await asyncio.gather(*(delegate_one(parent_id) for parent_id in parent_ids))
        assert all(result.status == "succeeded" for result in results)
        assert observed_max == expected_max
        assert observed_tool_max == expected_max
    finally:
        reset_global_scheduler()
