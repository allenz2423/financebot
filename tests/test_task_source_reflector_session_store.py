from __future__ import annotations

import json
import sqlite3

import pytest

from src.agent.task_controller import TaskController
from src.agent.task_source_reflector import build_task_source_context
from src.db.session_store import SessionStore, TaskNotFound


def test_source_context_uses_real_session_store_exact_origin_user_messages():
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=connection)
    controller = TaskController(store)

    store.begin_turn("owner", "session-a", turn_id="turn-origin")
    store.add_message(
        "owner", "session-a", role="user", turn_id="turn-origin",
        message_id="user-origin", content="I prefer concise hiking summaries.",
    )
    store.add_message(
        "owner", "session-a", role="assistant", turn_id="turn-origin",
        message_id="assistant-origin", content="I will keep that in mind.",
    )
    task = controller.create_turn("owner", "session-a", "turn-origin", "Remember my preference")

    store.begin_turn("owner", "session-a", turn_id="turn-later")
    store.add_message(
        "owner", "session-a", role="user", turn_id="turn-later",
        message_id="user-later", content="I prefer detailed finance reports.",
    )
    store.add_message(
        "owner", "session-a", role="assistant", turn_id="turn-later",
        message_id="assistant-later", content="I will prepare one.",
    )

    task = store.transition_task_run(
        "owner", task["task_id"], expected_status="queued",
        expected_version=int(task["version"]), new_status="running",
    )
    store.append_task_event(
        "owner", task["task_id"], "task.verification_passed",
        payload={"passed": True, "checks": [{"check_id": "complete", "evidence_refs": ["test"]}]},
    )
    store.transition_task_run(
        "owner", task["task_id"], expected_status="running",
        expected_version=int(task["version"]), new_status="succeeded",
    )

    context, catalog = build_task_source_context(
        store, receipt_store=None, user_id="owner", task_id=task["task_id"]
    )

    assert context == {
        "sources": [{
            "source_id": "source_1",
            "kind": "user_statement",
            "text": "I prefer concise hiking summaries.",
        }]
    }
    assert catalog["source_1"]["evidence_refs"][0].startswith(
        "user_message:user-origin:turn-origin:"
    )
    assert "user-later" not in json.dumps(catalog)
    assert "assistant-origin" not in json.dumps(catalog)
    assert "assistant-later" not in json.dumps(catalog)
    assert "finance reports" not in json.dumps(context)
    assert "I will" not in json.dumps(context)

    with pytest.raises(TaskNotFound):
        build_task_source_context(
            store, receipt_store=None, user_id="wrong-owner", task_id=task["task_id"]
        )
    with pytest.raises(TaskNotFound):
        store.get_task_origin_user_messages(
            "owner", task["task_id"], "wrong-session"
        )

    connection.close()
