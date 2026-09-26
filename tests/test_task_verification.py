from __future__ import annotations

import json
import sqlite3

import pytest

from src.agent.task_controller import TaskController
from src.db.session_store import ConcurrentTaskUpdate, SessionStore, TaskNotFound


@pytest.fixture
def verification_env():
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=connection)
    controller = TaskController(store)
    tasks = {}
    for owner in ("owner", "other"):
        task_id = f"task-{owner}"
        task = store.create_task_run(owner, f"session-{owner}", "Verify a summary", task_id=task_id)
        task = store.transition_task_phase(
            owner, task_id, expected_phase="received", expected_version=task["version"],
            new_phase="planning", next_action="prepare_verification",
        )
        task = store.transition_task_phase(
            owner, task_id, expected_phase="planning", expected_version=task["version"],
            new_phase="verifying", next_action="verify_completion_contract",
        )
        tasks[owner] = task
    yield store, controller, tasks
    store.close()


def test_pass_event_persists_only_bounded_check_metadata(verification_env):
    store, controller, tasks = verification_env
    task = tasks["owner"]
    result = controller.verify_and_persist(
        "owner", task["task_id"],
        {"required_deliverables": [{"check_id": "summary", "evidence_ref": "summary-ref"}]},
        {"deliverables": ["summary-ref"]},
        expected_version=task["version"],
    )

    persisted = store.get_task("owner", task["task_id"])
    event = persisted["events"][-1]
    assert result["outcome"]["passed"] is True
    assert result["repair_step"] is None
    assert persisted["phase"] == "verifying"
    assert persisted["version"] == task["version"] + 1
    assert event["event_type"] == "task.verification_passed"
    assert event["payload"] == {
        "passed": True,
        "checks": [{"check_id": "summary", "reason_code": "verified", "evidence_refs": ["summary-ref"]}],
        "check_ids": ["summary"],
        "failed_check_ids": [],
        "reason_codes": ["verified"],
        "evidence_refs": ["summary-ref"],
    }
    assert "deliverables" not in json.dumps(event["payload"])


def test_failure_atomically_parks_task_and_creates_one_ready_repair_step(verification_env):
    store, controller, tasks = verification_env
    task = tasks["owner"]
    result = controller.verify_and_persist(
        "owner", task["task_id"],
        {"required_deliverables": [
            {"check_id": "ledger", "evidence_ref": "ledger-ref"},
            {"check_id": "summary", "evidence_ref": "summary-ref"},
        ]},
        {"deliverables": ["ledger-ref"]},
        expected_version=task["version"],
    )

    persisted = store.get_task("owner", task["task_id"])
    repair = persisted["steps"]
    event = persisted["events"][-1]
    assert len(repair) == 1
    assert repair[0]["status"] == "ready"
    assert repair[0]["next_action"] == "repair_failed_checks"
    assert json.loads(repair[0]["completion_criteria"]) == ["summary"]
    assert result["repair_step"]["step_id"] == repair[0]["step_id"]
    assert persisted["status"] == "partial"
    assert persisted["phase"] == "partial"
    assert persisted["phase_next_action"] == "manual_repair_failed_checks"
    assert persisted["wait_reason"] == "verification_failed"
    assert event["event_type"] == "task.verification_failed"
    assert event["step_id"] == repair[0]["step_id"]
    assert event["payload"]["failed_check_ids"] == ["summary"]
    assert event["payload"]["checks"] == [
        {"check_id": "ledger", "reason_code": "verified", "evidence_refs": ["ledger-ref"]},
        {"check_id": "summary", "reason_code": "deliverable_missing", "evidence_refs": ["summary-ref"]},
    ]


def test_unavailable_verification_atomically_creates_manual_repair_once(verification_env):
    store, _controller, tasks = verification_env
    task = tasks["owner"]

    result = store.park_task_verification_unavailable(
        "owner", task["task_id"], expected_version=task["version"]
    )

    persisted = store.get_task("owner", task["task_id"])
    assert persisted["status"] == "partial"
    assert persisted["phase"] == "partial"
    assert persisted["phase_next_action"] == "manual_verification_repair"
    assert persisted["wait_reason"] == "verification_unavailable"
    assert persisted["version"] == task["version"] + 1
    assert len(persisted["steps"]) == 1
    repair = persisted["steps"][0]
    assert repair["status"] == "ready"
    assert repair["next_action"] == "manual_repair_verification_unavailable"
    assert json.loads(repair["completion_criteria"]) == ["verification_unavailable"]
    assert result["repair_step"]["step_id"] == repair["step_id"]
    event = persisted["events"][-1]
    assert event["event_type"] == "task.verification_unavailable"
    assert event["step_id"] == repair["step_id"]
    assert event["payload"] == {
        "reason_code": "verification_unavailable",
        "failed_check_ids": ["verification_unavailable"],
    }

    with pytest.raises(ConcurrentTaskUpdate, match="status/phase/version"):
        store.park_task_verification_unavailable(
            "owner", task["task_id"], expected_version=task["version"]
        )
    persisted_again = store.get_task("owner", task["task_id"])
    assert len(persisted_again["steps"]) == 1
    assert len(persisted_again["events"]) == len(persisted["events"])


