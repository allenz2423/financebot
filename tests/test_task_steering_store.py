import sqlite3
import tempfile
import threading

import pytest

from src.db.session_store import (
    ConcurrentTaskUpdate,
    InvalidLifecycleTransition,
    SessionStore,
    TaskNotFound,
)
from src.services.tool_receipts import ReceiptStore


def make_store():
    return SessionStore(
        connection=sqlite3.connect(":memory:", check_same_thread=False),
        max_messages_per_session=3,
        max_turns_per_session=2,
        max_tool_calls_per_session=2,
    )


def make_task_with_steps(store, *, owner="owner", task_id="task", target_status="ready"):
    task = store.create_task_run(owner, f"{task_id}-session", "complete a workflow", task_id=task_id)
    prior = store.add_task_step(
        owner, task_id, 0, step_id=f"{task_id}-prior", status="succeeded",
        next_action="read_ledger",
    )
    target = store.add_task_step(
        owner, task_id, 1, step_id=f"{task_id}-target", status=target_status,
        next_action="summarize",
    )
    return store.get_task(owner, task_id), prior, target


def test_steering_appends_exact_step_correction_and_preserves_succeeded_work():
    with make_store() as store:
        task, prior, target = make_task_with_steps(store)

        result = store.steer_task_step(
            "owner", task["task_id"], target["step_id"],
            "Compare only transactions posted since yesterday.",
            expected_task_version=task["version"],
            expected_step_version=target["version"],
        )

        assert result["task"]["version"] == task["version"] + 1
        assert result["step"]["version"] == target["version"] + 1
        assert result["step"]["status"] == "ready"
        event = result["event"]
        assert event["event_type"] == "task.step_steered"
        assert event["step_id"] == target["step_id"]
        assert event["payload"]["steered_step_id"] == target["step_id"]
        assert event["payload"]["correction"] == "Compare only transactions posted since yesterday."

        persisted = store.get_task("owner", task["task_id"])
        assert persisted["steps"][0]["step_id"] == prior["step_id"]
        assert persisted["steps"][0]["status"] == "succeeded"
        assert persisted["steps"][0]["version"] == prior["version"]
        assert persisted["steps"][1]["next_action"] == "summarize"
        assert [event["event_type"] for event in persisted["events"]][-1] == "task.step_steered"


def test_steering_accepts_pending_step():
    with make_store() as store:
        task, _, target = make_task_with_steps(store, target_status="pending")
        result = store.steer_task_step(
            "owner", task["task_id"], target["step_id"], "Use a shorter date range.",
            expected_task_version=task["version"], expected_step_version=target["version"],
        )
        assert result["step"]["status"] == "pending"
        assert result["event"]["payload"]["correction"] == "Use a shorter date range."


@pytest.mark.parametrize(
    "status", ["running", "succeeded", "partial", "failed", "cancelled", "needs_reconciliation"]
)
def test_steering_rejects_non_claimable_step_without_mutation(status):
    with make_store() as store:
        task, _, target = make_task_with_steps(store, target_status=status)
        before_events = len(store.get_task("owner", task["task_id"])["events"])
        with pytest.raises(InvalidLifecycleTransition):
            store.steer_task_step(
                "owner", task["task_id"], target["step_id"], "Change the scope.",
                expected_task_version=task["version"], expected_step_version=target["version"],
            )
        unchanged = store.get_task("owner", task["task_id"])
        assert unchanged["version"] == task["version"]
        assert unchanged["steps"][1]["version"] == target["version"]
        assert len(unchanged["events"]) == before_events


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "cancelled", "needs_reconciliation"])
def test_steering_rejects_terminal_task(status):
    with make_store() as store:
        task, _, target = make_task_with_steps(store)
        store.transition_task_run(
            "owner", task["task_id"], expected_status="queued", expected_version=task["version"],
            new_status=status,
        )
        current = store.get_task("owner", task["task_id"])
        with pytest.raises(InvalidLifecycleTransition):
            store.steer_task_step(
                "owner", task["task_id"], target["step_id"], "Change the scope.",
                expected_task_version=current["version"], expected_step_version=target["version"],
            )
        assert store.get_task("owner", task["task_id"])["version"] == current["version"]


