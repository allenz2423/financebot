"""Batched KEV transaction classification for Delilah.

The module deliberately has three separate stages:

``prepare -> predict -> apply``

Prediction writes only an auditable prediction record.  ``apply_auto`` is the
only function here that can change a transaction's classification fields, and
it changes no Plaid identity/accounting fields.  This makes shadow mode safe
and gives the existing correction system the final say over human-confirmed
labels.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from src.services.kev_decision import (
    DecisionAnswer,
    DecisionInvalidResponse,
    DecisionProvider,
    DecisionQuestion,
    DecisionRequest,
    DecisionUnavailable,
)
from src.services.transaction_taxonomy import (
    MAIN_CATEGORIES,
    TAXONOMY,
    TRANSACTION_CATEGORIES,
    UNCATEGORIZED_PATH,
    canonicalize_taxonomy_choice,
    category_is_valid,
    category_parts,
    legacy_category_for_path,
)
from src.services.transaction_evidence import registry_trust_tier


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class KevTransactionPolicy:
    """Runtime policy.  Defaults are conservative and production-safe."""

    enabled: bool = field(default_factory=lambda: _truthy(os.getenv("KEV_TX_ENABLED", "0")))
    shadow: bool = field(default_factory=lambda: _truthy(os.getenv("KEV_TX_SHADOW", "0")))
    fallback_enabled: bool = field(default_factory=lambda: _truthy(os.getenv("KEV_TX_FALLBACK_ENABLED", "1"), True))
    batch_size: int = field(default_factory=lambda: max(1, int(os.getenv("KEV_TX_BATCH_SIZE", "8"))))
    auto_accept_threshold: float = field(default_factory=lambda: _float_env("KEV_TX_AUTO_ACCEPT_THRESHOLD", 0.95))
    review_threshold: float = field(default_factory=lambda: _float_env("KEV_TX_REVIEW_THRESHOLD", 0.80))
    minimum_margin: float = field(default_factory=lambda: _float_env("KEV_TX_MIN_MARGIN", 0.20))
    max_context_chars: int = field(default_factory=lambda: max(400, int(os.getenv("KEV_TX_MAX_CONTEXT_CHARS", "2400"))))
    max_history_rows: int = field(default_factory=lambda: max(0, int(os.getenv("KEV_TX_MAX_HISTORY_ROWS", "8"))))
    retries: int = field(default_factory=lambda: max(0, min(3, int(os.getenv("KEV_TX_RETRIES", "1")))))
    quick_search_enabled: bool = field(default_factory=lambda: _truthy(os.getenv("KEV_TX_QUICK_SEARCH_ENABLED", "1"), True))
    quick_search_max_items: int = field(default_factory=lambda: max(1, int(os.getenv("KEV_TX_QUICK_SEARCH_MAX_ITEMS", "8"))))
    quick_search_max_chars: int = field(default_factory=lambda: max(300, int(os.getenv("KEV_TX_QUICK_SEARCH_MAX_CHARS", "1800"))))


@dataclass(frozen=True)
class TransactionClassification:
    transaction_row_id: str
    transaction_id: str | None
    category: str | None
    category_probabilities: Mapping[str, float] = field(default_factory=dict)
    category_confidence: float = 0.0
    category_margin: float = 0.0
    recurring: bool | None = None
    subscription: bool | None = None
    transfer: bool | None = None
    enrichment_needed: bool | None = None
    review_required: bool | None = None
    decision_confidences: Mapping[str, float] = field(default_factory=dict)
    source: str = "kev"
    # deterministic/auto/review are usable outcomes.  abstained means KEV
    # answered but policy would not accept it; invalid_response and
    # inference_error are actual execution failures.
    status: str = "abstained"
    model: str | None = None
    request_id: str | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in {"deterministic", "auto", "review"} and category_is_valid(self.category)

    @property
    def needs_fallback(self) -> bool:
        return self.status in {"review", "abstained", "invalid_response", "inference_error"}


@dataclass
class TransactionBatchResult:
    batch_id: str
    predictions: list[TransactionClassification]
    processed: int
    kev_classified: int
    deterministic: int
    auto_accepted: int
    review: int
    failed: int
    latency_ms: float
    model: str | None = None
    error: str | None = None
    abstained: int = 0

    @property
    def fallback_count(self) -> int:
        return sum(prediction.needs_fallback for prediction in self.predictions)

    def by_transaction(self) -> dict[str, TransactionClassification]:
        return {prediction.transaction_row_id: prediction for prediction in self.predictions}


def _row_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return getattr(item, key, default)


def _transaction_row_id(item: Any) -> str:
    value = _row_value(item, "transaction_row_id", None)
    if value is None:
        value = _row_value(item, "tx_id", None)
    if value is None:
        value = _row_value(item, "id", None)
    if value is None or str(value).strip() == "":
        raise ValueError("transaction is missing a stable row id")
    return str(value)


def _safe_json(value: Any, limit: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        text = str(value)
    return text[:limit]


_EMOJI_MARKERS = re.compile(
    "["
    "\\U0001F000-\\U0001FAFF"
    "\\U00002600-\\U000027BF"
    "\\U00002300-\\U000023FF"
    "]|[\\uFE0E\\uFE0F\\u200D]"
)


def _plain_category_label(value: Any) -> str:
    """Remove presentation emoji from legacy labels before model inference.

    Historical ledger labels still contain emoji for UI compatibility.  KEV's
    taxonomy is deliberately plain text, so those presentation markers should
    not become evidence for a classification decision.
    """
    return " ".join(_EMOJI_MARKERS.sub("", str(value or "")).split())


_REGISTRY_CATEGORY_ALIASES: dict[str, str] = {
    "Digital Services": "Discretionary Spending (Wants) → Subscriptions & Digital Services → Software, Cloud Storage & SaaS Apps",
    "Digital Subscriptions": "Discretionary Spending (Wants) → Subscriptions & Digital Services → Software, Cloud Storage & SaaS Apps",
    "Software & Cloud Services": "Discretionary Spending (Wants) → Subscriptions & Digital Services → Software, Cloud Storage & SaaS Apps",
    "Fitness & Gyms": "Discretionary Spending (Wants) → Personal Care & Wellness → Gym Memberships & Fitness Classes",
    "Health & Fitness": "Discretionary Spending (Wants) → Personal Care & Wellness → Gym Memberships & Fitness Classes",
    "Credit Card Bill Payment": "Transfers & Non-Expense Movements (Neutral) → Transfers → Credit Card Payments (Checking → Credit Card)",
    "Credit Card Payment": "Transfers & Non-Expense Movements (Neutral) → Transfers → Credit Card Payments (Checking → Credit Card)",
    "P2P Transfer": "Transfers & Non-Expense Movements (Neutral) → Transfers → P2P Transfers (Venmo, Zelle, Cash App)",
    "P2P Transfers": "Transfers & Non-Expense Movements (Neutral) → Transfers → P2P Transfers (Venmo, Zelle, Cash App)",
}


def _canonical_registry_category(value: Any) -> str | None:
    """Normalize trusted legacy registry labels into KEV taxonomy paths."""
    plain = _plain_category_label(value)
    if not plain:
        return None
    if plain in _REGISTRY_CATEGORY_ALIASES:
        return _REGISTRY_CATEGORY_ALIASES[plain]
    if category_is_valid(plain):
        return plain
    if category_is_valid(str(value or "").strip()):
        return str(value).strip()
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_kev_transaction_schema(conn: sqlite3.Connection) -> None:
    """Create only additive prediction/metrics tables; never alter ledger tables."""
    additive_columns = (
        ("category_path", "TEXT"),
        ("category_main", "TEXT"),
        ("category_group", "TEXT"),
        ("is_transfer", "INTEGER DEFAULT 0"),
    )
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in ("transactions", "transactions_original"):
        if table not in tables:
            continue
        existing_columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in additive_columns:
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transaction_classification_batches (
            batch_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            model TEXT,
            requested_count INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0,
            kev_count INTEGER NOT NULL DEFAULT 0,
            deterministic_count INTEGER NOT NULL DEFAULT 0,
            auto_count INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            abstained_count INTEGER NOT NULL DEFAULT 0,
            fallback_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            latency_ms REAL,
            confidence_summary TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transaction_classification_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            transaction_row_id INTEGER NOT NULL,
            transaction_id TEXT,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            model TEXT,
            category TEXT,
            category_probabilities TEXT,
            dimensions TEXT,
            confidence REAL,
            margin REAL,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES transaction_classification_batches(batch_id)
        );
        CREATE TABLE IF NOT EXISTS transaction_classification_reviews (
            review_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            transaction_row_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            reason TEXT NOT NULL,
            identity_status TEXT,
            evidence_json TEXT,
            candidate_category TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tx_classification_predictions_tx
            ON transaction_classification_predictions(user_id, transaction_row_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tx_classification_predictions_status
            ON transaction_classification_predictions(user_id, status, created_at DESC);
        """
    )
    batch_columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(transaction_classification_batches)"
        ).fetchall()
    }
    if "abstained_count" not in batch_columns:
        conn.execute(
            "ALTER TABLE transaction_classification_batches "
            "ADD COLUMN abstained_count INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def _human_correction_exists(conn: sqlite3.Connection, row_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM transaction_correction_log
        WHERE transaction_row_id = ?
          AND LOWER(COALESCE(source, '')) NOT LIKE 'kev%'
          AND LOWER(COALESCE(source, '')) NOT LIKE 'model%'
        LIMIT 1
        """,
        (row_id,),
    ).fetchone()
    return row is not None


def _history_summary(conn: sqlite3.Connection | None, item: Any, canonical_name: str, limit: int) -> dict[str, Any]:
    if conn is None or limit <= 0 or not canonical_name:
        return {}
    row_id = _transaction_row_id(item)
    rows = conn.execute(
        """
        SELECT t.category, COUNT(*)
        FROM transactions t
        LEFT JOIN transaction_correction_log l
          ON l.transaction_row_id = t.id
         AND LOWER(COALESCE(l.source, '')) NOT LIKE 'kev%'
         AND LOWER(COALESCE(l.source, '')) NOT LIKE 'model%'
        WHERE t.user_id = ?
          AND t.id != ?
          AND LOWER(COALESCE(t.clean_merchant, t.merchant, '')) = LOWER(?)
          AND COALESCE(t.category, '') != ''
        GROUP BY t.category
        ORDER BY COUNT(*) DESC
        LIMIT ?
        """,
        (
            str(_row_value(item, "user_id", "")),
            row_id,
            canonical_name,
            limit,
        ),
    ).fetchall()
    return {
        "category_counts": {
            _plain_category_label(category): int(count)
            for category, count in rows
            if category and _plain_category_label(category)
        }
    }


def prepare_transaction_context(
    item: Any,
    *,
    conn: sqlite3.Connection | None = None,
    policy: KevTransactionPolicy | None = None,
) -> dict[str, Any]:
    """Build a bounded feature representation without copying labels as truth."""
    policy = policy or KevTransactionPolicy()
    raw_merchant = str(_row_value(item, "merchant", "") or "")
    clean_merchant = str(_row_value(item, "clean_merchant", "") or "")
    if not clean_merchant:
        try:
            from src.services.merchants import clean_raw_merchant_descriptor
            clean_merchant = clean_raw_merchant_descriptor(raw_merchant)
        except Exception:
            clean_merchant = raw_merchant.strip()

    registry: dict[str, Any] = {}
    try:
        from src.services.merchants import resolve_canonical_merchant
        registry = resolve_canonical_merchant(raw_merchant or clean_merchant, conn=conn)
    except Exception:
        registry = {"canonical_name": clean_merchant, "category": None, "confidence": "none", "matched_by": "none"}
    canonical_name = str(registry.get("canonical_name") or clean_merchant)
    history = _history_summary(conn, item, canonical_name, policy.max_history_rows)
    registry_category = _canonical_registry_category(registry.get("category"))
    registry_source = str(registry.get("source") or "unknown")
    registry_trust = registry_trust_tier(registry_source, registry.get("matched_by"))
    context = {
        "row_id": _transaction_row_id(item),
        "merchant": raw_merchant[:300],
        "normalized_merchant": canonical_name[:200],
        "original_description": str(_row_value(item, "original_description", raw_merchant) or "")[:300],
        "amount": _row_value(item, "amount", None),
        "date": str(_row_value(item, "txn_date", _row_value(item, "date", "")) or "")[:40],
        "account": str(_row_value(item, "account_used", "") or "")[:120],
        "payment_channel": str(_row_value(item, "payment_channel", "") or "")[:80],
        "registry": {
            "matched_by": str(registry.get("matched_by") or "none"),
            "category": registry_category,
            "confidence": str(registry.get("confidence") or "none"),
            "source": registry_source,
            "trust_tier": registry_trust,
            "evidence_url": str(registry.get("evidence_url") or "")[:500],
            "updated_at": str(registry.get("updated_at") or "")[:40],
        },
        "historical_summary": history,
        "deterministic_signals": _row_value(item, "deterministic_signals", {}) or {},
    }
    research = str(_row_value(item, "web_context", _row_value(item, "merchant_research", "")) or "")
    if research.strip():
        context["quick_merchant_research"] = research[:policy.quick_search_max_chars]
    encoded = _safe_json(context, policy.max_context_chars)
    # Round-trip makes truncation impossible to mistake for a complete object.
    if len(encoded) > policy.max_context_chars:
        context["historical_summary"] = {}
        context["deterministic_signals"] = {}
        context["original_description"] = context["original_description"][:120]
    return context


def _known_merchant_prediction(item: Any, context: Mapping[str, Any]) -> TransactionClassification | None:
    registry = context.get("registry") if isinstance(context.get("registry"), Mapping) else {}
    category = registry.get("category")
    matched_by = str(registry.get("matched_by") or "")
    confidence = str(registry.get("confidence") or "").lower()
    trust_tier = str(registry.get("trust_tier") or "UNTRUSTED")
    if trust_tier != "TRUSTED" or not category_is_valid(category) or matched_by == "heuristic_cleaner" or confidence not in {"high", "medium"}:
        return None
    probabilities = {name: (1.0 if name == category else 0.0) for name in TRANSACTION_CATEGORIES}
    return TransactionClassification(
        transaction_row_id=_transaction_row_id(item),
        transaction_id=_row_value(item, "transaction_id", None),
        category=str(category),
        category_probabilities=probabilities,
        category_confidence=1.0,
        category_margin=1.0,
        source="known_merchant",
        status="deterministic",
    )


def _noul_question(question_id: str, instructions: str) -> tuple[str, DecisionQuestion]:
    return question_id, DecisionQuestion(type="noul", instructions=instructions)


TRANSFER_MAIN = "Transfers & Non-Expense Movements"


def _main_question(
    question_id: str,
    *,
    routing_signals: Mapping[str, bool | None] | None = None,
) -> tuple[str, DecisionQuestion]:
    """Build the normal broad router without exposing the abstention class.

    ``Uncategorized Purchase`` is deliberately not a criterion here.  It is a
    safe final representation for an abstained prediction, not a competing
    category that can win before subgroup/leaf classification happens.
    """
    signals = routing_signals or {}
    transfer = signals.get("transfer") is True
    subscription = signals.get("subscription") is True
    recurring = signals.get("recurring") is True

    # A positive transfer decision is a hard accounting constraint.  Other
    # dimensions remain soft routing evidence because, for example, some
    # recurring payments are utilities or debt rather than subscriptions.
    mains = (TRANSFER_MAIN,) if transfer else MAIN_CATEGORIES
    criteria = {
        main: f"The transaction belongs primarily to {main}."
        for main in mains
    }
    hints: list[str] = []
    if subscription:
        hints.append(
            "Subscription signal is positive: favor the branch containing "
            "Subscriptions & Digital Services when the merchant represents a service."
        )
    if recurring:
        hints.append(
            "Recurring signal is positive: use it as supporting evidence, not "
            "as proof that the transaction is a subscription."
        )
    if transfer:
        hints.append(
            "Transfer signal is positive: this is constrained to the transfers "
            "branch and must not be treated as ordinary spending."
        )
    instructions = (
        "Choose the broadest applicable taxonomy branch from the supplied choices. "
        "Do not choose a subgroup or leaf yet. An uncertain decision must be "
        "abstained by the controller; do not invent a fallback category."
    )
    if hints:
        instructions += " Routing evidence: " + " ".join(hints)
    return question_id, DecisionQuestion(
        type="choice",
        instructions=instructions,
        criteria=criteria,
    )


def _group_question(
    question_id: str,
    main: str,
    *,
    routing_signals: Mapping[str, bool | None] | None = None,
    merchant: str = "",
) -> tuple[str, DecisionQuestion]:
    groups = TAXONOMY[main]
    signals = routing_signals or {}
    instructions = (
        f"Within {main}, choose the single best subgroup. Do not choose a leaf "
        "from another subgroup. Subscription and recurring signals are routing "
        "evidence, not subgroup labels."
    )
    if signals.get("subscription") is True and main == "Discretionary Spending":
        instructions += (
            " A gym, fitness, salon, spa, or other physical membership belongs "
            "under Personal Care & Wellness even when it is billed as a subscription; "
            "digital products and online services belong under Subscriptions & Digital Services."
        )
    if merchant:
        instructions += f" Merchant evidence: {merchant[:180]}."
    return question_id, DecisionQuestion(
        type="choice",
        instructions=instructions,
        criteria={group: f"A {main} transaction in the {group} subgroup." for group in groups},
    )


def _leaf_question(question_id: str, main: str, group: str) -> tuple[str, DecisionQuestion]:
    return question_id, DecisionQuestion(
        type="choice",
        instructions=f"Within {main} → {group}, choose the most specific supported leaf.",
        criteria={leaf: f"{leaf} under {group}." for leaf in TAXONOMY[main][group]},
    )


def _dimension_questions(row_id: str) -> dict[str, DecisionQuestion]:
    return dict(
        [
            _noul_question(f"{row_id}:recurring", "Is this transaction likely part of a recurring pattern for the merchant or account?"),
            _noul_question(f"{row_id}:subscription", "Is this transaction likely payment for a subscription or membership service?"),
            _noul_question(f"{row_id}:transfer", "Is this transaction a transfer or payment movement rather than ordinary spending or income?"),
            _noul_question(f"{row_id}:enrichment", "Would merchant research or external enrichment materially improve safe classification?"),
            _noul_question(f"{row_id}:review", "Should this transaction be escalated for human or language-model review instead of automatic acceptance?"),
        ]
    )


def _answer_confidence(answer: DecisionAnswer) -> float:
    # Confidence is derived from the distribution, not trusted as a free-form
    # assertion. Calibration can later replace this with a learned mapping.
    return max(0.0, min(1.0, float(answer.top_probability)))


def _answer_margin(answer: DecisionAnswer) -> float:
    return max(0.0, min(1.0, float(answer.margin)))


def _parse_prediction(
    item: Any,
    *,
    main_answer: DecisionAnswer,
    group_answer: DecisionAnswer | None,
    leaf_answer: DecisionAnswer | None,
    dimension_answers: Mapping[str, DecisionAnswer],
    model: str | None,
    request_id: str | None,
    policy: KevTransactionPolicy,
) -> TransactionClassification:
    row_id = _transaction_row_id(item)
    if not isinstance(main_answer.selected, str):
        raise DecisionInvalidResponse(f"main category is not a string for transaction {row_id}")
    main = canonicalize_taxonomy_choice(main_answer.selected, "main")
    if main not in TAXONOMY or group_answer is None or leaf_answer is None:
        raise DecisionInvalidResponse(f"incomplete taxonomy path for transaction {row_id}")
    group = canonicalize_taxonomy_choice(group_answer.selected, "group", main=main)
    if group not in TAXONOMY[main]:
        raise DecisionInvalidResponse(f"invalid subgroup for transaction {row_id}")
    leaf = canonicalize_taxonomy_choice(leaf_answer.selected, "leaf", main=main, group=group)
    if leaf not in TAXONOMY[main][group]:
        raise DecisionInvalidResponse(f"invalid leaf for transaction {row_id}")
    category = f"{main} → {group} → {leaf}"
    category_answer = leaf_answer
    category_confidence = min(
        _answer_confidence(main_answer),
        _answer_confidence(group_answer),
        _answer_confidence(leaf_answer),
    )
    category_margin = min(
        _answer_margin(main_answer),
        _answer_margin(group_answer),
        _answer_margin(leaf_answer),
    )
    probabilities = {str(key): float(value) for key, value in category_answer.probabilities.items()}
    dimensions: dict[str, bool | None] = {}
    confidences: dict[str, float] = {"category": category_confidence}
    for name in ("recurring", "subscription", "transfer", "enrichment", "review"):
        answer = dimension_answers.get(name)
        if answer is None or not isinstance(answer.selected, bool):
            raise DecisionInvalidResponse(f"missing or invalid {name} for transaction {row_id}")
        dimensions[name] = answer.selected
        confidences[name] = _answer_confidence(answer)

    high_confidence = (
        category_confidence >= policy.auto_accept_threshold
        and category_margin >= policy.minimum_margin
        and all(value >= policy.auto_accept_threshold for value in confidences.values())
        and dimensions["review"] is False
        and dimensions["enrichment"] is False
    )
    review_ready = category_confidence >= policy.review_threshold
    status = "auto" if high_confidence else "review" if review_ready else "abstained"
    return TransactionClassification(
        transaction_row_id=row_id,
        transaction_id=_row_value(item, "transaction_id", None),
        category=category,
        category_probabilities=probabilities,
        category_confidence=category_confidence,
        category_margin=category_margin,
        recurring=dimensions["recurring"],
        subscription=dimensions["subscription"],
        transfer=dimensions["transfer"],
        enrichment_needed=dimensions["enrichment"],
        review_required=dimensions["review"],
        decision_confidences=confidences,
        status=status,
        model=model,
        request_id=request_id,
    )


async def _call_decision(
    provider: Any,
    request: DecisionRequest,
    policy: KevTransactionPolicy,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(policy.retries + 1):
        try:
            return await provider.decide(request)
        except Exception as exc:
            last_error = exc
            if attempt < policy.retries:
                continue
    raise last_error or DecisionUnavailable("Kev returned no response")


async def classify_transaction_batch(
    items: Sequence[Any],
    *,
    conn: sqlite3.Connection | None = None,
    provider: Any | None = None,
    policy: KevTransactionPolicy | None = None,
    user_id: str | None = None,
    persist: bool = True,
) -> TransactionBatchResult:
    """Classify many transactions with a coarse-to-fine taxonomy cascade.

    Each packed batch goes through small choice spaces in order: main branch,
    subgroup, leaf, then inexpensive boolean dimensions. The full taxonomy is
    never sent as one choice question.
    """
    policy = policy or KevTransactionPolicy()
    batch_id = f"kevtx_{uuid4().hex}"
    started = time.perf_counter()
    predictions: list[TransactionClassification] = []
    valid_items: list[Any] = []
    contexts: dict[str, dict[str, Any]] = {}
    seen_row_ids: set[str] = set()
    for item in items or []:
        try:
            row_id = _transaction_row_id(item)
            context = prepare_transaction_context(item, conn=conn, policy=policy)
        except (TypeError, ValueError) as exc:
            predictions.append(TransactionClassification(str(_row_value(item, "id", "invalid")), None, None, status="invalid_response", error=str(exc)))
            continue
        if row_id in seen_row_ids:
            predictions.append(TransactionClassification(row_id, _row_value(item, "transaction_id", None), None, status="invalid_response", error="duplicate transaction row in batch"))
            continue
        seen_row_ids.add(row_id)
        deterministic = _known_merchant_prediction(item, context)
        if deterministic:
            predictions.append(deterministic)
        else:
            valid_items.append(item)
            contexts[row_id] = context

    if valid_items:
        if provider is None:
            provider = DecisionProvider(
                mode=os.getenv("KEV_TX_MODE", os.getenv("KEV_MODE", "local")),
                local_url=os.getenv("KEV_TX_LOCAL_URL") or None,
                cloud_url=os.getenv("KEV_TX_CLOUD_URL") or None,
                cloud_api_key=os.getenv("KEV_TX_CLOUD_API_KEY") or None,
                model=os.getenv("KEV_TX_MODEL", os.getenv("KEV_MODEL", "kev-latest")),
                timeout_seconds=float(os.getenv("KEV_TX_TIMEOUT_SECONDS", os.getenv("KEV_TIMEOUT_SECONDS", "30"))),
            )
        for start in range(0, len(valid_items), policy.batch_size):
            packed = valid_items[start : start + policy.batch_size]
            stage_state = {"transactions": [contexts[_transaction_row_id(item)] for item in packed]}
            stage_round = start // policy.batch_size
            try:
                # Dimensions are collected first so the controller can use
                # them as routing evidence before asking for a taxonomy path.
                dimension_questions = {}
                for item in packed:
                    dimension_questions.update(_dimension_questions(_transaction_row_id(item)))
                dimension_response = await _call_decision(
                    provider,
                    DecisionRequest(
                        state=stage_state,
                        questions=dimension_questions,
                        turn_id=batch_id,
                        round_id=stage_round,
                        decision_kind="transaction_classification_dimensions",
                    ),
                    policy,
                )
                routing_signals: dict[str, dict[str, bool | None]] = {}
                for item in packed:
                    row_id = _transaction_row_id(item)
                    signals = {
                        name: dimension_response.answers.get(row_id + ":" + name).selected
                        if dimension_response.answers.get(row_id + ":" + name) is not None
                        else None
                        for name in ("recurring", "subscription", "transfer")
                    }
                    if any(value not in {True, False} for value in signals.values()):
                        raise DecisionInvalidResponse(f"missing or invalid routing dimensions for transaction {row_id}")
                    routing_signals[row_id] = signals

                main_response = await _call_decision(
                    provider,
                    DecisionRequest(
                        state=stage_state,
                        questions={
                            _transaction_row_id(item) + ":main": _main_question(
                                _transaction_row_id(item) + ":main",
                                routing_signals=routing_signals[_transaction_row_id(item)],
                            )[1]
                            for item in packed
                        },
                        turn_id=batch_id, round_id=stage_round + 1, decision_kind="transaction_main_category",
                    ),
                    policy,
                )
                main_answers = { _transaction_row_id(item): main_response.answers[_transaction_row_id(item) + ":main"] for item in packed }
                for item in packed:
                    row_id = _transaction_row_id(item)
                    answer = main_answers[row_id]
                    if isinstance(answer.selected, str):
                        # Providers upgraded independently may still return a
                        # historical main label; normalize it before building
                        # the next constrained question.
                        normalized = canonicalize_taxonomy_choice(answer.selected, "main")
                        if normalized != answer.selected:
                            main_answers[row_id] = DecisionAnswer(
                                question_id=answer.question_id,
                                type=answer.type,
                                selected=normalized,
                                probabilities=answer.probabilities,
                                confidence=answer.confidence,
                                abstained=answer.abstained,
                                reason=answer.reason,
                                noul_probability=answer.noul_probability,
                            )
                valid_main = [item for item in packed if isinstance(main_answers[_transaction_row_id(item)].selected, str) and main_answers[_transaction_row_id(item)].selected in TAXONOMY]
                group_answers: dict[str, DecisionAnswer] = {}
                group_items = [item for item in valid_main if main_answers[_transaction_row_id(item)].selected != UNCATEGORIZED_PATH]
                if group_items:
                    group_questions = {}
                    for item in group_items:
                        row_id = _transaction_row_id(item)
                        group_questions[row_id + ":group"] = _group_question(
                            row_id + ":group",
                            str(main_answers[row_id].selected),
                            routing_signals=routing_signals[row_id],
                            merchant=str(contexts[row_id].get("merchant") or contexts[row_id].get("normalized_merchant") or ""),
                        )[1]
                    group_response = await _call_decision(provider, DecisionRequest(state=stage_state, questions=group_questions, turn_id=batch_id, round_id=stage_round + 2, decision_kind="transaction_category_group"), policy)
                    group_answers = {_transaction_row_id(item): group_response.answers[_transaction_row_id(item) + ":group"] for item in group_items}
                leaf_items = [item for item in group_items if isinstance(group_answers.get(_transaction_row_id(item)), DecisionAnswer) and group_answers[_transaction_row_id(item)].selected in TAXONOMY[str(main_answers[_transaction_row_id(item)].selected)]]
                leaf_answers: dict[str, DecisionAnswer] = {}
                if leaf_items:
                    leaf_questions = {}
                    for item in leaf_items:
                        row_id = _transaction_row_id(item)
                        main = str(main_answers[row_id].selected)
                        group = str(group_answers[row_id].selected)
                        leaf_questions[row_id + ":leaf"] = _leaf_question(row_id + ":leaf", main, group)[1]
                    leaf_response = await _call_decision(provider, DecisionRequest(state=stage_state, questions=leaf_questions, turn_id=batch_id, round_id=stage_round + 3, decision_kind="transaction_category_leaf"), policy)
                    leaf_answers = {_transaction_row_id(item): leaf_response.answers[_transaction_row_id(item) + ":leaf"] for item in leaf_items}
                for item in packed:
                    row_id = _transaction_row_id(item)
                    main_answer = main_answers[row_id]
                    group_answer = group_answers.get(row_id)
                    leaf_answer = leaf_answers.get(row_id)
                    dimensions = {name: dimension_response.answers.get(row_id + ":" + name) for name in ("recurring", "subscription", "transfer", "enrichment", "review")}
                    try:
                        predictions.append(_parse_prediction(item, main_answer=main_answer, group_answer=group_answer, leaf_answer=leaf_answer, dimension_answers=dimensions, model=getattr(main_response, "model", None), request_id=getattr(main_response, "request_id", None), policy=policy))
                    except (DecisionInvalidResponse, TypeError, ValueError, KeyError) as exc:
                        predictions.append(TransactionClassification(row_id, _row_value(item, "transaction_id", None), None, status="invalid_response", model=getattr(main_response, "model", None), request_id=getattr(main_response, "request_id", None), error=str(exc)))
            except DecisionInvalidResponse as exc:
                for item in packed:
                    predictions.append(TransactionClassification(_transaction_row_id(item), _row_value(item, "transaction_id", None), None, status="invalid_response", error=str(exc)))
            except Exception as exc:
                for item in packed:
                    predictions.append(TransactionClassification(_transaction_row_id(item), _row_value(item, "transaction_id", None), None, status="inference_error", error=f"Kev unavailable: {type(exc).__name__}: {exc}"))

    latency_ms = (time.perf_counter() - started) * 1000.0
    result = TransactionBatchResult(
        batch_id=batch_id,
        predictions=predictions,
        processed=len(predictions),
        kev_classified=sum(prediction.source == "kev" for prediction in predictions),
        deterministic=sum(prediction.status == "deterministic" for prediction in predictions),
        auto_accepted=sum(prediction.status == "auto" for prediction in predictions),
        review=sum(prediction.status == "review" for prediction in predictions),
        failed=sum(prediction.status in {"invalid_response", "inference_error"} for prediction in predictions),
        latency_ms=latency_ms,
        model=getattr(provider, "model", None),
        abstained=sum(prediction.status == "abstained" for prediction in predictions),
    )
    if persist and conn is not None and user_id is not None:
        record_transaction_batch(conn, result, user_id=str(user_id), mode="shadow" if policy.shadow else "enforcing")
    return result


def _local_prediction(
    item: Mapping[str, Any],
    initial: TransactionClassification,
    local: Any,
    *,
    policy: KevTransactionPolicy,
) -> TransactionClassification:
    """Convert the local adjudicator's typed result into the KEV envelope."""
    if getattr(local, "status", "error") == "error":
        status = "inference_error"
    elif getattr(local, "status", "") == "invalid_response":
        status = "invalid_response"
    elif (
        getattr(local, "status", "") == "accepted"
        and getattr(local, "category", None)
        and getattr(local, "confidence", 0.0) >= policy.auto_accept_threshold
        and getattr(local, "margin", 0.0) >= policy.minimum_margin
        and getattr(local, "merchant_identity_confidence", 0.0) >= 0.90
    ):
        status = "auto"
    elif getattr(local, "category", None):
        status = "review"
    else:
        status = "abstained"
    return TransactionClassification(
        transaction_row_id=_transaction_row_id(item),
        transaction_id=_row_value(item, "transaction_id", None),
        category=getattr(local, "category", None) or initial.category,
        category_probabilities={},
        category_confidence=float(getattr(local, "confidence", 0.0) or 0.0),
        category_margin=float(getattr(local, "margin", 0.0) or 0.0),
        recurring=initial.recurring,
        subscription=initial.subscription,
        transfer=initial.transfer,
        enrichment_needed=initial.enrichment_needed,
        review_required=True if status in {"review", "abstained", "invalid_response", "inference_error"} else initial.review_required,
        decision_confidences={"category": float(getattr(local, "confidence", 0.0) or 0.0), "merchant_identity": float(getattr(local, "merchant_identity_confidence", 0.0) or 0.0)},
        source="local_llm",
        status=status,
        model=getattr(local, "model", None),
        error=getattr(local, "error", None) or getattr(local, "reason", None),
    )


def _record_review_requests(
    conn: sqlite3.Connection,
    result: TransactionBatchResult,
    *,
    user_id: str,
    evidence_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Persist an actionable review queue without changing ledger truth."""
    ensure_kev_transaction_schema(conn)
    evidence_by_id = evidence_by_id or {}
    for prediction in result.predictions:
        if prediction.status not in {"review", "abstained", "invalid_response", "inference_error"}:
            continue
        row_id = str(prediction.transaction_row_id)
        evidence = evidence_by_id.get(row_id, {})
        conn.execute(
            """INSERT OR REPLACE INTO transaction_classification_reviews
            (review_id, user_id, transaction_row_id, status, reason,
             identity_status, evidence_json, candidate_category, created_at)
            VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
            (
                f"review_{uuid4().hex}", str(user_id), int(row_id) if row_id.isdigit() else -1,
                prediction.error or "classification requires human review",
                str(evidence.get("identity_status") or "NO_IDENTITY"),
                json.dumps(evidence.get("results") or [], ensure_ascii=False, default=str)[:12000],
                prediction.category, _now(),
            ),
        )
    conn.commit()


async def classify_pending_transactions(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    limit: int = 100,
    policy: KevTransactionPolicy | None = None,
    provider: Any | None = None,
    apply: bool = False,
) -> TransactionBatchResult:
    """Run an explicit, bounded pass over pending rows for one user.

    Plaid ingestion remains non-mutating by default.  This entry point is the
    safe operational hook for shadow/enforcing rollout and is also useful for
    a scheduled worker once evaluation supports enabling it.
    """
    limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        """SELECT id, user_id, transaction_id, merchant, clean_merchant, amount,
                  date, account_used
           FROM transactions
           WHERE user_id = ?
             AND status IN ('Pending', 'Pending Evaluation')
             AND COALESCE(merchant, '') NOT LIKE '%System Balance Sync%'
           ORDER BY id ASC LIMIT ?""",
        (str(user_id), limit),
    ).fetchall()
    items = [
        {"id": row[0], "user_id": row[1], "transaction_id": row[2], "merchant": row[3],
         "clean_merchant": row[4], "amount": row[5], "date": row[6], "account_used": row[7]}
        for row in rows
    ]
    result = await classify_transaction_batch(
        items, conn=conn, provider=provider, policy=policy, user_id=str(user_id), persist=False
    )
    # Uncertain rows get one bounded identity lookup followed by the local
    # adjudicator. Search establishes merchant identity; it does not itself
    # establish category truth.
    if policy.quick_search_enabled and items:
        by_id = result.by_transaction()
        uncertain_items = [
            item for item in items
            if by_id.get(_transaction_row_id(item), None)
            and by_id[_transaction_row_id(item)].status in {"review", "abstained", "invalid_response", "inference_error"}
        ][:policy.quick_search_max_items]
        if uncertain_items:
            try:
                from src.services.search import search_merchant
                from src.services.local_transaction_classifier import classify_with_local_llm
                search_results = await asyncio.gather(
                    *[
                        search_merchant(
                            str(_row_value(item, "merchant", _row_value(item, "clean_merchant", ""))),
                            scrape=False,
                        )
                        for item in uncertain_items
                    ],
                    return_exceptions=True,
                )
                llm_items = []
                evidence_by_id: dict[str, Mapping[str, Any]] = {}
                for item, search_result in zip(uncertain_items, search_results):
                    if not isinstance(search_result, Mapping):
                        search_result = {
                            "identity_status": "NO_IDENTITY",
                            "identity_reasons": [f"search_error:{type(search_result).__name__}"],
                            "results": [],
                        }
                    evidence_by_id[_transaction_row_id(item)] = dict(search_result)
                    initial = result.by_transaction().get(_transaction_row_id(item))
                    if initial is None:
                        continue
                    enriched = dict(item)
                    enriched["identity_status"] = search_result.get("identity_status", "NO_IDENTITY")
                    enriched["search_evidence"] = search_result.get("results") or []
                    enriched["kev_signals"] = {
                        "category": initial.category,
                        "confidence": initial.category_confidence,
                        "margin": initial.category_margin,
                        "recurring": initial.recurring,
                        "subscription": initial.subscription,
                        "transfer": initial.transfer,
                    }
                    llm_items.append(enriched)
                if llm_items:
                    local_results = await classify_with_local_llm(llm_items)
                    local_by_id = {str(pred.transaction_row_id): pred for pred in local_results}
                    merged = result.by_transaction()
                    for item in llm_items:
                        row_id = _transaction_row_id(item)
                        initial = merged.get(row_id)
                        local = local_by_id.get(row_id)
                        if initial is not None and local is not None:
                            merged[row_id] = _local_prediction(item, initial, local, policy=policy)
                    result = TransactionBatchResult(
                        batch_id=result.batch_id,
                        predictions=list(merged.values()),
                        processed=len(merged),
                        kev_classified=sum(p.source == "kev" for p in merged.values()),
                        deterministic=sum(p.status == "deterministic" for p in merged.values()),
                        auto_accepted=sum(p.status == "auto" for p in merged.values()),
                        review=sum(p.status == "review" for p in merged.values()),
                        failed=sum(p.status in {"invalid_response", "inference_error"} for p in merged.values()),
                        latency_ms=result.latency_ms,
                        model=next((p.model for p in local_results if p.model), result.model),
                        abstained=sum(p.status == "abstained" for p in merged.values()),
                    )
                    _record_review_requests(conn, result, user_id=str(user_id), evidence_by_id=evidence_by_id)
            except Exception as enrichment_error:
                print(f" [KEV TX] quick enrichment skipped safely: {type(enrichment_error).__name__}: {enrichment_error}")
    if conn is not None:
        record_transaction_batch(conn, result, user_id=str(user_id), mode="shadow" if policy.shadow else "enforcing")
    if apply:
        apply_auto_predictions(conn, result, user_id=str(user_id), overwrite=False)
    return result


