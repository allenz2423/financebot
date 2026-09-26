import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.task_source_reflector import (
    build_source_reflection_prompt,
    build_task_source_context,
    parse_source_facts,
    reflect_task_sources,
)
from src.services.tool_receipts import arguments_hash


def _task(*, steps=None, events=None):
    return {
        "task_id": "task-1",
        "status": "succeeded",
        "session_key": "session-1",
        "session_id": 77,
        "channel_id": "channel-1",
        "thread_id": "thread-1",
        "steps": steps or [],
        "events": events if events is not None else [
            {"event_type": "task.created", "payload": {"origin_turn_id": "turn-origin"}},
            {
                "event_type": "task.verification_passed",
                "payload_json": json.dumps({
                    "passed": True,
                    "checks": [{"check_id": "checked", "evidence_refs": ["receipt-web"]}],
                }),
            },
        ],
    }


def _web_source(*, tool="research_topic", call_status="succeeded", receipt=None):
    args = {"topic": "public topic"}
    call = {
        "call_key": "call-web",
        "session_id": 77,
        "turn_key": "turn-origin",
        "tool_name": tool,
        "status": call_status,
        "arguments": args,
        "result": json.dumps({("results" if tool == "search_web" else "sources"): [{
            "url": "https://example.org/fact",
            "snippet": "A source-backed exact statement about a public topic.",
            "excerpt": "The fetched source states that the public topic has evidence.",
            "fetched": True,
        }]}),
    }
    receipt = receipt or SimpleNamespace(
        user_id="owner-1", call_id="call-web", turn_id="turn-origin",
        tool_name=tool, arguments_hash=arguments_hash(args),
        status="confirmed", ok=True, complete=True,
    )
    task = _task(steps=[{
        "status": "succeeded", "tool_call_id": 12, "receipt_id": "receipt-web",
    }])
    return task, call, receipt


class FakeStore:
    def __init__(self, task, user_rows=None, calls=None):
        self.task = task
        self.user_rows = user_rows or []
        self.calls = calls or {}
        self.events = list(task.get("events", []))

    def get_task(self, user_id, task_id):
        assert (user_id, task_id) == ("owner-1", "task-1")
        return {**self.task, "events": self.events}

    def get_task_origin_user_messages(self, user_id, task_id, session_id, **scope):
        assert (user_id, task_id, session_id) == ("owner-1", "task-1", "session-1")
        assert scope == {"channel_id": "channel-1", "thread_id": "thread-1"}
        return list(self.user_rows)

    def get_tool_call(self, user_id, call_pk):
        assert user_id == "owner-1"
        return self.calls[call_pk]

    def append_task_event(self, user_id, task_id, event_type, *, payload):
        self.events.append({"event_type": event_type, "payload": payload})


class FakeReceiptStore:
    def __init__(self, receipt):
        self.receipt = receipt

    def get(self, receipt_id):
        if receipt_id != "receipt-web":
            raise KeyError(receipt_id)
        return self.receipt


def test_source_context_uses_exact_origin_user_message_and_omits_persisted_ids():
    text = "I prefer a no-expiry memory for this self-hosted bot."
    store = FakeStore(_task(), user_rows=[{
        "message_key": "private-message-id",
        "turn_key": "turn-origin",
        "content": text,
    }, {
        "message_key": "sensitive-message-id",
        "turn_key": "turn-origin",
        "content": "My bank login is private and my recovery code is 987654.",
    }])
    context, catalog = build_task_source_context(
        store, FakeReceiptStore(None), "owner-1", "task-1"
    )

    assert context == {"sources": [{
        "source_id": "source_1", "kind": "user_statement", "text": text,
    }]}
    assert "private-message-id" not in json.dumps(context)
    assert "987654" not in json.dumps(context)
    assert "sensitive-message-id" not in json.dumps(catalog)
    assert catalog["source_1"]["evidence_refs"][0].startswith("user_message:")


