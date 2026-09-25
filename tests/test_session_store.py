import sqlite3

import pytest

from src.db.session_store import IdempotencyConflict, SessionStore


def make_store():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return SessionStore(connection=connection, max_messages_per_session=3, max_turns_per_session=2, max_tool_calls_per_session=2)


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
        store.connection.execute("SELECT name FROM sqlite_master WHERE name IN ('sessions','turns','messages','tool_calls')")
        store.connection.commit()
        # Re-running the migration runner must not duplicate schema objects.
        from src.db.migrations import apply_all
        apply_all(store.connection)
        store.connection.commit()
        store.add_message("u", "s", role="assistant", content="durable", message_id="a")
        if store._fts_enabled:
            assert store.connection.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 1
