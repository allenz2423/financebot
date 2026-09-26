import sqlite3

import pytest

from src.db.session_store import (
    ConcurrentTaskUpdate,
    IdempotencyConflict,
    SessionNotFound,
    SessionStore,
    TaskNotFound,
)


def make_store():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return SessionStore(connection=connection, max_messages_per_session=3, max_turns_per_session=2, max_tool_calls_per_session=2)


def make_awaiting_background_child(
    store,
    *,
    owner: str,
    parent_id: str,
    child_id: str,
    child_status: str = "queued",
    lane: str = "background",
    parent_phase: str = "awaiting_child",
    awaited_child_id: str | None = None,
):
    parent = store.create_task_run(owner, f"{parent_id}-session", "parent", task_id=parent_id)
    store.connection.execute(
        "UPDATE task_runs SET phase=?, wait_reason=? WHERE task_id=?",
        (parent_phase, f"awaiting_child:{awaited_child_id or child_id}", parent["task_id"]),
    )
    store.connection.commit()
    return store.create_task_run(
        owner,
        f"{child_id}-session",
        "child",
        task_id=child_id,
        parent_task_id=parent_id,
        status=child_status,
        lane=lane,
    )


def test_scoped_session_turn_and_restart(tmp_path):
    path = tmp_path / "sessions.db"
    with SessionStore(path) as store:
        session = store.get_or_create_session("u1", "s1", channel_id="discord", thread_id="t1")
        turn = store.begin_turn("u1", "s1", channel_id="discord", thread_id="t1", turn_id="turn-1")
        message = store.add_message("u1", "s1", channel_id="discord", thread_id="t1", role="user", content="hello", message_id="m1", turn_id="turn-1")
        assert session["session_key"] == "s1"
        assert turn["status"] == "running"
        assert message["message_key"] == "m1"
        store.finish_turn("u1", "s1", "turn-1", channel_id="discord", thread_id="t1")

    with SessionStore(path) as reopened:
        assert reopened.get_session("u1", "s1", channel_id="discord", thread_id="t1")["id"] == session["id"]
        assert reopened.get_session("u1", "s1", channel_id="slack", thread_id="t1") is None
        assert reopened.list_messages("u1", "s1", channel_id="discord", thread_id="t1")[0]["content"] == "hello"


def test_message_and_tool_call_writes_are_idempotent_and_conflicts_are_rejected():
    with make_store() as store:
        store.begin_turn("u", "s", turn_id="t")
        first = store.add_message("u", "s", role="user", content="same", message_id="m", turn_id="t")
        again = store.add_message("u", "s", role="user", content="same", message_id="m", turn_id="t")
        assert first["id"] == again["id"]
        with pytest.raises(IdempotencyConflict):
            store.add_message("u", "s", role="user", content="different", message_id="m", turn_id="t")

        call = store.record_tool_call("u", "s", tool_name="read_ledger", arguments={"account": "checking"}, call_id="c", turn_id="t")
        retry = store.record_tool_call("u", "s", tool_name="read_ledger", arguments={"account": "checking"}, call_id="c", turn_id="t")
        assert call["id"] == retry["id"]
        assert store.finish_tool_call("u", "s", "c", result={"ok": True})["status"] == "succeeded"


def test_pre_persisted_tool_call_block_advances_pending_call_at_dispatch():
    with make_store() as store:
        store.begin_turn("u", "s", turn_id="t")
        pending = store.record_tool_call(
            "u", "s", tool_name="read_ledger", arguments={"account": "checking"},
            call_id="provider-call", turn_id="t", status="pending",
        )
        started = store.record_tool_call(
            "u", "s", tool_name="read_ledger", arguments={"account": "checking"},
            call_id="provider-call", turn_id="t", status="running",
        )
        assert started["id"] == pending["id"]
        assert started["status"] == "running"