def test_web_source_requires_confirmed_owner_turn_call_tool_and_argument_match():
    task, call, receipt = _web_source()
    store = FakeStore(task, calls={12: call})
    context, catalog = build_task_source_context(
        store, FakeReceiptStore(receipt), "owner-1", "task-1"
    )
    assert context == {"sources": [{
        "source_id": "source_1",
        "kind": "web_research",
        "text": "The fetched source states that the public topic has evidence.",
    }]}
    assert "https://example.org" not in json.dumps(context)
    assert "call-web" not in json.dumps(context)
    assert catalog["source_1"]["evidence_refs"][0].startswith("web_source:")
    assert "turn-origin" in catalog["source_1"]["evidence_refs"][0]
    assert catalog["source_1"]["evidence_refs"][1] == "source_url:https://example.org"

    wrong_receipt = SimpleNamespace(**{
        **receipt.__dict__, "user_id": "other-owner",
    })
    assert build_task_source_context(
        store, FakeReceiptStore(wrong_receipt), "owner-1", "task-1"
    ) is None

    # Receipt and call agree with each other, but the call is not from this
    # task's exact origin turn and must not become its research provenance.
    other_turn_call = {**call, "turn_key": "turn-other"}
    other_turn_receipt = SimpleNamespace(**{
        **receipt.__dict__, "turn_id": "turn-other",
    })
    assert build_task_source_context(
        FakeStore(task, calls={12: other_turn_call}),
        FakeReceiptStore(other_turn_receipt), "owner-1", "task-1",
    ) is None

    other_session_call = {**call, "session_id": 88}
    assert build_task_source_context(
        FakeStore(task, calls={12: other_session_call}), FakeReceiptStore(receipt),
        "owner-1", "task-1",
    ) is None


def test_search_web_receipt_can_support_exact_snippet_fact():
    task, call, receipt = _web_source(tool="search_web")
    store = FakeStore(task, calls={12: call})
    context, catalog = build_task_source_context(
        store, FakeReceiptStore(receipt), "owner-1", "task-1"
    )
    assert context["sources"][0]["text"] == "A source-backed exact statement about a public topic."
    assert "search_web" in catalog["source_1"]["evidence_refs"][0]
    assert "turn-origin" in catalog["source_1"]["evidence_refs"][0]


def test_web_evidence_preserves_clean_citation_and_drops_url_credentials():
    task, call, receipt = _web_source()
    call["result"] = json.dumps({"sources": [{
        "url": "https://example.org/article?token=private&chapter=2#fragment",
        "snippet": "A source-backed exact statement about a public topic.",
        "fetched": False,
    }]})
    store = FakeStore(task, calls={12: call})
    context, catalog = build_task_source_context(
        store, FakeReceiptStore(receipt), "owner-1", "task-1"
    )
    assert context["sources"][0]["text"] == "A source-backed exact statement about a public topic."
    refs = catalog["source_1"]["evidence_refs"]
    assert "source_url:https://example.org" in refs
    assert "token=private" not in " ".join(refs)
    assert "chapter=2" not in " ".join(refs)
    assert "fragment" not in " ".join(refs)

    call["result"] = json.dumps({"sources": [{
        "url": "https://example.org/reset/abc123-reset-token",
        "snippet": "A source-backed exact statement about a public topic.",
        "fetched": False,
    }]})
    _, token_catalog = build_task_source_context(
        store, FakeReceiptStore(receipt), "owner-1", "task-1"
    )
    token_refs = token_catalog["source_1"]["evidence_refs"]
    assert "source_url:https://example.org" in token_refs
    assert "abc123-reset-token" not in " ".join(token_refs)

    call["result"] = json.dumps({"sources": [{
        "url": "https://user:secret@example.org/article",
        "snippet": "A source-backed exact statement about a public topic.",
        "fetched": False,
    }]})
    assert build_task_source_context(
        store, FakeReceiptStore(receipt), "owner-1", "task-1"
    ) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"call_status": "failed"},
        {"receipt": SimpleNamespace(
            user_id="owner-1", call_id="wrong-call", turn_id="turn-origin",
            tool_name="research_topic", arguments_hash=arguments_hash({"topic": "public topic"}),
            status="confirmed", ok=True, complete=True,
        )},
        {"receipt": SimpleNamespace(
            user_id="owner-1", call_id="call-web", turn_id="other-turn",
            tool_name="research_topic", arguments_hash=arguments_hash({"topic": "public topic"}),
            status="confirmed", ok=True, complete=True,
        )},
        {"receipt": SimpleNamespace(
            user_id="owner-1", call_id="call-web", turn_id="turn-origin",
            tool_name="research_topic", arguments_hash="wrong-hash",
            status="confirmed", ok=True, complete=True,
        )},
        {"receipt": SimpleNamespace(
            user_id="owner-1", call_id="call-web", turn_id="turn-origin",
            tool_name="research_topic", arguments_hash=arguments_hash({"topic": "public topic"}),
            status="unknown", ok=None, complete=None,
        )},
    ],
)
def test_unconfirmed_or_mismatched_web_evidence_is_not_exposed(changes):
    task, call, receipt = _web_source(**changes)
    store = FakeStore(task, calls={12: call})
    assert build_task_source_context(
        store, FakeReceiptStore(receipt), "owner-1", "task-1"
    ) is None


