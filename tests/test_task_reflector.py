import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.agent.task_reflector import (
    build_reflection_prompt,
    reflect_verified_task,
    reflection_expiry_from_env,
)

REGISTERED_TOOLS = frozenset({"browser_submit"})


def _task(status="succeeded", *, verified=True, events=None):
    task_events = events
    if task_events is None:
        task_events = ([{
            "event_type": "task.verification_passed",
            "payload_json": json.dumps({
                "passed": True,
                "checks": [{
                    "check_id": "result-confirmed",
                    "reason_code": "verified",
                    "passed": True,
                    "evidence_refs": ["receipt:confirmed", "artifact:confirmation"],
                }],
            }),
        }] if verified else [])
    return {
        "task_id": "task-17",
        "status": status,
        "objective": "Submit the reimbursement form",
        "steps": [{
            "step_id": "step-1",
            "status": "succeeded",
            "next_action": "dispatch:browser_submit",
            "receipt_id": "receipt:confirmed",
            "arguments": {"secret": "not for reflection"},
            "result_summary": "private result",
        }],
        "events": task_events,
    }


class FakeStore:
    def __init__(self, task):
        self.task = task
        self.events = []

    def get_task(self, user_id, task_id):
        assert (user_id, task_id) == ("owner-1", "task-17")
        return self.task

    def append_task_event(self, user_id, task_id, event_type, *, payload):
        self.events.append({"event_type": event_type, "payload": payload})


def _valid_model_output():
    return json.dumps({"lessons": [{
        "pattern": "verify_receipt_before_reporting",
        "choice_id": "choice_1",
    }]})


def test_reflection_ttl_defaults_to_no_expiry_and_owner_can_set_it():
    assert reflection_expiry_from_env(value="0") is None
    assert reflection_expiry_from_env(
        value="30", now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    ) == "2026-01-31T00:00:00+00:00"
    with pytest.raises(ValueError):
        reflection_expiry_from_env(value="-1")


def test_reflection_prompt_labels_task_data_untrusted_and_disallows_permission():
    prompt = build_reflection_prompt({"verified_procedure_choices": []})
    assert "untrusted labels" in prompt[0]["content"]
    assert "not permission" in prompt[0]["content"]
    assert "<untrusted_verified_task_choices>" in prompt[1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("task", [
    _task("failed"),
    _task("partial"),
    _task("cancelled"),
    _task("needs_reconciliation"),
    _task(verified=False),
])
async def test_only_verified_successful_task_reaches_model(task):
    store = FakeStore(task)
    generate = AsyncMock(return_value=_valid_model_output())

    assert await reflect_verified_task(
        store, "owner-1", "task-17", generate, registered_tools=REGISTERED_TOOLS
    ) == 0
    generate.assert_not_awaited()
    assert store.events == []


@pytest.mark.asyncio
async def test_reflection_persists_one_owner_scoped_idempotent_lesson_bundle(
    monkeypatch,
):
    import src.db.memory as memory

    save = AsyncMock(return_value="Memory successfully saved.")
    monkeypatch.setattr(memory, "save_epistemic_memory", save)
    store = FakeStore(_task())
    generate = AsyncMock(return_value=_valid_model_output())

    assert await reflect_verified_task(
        store, "owner-1", "task-17", generate, registered_tools=REGISTERED_TOOLS
    ) == 1
    kwargs = save.await_args.kwargs
    assert kwargs["user_id"] == "owner-1"
    assert kwargs["memory_type"] == "lesson"
    assert kwargs["provenance_type"] == "verified_task_reflection"
    assert kwargs["sensitivity"] == "internal"
    assert kwargs["expires_at"] is None
    assert kwargs["dedupe_key"].startswith("task-reflection-v1:")
    assert kwargs["evidence_refs"] == [
        "receipt:confirmed", "task:task-17",
    ]
    assert kwargs["content"] == (
        "1. After using browser_submit, confirm its persisted completion "
        "evidence before reporting the task as complete."
    )
    assert "secret" not in kwargs["content"]
    assert store.events[0]["event_type"] == "task.reflection_completed"

    # A recovered snapshot carrying the completion marker performs no new LLM
    # request or memory write.
    store.task["events"].append(store.events[0])
    assert await reflect_verified_task(
        store, "owner-1", "task-17", generate, registered_tools=REGISTERED_TOOLS
    ) == 0
    assert generate.await_count == 1
    assert save.await_count == 1


@pytest.mark.asyncio
async def test_reflection_storage_failure_does_not_mark_task_complete(monkeypatch):
    import src.db.memory as memory

    save = AsyncMock(side_effect=OSError("memory store unavailable"))
    monkeypatch.setattr(memory, "save_epistemic_memory", save)
    store = FakeStore(_task())

    with pytest.raises(OSError):
        await reflect_verified_task(
            store,
            "owner-1",
            "task-17",
            AsyncMock(return_value=_valid_model_output()),
            registered_tools=REGISTERED_TOOLS,
        )
    assert store.events == []