def test_assistant_tool_call_block_persists_calls_and_ordered_manifest_atomically():
    with make_store() as store:
        store.begin_turn("u", "s", turn_id="t")
        task = store.create_task_run("u", "s", "objective", task_id="task-t")
        calls = store.persist_assistant_tool_call_block(
            "u", "s", task["task_id"], "t",
            [
                {"tool_name": "read_one", "arguments": {"n": 1}, "call_id": "call-1"},
                {"tool_name": "read_two", "arguments": {"n": 2}, "call_id": "call-2"},
            ],
        )
        assert [row["call_key"] for row in calls] == ["call-1", "call-2"]
        assert [row["arguments"] for row in calls] == [{"n": 1}, {"n": 2}]
        assert [row["status"] for row in calls] == ["pending", "pending"]
        event = store.get_task("u", task["task_id"])["events"][-1]
        assert event["event_type"] == "assistant.tool_call_block_persisted"
        assert event["payload"]["provider_call_ids"] == ["call-1", "call-2"]
        assert event["payload"]["tool_names"] == ["read_one", "read_two"]
        assert store.list_assistant_tool_call_blocks("u", task["task_id"]) == [{
            "event_id": event["event_id"],
            "calls": [
                {"call_id": "call-1", "tool_name": "read_one", "arguments": {"n": 1}, "status": "pending", "turn_id": "t"},
                {"call_id": "call-2", "tool_name": "read_two", "arguments": {"n": 2}, "status": "pending", "turn_id": "t"},
            ],
        }]

        with pytest.raises(IdempotencyConflict):
            store.persist_assistant_tool_call_block(
                "u", "s", task["task_id"], "t",
                [
                    {"tool_name": "new_call", "arguments": {"n": 3}, "call_id": "call-3"},
                    {"tool_name": "different", "arguments": {"n": 2}, "call_id": "call-2"},
                ],
            )
        assert store.get_task("u", task["task_id"])["events"][-1] == event
        assert store.connection.execute(
            "SELECT 1 FROM tool_calls WHERE call_key='call-3'"
        ).fetchone() is None


def test_task_phase_migration_backfills_existing_lifecycle_state():
    with make_store() as store:
        queued = store.create_task_run("u", "s", "queued", task_id="queued-task")
        running = store.create_task_run("u", "s", "running", task_id="running-task")
        running = store.transition_task_run(
            "u", running["task_id"], expected_status="queued",
            expected_version=int(running["version"]), new_status="running",
        )
        store.connection.execute(
            "UPDATE task_runs SET phase='received' WHERE task_id IN (?, ?)",
            (queued["task_id"], running["task_id"]),
        )
        from importlib import import_module
        import_module("src.db.migrations.010_durable_task_phases").apply(store.connection)
        assert store.get_task("u", queued["task_id"])["phase"] == "planning"
        assert store.get_task("u", running["task_id"])["phase"] == "executing"


def test_tool_call_block_rows_survive_ordinary_retention_cap():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = SessionStore(connection=connection, max_tool_calls_per_session=1)
    store.begin_turn("u", "s", turn_id="t")
    task = store.create_task_run("u", "s", "objective", task_id="task-t")
    store.persist_assistant_tool_call_block(
        "u", "s", task["task_id"], "t",
        [
            {"tool_name": "read_one", "arguments": {}, "call_id": "call-1"},
            {"tool_name": "read_two", "arguments": {}, "call_id": "call-2"},
        ],
    )
    assert len(store.list_assistant_tool_call_blocks("u", task["task_id"])[0]["calls"]) == 2


def test_bounded_retention_and_safe_fts_search_with_like_fallback():
    with make_store() as store:
        for index in range(5):
            store.add_message("u", "s", role="user", content=f"budget note {index}", message_id=f"m{index}")
        rows = store.list_messages("u", "s")
        assert [row["message_key"] for row in rows] == ["m2", "m3", "m4"]
        assert [row["message_key"] for row in store.search_messages("u", 'budget OR *; DROP TABLE messages;')]

        store._fts_enabled = False
        fallback = store.search_messages("u", "note 4")
        assert fallback and fallback[0]["message_key"] == "m4"