def test_unavailable_verification_cas_failure_preserves_task_and_writes_nothing(
    verification_env,
):
    store, _controller, tasks = verification_env
    task = tasks["owner"]
    current = store.transition_task_phase(
        "owner", task["task_id"], expected_phase="verifying",
        expected_version=task["version"], new_phase="planning",
        next_action="verification_inputs_changed",
    )
    before = store.get_task("owner", task["task_id"])

    with pytest.raises(ConcurrentTaskUpdate, match="status/phase/version"):
        store.park_task_verification_unavailable(
            "owner", task["task_id"], expected_version=current["version"]
        )

    after = store.get_task("owner", task["task_id"])
    assert after["status"] == before["status"]
    assert after["phase"] == before["phase"] == "planning"
    assert after["version"] == before["version"]
    assert after["steps"] == before["steps"] == []
    assert after["events"] == before["events"]


def test_unavailable_verification_requires_active_status(verification_env):
    store, _controller, tasks = verification_env
    task = tasks["owner"]
    terminal = store.transition_task_run(
        "owner", task["task_id"], expected_status=task["status"],
        expected_version=task["version"], new_status="succeeded",
    )
    before = store.get_task("owner", task["task_id"])
    assert terminal["phase"] == "verifying"

    with pytest.raises(ConcurrentTaskUpdate, match="status/phase/version"):
        store.park_task_verification_unavailable(
            "owner", task["task_id"], expected_version=terminal["version"]
        )

    after = store.get_task("owner", task["task_id"])
    assert after["status"] == before["status"] == "succeeded"
    assert after["phase"] == before["phase"] == "verifying"
    assert after["version"] == before["version"]
    assert after["steps"] == before["steps"] == []
    assert after["events"] == before["events"]


def test_verification_is_owner_scoped(verification_env):
    _store, controller, tasks = verification_env
    other_task = tasks["other"]
    with pytest.raises(TaskNotFound):
        controller.verify_and_persist(
            "owner", other_task["task_id"], {}, {}, expected_version=other_task["version"]
        )


def test_stale_version_cas_writes_no_event_or_repair_step(verification_env):
    store, controller, tasks = verification_env
    task = tasks["owner"]
    newer = store.transition_task_phase(
        "owner", task["task_id"], expected_phase="verifying",
        expected_version=task["version"], new_phase="verifying",
        next_action="verification_inputs_refreshed",
    )
    before_event_count = len(store.get_task("owner", task["task_id"])["events"])

    with pytest.raises(ConcurrentTaskUpdate, match="phase/version"):
        controller.verify_and_persist(
            "owner", task["task_id"],
            {"required_deliverables": [{"check_id": "missing", "evidence_ref": "artifact"}]},
            {"deliverables": []}, expected_version=task["version"],
        )

    persisted = store.get_task("owner", task["task_id"])
    assert persisted["version"] == newer["version"]
    assert len(persisted["events"]) == before_event_count
    assert persisted["steps"] == []


def test_duplicate_failed_check_ids_are_normalized_before_atomic_repair(verification_env):
    store, controller, tasks = verification_env
    task = tasks["owner"]
    result = controller.verify_and_persist(
        "owner", task["task_id"],
        {"required_deliverables": [
            {"check_id": "same", "evidence_ref": "first"},
            {"check_id": "same", "evidence_ref": "second"},
        ], "required_check_ids": ["same"]},
        {"deliverables": []},
        expected_version=task["version"],
    )

    persisted = store.get_task("owner", task["task_id"])
    ids = [check["check_id"] for check in result["outcome"]["checks"]]
    assert result["outcome"]["passed"] is False
    assert len(ids) == len(set(ids))
    assert len(persisted["steps"]) == 1
    assert persisted["steps"][0]["next_action"] == "repair_failed_checks"
    assert persisted["status"] == "partial"
    assert persisted["events"][-1]["event_type"] == "task.verification_failed"


def test_persistence_limit_failure_is_itself_persistable_as_repair(verification_env):
    store, controller, tasks = verification_env
    task = tasks["owner"]
    contract = {"required_deliverables": [{
        "check_id": "c" * (256 - len(str(index))) + str(index), "evidence_ref": "ref",
    } for index in range(100)]}
    result = controller.verify_and_persist(
        "owner", task["task_id"], contract, {"deliverables": ["ref"]},
        expected_version=task["version"],
    )

    persisted = store.get_task("owner", task["task_id"])
    assert result["outcome"]["checks"][0]["reason_code"] == (
        "verification_result_exceeds_persistence_limits"
    )
    assert len(persisted["steps"]) == 1
    assert persisted["status"] == "partial"
    assert persisted["events"][-1]["event_type"] == "task.verification_failed"
