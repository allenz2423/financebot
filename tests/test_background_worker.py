from __future__ import annotations

import asyncio
import uuid

import pytest

from src.agent.delegation import DelegationResult
from src.agent.task_controller import TaskController
from src.db.session_store import SessionStore
from src.services.tool_receipts import ReceiptStore
from src.services import llm

from test_delegation import (
    _create_waiting_child,
    _record_parent_delegation_receipt,
    delegation_env,
)


def _make_second_owner_waiting_child(store: SessionStore, receipts: ReceiptStore) -> None:
    """Create a durable delegated child for another owner in the shared test DB."""
    tasks = TaskController(store)
    parent = store.create_task_run(
        "owner-b", "owner-b-session", "delegate work", task_id="parent-b",
        channel_id="channel-b", thread_id="thread-b",
    )
    tasks.transition_phase("owner-b", "parent-b", "planning", next_action="delegate")

    arguments = {
        "goal": "resume after restart",
        "allowed_tools": ["search_gmail"],
        "budget": {"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
    }
    turn_id = f"parent-b-turn-{uuid.uuid4().hex}"
    store.begin_turn(
        "owner-b", "owner-b-session", turn_id=turn_id,
        channel_id="channel-b", thread_id="thread-b",
    )
    call = store.record_tool_call(
        "owner-b", "owner-b-session", tool_name="delegate_task",
        arguments=arguments, call_id=f"parent-b-call-{uuid.uuid4().hex}",
        turn_id=turn_id, status="running", channel_id="channel-b", thread_id="thread-b",
    )
    receipt_id = f"parent-b-receipt-{uuid.uuid4().hex}"
    receipts.prepare(
        receipt_id=receipt_id, call_id=call["call_key"], user_id="owner-b",
        turn_id=turn_id, round_id=1, tool_name="delegate_task", origin="native",
        arguments=arguments,
    )
    step = tasks.prepare_step("owner-b", "parent-b", tool_name="delegate_task")
    tasks.link_call_to_step("owner-b", "parent-b", step["step_id"], tool_call_id=call["id"])
    tasks.claim_step("owner-b", "parent-b", step["step_id"], receipt_id=receipt_id)
    receipts.start(receipt_id)

    current = store.get_task("owner-b", "parent-b")
    store.create_child_task_and_await_parent(
        "owner-b", "parent-b", child_task_id="child-b",
        child_session_id="child:child-b", objective="resume after restart",
        expected_parent_status=current["status"],
        expected_parent_version=int(current["version"]),
        expected_parent_phase=current["phase"], next_action="await_child:child-b",
        allowed_tools=["search_gmail"],
        budget={"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
        delegation_grant_id="delegation_grant_owner_b",
    )


@pytest.fixture(autouse=True)
def _reset_background_worker_state(monkeypatch):
    llm._RECOVERED_CHILD_RESUMPTIONS.clear()
    llm._RECOVERED_CHILD_TASKS.clear()
    monkeypatch.setattr(llm, "_BACKGROUND_COMPLETION_DELIVERY", None)
    yield
    llm._RECOVERED_CHILD_RESUMPTIONS.clear()
    llm._RECOVERED_CHILD_TASKS.clear()


@pytest.mark.asyncio
async def test_one_time_startup_recovery_resumes_safe_queued_and_running_children_once(
    delegation_env, monkeypatch
):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)
    _make_second_owner_waiting_child(store, receipts)
    # Simulate a safe task interrupted after planning began. Startup receipt
    # recovery should make this queued for a single safe resume.
    child_b = store.get_task("owner-b", "child-b", include_steps=False, include_events=False)
    store.transition_task_run(
        "owner-b", "child-b", expected_status="queued",
        expected_version=int(child_b["version"]), new_status="running",
    )
    TaskController(store).transition_phase(
        "owner-b", "child-b", "planning", next_action="resume_after_restart"
    )
    resumed = []
    delivered = []

    async def runner(child_id, owner, _goal, _tools, context, _budget):
        resumed.append((owner, child_id, bool(context["recovered"])))
        parent_id = "parent-task" if owner == "owner" else "parent-b"
        return DelegationResult(child_id, parent_id, "succeeded", "safe resume")

    async def completion_callback(owner, child_id, result):
        delivered.append((owner, child_id, result.status))

    monkeypatch.setattr(llm, "_durable_session_store", lambda: store)
    monkeypatch.setattr(llm, "_run_delegated_child_runtime", runner)
    llm.set_background_completion_delivery(completion_callback)

    # Startup is an explicit awaited operation. The recovery API owns discovery,
    # reconciliation and dispatch; it must not require a live interaction.
    await llm.recover_durable_background_tasks_once()
    active = list(llm._RECOVERED_CHILD_TASKS)
    assert len(active) == 2
    await asyncio.gather(*active)

    assert set(resumed) == {
        ("owner", "child-restart", True),
        ("owner-b", "child-b", True),
    }
    assert len(resumed) == 2
    assert set(delivered) == {
        ("owner", "child-restart", "succeeded"),
        ("owner-b", "child-b", "succeeded"),
    }
    assert len(delivered) == 2
    assert store.get_task("owner", "child-restart")["status"] == "succeeded"
    assert store.get_task("owner-b", "child-b")["status"] == "succeeded"


def test_invalid_delegation_spec_is_parked_instead_of_retried_forever(
    delegation_env, monkeypatch
):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)
    # Simulate a corrupt/unreadable parent-issued manifest without mutating the
    # append-only event ledger used as the source of truth.
    monkeypatch.setattr(
        store, "get_child_delegation_spec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid")),
    )
    child = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    scheduled = llm._schedule_recovered_delegated_children(
        store, candidate_children=[child]
    )

    assert scheduled == []
    assert store.get_task("owner", "child-restart", include_steps=False)["status"] == "needs_reconciliation"
    parent = store.get_task("owner", "parent-task", include_steps=False)
    assert parent["status"] == "partial"
    assert parent["phase"] == "partial"
    assert any(
        event["event_type"] == "background.child_parked_undispatchable"
        and event["payload"] == {"reason_code": "invalid_delegation_spec"}
        for event in store.get_task("owner", "child-restart")["events"]
    )


