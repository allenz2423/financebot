from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db.session_store import SessionStore


class FakeChannel:
    def __init__(self, channel_id: int, *, send_error: Exception | None = None):
        self.id = channel_id
        self.send_error = send_error
        self.sent = []

    async def send(self, content, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((content, kwargs))


class FakeBot:
    def __init__(self, channels):
        self.channels = {channel.id: channel for channel in channels}
        self.fetch_channel = AsyncMock(side_effect=self._fetch_channel)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def _fetch_channel(self, channel_id):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise LookupError(channel_id)
        return channel


def _make_terminal_child(db_path, *, channel_id="100", thread_id="200", status="succeeded"):
    with SessionStore(db_path) as store:
        store.create_task_run(
            "owner-1", "parent-session", "Parent work", task_id="parent-task",
            channel_id="100", thread_id="200",
        )
        store.create_task_run(
            "owner-1", "child-session", "Background work", task_id="child-task",
            parent_task_id="parent-task", lane="background", status=status,
            channel_id=channel_id, thread_id=thread_id,
        )


def _completion_result(status="succeeded", summary="Found three useful updates."):
    return SimpleNamespace(status=status, summary=summary)


@pytest.mark.asyncio
async def test_completion_resolves_stored_thread_and_sends_status_only_notice(
    tmp_path, monkeypatch
):
    import src.bot.commands as commands

    db_path = tmp_path / "background-delivery.sqlite3"
    _make_terminal_child(db_path)
    thread = FakeChannel(200)
    channel = FakeChannel(100)
    fake_bot = FakeBot([thread, channel])
    monkeypatch.setattr(commands.src.core.state, "DB_PATH", str(db_path))
    monkeypatch.setattr(commands, "bot", fake_bot)

    await commands._deliver_background_task_completion(
        "owner-1", "child-task", _completion_result()
    )

    assert len(thread.sent) == 1
    assert channel.sent == []
    message, kwargs = thread.sent[0]
    assert message == "Background task `child-task` finished: **SUCCEEDED**."
    assert "Found three useful updates." not in message
    allowed_mentions = kwargs["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    fake_bot.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_lookup_does_not_resolve_another_owners_task(
    tmp_path, monkeypatch
):
    import src.bot.commands as commands

    db_path = tmp_path / "background-delivery.sqlite3"
    _make_terminal_child(db_path)
    thread = FakeChannel(200)
    fake_bot = FakeBot([thread])
    monkeypatch.setattr(commands.src.core.state, "DB_PATH", str(db_path))
    monkeypatch.setattr(commands, "bot", fake_bot)

    await commands._deliver_background_task_completion(
        "different-owner", "child-task", _completion_result()
    )

    assert thread.sent == []
    fake_bot.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_completion_invocation_does_not_resend_after_atomic_claim(
    tmp_path, monkeypatch
):
    import src.bot.commands as commands

    db_path = tmp_path / "background-delivery.sqlite3"
    _make_terminal_child(db_path)
    thread = FakeChannel(200)
    monkeypatch.setattr(commands.src.core.state, "DB_PATH", str(db_path))
    monkeypatch.setattr(commands, "bot", FakeBot([thread]))

    await commands._deliver_background_task_completion(
        "owner-1", "child-task", _completion_result()
    )
    await commands._deliver_background_task_completion(
        "owner-1", "child-task", _completion_result(summary="A duplicate result.")
    )

    assert len(thread.sent) == 1
    assert thread.sent[0][0] == "Background task `child-task` finished: **SUCCEEDED**."
    with SessionStore(db_path) as store:
        events = store.get_task("owner-1", "child-task")["events"]
    assert sum(e["event_type"] == "task.background_completion_notice_claimed" for e in events) == 1
    assert sum(e["event_type"] == "task.background_completion_notice_started" for e in events) == 1
    assert sum(e["event_type"] == "task.background_completion_notice_delivered" for e in events) == 1


@pytest.mark.asyncio
async def test_send_exception_records_unknown_and_later_invocation_does_not_retry(
    tmp_path, monkeypatch
):
    import src.bot.commands as commands

    db_path = tmp_path / "background-delivery.sqlite3"
    _make_terminal_child(db_path)
    thread = FakeChannel(200, send_error=TimeoutError("acknowledgement timed out"))
    monkeypatch.setattr(commands.src.core.state, "DB_PATH", str(db_path))
    monkeypatch.setattr(commands, "bot", FakeBot([thread]))

    await commands._deliver_background_task_completion(
        "owner-1", "child-task", _completion_result()
    )
    await commands._deliver_background_task_completion(
        "owner-1", "child-task", _completion_result(summary="Must not be resent.")
    )

    assert len(thread.sent) == 0
    with SessionStore(db_path) as store:
        events = store.get_task("owner-1", "child-task")["events"]
    claim_events = [
        event for event in events
        if event["event_type"] == "task.background_completion_notice_claimed"
    ]
    unknown_events = [
        event for event in events
        if event["event_type"] == "task.background_completion_delivery_unknown"
    ]
    assert len(claim_events) == 1
    assert sum(
        event["event_type"] == "task.background_completion_notice_started"
        for event in events
    ) == 1
    assert len(unknown_events) == 1
    assert "task.background_completion_notice_delivered" not in {
        event["event_type"] for event in events
    }
    payload = unknown_events[0]["payload"]
    assert payload == {"status": "SUCCEEDED", "reason_code": "send_exception"}
    assert "TimeoutError" not in repr(payload)
    assert "acknowledgement timed out" not in repr(payload)


@pytest.mark.asyncio
async def test_scoped_destination_falls_back_to_fetch_for_stored_channel(
    tmp_path, monkeypatch
):
    import src.bot.commands as commands

    db_path = tmp_path / "background-delivery.sqlite3"
    _make_terminal_child(db_path, channel_id="300", thread_id=None, status="failed")
    channel = FakeChannel(300)
    fake_bot = FakeBot([channel])
    # The cache intentionally omits the channel so resolution must fetch the
    # exact destination persisted on this owner's task session.
    fake_bot.channels.clear()

    async def fetch_channel(channel_id):
        if channel_id != channel.id:
            raise LookupError(channel_id)
        return channel

    fake_bot.fetch_channel.side_effect = fetch_channel
    monkeypatch.setattr(commands.src.core.state, "DB_PATH", str(db_path))
    monkeypatch.setattr(commands, "bot", fake_bot)

    await commands._deliver_background_task_completion(
        "owner-1", "child-task", _completion_result("failed", "Provider unavailable.")
    )

    fake_bot.fetch_channel.assert_awaited_once_with(300)
    assert len(channel.sent) == 1
    assert channel.sent[0][0] == "Background task `child-task` finished: **FAILED**."
    assert "Provider unavailable." not in channel.sent[0][0]


@pytest.mark.asyncio
async def test_crash_after_claim_before_send_remains_recoverable(tmp_path, monkeypatch):
    import src.bot.commands as commands

    db_path = tmp_path / "background-delivery.sqlite3"
    _make_terminal_child(db_path)
    with SessionStore(db_path) as store:
        assert store.claim_background_completion_notice("owner-1", "child-task")
        assert [task["task_id"] for task in store.list_pending_background_completion_notices()] == [
            "child-task"
        ]

    thread = FakeChannel(200)
    monkeypatch.setattr(commands.src.core.state, "DB_PATH", str(db_path))
    monkeypatch.setattr(commands, "bot", FakeBot([thread]))
    await commands._deliver_background_task_completion(
        "owner-1", "child-task", _completion_result()
    )

    assert len(thread.sent) == 1
    with SessionStore(db_path) as store:
        events = store.get_task("owner-1", "child-task")["events"]
    assert sum(
        event["event_type"] == "task.background_completion_notice_started"
        for event in events
    ) == 1