def record_transaction_batch(conn: sqlite3.Connection, result: TransactionBatchResult, *, user_id: str, mode: str) -> None:
    ensure_kev_transaction_schema(conn)
    confidence_values = [prediction.category_confidence for prediction in result.predictions if prediction.category is not None]
    summary = {
        "min": min(confidence_values) if confidence_values else None,
        "mean": sum(confidence_values) / len(confidence_values) if confidence_values else None,
        "max": max(confidence_values) if confidence_values else None,
    }
    conn.execute(
        """INSERT OR REPLACE INTO transaction_classification_batches
        (batch_id, mode, model, requested_count, processed_count, kev_count,
         deterministic_count, auto_count, review_count, abstained_count,
         fallback_count, failed_count, latency_ms, confidence_summary, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (result.batch_id, mode, result.model, result.processed, result.processed,
         result.kev_classified, result.deterministic, result.auto_accepted,
         result.review, result.abstained, result.fallback_count, result.failed, result.latency_ms,
         json.dumps(summary), result.error, _now()),
    )
    for prediction in result.predictions:
        conn.execute(
            """INSERT INTO transaction_classification_predictions
            (batch_id, user_id, transaction_row_id, transaction_id, source, status,
             model, category, category_probabilities, dimensions, confidence, margin,
             error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result.batch_id, str(user_id), int(prediction.transaction_row_id) if prediction.transaction_row_id.isdigit() else -1,
             prediction.transaction_id, prediction.source, prediction.status,
             prediction.model, prediction.category,
             json.dumps(dict(prediction.category_probabilities)),
             json.dumps({"recurring": prediction.recurring, "subscription": prediction.subscription,
                         "transfer": prediction.transfer, "enrichment_needed": prediction.enrichment_needed,
                         "review_required": prediction.review_required,
                         "confidences": dict(prediction.decision_confidences)}),
             prediction.category_confidence, prediction.category_margin, prediction.error, _now()),
        )
    conn.commit()