def test_steering_cas_failure_rolls_back_task_update_and_event():
    with make_store() as store:
        task, _, target = make_task_with_steps(store)
        before_events = len(store.get_task("owner", task["task_id"])["events"])

        with pytest.raises(ConcurrentTaskUpdate):
            store.steer_task_step(
                "owner", task["task_id"], target["step_id"], "Change the scope.",
                expected_task_version=task["version"], expected_step_version=target["version"] + 1,
            )

        unchanged = store.get_task("owner", task["task_id"])
        assert unchanged["version"] == task["version"]
        assert unchanged["steps"][1]["version"] == target["version"]
        assert len(unchanged["events"]) == before_events


def test_steering_is_rejected_while_task_batch_is_executing():
    with make_store() as store:
        task, _, target = make_task_with_steps(store)
        executing = store.transition_task_phase(
            "owner", task["task_id"], expected_phase="received",
            expected_version=task["version"], new_phase="planning",
            next_action="prepare",
        )
        executing = store.transition_task_phase(
            "owner", task["task_id"], expected_phase="planning",
            expected_version=executing["version"], new_phase="executing",
            next_action="dispatch",
        )
        with pytest.raises(InvalidLifecycleTransition, match="executing phase"):
            store.steer_task_step(
                "owner", task["task_id"], target["step_id"], "Change the scope.",
                expected_task_version=executing["version"],
                expected_step_version=target["version"],
            )


def test_tool_call_block_rejects_steering_revision_changed_since_prompt_snapshot():
    with make_store() as store:
        store.begin_turn("owner", "session", turn_id="turn")
        task, _, target = make_task_with_steps(store)
        snapshot = store.task_prompt_snapshot("owner", task["task_id"])
        store.steer_task_step(
            "owner", task["task_id"], target["step_id"], "Change the scope.",
            expected_task_version=task["version"],
            expected_step_version=target["version"],
        )

        with pytest.raises(ConcurrentTaskUpdate, match="steering changed"):
            store.persist_assistant_tool_call_block(
                "owner", "session", task["task_id"], "turn",
                [{"tool_name": "write_record", "arguments": {}, "call_id": "stale-call"}],
                expected_steering_event_id=snapshot["latest_steering_event_id"],
            )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE call_key='stale-call'"
        ).fetchone()[0] == 0
        assert store.list_assistant_tool_call_blocks("owner", task["task_id"]) == []


def test_steering_is_owner_scoped_and_rejects_foreign_step():
    with make_store() as store:
        task, _, target = make_task_with_steps(store, owner="owner-a")
        with pytest.raises(TaskNotFound):
            store.steer_task_step(
                "owner-b", task["task_id"], target["step_id"], "Change the scope.",
                expected_task_version=task["version"], expected_step_version=target["version"],
            )
        foreign_task = store.create_task_run("owner-b", "other-session", "other task", task_id="other")
        foreign_step = store.add_task_step("owner-b", "other", 0, step_id="foreign-step", status="ready")
        foreign_task = store.get_task("owner-b", "other")
        with pytest.raises(TaskNotFound):
            store.steer_task_step(
                "owner-a", task["task_id"], foreign_step["step_id"], "Change the scope.",
                expected_task_version=task["version"], expected_step_version=foreign_step["version"],
            )
        assert store.get_task("owner-a", task["task_id"])["version"] == task["version"]
        assert store.get_task("owner-b", foreign_task["task_id"])["version"] == foreign_task["version"]


@pytest.mark.parametrize("correction", ["", "   ", "x" * 2001])
def test_steering_rejects_empty_or_oversized_correction(correction):
    with make_store() as store:
        task, _, target = make_task_with_steps(store)
        with pytest.raises(ValueError):
            store.steer_task_step(
                "owner", task["task_id"], target["step_id"], correction,
                expected_task_version=task["version"], expected_step_version=target["version"],
            )
        assert store.get_task("owner", task["task_id"])["version"] == task["version"]


