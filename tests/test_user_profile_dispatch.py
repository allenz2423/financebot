from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

import src.services.llm as llm
from src.db import prefs
from src.db.session_store import SessionStore
from src.agent.task_controller import TaskController
from src.services.tool_receipts import ReceiptStore


class _Sent:
    async def edit(self, **_kwargs):
        return self

    async def delete(self):
        return None


class _Channel:
    id = "profile-channel"

    async def send(self, *_args, **_kwargs):
        return _Sent()


class _Reply:
    channel = _Channel()


class _Response:
    status_code = 200

    def __init__(self, responses):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_lines(self):
        response = self.responses.pop(0)
        if "call" in response:
            call = response["call"]
            payload = {"choices": [{
                "delta": {"tool_calls": [{
                    "index": 0, "id": call["id"], "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    },
                }]},
                "finish_reason": "tool_calls",
            }]}
        else:
            payload = {"choices": [{
                "delta": {"content": response.get("content", "Done.")},
                "finish_reason": "stop",
            }]}
        yield "data: " + json.dumps(payload)
        yield "data: [DONE]"

    async def aread(self):
        return b""

    def raise_for_status(self):
        raise AssertionError("mock provider unexpectedly returned an error")


class _Client:
    responses = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    @asynccontextmanager
    async def stream(self, *_args, **_kwargs):
        yield _Response(self.responses)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expected_saved"),
    [
        ("Set my profile field risk_tolerance to cautious.", True),
        ("Can you change my risk tolerance to cautious?", False),
    ],
)
async def test_profile_edit_dispatch_requires_exact_user_turn_before_grant_or_receipt(
    monkeypatch, tmp_path, prompt, expected_saved,
):
    db = tmp_path / "profile.sqlite3"
    monkeypatch.setattr(prefs, "DB_PATH", str(db))
    prefs.init_prefs_schema()
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=connection)
    receipts = ReceiptStore(connection)
    receipts.ensure_schema()
    store.begin_turn(
        "profile-owner", "profile-session", turn_id="profile-turn",
        channel_id="profile-channel", thread_id=None,
    )
    task_id = "profile-task"
    store.create_task_run(
        "profile-owner", "profile-session", "Update the operating profile",
        task_id=task_id, channel_id="profile-channel", thread_id=None,
    )
    TaskController(store).transition_phase(
        "profile-owner", task_id, "planning", next_action="manage_user_profile"
    )
    turn_id = "profile-turn"

    _Client.responses = [
        {"call": {
            "id": "profile-call",
            "name": "manage_user_profile",
            "arguments": {
                "action": "set",
                "values": {"risk_tolerance": "cautious"},
            },
        }},
        {"content": "The profile request was handled."},
        {"content": "The requested profile update was saved."},
        {"content": "The profile update completed."},
    ]
    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm, "_DURABLE_SESSION_STORE", store)
    monkeypatch.setattr(llm, "conn", connection)
    monkeypatch.setattr(llm, "build_advisor_context", lambda **_kwargs: "")

    async def empty_world_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(llm, "build_semantic_world_model_context", empty_world_context)
    monkeypatch.setattr(llm, "_auto_session_recall_evidence", lambda *_args, **_kwargs: "")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENROUTER_PAID_MODEL", "openai/test-model")
    monkeypatch.setenv("ADVISOR_LIVE_PREVIEW", "0")

    calls = {"grant": 0, "consume": 0}
    original_issue = llm.issue_grant
    original_consume = llm.consume_grant

    def issue(*args, **kwargs):
        calls["grant"] += 1
        return original_issue(*args, **kwargs)

    def consume(*args, **kwargs):
        calls["consume"] += 1
        return original_consume(*args, **kwargs)

    monkeypatch.setattr(llm, "issue_grant", issue)
    monkeypatch.setattr(llm, "consume_grant", consume)
    context_tokens = [
        (llm.CURRENT_TASK_ID, llm.CURRENT_TASK_ID.set(task_id)),
        (llm.CURRENT_SESSION_KEY, llm.CURRENT_SESSION_KEY.set("profile-session")),
        (llm.CURRENT_CHANNEL_ID, llm.CURRENT_CHANNEL_ID.set("profile-channel")),
        (llm.CURRENT_THREAD_ID, llm.CURRENT_THREAD_ID.set(None)),
        (llm.CURRENT_TURN_ID, llm.CURRENT_TURN_ID.set(turn_id)),
    ]
    try:
        chat_result = await llm._chat_with_delilah_impl(prompt, "profile-owner", _Reply())
        if expected_saved:
            assert "profile" in str(chat_result).casefold()
    finally:
        for context_var, token in reversed(context_tokens):
            context_var.reset(token)

    profile = prefs.get_operating_profile("profile-owner")
    receipt_count = connection.execute(
        "SELECT COUNT(*) FROM tool_receipts WHERE turn_id=?", (turn_id,)
    ).fetchone()[0]
    assert (profile["values"].get("risk_tolerance") == "cautious") is expected_saved
    assert calls == ({"grant": 1, "consume": 1} if expected_saved else {"grant": 0, "consume": 0})
    assert receipt_count == (1 if expected_saved else 0)
    assert (
        profile["provenance"].get("risk_tolerance", {}).get("source_turn_id") == turn_id
    ) is expected_saved