def test_migration_is_idempotent_and_fts_is_derived():
    with make_store() as store:
        store.connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('sessions','turns','messages','tool_calls','task_runs','task_steps','task_events')"
        )
        store.connection.commit()
        # Re-running the migration runner must not duplicate schema objects.
        from src.db.migrations import apply_all
        apply_all(store.connection)
        store.connection.commit()
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('task_runs','task_steps','task_events')"
        ).fetchone()[0] == 3
        store.add_message("u", "s", role="assistant", content="durable", message_id="a")
        if store._fts_enabled:
            assert store.connection.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 1


def test_task_steps_events_and_existing_evidence_links_survive_restart(tmp_path):
    path = tmp_path / "durable-tasks.db"
    with SessionStore(path) as store:
        store.begin_turn("owner-1", "session-1", turn_id="turn-1")
        call = store.record_tool_call(
            "owner-1", "session-1", tool_name="search_gmail",
            arguments={"query": "newer_than:1d"}, call_id="gmail-call", turn_id="turn-1",
        )
        from src.services.tool_receipts import ReceiptStore
        receipt = ReceiptStore(store.connection).prepare(
            receipt_id="gmail-receipt", call_id="gmail-call", user_id="owner-1",
            turn_id="turn-1", round_id=1, tool_name="search_gmail", origin="native",
            arguments={"query": "newer_than:1d"},
        )
        task = store.create_task_run(
            "owner-1", "session-1", "Find recent mail", task_id="task-1", lane="background"
        )
        first = store.add_task_step(
            "owner-1", task["task_id"], 0, step_id="step-1", status="ready",
            next_action="search_gmail", retry_policy={"mode": "safe_read"},
            description="Search the inbox", completion_criteria="A confirmed receipt has message IDs.",
            tool_call_id=call["id"], receipt_id=receipt.receipt_id,
        )
        second = store.add_task_step(
            "owner-1", task["task_id"], 1, step_id="step-2",
            dependency_ids=["step-1"], status="pending", next_action="summarize",
        )
        advanced = store.transition_task_step(
            "owner-1", task["task_id"], first["step_id"], expected_status="ready",
            expected_version=0, new_status="running",
        )
        task = store.get_task("owner-1", task["task_id"])
        task = store.transition_task_run(
            "owner-1", task["task_id"], expected_status="queued",
            expected_version=task["version"], new_status="running",
            current_step_id=advanced["step_id"],
        )
        assert second["dependency_ids"] == ["step-1"]

    with SessionStore(path) as reopened:
        recovered = reopened.get_task("owner-1", "task-1")
        assert recovered["status"] == "running"
        assert recovered["current_step_id"] == "step-1"
        assert recovered["steps"][0]["tool_call_id"] == call["id"]
        assert recovered["steps"][0]["receipt_id"] == "gmail-receipt"
        assert recovered["steps"][0]["retry_policy"] == {"mode": "safe_read"}
        assert recovered["steps"][0]["description"] == "Search the inbox"
        assert recovered["steps"][0]["completion_criteria"] == "A confirmed receipt has message IDs."
        assert recovered["steps"][0]["retry_count"] == 0
        assert [event["event_type"] for event in recovered["events"]] == [
            "task.created", "step.created", "step.created",
            "step.transitioned", "task.transitioned",
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            reopened.connection.execute(
                "UPDATE task_events SET event_type='rewritten' WHERE task_id='task-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            reopened.connection.execute("DELETE FROM task_events WHERE task_id='task-1'")
        with pytest.raises(sqlite3.IntegrityError):
            reopened.connection.execute("DELETE FROM task_runs WHERE task_id='task-1'")
        with pytest.raises(sqlite3.IntegrityError):
            reopened.connection.execute("DELETE FROM sessions WHERE id=?", (recovered["session_id"],))


def test_task_reads_and_evidence_links_are_owner_scoped():
    with make_store() as store:
        task = store.create_task_run("owner-a", "session-a", "private objective", task_id="task-a")
        store.add_task_step("owner-a", task["task_id"], 0, step_id="step-a")
        call = store.record_tool_call(
            "owner-b", "session-b", tool_name="read_ledger", call_id="other-owner-call"
        )
        from src.services.tool_receipts import ReceiptStore
        receipt = ReceiptStore(store.connection).prepare(
            receipt_id="other-owner-receipt", call_id="other-owner-call",
            user_id="owner-b", turn_id="turn-b", round_id=1,
            tool_name="read_ledger", origin="native", arguments={},
        )
        owner_call = store.record_tool_call(
            "owner-a", "session-a", tool_name="read_ledger", call_id="owner-call"
        )
        mismatched_receipt = ReceiptStore(store.connection).prepare(
            receipt_id="mismatched-receipt", call_id="different-call",
            user_id="owner-a", turn_id="turn-a", round_id=1,
            tool_name="read_ledger", origin="native", arguments={},
        )
        assert store.list_tasks("owner-b") == []
        with pytest.raises(TaskNotFound):
            store.get_task("owner-b", "task-a")
        with pytest.raises(TaskNotFound):
            store.get_task_step("owner-b", "task-a", "step-a")
        with pytest.raises(TaskNotFound):
            store.add_task_step("owner-b", "task-a", 0, step_id="foreign-step")
        with pytest.raises(TaskNotFound):
            store.add_task_step(
                "owner-a", "task-a", 1, step_id="foreign-call-step",
                tool_call_id=call["id"],
            )
        with pytest.raises(TaskNotFound):
            store.add_task_step(
                "owner-a", "task-a", 1, step_id="foreign-receipt-step",
                receipt_id=receipt.receipt_id,
            )
        with pytest.raises(ValueError, match="same call ID"):
            store.add_task_step(
                "owner-a", "task-a", 1, step_id="mismatched-evidence-step",
                tool_call_id=owner_call["id"], receipt_id=mismatched_receipt.receipt_id,
            )
        with pytest.raises(ValueError, match="unsupported task event payload field"):
            store.append_task_event(
                "owner-a", "task-a", "bad.event",
                payload={"nested": {"receipt_status": "confirmed"}},
            )
        with pytest.raises(ValueError, match="unsupported task event payload field"):
            store.append_task_event(
                "owner-a", "task-a", "bad.event",
                payload={"tool_arguments": {"account": "checking"}},
            )
        with pytest.raises(ValueError, match="unsupported task event payload field"):
            store.append_task_event(
                "owner-a", "task-a", "bad.event",
                payload={"receipt": {"status": "confirmed"}},
            )
        with pytest.raises(ValueError, match="unsupported task event payload field"):
            store.append_task_event(
                "owner-a", "task-a", "bad.event",
                payload={"result": {"balance": 123}},
            )
        with pytest.raises(SessionNotFound):
            store.list_messages("owner-b", "session-a")


def test_recoverable_background_children_are_fair_and_exclude_nonmatching_tasks():
    with make_store() as store:
        # One owner has several eligible children; a second owner must still
        # receive a slot in the same bounded startup sweep.
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-a-1", child_id="child-a-1"
        )
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-a-2", child_id="child-a-2"
        )
        make_awaiting_background_child(
            store, owner="owner-b", parent_id="parent-b-1", child_id="child-b-1"
        )

        # Background work without a parent is not a delegated child.
        store.create_task_run(
            "owner-a", "unrelated-session", "unrelated", task_id="unrelated-background",
            lane="background",
        )
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-not-awaiting",
            child_id="child-not-awaiting", parent_phase="planning",
        )
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-mismatched",
            child_id="child-mismatched", awaited_child_id="different-child",
        )
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-interactive",
            child_id="child-interactive", lane="interactive",
        )
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-terminal",
            child_id="child-terminal", child_status="succeeded",
        )

        rows = store.list_recoverable_background_children(limit=500)
        all_fair_rows = store.list_recoverable_background_children(
            limit=500, per_owner_limit=8
        )

        assert {row["task_id"] for row in rows} == {"child-a-1", "child-b-1"}
        assert {row["task_id"] for row in all_fair_rows} == {
            "child-a-1", "child-a-2", "child-b-1"
        }
        assert len([row for row in rows if row["user_id"] == "owner-a"]) == 1
        assert len([row for row in rows if row["user_id"] == "owner-b"]) == 1
        assert all(row["lane"] == "background" for row in rows)
        for row in rows:
            parent = store.get_task(row["user_id"], row["parent_task_id"])
            assert parent["phase"] == "awaiting_child"
            assert parent["wait_reason"] == f"awaiting_child:{row['task_id']}"