def test_steering_accepts_correction_at_bound():
    with make_store() as store:
        task, _, target = make_task_with_steps(store)
        correction = "x" * 2000
        result = store.steer_task_step(
            "owner", task["task_id"], target["step_id"], correction,
            expected_task_version=task["version"], expected_step_version=target["version"],
        )
        assert result["event"]["payload"]["correction"] == correction


@pytest.mark.parametrize("receipt_status", ["started", "unknown", "confirmed"])
def test_steering_rejects_started_ambiguous_or_confirmed_receipt(receipt_status):
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    with SessionStore(connection=connection) as store:
        receipts = ReceiptStore(connection)
        receipts.ensure_schema()
        task = store.create_task_run("owner", "session", "objective", task_id="task")
        store.begin_turn("owner", "session", turn_id="turn")
        call = store.record_tool_call(
            "owner", "session", tool_name="query_spending", arguments={},
            call_id="call", turn_id="turn", status="running",
        )
        receipts.prepare(
            receipt_id="receipt", call_id="call", user_id="owner", turn_id="turn",
            round_id=1, tool_name="query_spending", origin="native", arguments={},
        )
        receipts.start("receipt")
        if receipt_status in {"unknown", "confirmed"}:
            receipts.finish(
                "receipt", status=receipt_status, ok=receipt_status == "confirmed",
                complete=receipt_status == "confirmed", result_summary="result",
            )
        step = store.add_task_step(
            "owner", task["task_id"], 0, step_id="step", status="pending",
            tool_call_id=call["id"], receipt_id="receipt",
        )
        current_task = store.get_task("owner", task["task_id"], include_steps=False, include_events=False)
        with pytest.raises(InvalidLifecycleTransition, match="receipt evidence"):
            store.steer_task_step(
                "owner", task["task_id"], step["step_id"], "Correct this step.",
                expected_task_version=current_task["version"],
                expected_step_version=step["version"],
            )
        unchanged = store.get_task("owner", task["task_id"])
        assert unchanged["steps"][0]["version"] == step["version"]
        assert not any(event["event_type"] == "task.step_steered" for event in unchanged["events"])


def test_steering_rejects_when_another_step_is_running():
    with make_store() as store:
        task = store.create_task_run("owner", "session", "objective", task_id="task")
        running = store.add_task_step(
            "owner", "task", 0, step_id="running", status="running"
        )
        ready = store.add_task_step(
            "owner", "task", 1, step_id="ready", status="ready"
        )
        current_task = store.get_task("owner", "task", include_steps=False, include_events=False)
        with pytest.raises(InvalidLifecycleTransition, match="while a task step is running"):
            store.steer_task_step(
                "owner", "task", ready["step_id"], "Correct this step.",
                expected_task_version=current_task["version"],
                expected_step_version=ready["version"],
            )
        assert store.get_task_step("owner", "task", running["step_id"])["status"] == "running"


def test_latest_open_step_guidance_survives_many_later_corrections():
    with make_store() as store:
        task = store.create_task_run("owner", "session", "objective", task_id="task")
        first = store.add_task_step("owner", "task", 0, step_id="first", status="ready")
        second = store.add_task_step("owner", "task", 1, step_id="second", status="ready")

        current_task = store.get_task("owner", "task", include_steps=False, include_events=False)
        current_first = store.get_task_step("owner", "task", first["step_id"])
        store.steer_task_step(
            "owner", "task", first["step_id"], "Keep the original completed steps.",
            expected_task_version=current_task["version"],
            expected_step_version=current_first["version"],
        )
        for index in range(24):
            current_task = store.get_task("owner", "task", include_steps=False, include_events=False)
            current_second = store.get_task_step("owner", "task", second["step_id"])
            store.steer_task_step(
                "owner", "task", second["step_id"], f"Correction {index}.",
                expected_task_version=current_task["version"],
                expected_step_version=current_second["version"],
            )

        guidance = store.latest_task_step_guidance("owner", "task", limit=20)
        by_step = {
            item["step_id"]: item["payload"]["correction"] for item in guidance
        }
        assert by_step == {
            "first": "Keep the original completed steps.",
            "second": "Correction 23.",
        }