@pytest.mark.asyncio
async def test_pre_dispatch_resume_error_is_parked(delegation_env, monkeypatch):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)

    async def fail_before_claim(*_args, **_kwargs):
        raise RuntimeError("temporary pre-dispatch error")

    from src.agent.delegation import DelegationController

    monkeypatch.setattr(DelegationController, "resume_queued_child", fail_before_claim)
    child = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    scheduled = llm._schedule_recovered_delegated_children(
        store, candidate_children=[child]
    )
    await asyncio.gather(*scheduled)

    parked = store.get_task("owner", "child-restart")
    parent = store.get_task("owner", "parent-task", include_steps=False)
    assert parked["status"] == "needs_reconciliation"
    assert parent["status"] == "partial"
    assert any(
        event["event_type"] == "background.child_parked_undispatchable"
        and event["payload"] == {"reason_code": "resume_recovery_failed"}
        for event in parked["events"]
    )


@pytest.mark.asyncio
async def test_post_claim_recovery_error_parks_running_child(delegation_env, monkeypatch):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)

    async def fail_after_claim(_self, owner, child_id, **_kwargs):
        child = store.get_task(owner, child_id, include_steps=False, include_events=False)
        store.transition_task_run(
            owner, child_id, expected_status="queued",
            expected_version=int(child["version"]), new_status="running",
        )
        raise RuntimeError("failure after durable claim")

    from src.agent.delegation import DelegationController

    monkeypatch.setattr(DelegationController, "resume_queued_child", fail_after_claim)
    child = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    scheduled = llm._schedule_recovered_delegated_children(
        store, candidate_children=[child]
    )
    await asyncio.gather(*scheduled)

    parked = store.get_task("owner", "child-restart")
    parent = store.get_task("owner", "parent-task", include_steps=False)
    assert parked["status"] == "needs_reconciliation"
    assert parent["status"] == "partial"
    assert any(
        event["event_type"] == "background.child_parked_undispatchable"
        and event["payload"] == {"reason_code": "resume_recovery_failed"}
        for event in parked["events"]
    )