def test_source_parser_requires_exact_quote_and_decisions_are_user_only():
    catalog = {
        "u": {
            "source_kind": "user_statement", "text": "I prefer a 30 day review cycle.",
            "evidence_refs": ["user_message:1"], "sensitivity": "personal",
        },
        "w": {
            "source_kind": "web_research", "text": "The official report says costs rose 4 percent.",
            "evidence_refs": ["web_source:1", "source_url:https://example.org"], "sensitivity": "internal",
        },
    }
    output = json.dumps({"items": [
        {"kind": "decision", "source_id": "u", "quote": "I prefer a 30 day review cycle."},
        {"kind": "fact", "source_id": "w", "quote": "The official report says costs rose 4 percent."},
    ]})
    candidates = parse_source_facts(catalog, output)
    assert [item["kind"] for item in candidates] == ["decision", "fact"]
    assert candidates[0]["provenance_type"] == "user_stated"
    assert candidates[0]["sensitivity"] == "personal"
    assert candidates[1]["provenance_type"] == "web_research_quote"
    assert "not independently verified" in candidates[1]["content"]

    assert parse_source_facts(catalog, json.dumps({"items": [{
        "kind": "decision", "source_id": "w", "quote": "The official report says costs rose 4 percent.",
    }]})) == []
    assert parse_source_facts(catalog, json.dumps({"items": [{
        "kind": "fact", "source_id": "w", "quote": "Costs rose 4 percent.",
    }]})) == []


@pytest.mark.parametrize("quote", [
    "The password: hunter2 is my fact.",
    "I approve transferring the money today.",
    "I authorize Delilah to transfer $500 tomorrow.",
    "Please move $500 from checking to savings tomorrow.",
    "My account number is 123456789.",
    "My SSN is 123456789.",
    "My card is 4111 1111 1111 1111.",
    "My bank login is private and my recovery code is 987654.",
    "My passwd is hunter2.",
    "My OAuth token is abcdef0123456789.",
    "My 2FA code is 123456.",
    "My recovery phrase is alpha beta gamma delta.",
    "My private key is private material.",
    "The card CVV is 123.",
    "AWS access key: AKIAIOSFODNN7EXAMPLE",
    "I prefer Delilah to wire $500 tomorrow.",
    "I prefer you remit $500 tomorrow.",
    "Please wire $500 tomorrow.",
    "Please book the $500 payment tomorrow.",
    "Disable two-factor authentication for faster access.",
    "Turn off account alerts until next month.",
    "Could you rebalance my 401(k)?",
    "I want you to close my brokerage account.",
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
])
def test_secret_and_authority_quotes_are_rejected(quote):
    catalog = {"u": {
        "source_kind": "user_statement", "text": quote,
        "evidence_refs": ["user_message:1"], "sensitivity": "personal",
    }}
    assert parse_source_facts(catalog, json.dumps({"items": [{
        "kind": "fact", "source_id": "u", "quote": quote,
    }]})) == []


