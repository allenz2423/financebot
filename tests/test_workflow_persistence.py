from __future__ import annotations

import pytest

from src.agent.task_controller import TaskController
from src.db.session_store import ConcurrentTaskUpdate, SessionStore
from src.services.tool_receipts import ReceiptStore
from src.services.workflow_registry import WorkflowRegistry, built_in_workflows


PLAN_STEPS = [
    {
        "tool_name": "search_gmail",
        "description": "Find candidate messages matching the request.",
        "completion_criteria": "The search result contains the selected message IDs.",
    },
    {
        "tool_name": "read_gmail_message",
        "description": "Read the selected messages.",
        "completion_criteria": "The selected message bodies are available for summary.",
    },
]
WORKFLOW_REF = {
    "workflow_id": "email-triage",
    "version": "1.0.0",
    "digest": "a" * 64,
}


def test_workflow_pin_is_atomic_with_plan_and_survives_restart(tmp_path):
    path = tmp_path / "workflow-tasks.db"
    with SessionStore(path) as store:
        task = store.create_task_run(
            "owner", "session", "Summarize recent email", task_id="workflow-task"
        )
        controller = TaskController(store)
        created_steps = controller.create_plan(
            "owner", task["task_id"], PLAN_STEPS, workflow_ref=WORKFLOW_REF
        )

        assert len(created_steps) == 2
        assert store.get_task_workflow_pin("owner", task["task_id"]) == WORKFLOW_REF
        events = store.get_task("owner", task["task_id"])["events"]
        plan_events = [event for event in events if event["event_type"] == "task.plan_created"]
        assert len(plan_events) == 1
        plan_payload = plan_events[0]["payload"]
        assert plan_payload["workflow_id"] == WORKFLOW_REF["workflow_id"]
        assert plan_payload["workflow_version"] == WORKFLOW_REF["version"]
        assert plan_payload["workflow_digest"] == WORKFLOW_REF["digest"]

    with SessionStore(path) as reopened:
        assert reopened.get_task_workflow_pin("owner", "workflow-task") == WORKFLOW_REF
        task = reopened.get_task("owner", "workflow-task")
        assert [step["next_action"] for step in task["steps"]] == [
            "dispatch:search_gmail", "dispatch:read_gmail_message",
        ]


@pytest.mark.parametrize(
    "workflow_ref",
    [
        {"workflow_id": "email-triage", "version": "1.0.0"},
        {"workflow_id": "email-triage", "version": "1.0.0", "digest": "not-a-digest"},
        {"workflow_id": "", "version": "1.0.0", "digest": "a" * 64},
    ],
)
def test_malformed_workflow_pin_fails_before_plan_state_is_created(tmp_path, workflow_ref):
    with SessionStore(tmp_path / "invalid-workflow.db") as store:
        task = store.create_task_run("owner", "session", "objective", task_id="task")

        with pytest.raises(ValueError):
            store.create_task_plan(
                "owner", task["task_id"], PLAN_STEPS, workflow_ref=workflow_ref
            )

        current = store.get_task("owner", task["task_id"])
        assert current["steps"] == []
        assert store.get_task_workflow_pin("owner", task["task_id"]) is None


def test_workflow_plan_pin_cannot_be_replaced_by_a_second_plan(tmp_path):
    with SessionStore(tmp_path / "one-workflow-plan.db") as store:
        task = store.create_task_run("owner", "session", "objective", task_id="task")
        store.create_task_plan(
            "owner", task["task_id"], PLAN_STEPS, workflow_ref=WORKFLOW_REF
        )

        with pytest.raises(ValueError, match="already has steps"):
            store.create_task_plan(
                "owner",
                task["task_id"],
                PLAN_STEPS,
                workflow_ref={**WORKFLOW_REF, "version": "2.0.0"},
            )

        assert store.get_task_workflow_pin("owner", task["task_id"]) == WORKFLOW_REF


def test_controller_resolves_exact_definition_digest_before_materialization(tmp_path):
    with SessionStore(tmp_path / "workflow-resolution.db") as store:
        owner = "owner"
        task = store.create_task_run(
            owner, "session", "Summarize email", task_id="workflow-task"
        )
        registry = built_in_workflows(
            {
                "search_gmail", "read_gmail_message",
                "monitor_create_natural_rule", "monitor_list_rules",
            }
        )
        controller = TaskController(store)
        digest = registry.digest("gmail.triage", "1.0.0")

        with pytest.raises(ValueError, match="digest"):
            controller.create_workflow_plan(
                "owner", task["task_id"], registry=registry,
                workflow_id="gmail.triage", version="1.0.0",
                expected_digest="0" * 64,
            )
        assert store.get_task(owner, task["task_id"])["steps"] == []

        controller.create_workflow_plan(
            owner, task["task_id"], registry=registry,
            workflow_id="gmail.triage", version="1.0.0",
            expected_digest=digest,
        )
        assert store.get_task_workflow_pin(owner, task["task_id"]) == {
            "workflow_id": "gmail.triage", "version": "1.0.0", "digest": digest,
        }


