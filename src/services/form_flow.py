"""Interactive, LLM-generated questionnaire forms for Delilah Financial OS.

A Delilah advisor tool (`request_user_form`) lets the model ask the user for
structured information across multiple pages of short-answer questions. This
module is the state layer only: it validates the LLM-emitted form schema,
manages the in-memory session lifecycle, and — once the user has answered
every page — asserts each answer as a Knowledge-Graph claim
(`src.services.world_model.assert_claim`) and embeds the claim via the Qdrant
vector pipeline (`src.services.qdrant_client.index_single_claim`).

The Discord UI that renders the modal-per-page flow lives in
`src/bot/form_flow.py`, which imports this module. Keeping discord out of this
file makes the validation/assertion logic trivially unit-testable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from src.services.world_model import assert_claim
from src.services.qdrant_client import index_single_claim

# In-memory form sessions. These are ephemeral by design: a form is tied to an
# active advisor turn and is discarded once answered or when the session
# expires. No persistence is required — answers are written straight into the
# Knowledge Graph on completion.
PENDING_FORMS: Dict[str, "FormSession"] = {}

# Allowed input_type values in a question spec. Anything else falls back to
# "short" so the model cannot break the UI with an unknown type.
INPUT_TYPES = {"short", "long", "number", "email", "paragraph"}


class FormValidationError(ValueError):
    """Raised when an LLM-emitted form schema is structurally invalid."""


@dataclass
class FormSession:
    session_id: str
    owner_uid: str
    channel_id: int
    title: str
    purpose: str
    pages: List[Dict[str, Any]]
    answers: Dict[str, str] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    )


def validate_form_schema(form_schema: Any) -> tuple[str, str, List[Dict[str, Any]]]:
    """Validate and normalize an LLM-emitted form schema.

    Returns (title, purpose, normalized_pages) where each question is normalized
    to a stable dict with keys: key, label, input_type, required, help_text,
    predicate.
    """
    if not isinstance(form_schema, dict):
        raise FormValidationError("form_schema must be a JSON object")

    title = str(form_schema.get("title") or form_schema.get("form_title") or "").strip()
    if not title:
        raise FormValidationError("form_schema.title is required")
    if len(title) > 200:
        raise FormValidationError("form_schema.title must be <= 200 chars")

    purpose = str(form_schema.get("purpose", "")).strip()

    pages_in = form_schema.get("pages")
    if not isinstance(pages_in, list) or not pages_in:
        raise FormValidationError("form_schema.pages must be a non-empty array")
    if len(pages_in) > 10:
        raise FormValidationError("form_schema.pages must have <= 10 pages")

    norm_pages: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for i, page in enumerate(pages_in):
        if not isinstance(page, dict):
            raise FormValidationError(f"page {i} must be an object")
        page_title = str(page.get("page_title") or f"Page {i + 1}").strip()
        if len(page_title) > 200:
            raise FormValidationError(f"page {i} title must be <= 200 chars")

        questions = page.get("questions")
        if not isinstance(questions, list) or not questions:
            raise FormValidationError(f"page {i} ({page_title}) requires a non-empty questions array")

        # Discord modals hold at most 5 text inputs, so an over-long page is
        # split into multiple pages instead of rejected. Models routinely emit
        # 6+ questions per page; failing here costs a whole cloud round-trip.
        chunks = [questions[k : k + 5] for k in range(0, len(questions), 5)]
        for ci, chunk in enumerate(chunks):
            norm_qs: List[Dict[str, Any]] = []
            for j, q in enumerate(chunk):
                if not isinstance(q, dict):
                    raise FormValidationError(f"question {j} on page {i} must be an object")
                key = str(q.get("key") or "").strip()
                if not key:
                    raise FormValidationError(f"question {j} on page {i} requires a 'key'")
                if len(key) > 100:
                    raise FormValidationError(f"question {j} on page {i} has a 'key' over 100 chars")
                if key in seen_keys:
                    raise FormValidationError(f"duplicate question key '{key}' across the form")
                seen_keys.add(key)

                label = str(q.get("label") or key).strip()[:200] or key
                input_type = str(q.get("input_type") or "short").strip().lower()
                if input_type not in INPUT_TYPES:
                    input_type = "short"
                norm_qs.append({
                    "key": key,
                    "label": label,
                    "input_type": input_type,
                    "required": bool(q.get("required", True)),
                    "help_text": str(q.get("help_text", "") or ""),
                    "predicate": str(q.get("predicate") or key).strip()
                    or key,
                })
            chunk_title = page_title if ci == 0 else f"{page_title} (cont.)"
            norm_pages.append({"page_title": chunk_title, "questions": norm_qs})

    if len(norm_pages) > 10:
        raise FormValidationError(
            f"form_schema expands to {len(norm_pages)} pages after splitting "
            "over-long pages; keep it to 10 pages max"
        )

    return title, purpose, norm_pages


def queue_form(owner_uid: Any, channel_id: Any, form_schema: Any) -> Dict[str, Any]:
    """Validate the schema and register a new form session.

    Returns a small JSON-serialisable status dict that the LLM tool-result
    handler turns into the model's tool feedback.
    """
    title, purpose, pages = validate_form_schema(form_schema)
    total_questions = sum(len(p["questions"]) for p in pages)
    session_id = uuid.uuid4().hex
    session = FormSession(
        session_id=session_id,
        owner_uid=str(owner_uid),
        channel_id=int(channel_id),
        title=title,
        purpose=purpose,
        pages=pages,
    )
    PENDING_FORMS[session_id] = session
    print(
        f" [FORM] queued session={session_id[:8]} title={title!r} "
        f"pages={len(pages)} questions={total_questions} uid={session.owner_uid}",
        flush=True,
    )
    return {
        "status": "FORM_QUEUED",
        "session_id": session_id,
        "form_title": title,
        "pages": len(pages),
        "questions_total": total_questions,
    }


def get_session(session_id: str) -> FormSession | None:
    return PENDING_FORMS.get(session_id)


def record_answers(session_id: str, answers: Dict[str, str]) -> bool:
    """Merge a page's answers into the session. Returns False if expired."""
    sess = PENDING_FORMS.get(session_id)
    if sess is None:
        return False
    sess.answers.update(answers)
    return True


