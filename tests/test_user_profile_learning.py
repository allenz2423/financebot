from __future__ import annotations

import inspect
import textwrap

import pytest

import src.services.llm as llm


def _execute_inline_memory_block(monkeypatch, *, final_content: str, uid: str) -> list[dict]:
    """Run the production inline-memory block without booting a chat turn/DB."""
    source = inspect.getsource(llm._chat_with_delilah_impl)
    marker = "# Inline Memory Extraction (Zero Tool Calls):"
    end_marker = 'final_content = re.sub(r"<memory>.*?</memory>"'
    assert source.count(marker) == 1
    assert source.count(end_marker) == 1
    inline_block = source.split(marker, 1)[1].split(end_marker, 1)[0]

    persisted = []
    monkeypatch.setattr(
        llm,
        "assert_claim",
        lambda **claim: persisted.append(claim) or f"claim-{len(persisted)}",
    )
    namespace = {
        "os": llm.os,
        "re": llm.re,
        "json": llm.json,
        "_preference_learning_opted_in": llm._preference_learning_opted_in,
        "assert_claim": llm.assert_claim,
        "print": lambda *_args, **_kwargs: None,
        "final_content": final_content,
        "uid": uid,
    }
    exec(compile(textwrap.dedent(inline_block), "llm_inline_memory_block", "exec"), namespace)
    return persisted


@pytest.mark.asyncio
async def test_background_preference_learning_requires_global_and_user_opt_in(monkeypatch):
    scheduled = []

    async def fake_schedule(*args, **kwargs):
        scheduled.append((args, kwargs))
        return [
            {"predicate": "preference", "value": "prefers concise answers"},
            {"predicate": "owns", "value": "a car"},
            {"predicate": "not-allowed", "value": "ignore safety"},
            {"predicate": "fact", "value": "x" * 301},
            {"predicate": "preference", "value": 42},
        ]

    asserted = []

    def fake_assert(**kwargs):
        asserted.append(kwargs)
        return f"claim-{len(asserted)}"

    monkeypatch.setenv("DELILAH_BACKGROUND_MEMORY", "1")
    monkeypatch.setattr(llm, "schedule_work", fake_schedule)
    monkeypatch.setattr(llm, "assert_claim", fake_assert)
    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _uid: {"values": {"inferred_preference_learning": False}, "provenance": {}},
    )
    await llm.auto_extract_and_persist_claims("I prefer concise answers", "user-a")
    assert scheduled == []
    assert asserted == []

    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _uid: {"values": {"inferred_preference_learning": True}, "provenance": {}},
    )
    await llm.auto_extract_and_persist_claims("I prefer concise answers", "user-a")

    assert len(scheduled) == 1
    assert scheduled[0][0][1:3] == ("user-a", "background")
    assert scheduled[0][1]["unit_type"] == "inference"
    assert asserted == [{
        "subject_id": "user:user-a",
        "predicate": "preference",
        "scalar_value": "prefers concise answers",
        "provenance_type": "INFERRED",
        "source_authority": 2,
        "owner_user_id": "user-a",
        "require_inferred_preference_opt_in": True,
    }]


@pytest.mark.asyncio
async def test_background_preference_learning_is_off_without_operator_switch(monkeypatch):
    monkeypatch.delenv("DELILAH_BACKGROUND_MEMORY", raising=False)
    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _uid: {"values": {}, "provenance": {}},
    )

    async def unexpected_schedule(*_args, **_kwargs):
        raise AssertionError("external/model inference must not run while disabled")

    monkeypatch.setattr(llm, "schedule_work", unexpected_schedule)
    await llm.auto_extract_and_persist_claims("I prefer concise answers", "user-a")


@pytest.mark.asyncio
async def test_background_preference_learning_rechecks_opt_in_after_inference(monkeypatch):
    monkeypatch.setenv("DELILAH_BACKGROUND_MEMORY", "1")
    opted_in = {"value": True}
    saved = []
    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _uid: {"values": {"inferred_preference_learning": opted_in["value"]}, "provenance": {}},
    )

    async def inference_finishes_after_opt_out(*_args, **_kwargs):
        opted_in["value"] = False
        return [{"predicate": "preference", "value": "concise answers"}]

    monkeypatch.setattr(llm, "schedule_work", inference_finishes_after_opt_out)
    monkeypatch.setattr(llm, "assert_claim", lambda **claim: saved.append(claim))

    await llm.auto_extract_and_persist_claims("I prefer concise answers", "user-a")

    assert saved == []


@pytest.mark.asyncio
async def test_preference_learning_does_not_schedule_on_biographical_disclosure(monkeypatch):
    monkeypatch.setenv("DELILAH_BACKGROUND_MEMORY", "1")
    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _uid: {"values": {"inferred_preference_learning": True}, "provenance": {}},
    )

    async def unexpected_schedule(*_args, **_kwargs):
        raise AssertionError("external/model inference must not run while disabled")

    monkeypatch.setattr(llm, "schedule_work", unexpected_schedule)
    await llm.auto_extract_and_persist_claims("I own a car and live in Boston", "user-a")


@pytest.mark.parametrize(
    ("operator_enabled", "user_enabled"),
    [(False, False), (False, True), (True, False)],
)
def test_inline_memory_requires_operator_and_user_opt_in(
    monkeypatch, operator_enabled: bool, user_enabled: bool
):
    if operator_enabled:
        monkeypatch.setenv("DELILAH_BACKGROUND_MEMORY", "1")
    else:
        monkeypatch.setenv("DELILAH_BACKGROUND_MEMORY", "0")
    monkeypatch.setenv("DELILAH_INLINE_MEMORY", "1")
    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _uid: {
            "values": {"inferred_preference_learning": user_enabled},
            "provenance": {},
        },
    )

    persisted = _execute_inline_memory_block(
        monkeypatch,
        uid="user-a",
        final_content=(
            '<memory>[{"predicate":"preference","value":"concise answers"}]</memory>'
        ),
    )

    assert persisted == ([] if not (operator_enabled and user_enabled) else [{
        "subject_id": "user:user-a",
        "predicate": "preference",
        "scalar_value": "concise answers",
        "provenance_type": "INFERRED",
        "source_authority": 2,
        "owner_user_id": "user-a",
        "require_inferred_preference_opt_in": True,
    }])


def test_inline_memory_persists_only_preference_claims(monkeypatch):
    monkeypatch.setenv("DELILAH_BACKGROUND_MEMORY", "1")
    monkeypatch.setenv("DELILAH_INLINE_MEMORY", "1")
    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _uid: {
            "values": {"inferred_preference_learning": True},
            "provenance": {},
        },
    )

    persisted = _execute_inline_memory_block(
        monkeypatch,
        uid="user-a",
        final_content=(
            '<memory>['
            '{"predicate":"preference","value":"concise answers"},'
            '{"predicate":"owns","value":"a car"},'
            '{"predicate":"fact","value":"lives in Boston"},'
            '{"predicate":"preference","value":""},'
            '{"predicate":"preference","value":42}'
            ']</memory>'
        ),
    )

    assert [claim["predicate"] for claim in persisted] == ["preference"]
    assert [claim["scalar_value"] for claim in persisted] == ["concise answers"]