def test_task_prompt_snapshot_returns_version_latest_event_and_open_step_guidance():
    with make_store() as store:
        task = store.create_task_run("owner", "session", "objective", task_id="task")
        open_step = store.add_task_step("owner", "task", 0, step_id="open", status="ready")
        closed_step = store.add_task_step("owner", "task", 1, step_id="closed", status="succeeded")
        current_task = store.get_task("owner", "task", include_steps=False, include_events=False)
        store.steer_task_step(
            "owner", "task", open_step["step_id"], "Use posted transactions only.",
            expected_task_version=current_task["version"],
            expected_step_version=open_step["version"],
        )
        current_task = store.get_task("owner", "task", include_steps=False, include_events=False)
        current_step = store.get_task_step("owner", "task", open_step["step_id"])
        store.steer_task_step(
            "owner", "task", open_step["step_id"], "Use posted transactions from this month only.",
            expected_task_version=current_task["version"],
            expected_step_version=current_step["version"],
        )
        current_task = store.get_task("owner", "task", include_steps=False, include_events=False)
        non_guidance_event = store.append_task_event(
            "owner", "task", "task.step_steered",
            step_id=closed_step["step_id"],
            payload={"correction": "Closed-step event is not prompt guidance."},
        )

        snapshot = store.task_prompt_snapshot("owner", "task")

        assert set(snapshot) == {"task_version", "latest_steering_event_id", "guidance"}
        assert snapshot["task_version"] == current_task["version"]
        assert snapshot["latest_steering_event_id"] == non_guidance_event["event_id"]
        assert len(snapshot["guidance"]) == 1
        assert snapshot["guidance"][0]["step_id"] == "open"
        assert snapshot["guidance"][0]["correction"] == "Use posted transactions from this month only."
        assert set(snapshot["guidance"][0]) == {"step_id", "correction", "event_id", "created_at"}


def test_task_prompt_snapshot_keeps_version_and_guidance_from_one_read_snapshot():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = f"{temp_dir}/tasks.sqlite"
        first = SessionStore(db_path)
        second = None
        try:
            first.create_task_run("owner", "session", "objective", task_id="task")
            step = first.add_task_step("owner", "task", 0, step_id="step", status="ready")
            current_task = first.get_task("owner", "task", include_steps=False, include_events=False)
            second = SessionStore(db_path)
            guidance_query_started = threading.Event()
            allow_guidance_query = threading.Event()
            snapshots = []
            failures = []

            def pause_before_guidance_query(statement):
                if statement.lstrip().startswith("WITH ranked AS"):
                    guidance_query_started.set()
                    if not allow_guidance_query.wait(timeout=5):
                        failures.append(TimeoutError("snapshot query was not released"))

            first.connection.set_trace_callback(pause_before_guidance_query)

            def read_snapshot():
                try:
                    snapshots.append(first.task_prompt_snapshot("owner", "task"))
                except Exception as exc:  # surfaced in the test thread
                    failures.append(exc)

            reader = threading.Thread(target=read_snapshot)
            reader.start()
            assert guidance_query_started.wait(timeout=5), "snapshot did not reach guidance query"

            second.steer_task_step(
                "owner", "task", step["step_id"], "New correction committed concurrently.",
                expected_task_version=current_task["version"],
                expected_step_version=step["version"],
            )
            allow_guidance_query.set()
            reader.join(timeout=5)
            assert not reader.is_alive(), "snapshot reader did not finish"
            assert not failures

            snapshot = snapshots[0]
            assert snapshot["task_version"] == current_task["version"]
            assert snapshot["latest_steering_event_id"] == 0
            assert snapshot["guidance"] == []

            current = first.task_prompt_snapshot("owner", "task")
            assert current["task_version"] == current_task["version"] + 1
            assert current["latest_steering_event_id"] > 0
            assert current["guidance"][0]["correction"] == "New correction committed concurrently."
        finally:
            if second is not None:
                second.close()
            first.close()