async def finalize_form(session_id: str) -> Dict[str, Any]:
    """Persist a completed form: assert each answer as a KG claim + embed it.

    Runs the Knowledge-Graph assertion (synchronous SQLite) and the Qdrant
    vector embedding (async) for every answered question. Embedding failures
    are isolated — a vector hiccup must never prevent the claim from being
    recorded in SQLite.
    """
    session = PENDING_FORMS.pop(session_id, None)
    if session is None:
        raise KeyError("form session not found (expired or already submitted)")

    subject_id = f"user:{session.owner_uid}"
    claims: List[Dict[str, Any]] = []
    embedded = 0
    errors: List[str] = []

    for page in session.pages:
        for q in page["questions"]:
            answer = session.answers.get(q["key"])
            if answer is None or str(answer).strip() == "":
                continue  # required-field gaps are skipped; the UI already enforces presence
            predicate = q["predicate"]
            try:
                claim_id = assert_claim(
                    subject_id=subject_id,
                    predicate=predicate,
                    scalar_value=str(answer),
                    provenance_type="USER_PROVIDED",
                    source_authority=5,
                    owner_user_id=session.owner_uid,
                )
                claims.append({"key": q["key"], "predicate": predicate, "claim_id": claim_id, "value": str(answer)})
            except Exception as exc:  # noqa: BLE001 — claim persistence must be resilient
                print(f" [FORM] assert_claim failed for {predicate!r}: {type(exc).__name__}: {exc}", flush=True)
                errors.append(f"{predicate}: {type(exc).__name__}")
                continue
            try:
                await index_single_claim(
                    claim_id=claim_id,
                    subject_id=subject_id,
                    predicate=predicate,
                    value=str(answer),
                    authority=5,
                    user_id=session.owner_uid,
                )
                embedded += 1
            except Exception as exc:  # noqa: BLE001 — embedding is best-effort
                print(f" [FORM] index_single_claim failed for {predicate!r}: {type(exc).__name__}: {exc}", flush=True)
                errors.append(f"embed {predicate}: {type(exc).__name__}")

    return {
        "status": "SAVED",
        "form_title": session.title,
        "claims": claims,
        "embedded": embedded,
        "errors": errors,
    }


__all__ = [
    "PENDING_FORMS",
    "FormSession",
    "FormValidationError",
    "INPUT_TYPES",
    "validate_form_schema",
    "queue_form",
    "get_session",
    "record_answers",
    "finalize_form",
]