def test_recoverable_background_children_are_owner_scoped_and_limit_is_bounded():
    with make_store() as store:
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-a-1", child_id="child-a-1"
        )
        make_awaiting_background_child(
            store, owner="owner-b", parent_id="parent-b-1", child_id="child-b-1"
        )
        make_awaiting_background_child(
            store, owner="owner-c", parent_id="parent-c-1", child_id="child-c-1"
        )

        rows = store.list_recoverable_background_children(limit=2)

        assert len(rows) == 2
        assert {row["user_id"] for row in rows} == {"owner-a", "owner-b"}
        assert {row["task_id"] for row in rows} == {"child-a-1", "child-b-1"}


def test_recoverable_background_children_cursor_pages_without_starving_later_owners():
    with make_store() as store:
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-a", child_id="child-a"
        )
        make_awaiting_background_child(
            store, owner="owner-b", parent_id="parent-b", child_id="child-b"
        )
        make_awaiting_background_child(
            store, owner="owner-c", parent_id="parent-c", child_id="child-c"
        )

        first = store.list_recoverable_background_children(limit=1)
        last = first[-1]
        next_page = store.list_recoverable_background_children(
            limit=2,
            after=(last["enqueued_at"], last["user_id"], last["task_id"]),
        )

        assert len(first) == 1 and len(next_page) == 2
        assert len({row["task_id"] for row in [*first, *next_page]}) == 3


