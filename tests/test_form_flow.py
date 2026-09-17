"""Unit tests for the interactive questionnaire form state layer.

Covers schema validation, session queuing, answer recording, and finalization
(claim assertion + embedding) with ``assert_claim`` and ``index_single_claim``
monkeypatched so no live database or vector backend is required.
"""

import pytest

from src.services import form_flow as ff
from src.services.form_flow import (
    FormSession,
    FormValidationError,
    INPUT_TYPES,
    PENDING_FORMS,
    finalize_form,
    get_session,
    queue_form,
    record_answers,
    validate_form_schema,
)


VALID_SCHEMA = {
    "title": "Risk Tolerance Questionnaire",
    "purpose": "calibrate investment recommendation",
    "pages": [
        {
            "page_title": "Personal Profile",
            "questions": [
                {"key": "first_name", "label": "First name", "input_type": "short", "required": True},
                {"key": "last_name", "label": "Last name", "input_type": "short", "required": True},
            ],
        },
        {
            "page_title": "Risk Profile",
            "questions": [
                {"key": "annual_income", "label": "Annual income (USD)", "input_type": "number", "required": True},
                {"key": "risk_tolerance", "label": "Risk tolerance 1-10", "input_type": "number", "required": False},
            ],
        },
    ],
}


@pytest.fixture(autouse=True)
def _clear_sessions():
    PENDING_FORMS.clear()
    yield
    PENDING_FORMS.clear()


# --------------------------------------------------------------------------- #
# validate_form_schema
# --------------------------------------------------------------------------- #
def test_validate_rejects_non_object():
    with pytest.raises(FormValidationError):
        validate_form_schema("not-a-dict")


def test_validate_rejects_missing_title():
    with pytest.raises(FormValidationError):
        validate_form_schema({"pages": [{"page_title": "P", "questions": [{"key": "k", "label": "L"}]}]})


def test_validate_rejects_empty_pages():
    with pytest.raises(FormValidationError):
        validate_form_schema({"title": "T", "pages": []})


def test_validate_rejects_too_many_pages():
    pages = [{"page_title": f"Page {i}", "questions": [{"key": f"k{i}", "label": "L"}]} for i in range(11)]
    with pytest.raises(FormValidationError):
        validate_form_schema({"title": "T", "pages": pages})


def test_validate_splits_overlong_page_into_chunks():
    schema = {
        "title": "T",
        "pages": [
            {
                "page_title": "Bulk",
                "questions": [
                    {"key": f"q{i}", "label": f"Q{i}"} for i in range(6)
                ],
            }
        ],
    }
    title, purpose, pages = validate_form_schema(schema)
    # 6 questions -> 2 pages of <=5, each keeping the original title on part 1
    assert len(pages) == 2
    assert [len(p["questions"]) for p in pages] == [5, 1]
    assert pages[0]["page_title"] == "Bulk"
    assert pages[1]["page_title"] == "Bulk (cont.)"
    all_keys = [q["key"] for p in pages for q in p["questions"]]
    assert all_keys == [f"q{i}" for i in range(6)]


def test_validate_raises_when_split_exceeds_ten_pages():
    # 2 * 6 questions -> 4 pages, still within limits after split
    pages = [
        {"page_title": f"P{i}", "questions": [
            {"key": f"p{i}q{j}", "label": f"Q{j}"} for j in range(6)
        ]}
        for i in range(2)
    ]
    title, purpose, norm = validate_form_schema({"title": "T", "pages": pages})
    assert len(norm) == 4
    # 6 * 6 questions -> 12 pages after splitting: input passes the <=10 page
    # check, the post-split guard must reject the expansion
    many = [
        {"page_title": f"P{i}", "questions": [
            {"key": f"p{i}q{j}", "label": f"Q{j}"} for j in range(6)
        ]}
        for i in range(6)
    ]
    with pytest.raises(FormValidationError):
        validate_form_schema({"title": "T", "pages": many})


def test_validate_rejects_question_without_key():
    bad = {
        "title": "T",
        "pages": [{"page_title": "P", "questions": [{"label": "No key here"}]}],
    }
    with pytest.raises(FormValidationError):
        validate_form_schema(bad)


def test_validate_rejects_duplicate_keys():
    dup = {
        "title": "T",
        "pages": [{"page_title": "P", "questions": [
            {"key": "dup", "label": "One"},
            {"key": "dup", "label": "Two"},
        ]}],
    }
    with pytest.raises(FormValidationError):
        validate_form_schema(dup)


def test_validate_normalizes_unknown_input_type_to_short():
    schema = {
        "title": "T",
        "pages": [{"page_title": "P", "questions": [
            {"key": "q1", "label": "L", "input_type": "weird_type"},
        ]}],
    }
    title, purpose, pages = validate_form_schema(schema)
    assert pages[0]["questions"][0]["input_type"] == "short"


def test_validate_rejects_overlong_key():
    long_key = "k" * 101
    bad = {
        "title": "T",
        "pages": [{"page_title": "P", "questions": [{"key": long_key, "label": "L"}]}],
    }
    with pytest.raises(FormValidationError):
        validate_form_schema(bad)


def test_validate_accepts_form_title_alias_and_defaults():
    schema = {
        "form_title": "Alias Title",
        "pages": [{"page_title": "P", "questions": [{"key": "q1", "label": "L"}]}],
    }
    title, purpose, pages = validate_form_schema(schema)
    assert title == "Alias Title"
    assert purpose == ""
    assert pages[0]["questions"][0]["required"] is True
    assert pages[0]["questions"][0]["predicate"] == "q1"