def apply_auto_predictions(
    conn: sqlite3.Connection,
    result: TransactionBatchResult,
    *,
    user_id: str,
    overwrite: bool = False,
) -> dict[str, int]:
    """Apply only high-confidence classifications to still-pending rows.

    The SQL update intentionally names only classification columns.  Amount,
    date, merchant, account, Plaid IDs, and the original baseline are never
    touched. Human correction history wins over an automatic prediction.
    """
    ensure_kev_transaction_schema(conn)
    counts = Counter(applied=0, skipped=0, locked=0, human_override=0, missing=0)
    applied_ids: list[str] = []
    now = _now()
    for prediction in result.predictions:
        if prediction.status not in {"auto", "deterministic"} or not prediction.category:
            counts["skipped"] += 1
            continue
        row = conn.execute(
            """SELECT id, transaction_id, date, merchant, amount, account_used,
                      category, status, judgment, clean_merchant, is_locked,
                      category_path, category_main, category_group, is_transfer
               FROM transactions WHERE user_id = ? AND id = ?""",
            (str(user_id), int(prediction.transaction_row_id)),
        ).fetchone()
        if not row:
            counts["missing"] += 1
            continue
        if row[10]:
            counts["locked"] += 1
            continue
        if _human_correction_exists(conn, prediction.transaction_row_id):
            counts["human_override"] += 1
            continue
        if not overwrite and row[7] not in {None, "Pending", "Pending Evaluation"}:
            counts["skipped"] += 1
            continue
        parts = category_parts(prediction.category)
        legacy_category = legacy_category_for_path(prediction.category)
        before = {
            "category": row[6], "category_path": row[11], "category_main": row[12],
            "category_group": row[13], "is_transfer": row[14], "status": row[7],
            "judgment": row[8], "clean_merchant": row[9],
        }
        judgment = f"KEV classification ({prediction.category_confidence:.3f} distribution confidence): {prediction.category}"
        conn.execute(
            """UPDATE transactions
               SET category = ?, category_path = ?, category_main = ?,
                   category_group = ?, is_transfer = ?, status = 'Evaluated', judgment = ?
               WHERE user_id = ? AND id = ?""",
            (legacy_category, parts["path"], parts["main"], parts["group"],
             1 if parts["is_transfer"] is True else 0, judgment,
             str(user_id), int(prediction.transaction_row_id)),
        )
        after = dict(
            before, category=legacy_category, category_path=parts["path"],
            category_main=parts["main"], category_group=parts["group"],
            is_transfer=1 if parts["is_transfer"] is True else 0,
            status="Evaluated", judgment=judgment,
        )
        conn.execute(
            """INSERT INTO transaction_correction_log
               (user_id, transaction_row_id, transaction_id, occurred_at, action,
                source, reason, changes, before_state, after_state)
               VALUES (?, ?, ?, ?, 'classify', 'kev_auto', ?, ?, ?, ?)""",
            (str(user_id), int(prediction.transaction_row_id), row[1], now,
             "High-confidence KEV classification; human corrections remain authoritative.",
             json.dumps({"category": prediction.category, "dimensions": {
                 "recurring": prediction.recurring, "subscription": prediction.subscription,
                 "transfer": prediction.transfer, "enrichment_needed": prediction.enrichment_needed}},
                 ensure_ascii=False), json.dumps(before, ensure_ascii=False, default=str),
             json.dumps(after, ensure_ascii=False, default=str)),
        )
        counts["applied"] += 1
        applied_ids.append(prediction.transaction_row_id)
    conn.commit()
    output = dict(counts)
    output["applied_ids"] = applied_ids
    return output