@pytest.mark.parametrize(
    "status",
    ["succeeded", "partial", "failed", "cancelled", "needs_reconciliation"],
)
def test_background_completion_notice_can_only_be_claimed_for_terminal_child(status):
    with make_store() as store:
        child = make_awaiting_background_child(
            store,
            owner="owner-a",
            parent_id=f"parent-{status}",
            child_id=f"child-{status}",
            child_status=status,
        )

        assert store.claim_background_completion_notice("owner-a", child["task_id"])
        assert not store.claim_background_completion_notice("owner-a", child["task_id"])


def test_background_completion_notice_rejects_nonterminal_interactive_and_foreign_tasks():
    with make_store() as store:
        queued = make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-queued", child_id="child-queued"
        )
        interactive = make_awaiting_background_child(
            store,
            owner="owner-a",
            parent_id="parent-interactive-terminal",
            child_id="child-interactive-terminal",
            child_status="succeeded",
            lane="interactive",
        )

        assert not store.claim_background_completion_notice("owner-a", queued["task_id"])
        assert not store.claim_background_completion_notice("owner-a", interactive["task_id"])
        with pytest.raises(TaskNotFound):
            store.claim_background_completion_notice("owner-b", interactive["task_id"])


def test_background_completion_notice_claim_is_atomic_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "background-notice-claim.db"
    with SessionStore(path) as setup:
        child = make_awaiting_background_child(
            setup,
            owner="owner-a",
            parent_id="parent-terminal",
            child_id="child-terminal",
            child_status="succeeded",
        )
        child_id = child["task_id"]

    stores = [SessionStore(path), SessionStore(path)]
    barrier = Barrier(2)

    def claim(store):
        barrier.wait(timeout=5)
        return store.claim_background_completion_notice("owner-a", child_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, stores))
        assert sorted(outcomes) == [False, True]
        assert sum(
            event["event_type"] == "task.background_completion_notice_claimed"
            for event in stores[0].get_task("owner-a", child_id)["events"]
        ) == 1
    finally:
        for store in stores:
            store.close()