# --------------------------------------------------------------------------- #
# queue_form / get_session
# --------------------------------------------------------------------------- #
def test_queue_form_returns_session_and_stores_it():
    result = queue_form("4242", 123456789, VALID_SCHEMA)
    assert result["status"] == "FORM_QUEUED"
    assert result["pages"] == 2
    assert result["questions_total"] == 4
    assert result["form_title"] == "Risk Tolerance Questionnaire"
    session = get_session(result["session_id"])
    assert session is not None
    assert session.owner_uid == "4242"
    assert session.channel_id == 123456789
    assert len(session.pages) == 2


def test_queue_form_propagates_validation_errors():
    with pytest.raises(FormValidationError):
        queue_form("1", 2, {"title": "T", "pages": []})


# --------------------------------------------------------------------------- #
# record_answers / finalize_form
# --------------------------------------------------------------------------- #
def test_record_answers_merges_into_session():
    result = queue_form("1", 2, VALID_SCHEMA)
    session = get_session(result["session_id"])
    session.answers = {"first_name": "Alice"}
    assert record_answers(result["session_id"], {"last_name": "Smith"}) is True
    assert session.answers["first_name"] == "Alice"
    assert session.answers["last_name"] == "Smith"


def test_record_answers_returns_false_for_expired_session():
    assert record_answers("nonexistent-session", {"k": "v"}) is False


@pytest.mark.asyncio
async def test_finalize_form_asserts_and_embeds(monkeypatch):
    assert_calls = []

    def fake_assert_claim(
        subject_id, predicate, scalar_value=None, object_id=None,
        provenance_type="DIRECT_OBSERVATION", source_authority=4, **kw,
    ):
        claim_id = f"claim-{len(assert_calls)}"
        assert_calls.append({
            "subject_id": subject_id,
            "predicate": predicate,
            "scalar_value": scalar_value,
            "provenance_type": provenance_type,
            "source_authority": source_authority,
        })
        return claim_id

    embed_calls = []

    async def fake_index_single_claim(claim_id, subject_id, predicate, value, authority, user_id):
        embed_calls.append({
            "claim_id": claim_id,
            "subject_id": subject_id,
            "predicate": predicate,
            "value": value,
            "authority": authority,
            "user_id": user_id,
        })

    monkeypatch.setattr(ff, "assert_claim", fake_assert_claim)
    monkeypatch.setattr(ff, "index_single_claim", fake_index_single_claim)

    result = queue_form("777", 555, VALID_SCHEMA)
    session_id = result["session_id"]
    record_answers(session_id, {
        "first_name": "Alice",
        "last_name": "Smith",
        "annual_income": "60000",
        "risk_tolerance": "7",
    })

    summary = await finalize_form(session_id)

    assert summary["status"] == "SAVED"
    assert len(summary["claims"]) == 4
    assert len(assert_calls) == 4
    assert len(embed_calls) == 4
    # every answer is asserted against the user-scoped subject
    for c in assert_calls:
        assert c["subject_id"] == "user:777"
        assert c["provenance_type"] == "USER_PROVIDED"
        assert c["source_authority"] == 5
    # session is consumed
    assert get_session(session_id) is None


@pytest.mark.asyncio
async def test_finalize_form_isolates_embedding_failure(monkeypatch):
    claim_ids = []

    def fake_assert_claim(subject_id, predicate, scalar_value=None, **kw):
        cid = f"claim-{len(claim_ids)}"
        claim_ids.append(cid)
        return cid

    async def boom(*a, **kw):
        raise RuntimeError("vector backend down")

    monkeypatch.setattr(ff, "assert_claim", fake_assert_claim)
    monkeypatch.setattr(ff, "index_single_claim", boom)

    result = queue_form("9", 1, {
        "title": "T",
        "pages": [{"page_title": "P", "questions": [
            {"key": "q1", "label": "L", "predicate": "mood"},
        ]}],
    })
    record_answers(result["session_id"], {"q1": "fantastic"})
    summary = await finalize_form(result["session_id"])

    # claim is still persisted even though embedding failed
    assert len(summary["claims"]) == 1
    assert len(claim_ids) == 1
    assert summary["embedded"] == 0
    assert len(summary["errors"]) == 1


@pytest.mark.asyncio
async def test_finalize_form_empty_answer_is_skipped(monkeypatch):
    monkeypatch.setattr(ff, "assert_claim", lambda **kw: "cid")
    calls = []

    async def fake_index(*a, **kw):
        calls.append(a)

    monkeypatch.setattr(ff, "index_single_claim", fake_index)

    result = queue_form("1", 2, {
        "title": "T",
        "pages": [{"page_title": "P", "questions": [
            {"key": "present", "label": "L"},
            {"key": "missing", "label": "L2"},
        ]}],
    })
    # only one answer provided
    record_answers(result["session_id"], {"present": "hi"})
    summary = await finalize_form(result["session_id"])
    assert len(summary["claims"]) == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_finalize_form_missing_session_raises():
    with pytest.raises(KeyError):
        await finalize_form("missing-session-id")


def test_input_types_constant():
    assert "short" in INPUT_TYPES
    assert "long" in INPUT_TYPES
    assert "number" in INPUT_TYPES
    assert "paragraph" in INPUT_TYPES