def _label_from_log_row(row: sqlite3.Row | tuple[Any, ...]) -> tuple[str, str] | None:
    # Expected query columns: row id, transaction row id, source, after_state.
    source = str(row[2] or "").lower()
    if source.startswith("kev") or source.startswith("model"):
        return None
    try:
        after = json.loads(row[3] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(after, Mapping):
        return None
    category = after.get("category")
    if not category_is_valid(category):
        return None
    return str(row[1]), str(category)


def load_heldout_labels(conn: sqlite3.Connection, *, user_id: str | None = None, holdout: str = "test", test_ratio: float = 0.2) -> list[dict[str, str]]:
    """Load reproducible labels from corrections, excluding KEV/model labels."""
    where = ""
    params: list[Any] = []
    if user_id is not None:
        where = "WHERE l.user_id = ?"
        params.append(str(user_id))
    rows = conn.execute(
        f"""SELECT l.id, l.transaction_row_id, l.source, l.after_state
            FROM transaction_correction_log l {where}
            ORDER BY l.transaction_row_id, l.id DESC""",
        params,
    ).fetchall()
    latest: dict[str, tuple[str, str]] = {}
    for row in rows:
        label = _label_from_log_row(row)
        if label and label[0] not in latest:
            latest[label[0]] = label
    selected: list[dict[str, str]] = []
    for row_id, category in latest.values():
        digest = hashlib.sha256(row_id.encode()).digest()[0] / 255.0
        is_test = digest < max(0.0, min(1.0, test_ratio))
        if (holdout == "test") == is_test:
            selected.append({"transaction_row_id": row_id, "category": category})
    return selected


def classification_metrics(predictions: Sequence[TransactionClassification], labels: Mapping[str, str], *, thresholds: Sequence[float] = (0.80, 0.90, 0.95)) -> dict[str, Any]:
    """Compute accuracy, per-category metrics, coverage, and ECE."""
    paired = [prediction for prediction in predictions if prediction.transaction_row_id in labels and prediction.category]
    correct = [prediction for prediction in paired if prediction.category == labels[prediction.transaction_row_id]]
    by_category: dict[str, dict[str, int]] = {}
    for category in TRANSACTION_CATEGORIES:
        tp = sum(prediction.category == category and labels[prediction.transaction_row_id] == category for prediction in paired)
        fp = sum(prediction.category == category and labels[prediction.transaction_row_id] != category for prediction in paired)
        fn = sum(prediction.category != category and labels[prediction.transaction_row_id] == category for prediction in paired)
        by_category[category] = {"true_positive": tp, "false_positive": fp, "false_negative": fn}
    threshold_metrics = {}
    for threshold in thresholds:
        covered = [prediction for prediction in paired if prediction.category_confidence >= threshold]
        threshold_metrics[str(threshold)] = {
            "coverage": len(covered) / len(paired) if paired else 0.0,
            "accuracy": sum(prediction.category == labels[prediction.transaction_row_id] for prediction in covered) / len(covered) if covered else 0.0,
            "count": len(covered),
        }
    bins: dict[int, list[TransactionClassification]] = defaultdict(list)
    for prediction in paired:
        bins[min(9, max(0, int(prediction.category_confidence * 10)))].append(prediction)
    ece = 0.0
    for bucket, values in bins.items():
        accuracy = sum(prediction.category == labels[prediction.transaction_row_id] for prediction in values) / len(values)
        confidence = sum(prediction.category_confidence for prediction in values) / len(values)
        ece += len(values) / len(paired) * abs(accuracy - confidence)
    return {
        "labeled": len(paired),
        "accuracy": len(correct) / len(paired) if paired else 0.0,
        "per_category": by_category,
        "coverage_by_threshold": threshold_metrics,
        "expected_calibration_error": ece,
    }


def export_training_jsonl(conn: sqlite3.Connection, path: str, *, user_id: str | None = None, holdout: str = "train") -> int:
    """Export reproducible human-labelled examples; never exports model labels."""
    labels = load_heldout_labels(conn, user_id=user_id, holdout=holdout)
    count = 0
    with open(path, "w", encoding="utf-8") as output:
        for label in labels:
            row = conn.execute(
                """SELECT id, user_id, transaction_id, merchant, clean_merchant,
                          amount, date, account_used
                   FROM transactions WHERE id = ?""",
                (int(label["transaction_row_id"]),),
            ).fetchone()
            if not row:
                continue
            item = {"id": row[0], "user_id": row[1], "transaction_id": row[2], "merchant": row[3], "clean_merchant": row[4], "amount": row[5], "date": row[6], "account_used": row[7]}
            context = prepare_transaction_context(item, conn=conn)
            output.write(json.dumps({"state": {"transaction": context}, "label": label["category"]}, ensure_ascii=False) + "\n")
            count += 1
    return count


__all__ = [
    "KevTransactionPolicy",
    "TransactionBatchResult",
    "TransactionClassification",
    "apply_auto_predictions",
    "classification_metrics",
    "classify_transaction_batch",
    "classify_pending_transactions",
    "ensure_kev_transaction_schema",
    "export_training_jsonl",
    "load_heldout_labels",
    "prepare_transaction_context",
    "record_transaction_batch",
]