def test_controller_enforces_pinned_step_order_and_argument_schema(tmp_path):
    with SessionStore(tmp_path / "workflow-dispatch.db") as store:
        owner = "owner"
        task = store.create_task_run(
            owner, "session", "Summarize recent email", task_id="workflow-task"
        )
        registry = built_in_workflows(
            {
                "search_gmail", "read_gmail_message",
                "monitor_create_natural_rule", "monitor_list_rules",
            }
        )
        controller = TaskController(store)
        digest = registry.digest("gmail.triage", "1.0.0")
        controller.create_workflow_plan(
            owner, task["task_id"], registry=registry,
            workflow_id="gmail.triage", version="1.0.0",
            expected_digest=digest,
        )

        controller.validate_workflow_dispatch(
            owner, task["task_id"], registry=registry,
            tool_name="search_gmail", arguments={"query": "newer_than:2d"},
        )
        with pytest.raises(PermissionError, match="requires 'search_gmail' next"):
            controller.validate_workflow_dispatch(
                owner, task["task_id"], registry=registry,
                tool_name="read_gmail_message", arguments={"message_id": "m1"},
            )
        with pytest.raises(ValueError, match="missing required properties"):
            controller.validate_workflow_dispatch(
                owner, task["task_id"], registry=registry,
                tool_name="search_gmail", arguments={},
            )
        with pytest.raises(ValueError, match="unknown properties"):
            controller.validate_workflow_dispatch(
                owner, task["task_id"], registry=registry,
                tool_name="search_gmail", arguments={"query": "recent", "user_id": "other"},
            )


def test_pinned_workflow_fails_closed_when_exact_version_is_unavailable(tmp_path):
    with SessionStore(tmp_path / "missing-workflow-version.db") as store:
        owner = "owner"
        task = store.create_task_run(owner, "session", "Summarize email", task_id="workflow-task")
        registry = built_in_workflows(
            {"search_gmail", "read_gmail_message", "monitor_create_natural_rule", "monitor_list_rules"}
        )
        digest = registry.digest("gmail.triage", "1.0.0")
        controller = TaskController(store)
        controller.create_workflow_plan(
            owner, task["task_id"], registry=registry,
            workflow_id="gmail.triage", version="1.0.0", expected_digest=digest,
        )
        # An empty validated catalog simulates an install/version that no longer
        # contains the pinned immutable definition.
        empty_registry = WorkflowRegistry({"search_gmail", "read_gmail_message"})

        with pytest.raises(PermissionError, match="WORKFLOW_PIN_UNAVAILABLE"):
            controller.validate_workflow_dispatch(
                owner, task["task_id"], registry=empty_registry,
                tool_name="search_gmail", arguments={"query": "recent"},
            )


def test_running_workflow_step_blocks_later_dispatch_and_atomic_claim(tmp_path):
    with SessionStore(tmp_path / "workflow-step-order.db") as store:
        receipts = ReceiptStore(store.connection)
        receipts.ensure_schema()
        owner = "owner"
        task = store.create_task_run(owner, "session", "Summarize recent email", task_id="workflow-task")
        registry = built_in_workflows(
            {"search_gmail", "read_gmail_message", "monitor_create_natural_rule", "monitor_list_rules"}
        )
        controller = TaskController(store)
        digest = registry.digest("gmail.triage", "1.0.0")
        controller.create_workflow_plan(
            owner, task["task_id"], registry=registry,
            workflow_id="gmail.triage", version="1.0.0", expected_digest=digest,
        )
        store.begin_turn(owner, "session", turn_id="turn")

        first = controller.prepare_step(owner, task["task_id"], tool_name="search_gmail")
        first_call = store.record_tool_call(
            owner, "session", tool_name="search_gmail", arguments={"query": "recent"},
            call_id="search-call", turn_id="turn", status="running",
        )
        controller.link_call_to_step(owner, task["task_id"], first["step_id"], tool_call_id=first_call["id"])
        receipts.prepare(
            receipt_id="search-receipt", call_id="search-call", user_id=owner,
            turn_id="turn", round_id=1, tool_name="search_gmail", origin="native",
            arguments={"query": "recent"},
        )
        controller.claim_step(owner, task["task_id"], first["step_id"], receipt_id="search-receipt")

        with pytest.raises(PermissionError, match="WORKFLOW_STEP_IN_PROGRESS"):
            controller.validate_workflow_dispatch(
                owner, task["task_id"], registry=registry,
                tool_name="read_gmail_message", arguments={"message_id": "m1"},
            )
        with pytest.raises(ValueError, match="still running"):
            controller.prepare_step(owner, task["task_id"], tool_name="read_gmail_message")

        # Even if a caller bypasses controller preparation, the transactional
        # store claim rejects a later step while any prior step remains running.
        second = store.get_task(owner, task["task_id"])["steps"][1]
        second = store.transition_task_step(
            owner, task["task_id"], second["step_id"], expected_status="ready",
            expected_version=int(second["version"]), new_status="pending",
            next_action=second["next_action"],
        )
        second_call = store.record_tool_call(
            owner, "session", tool_name="read_gmail_message", arguments={"message_id": "m1"},
            call_id="read-call", turn_id="turn", status="running",
        )
        controller.link_call_to_step(owner, task["task_id"], second["step_id"], tool_call_id=second_call["id"])
        receipts.prepare(
            receipt_id="read-receipt", call_id="read-call", user_id=owner,
            turn_id="turn", round_id=2, tool_name="read_gmail_message", origin="native",
            arguments={"message_id": "m1"},
        )
        current_task = store.get_task(owner, task["task_id"])
        current_step = store.get_task_step(owner, task["task_id"], second["step_id"])
        with pytest.raises(ConcurrentTaskUpdate, match="another task step is already running"):
            store.claim_task_step(
                owner, task["task_id"], second["step_id"],
                expected_task_status=current_task["status"],
                expected_task_version=int(current_task["version"]),
                expected_step_version=int(current_step["version"]),
                receipt_id="read-receipt",
            )