def test_pending_background_completion_notices_are_fair_and_exclude_started(tmp_path):
    with SessionStore(tmp_path / "background-notice-queue.db") as store:
        first = make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-a-1", child_id="terminal-a-1",
            child_status="succeeded",
        )
        make_awaiting_background_child(
            store, owner="owner-a", parent_id="parent-a-2", child_id="terminal-a-2",
            child_status="failed",
        )
        second = make_awaiting_background_child(
            store, owner="owner-b", parent_id="parent-b-1", child_id="terminal-b",
            child_status="needs_reconciliation",
        )
        assert store.begin_background_completion_notice("owner-a", first["task_id"])

        pending = store.list_pending_background_completion_notices(limit=10)

        assert {(row["user_id"], row["task_id"]) for row in pending} == {
            ("owner-a", "terminal-a-2"),
            ("owner-b", second["task_id"]),
        }
        assert len([row for row in pending if row["user_id"] == "owner-a"]) == 1
        assert store.begin_background_completion_notice("owner-a", "terminal-a-2")
        assert {row["task_id"] for row in store.list_pending_background_completion_notices()} == {
            second["task_id"]
        }


def test_background_notice_send_start_is_a_single_atomic_claim_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "background-notice-start.db"
    with SessionStore(path) as setup:
        make_awaiting_background_child(
            setup, owner="owner", parent_id="parent", child_id="child",
            child_status="succeeded",
        )
    stores = [SessionStore(path), SessionStore(path)]
    barrier = Barrier(2)

    def begin(store):
        barrier.wait(timeout=5)
        return store.begin_background_completion_notice("owner", "child")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(begin, stores))
        assert sorted(outcomes) == [False, True]
        events = stores[0].get_task("owner", "child")["events"]
        assert sum(e["event_type"] == "task.background_completion_notice_claimed" for e in events) == 1
        assert sum(e["event_type"] == "task.background_completion_notice_started" for e in events) == 1
    finally:
        for store in stores:
            store.close()