@pytest.mark.asyncio
async def test_startup_recovery_error_parks_exact_child_and_parent(
    delegation_env, monkeypatch
):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)

    def fail_owner_recovery(_self, owner, _receipts):
        if owner == "owner":
            raise RuntimeError("receipt store unavailable")
        return []

    monkeypatch.setattr(TaskController, "recover_incomplete", fail_owner_recovery)
    monkeypatch.setattr(llm, "_durable_session_store", lambda: store)

    await llm.recover_durable_background_tasks_once()

    child = store.get_task("owner", "child-restart", include_steps=False)
    parent = store.get_task("owner", "parent-task", include_steps=False)
    assert child["status"] == "needs_reconciliation"
    assert parent["status"] == "partial"
    assert any(
        event["event_type"] == "background.child_parked_undispatchable"
        and event["payload"] == {"reason_code": "startup_recovery_failed"}
        for event in child["events"]
    )


@pytest.mark.asyncio
async def test_startup_queue_pagination_advances_even_when_a_page_is_filtered(
    monkeypatch,
):
    import sqlite3

    connection = sqlite3.connect(":memory:")
    seen_pages = []
    bad_page = [
        {"user_id": "bad", "task_id": f"bad-{i}", "enqueued_at": f"{i:04d}"}
        for i in range(500)
    ]
    good_row = {"user_id": "good", "task_id": "good-1", "enqueued_at": "0500"}

    class FakeStore:
        def __init__(self):
            self.connection = connection

        def list_recoverable_background_children(
            self, *, include_running, after=None, **_kwargs
        ):
            if include_running:
                return ([bad_page[0], good_row] if after is None else [])
            if after is None:
                return bad_page
            return [good_row]

    fake_store = FakeStore()
    scheduled = []

    def fail_only_bad_owner(_self, owner, _receipts):
        if owner == "bad":
            raise RuntimeError("cannot recover this owner's tasks")
        return []

    def record_schedule(_store, *, candidate_children, scheduled_owners):
        scheduled.extend(candidate_children)
        return []

    async def no_delivery(_store):
        return None

    monkeypatch.setattr(TaskController, "recover_incomplete", fail_only_bad_owner)
    monkeypatch.setattr(llm, "_durable_session_store", lambda: fake_store)
    monkeypatch.setattr(llm, "_park_undispatchable_background_child", lambda *_a, **_k: True)
    monkeypatch.setattr(llm, "_schedule_recovered_delegated_children", record_schedule)
    monkeypatch.setattr(llm, "deliver_pending_background_completion_notices", no_delivery)

    await llm.recover_durable_background_tasks_once()

    assert scheduled == [good_row]
    assert connection is not None
