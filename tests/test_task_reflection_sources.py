from __future__ import annotations

import sqlite3

import pytest

from src.agent.task_controller import TaskController
from src.db.session_store import SessionStore, TaskNotFound


@pytest.fixture
def source_env():
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=connection)
    controller = TaskController(store)
    store.begin_turn("owner", "session-a", turn_id="turn-origin")
    store.add_message(
        "owner", "session-a", role="user", turn_id="turn-origin",
        message_id="message-user-origin", content="Please remember this exact request.",
    )
    store.add_message(
        "owner", "session-a", role="assistant", turn_id="turn-origin",
        message_id="message-assistant-origin", content="I will do that.",
    )
    controller.create_turn("owner", "session-a", "turn-origin", "Complete the request")
    return store, controller


def _lookup(store: SessionStore, *, owner: str = "owner", session: str = "session-a"):
    return store.get_task_origin_user_messages(
        owner, "task_turn-origin", session
    )


def test_returns_user_authored_rows_from_only_the_exact_origin_turn(source_env):
    store, _controller = source_env
    store.begin_turn("owner", "session-a", turn_id="turn-later")
    store.add_message(
        "owner", "session-a", role="user", turn_id="turn-later",
        message_id="message-user-later", content="A different later request.",
    )

    assert _lookup(store) == [{
        "message_key": "message-user-origin",
        "turn_key": "turn-origin",
        "content": "Please remember this exact request.",
    }]
    created_event = store.get_task("owner", "task_turn-origin")["events"][0]
    assert created_event["payload"]["origin_turn_id"] == "turn-origin"


def test_wrong_owner_or_session_cannot_read_origin_messages(source_env):
    store, _controller = source_env

    with pytest.raises(TaskNotFound):
        _lookup(store, owner="different-owner")
    with pytest.raises(TaskNotFound):
        _lookup(store, session="different-session")


def test_legacy_task_without_origin_turn_returns_no_messages(source_env):
    store, _controller = source_env
    store.begin_turn("owner", "session-a", turn_id="turn-legacy")
    store.add_message(
        "owner", "session-a", role="user", turn_id="turn-legacy",
        message_id="message-legacy", content="Do not infer this as provenance.",
    )
    store.create_task_run(
        "owner", "session-a", "legacy task", task_id="task_turn-legacy"
    )

    assert store.get_task_origin_user_messages(
        "owner", "task_turn-legacy", "session-a"
    ) == []


def test_lookup_returns_persisted_redacted_text_without_unredacting(source_env):
    store, _controller = source_env
    redacted = "Email me at [REDACTED_EMAIL] using [REDACTED_SECRET]."
    store.add_message(
        "owner", "session-a", role="user", turn_id="turn-origin",
        message_id="message-redacted", content=redacted,
    )

    rows = _lookup(store)
    assert rows[-1]["content"] == redacted
    assert "[REDACTED_EMAIL]" in rows[-1]["content"]
    assert "[REDACTED_SECRET]" in rows[-1]["content"]
    assert "real-secret-value" not in rows[-1]["content"]
    assert set(rows[-1]) == {"message_key", "turn_key", "content"}