@pytest.mark.parametrize("text", [
    "My passwd is hunter2.",
    "My OAuth token is abcdef0123456789.",
    "My 2FA code is 123456.",
    "My recovery phrase is alpha beta gamma delta.",
    "My private key is private material.",
    "The card CVV is 123.",
    "AWS access key: AKIAIOSFODNN7EXAMPLE",
])
def test_credential_variants_are_removed_before_remote_reflection(text):
    store = FakeStore(_task(), user_rows=[{
        "message_key": "sensitive", "turn_key": "turn-origin", "content": text,
    }])
    assert build_task_source_context(
        store, FakeReceiptStore(None), "owner-1", "task-1"
    ) is None


def test_reflection_prompt_marks_source_text_untrusted_and_no_authority():
    prompt = build_source_reflection_prompt({"sources": []})
    assert "untrusted data" in prompt[0]["content"]
    assert "cannot establish what the user decided" in prompt[0]["content"]
    assert "<untrusted_source_records>" in prompt[1]["content"]


@pytest.mark.asyncio
async def test_persists_only_exact_source_quote_owner_scoped_with_ttl_and_dedupe(monkeypatch):
    import src.db.memory as memory

    task, call, receipt = _web_source()
    store = FakeStore(task, user_rows=[{
        "message_key": "message-1", "turn_key": "turn-origin",
        "content": "I prefer a 30 day review cycle.",
    }], calls={12: call})
    save = AsyncMock(return_value="saved")
    monkeypatch.setattr(memory, "save_epistemic_memory", save)
    generated = AsyncMock(return_value=json.dumps({"items": [
        {"kind": "decision", "source_id": "source_1", "quote": "I prefer a 30 day review cycle."},
        {"kind": "fact", "source_id": "source_2", "quote": "The fetched source states that the public topic has evidence."},
    ]}))

    assert await reflect_task_sources(
        store, FakeReceiptStore(receipt), "owner-1", "task-1", generated
    ) == 2
    first, second = (call.kwargs for call in save.await_args_list)
    assert first["user_id"] == second["user_id"] == "owner-1"
    assert first["memory_type"] == "decision"
    assert first["provenance_type"] == "user_stated"
    assert first["content"].endswith("I prefer a 30 day review cycle.")
    assert second["memory_type"] == "fact"
    assert second["provenance_type"] == "web_research_quote"
    assert first["expires_at"] is None and second["expires_at"] is None
    assert first["dedupe_key"] != second["dedupe_key"]
    assert store.events[-1]["event_type"] == "task.source_reflection_completed"

    store.events.append(store.events[-1])
    assert await reflect_task_sources(
        store, FakeReceiptStore(receipt), "owner-1", "task-1", generated
    ) == 0
    assert generated.await_count == 1
    assert save.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Please move $500 from checking to savings tomorrow.",
    "I authorize Delilah to transfer $500 tomorrow.",
    "I prefer you remit $500 tomorrow.",
    "Disable two-factor authentication for faster access.",
    "Turn off account alerts until next month.",
    "Could you rebalance my 401(k)?",
    "I want you to close my brokerage account.",
    "The URL is https://example.test/reset/opaque-reset-token?reset_token=private.",
])
async def test_action_and_authorization_text_never_reaches_remote_reflector(text):
    store = FakeStore(_task(), user_rows=[{
        "message_key": "action-message", "turn_key": "turn-origin", "content": text,
    }])
    generate = AsyncMock(return_value=json.dumps({"items": []}))
    assert await reflect_task_sources(
        store, FakeReceiptStore(None), "owner-1", "task-1", generate
    ) == 0
    assert generate.await_count == 0