@pytest.mark.asyncio
async def test_periodic_loop_only_polls_terminal_notices_and_never_recovers_live_tasks(
    delegation_env, monkeypatch
):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)
    # A live task must be left byte-for-byte at the same status/version by
    # periodic notice polling; only the awaited startup pass may reconcile it.
    live = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    store.transition_task_run(
        "owner", "child-restart", expected_status="queued",
        expected_version=int(live["version"]), new_status="running",
    )
    before = store.get_task("owner", "child-restart", include_steps=False, include_events=False)

    store.create_task_run(
        "notice-owner", "notice-parent-session", "parent", task_id="notice-parent",
        channel_id="notice-channel",
    )
    store.create_task_run(
        "notice-owner", "notice-child-session", "terminal task", task_id="notice-child",
        parent_task_id="notice-parent", lane="background", status="succeeded",
        channel_id="notice-channel",
    )
    callback_attempts = []
    poll_count = 0
    queue_scans = []
    real_sleep = asyncio.sleep

    original_list_pending = store.list_pending_background_completion_notices

    def list_pending(*, limit=500):
        nonlocal poll_count
        poll_count += 1
        return original_list_pending(limit=limit)

    async def completion_callback(owner, child_id, result):
        callback_attempts.append((owner, child_id, result.status))
        assert store.begin_background_completion_notice(owner, child_id)
        # Simulate a worker crash after durable at-most-once claim and before
        # it can record delivery. The next poll must not retry this notice.
        raise RuntimeError("crash after notice claim")

    async def controlled_sleep(_delay):
        # One initial sleep, then let two polling passes run and stop the loop.
        if poll_count < 2:
            await real_sleep(0)
            return
        raise asyncio.CancelledError

    def forbidden_recovery(*_args, **_kwargs):
        pytest.fail("periodic completion polling must not recover live task receipts")

    original_list_recoverable = store.list_recoverable_background_children

    def list_queued_only(**kwargs):
        queue_scans.append(kwargs)
        assert kwargs.get("include_running") is False
        return original_list_recoverable(**kwargs)

    monkeypatch.setattr(store, "list_pending_background_completion_notices", list_pending)
    monkeypatch.setattr(store, "list_recoverable_background_children", list_queued_only)
    monkeypatch.setattr(TaskController, "recover_incomplete", forbidden_recovery)
    monkeypatch.setattr(llm, "_durable_session_store", lambda: store)
    llm.set_background_completion_delivery(completion_callback)
    monkeypatch.setattr(llm.asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await llm.durable_background_recovery_loop()

    after = store.get_task("owner", "child-restart", include_steps=False, include_events=False)
    assert (after["status"], after["version"], after["phase"]) == (
        before["status"], before["version"], before["phase"]
    )
    assert poll_count == 2
    assert len(queue_scans) == 2
    assert callback_attempts == [("notice-owner", "notice-child", "succeeded")]
    assert store.list_pending_background_completion_notices() == []


def test_interaction_recovery_excludes_child_already_active_in_this_process(delegation_env):
    store, receipts, tasks, _controller = delegation_env
    _record_parent_delegation_receipt(store, receipts, tasks)
    _create_waiting_child(store)
    child = store.get_task(
        "owner", "child-restart", include_steps=False, include_events=False
    )
    store.transition_task_run(
        "owner", "child-restart", expected_status="queued",
        expected_version=int(child["version"]), new_status="running",
    )
    tasks.transition_phase("owner", "child-restart", "planning", next_action="resume")
    tasks.transition_phase("owner", "child-restart", "executing", next_action="execute")
    before = store.get_task(
        "owner", "child-restart", include_steps=False, include_events=False
    )

    from src.agent.delegation import ACTIVE_DELEGATION_CHILDREN
    ACTIVE_DELEGATION_CHILDREN["child-restart"] = "owner"
    try:
        recovered = tasks.recover_incomplete(
            "owner", receipts, exclude_task_ids=frozenset(ACTIVE_DELEGATION_CHILDREN)
        )
    finally:
        ACTIVE_DELEGATION_CHILDREN.pop("child-restart", None)

    after = store.get_task(
        "owner", "child-restart", include_steps=False, include_events=False
    )
    assert all(item["task_id"] != "child-restart" for item in recovered)
    assert (after["status"], after["version"], after["phase"]) == (
        before["status"], before["version"], before["phase"]
    )


@pytest.mark.asyncio
async def test_periodic_notice_callback_receives_terminal_result_once(
    delegation_env, monkeypatch
):
    store, _receipts, _tasks, _controller = delegation_env
    store.create_task_run(
        "notice-owner", "notice-parent-session", "parent", task_id="notice-parent",
        channel_id="notice-channel",
    )
    store.create_task_run(
        "notice-owner", "notice-child-session", "terminal task", task_id="notice-child",
        parent_task_id="notice-parent", lane="background", status="failed",
        channel_id="notice-channel",
    )
    delivered = []
    real_sleep = asyncio.sleep
    polls = 0

    async def completion_callback(owner, child_id, result):
        delivered.append((owner, child_id, result.status))
        store.claim_background_completion_notice(owner, child_id)

    async def controlled_sleep(_delay):
        nonlocal polls
        if polls < 1:
            await real_sleep(0)
            return
        raise asyncio.CancelledError

    original_list_pending = store.list_pending_background_completion_notices

    def list_pending(*, limit=500):
        nonlocal polls
        polls += 1
        return original_list_pending(limit=limit)

    monkeypatch.setattr(llm, "_durable_session_store", lambda: store)
    monkeypatch.setattr(store, "list_pending_background_completion_notices", list_pending)
    llm.set_background_completion_delivery(completion_callback)
    monkeypatch.setattr(llm.asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await llm.durable_background_recovery_loop()

    assert polls == 1
    assert delivered == [("notice-owner", "notice-child", "failed")]