def test_background_child_queue_cap_is_enforced_in_creation_transaction(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_QUEUE_PER_OWNER", "1")
    with make_store() as store:
        first_parent = store.create_task_run(
            "owner", "parent-one-session", "parent one", task_id="parent-one"
        )
        first_parent = store.transition_task_phase(
            "owner", "parent-one", expected_phase="received",
            expected_version=int(first_parent["version"]), new_phase="planning",
            next_action="delegate",
        )
        store.create_child_task_and_await_parent(
            "owner", "parent-one", child_task_id="child-one",
            child_session_id="child:one", objective="first child",
            expected_parent_status=first_parent["status"],
            expected_parent_version=int(first_parent["version"]),
            expected_parent_phase=first_parent["phase"],
            next_action="await_child:child-one", allowed_tools=["search_gmail"],
            budget={"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
            delegation_grant_id="delegation_grant_one",
        )
        second_parent = store.create_task_run(
            "owner", "parent-two-session", "parent two", task_id="parent-two"
        )
        second_parent = store.transition_task_phase(
            "owner", "parent-two", expected_phase="received",
            expected_version=int(second_parent["version"]), new_phase="planning",
            next_action="delegate",
        )

        with pytest.raises(ValueError, match="background queue is full"):
            store.create_child_task_and_await_parent(
                "owner", "parent-two", child_task_id="child-two",
                child_session_id="child:two", objective="second child",
                expected_parent_status=second_parent["status"],
                expected_parent_version=int(second_parent["version"]),
                expected_parent_phase=second_parent["phase"],
                next_action="await_child:child-two", allowed_tools=["search_gmail"],
                budget={"max_steps": 2, "timeout_seconds": 5, "max_tokens": 100},
                delegation_grant_id="delegation_grant_two",
            )

        with pytest.raises(TaskNotFound):
            store.get_task("owner", "child-two")


def test_step_partial_status_and_task_linked_calls_survive_retention_pruning():
    with make_store() as store:
        store.begin_turn("u", "s", turn_id="old-linked-turn")
        call = store.record_tool_call(
            "u", "s", tool_name="read_ledger", call_id="old-linked-call",
            turn_id="old-linked-turn",
        )
        task = store.create_task_run("u", "s", "retain evidence", task_id="retained-task")
        step = store.add_task_step(
            "u", task["task_id"], 0, step_id="retained-step",
            tool_call_id=call["id"],
        )
        transitioned = store.transition_task_step(
            "u", task["task_id"], step["step_id"], expected_status="pending",
            expected_version=0, new_status="partial",
        )
        assert transitioned["status"] == "partial"
        store.finish_turn("u", "s", "old-linked-turn", status="completed")

        for index in range(5):
            later_turn = f"later-turn-{index}"
            store.begin_turn("u", "s", turn_id=later_turn)
            store.record_tool_call(
                "u", "s", tool_name="read_ledger", call_id=f"later-call-{index}",
                turn_id=later_turn,
            )
            store.finish_turn("u", "s", later_turn, status="completed")
        assert store.connection.execute(
            "SELECT id FROM tool_calls WHERE id=?", (call["id"],)
        ).fetchone()[0] == call["id"]
        assert store.get_task_step("u", task["task_id"], step["step_id"])["tool_call_id"] == call["id"]


def test_competing_task_and_step_transitions_use_status_version_cas(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "task-cas.db"
    with SessionStore(path) as setup:
        task = setup.create_task_run("owner-1", "session-1", "CAS objective", task_id="cas-task")
        step = setup.add_task_step("owner-1", task["task_id"], 0, step_id="cas-step")

    stores = [SessionStore(path), SessionStore(path)]

    def race_task(index, target_status):
        try:
            stores[index].transition_task_run(
                "owner-1", "cas-task", expected_status="queued", expected_version=1,
                new_status=target_status,
            )
            return "won"
        except ConcurrentTaskUpdate:
            return "lost"

    barrier = Barrier(2)

    def concurrent_task(item):
        index, target_status = item
        barrier.wait(timeout=5)
        return race_task(index, target_status)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(concurrent_task, enumerate(("running", "cancelled"))))
    assert sorted(outcomes) == ["lost", "won"]

    step_barrier = Barrier(2)

    def concurrent_step(item):
        index, target_status = item
        step_barrier.wait(timeout=5)
        try:
            stores[index].transition_task_step(
                "owner-1", "cas-task", "cas-step", expected_status="pending",
                expected_version=0, new_status=target_status,
            )
            return "won"
        except ConcurrentTaskUpdate:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        step_outcomes = list(pool.map(concurrent_step, enumerate(("ready", "cancelled"))))
    assert sorted(step_outcomes) == ["lost", "won"]
    for store in stores:
        store.close()


@pytest.mark.parametrize("child_status", ["queued", "running"])
def test_undispatchable_background_child_parks_exact_pair_with_reason_only_events(child_status):
    with make_store() as store:
        parent = store.create_task_run("owner-a", "parent-session", "parent", task_id="parent-a")
        store.connection.execute(
            "UPDATE task_runs SET phase='awaiting_child', wait_reason=? WHERE task_id=?",
            ("awaiting_child:child-a", parent["task_id"]),
        )
        store.connection.commit()
        child = store.create_task_run(
            "owner-a", "child-session", "child", task_id="child-a",
            parent_task_id="parent-a", lane="background",
        )
        if child_status == "running":
            child = store.transition_task_run(
                "owner-a", "child-a", expected_status="queued",
                expected_version=int(child["version"]), new_status="running",
            )
        unrelated = store.create_task_run(
            "owner-a", "other-session", "unrelated", task_id="unrelated-task"
        )

        parked = store.park_undispatchable_background_child(
            "owner-a", "parent-a", "child-a",
            expected_parent_status=parent["status"],
            expected_parent_version=int(parent["version"]),
            expected_child_status=child_status,
            expected_child_version=int(child["version"]),
            reason_code="invalid_dispatch_spec",
        )

        assert parked["child"]["status"] == "needs_reconciliation"
        assert parked["child"]["phase"] == "partial"
        assert parked["child"]["wait_reason"] is None
        assert parked["child"]["version"] == child["version"] + 1
        assert parked["parent"]["status"] == "partial"
        assert parked["parent"]["phase"] == "partial"
        assert parked["parent"]["wait_reason"] is None
        assert parked["parent"]["version"] == parent["version"] + 1
        assert store.get_task("owner-a", "unrelated-task")["version"] == unrelated["version"]

        for task_id, expected_type in (
            ("child-a", "background.child_parked_undispatchable"),
            ("parent-a", "background.parent_parked_undispatchable"),
        ):
            event = store.get_task("owner-a", task_id)["events"][-1]
            assert event["event_type"] == expected_type
            assert event["payload"] == {"reason_code": "invalid_dispatch_spec"}


def test_undispatchable_child_parking_rejects_wrong_owner_binding_and_stale_cas():
    with make_store() as store:
        parent = store.create_task_run("owner-a", "parent-session", "parent", task_id="parent-a")
        store.connection.execute(
            "UPDATE task_runs SET phase='awaiting_child', wait_reason=? WHERE task_id=?",
            ("awaiting_child:child-a", parent["task_id"]),
        )
        store.connection.commit()
        child = store.create_task_run(
            "owner-a", "child-session", "child", task_id="child-a",
            parent_task_id="parent-a", lane="background",
        )
        other_parent = store.create_task_run(
            "owner-a", "other-parent-session", "other parent", task_id="other-parent"
        )
        foreign_parent = store.create_task_run(
            "owner-b", "foreign-session", "foreign", task_id="foreign-parent"
        )

        def park(parent_id="parent-a", owner="owner-a", parent_version=None, child_version=None):
            return store.park_undispatchable_background_child(
                owner, parent_id, "child-a",
                expected_parent_status="queued",
                expected_parent_version=(parent["version"] if parent_version is None else parent_version),
                expected_child_status="queued",
                expected_child_version=(child["version"] if child_version is None else child_version),
                reason_code="dispatch_unavailable",
            )

        with pytest.raises(TaskNotFound):
            park(owner="owner-b", parent_id="foreign-parent")
        with pytest.raises(ConcurrentTaskUpdate):
            park(parent_id="other-parent", parent_version=other_parent["version"])
        with pytest.raises(ConcurrentTaskUpdate):
            park(parent_version=parent["version"] + 1)
        with pytest.raises(ConcurrentTaskUpdate):
            park(child_version=child["version"] + 1)
        store.connection.execute(
            "UPDATE task_runs SET wait_reason='awaiting_child:someone-else' WHERE task_id='parent-a'"
        )
        store.connection.commit()
        with pytest.raises(ConcurrentTaskUpdate):
            park()

        # Failed attempts are atomic: neither member of the pair nor their
        # append-only event histories are changed.
        assert store.get_task("owner-a", "parent-a")["status"] == "queued"
        assert store.get_task("owner-a", "child-a")["status"] == "queued"
        assert len(store.get_task("owner-a", "parent-a")["events"]) == 1
        assert len(store.get_task("owner-a", "child-a")["events"]) == 1
        assert store.get_task("owner-b", "foreign-parent")["version"] == foreign_parent["version"]


def test_undispatchable_child_parking_rejects_non_code_reason_values():
    with make_store() as store:
        with pytest.raises(ValueError, match="lowercase identifier"):
            store.park_undispatchable_background_child(
                "owner", "missing-parent", "missing-child",
                expected_parent_status="queued", expected_parent_version=0,
                expected_child_status="queued", expected_child_version=0,
                reason_code="provider failed: sensitive details",
            )
