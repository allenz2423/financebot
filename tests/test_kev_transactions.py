import asyncio
import json
import sqlite3

import pytest

from src.services.kev_decision import DecisionAnswer, DecisionResponse
from src.services.kev_transactions import (
    KevTransactionPolicy,
    TransactionClassification,
    TransactionBatchResult,
    apply_auto_predictions,
    classify_pending_transactions,
    classify_transaction_batch,
    classification_metrics,
    ensure_kev_transaction_schema,
    prepare_transaction_context,
)
from src.services.transaction_taxonomy import TRANSACTION_CATEGORIES
from src.services.transaction_taxonomy import UNCATEGORIZED_PATH, category_parts, category_is_valid, legacy_category_for_path


def _item(row_id=1, **extra):
    value = {
        "id": row_id,
        "transaction_id": f"plaid-{row_id}",
        "user_id": "fake-kev-user",
        "merchant": "Example Market #12",
        "clean_merchant": "Example Market",
        "amount": 42.50,
        "date": "2026-09-22",
        "account_used": "checking",
    }
    value.update(extra)
    return value


def test_hierarchical_taxonomy_validates_transfer_semantics():
    transfer = "Transfers & Non-Expense Movements (Neutral) → Transfers → Credit Card Payments (Checking → Credit Card)"
    expense = "Essential / Fixed Living Expenses (Needs) → Groceries & Household → Food & Groceries"
    assert category_is_valid(transfer)
    assert category_parts(transfer)["is_transfer"] is True
    assert category_parts(expense)["is_transfer"] is False
    assert legacy_category_for_path(transfer) == "Credit Card Bill Payment"
    assert legacy_category_for_path(expense) == "Groceries"


class FakeProvider:
    model = "fake-kev-4b"

    def __init__(self, confidence=0.97, category="Groceries", fail=False):
        self.confidence = confidence
        self.category = category
        self.fail = fail
        self.calls = []

    async def decide(self, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("synthetic model failure")
        answers = {}
        for question_id, question in request.questions.items():
            if question.type == "choice":
                if question_id.endswith(":main"):
                    selected = "Essential / Fixed Living Expenses (Needs)"
                elif question_id.endswith(":group"):
                    selected = "Groceries & Household"
                elif question_id.endswith(":leaf"):
                    selected = "Food & Groceries"
                else:
                    selected = next(iter(question.criteria))
                options = list(question.criteria)
                remainder = [name for name in options if name != selected]
                probabilities = {name: 0.0 for name in options}
                probabilities[selected] = self.confidence
                if remainder:
                    probabilities[remainder[0]] = round(1.0 - self.confidence, 5)
                answers[question_id] = DecisionAnswer(
                    question_id, "choice", selected,
                    probabilities=probabilities,
                    confidence=self.confidence,
                )
            else:
                answers[question_id] = DecisionAnswer(
                    question_id, "noul", False,
                    confidence=self.confidence,
                    noul_probability=1.0 - self.confidence,
                )
        return DecisionResponse("kev", "local", self.model, "fake-request", answers, 1.0)


def test_context_is_bounded_and_does_not_include_ledger_label(monkeypatch):
    monkeypatch.setattr(
        "src.services.merchants.resolve_canonical_merchant",
        lambda raw, conn=None: {
            "canonical_name": "Example Market",
            "category": None,
            "confidence": "none",
            "matched_by": "heuristic_cleaner",
        },
    )
    context = prepare_transaction_context(
        _item(original_description="a" * 1000, category="Should never be copied"),
        policy=KevTransactionPolicy(max_context_chars=500, max_history_rows=0),
    )
    assert len(json.dumps(context)) <= 900
    assert "Should never be copied" not in json.dumps(context)
    assert context["row_id"] == "1"


def test_emoji_registry_category_is_normalized_to_deterministic_taxonomy(monkeypatch):
    monkeypatch.setattr(
        "src.services.merchants.resolve_canonical_merchant",
        lambda raw, conn=None: {
            "canonical_name": raw,
            "category": "Digital Services 💬",
            "confidence": "medium",
            "source": "user_alias",
            "matched_by": "registry_exact",
        },
    )
    item = _item(1, merchant="AplPay WE ARE SISTERBROOKLYN", clean_merchant="WE ARE SISTER")
    result = asyncio.run(classify_transaction_batch([item], provider=FakeProvider(fail=True), persist=False))
    prediction = result.predictions[0]
    assert prediction.source == "known_merchant"
    assert prediction.status == "deterministic"
    assert prediction.category.endswith("Software, Cloud Storage & SaaS Apps")


def test_batch_packs_transactions_into_one_typed_request(monkeypatch):
    monkeypatch.setattr(
        "src.services.merchants.resolve_canonical_merchant",
        lambda raw, conn=None: {"canonical_name": raw, "category": None, "confidence": "none", "matched_by": "none"},
    )
    provider = FakeProvider()
    result = asyncio.run(
        classify_transaction_batch(
            [_item(1), _item(2)],
            provider=provider,
            policy=KevTransactionPolicy(batch_size=8),
            persist=False,
        )
    )
    assert len(provider.calls) == 4
    assert [len(call.questions) for call in provider.calls] == [10, 2, 2, 2]
    assert {p.transaction_row_id for p in result.predictions} == {"1", "2"}
    assert result.auto_accepted == 2
    main_call = provider.calls[1]
    assert all(UNCATEGORIZED_PATH not in question.criteria for question in main_call.questions.values())


def test_abstention_is_distinct_from_inference_failure(monkeypatch):
    monkeypatch.setattr(
        "src.services.merchants.resolve_canonical_merchant",
        lambda raw, conn=None: {"canonical_name": raw, "category": None, "confidence": "none", "matched_by": "none"},
    )
    result = asyncio.run(
        classify_transaction_batch(
            [_item()], provider=FakeProvider(confidence=0.60),
            policy=KevTransactionPolicy(batch_size=1), persist=False,
        )
    )
    assert result.predictions[0].status == "abstained"
    assert result.failed == 0
    assert result.abstained == 1


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.97, "auto"), (0.84, "review"), (0.60, "abstained")],
)
def test_confidence_policy_is_conservative(monkeypatch, confidence, expected):
    monkeypatch.setattr(
        "src.services.merchants.resolve_canonical_merchant",
        lambda raw, conn=None: {"canonical_name": raw, "category": None, "confidence": "none", "matched_by": "none"},
    )
    result = asyncio.run(
        classify_transaction_batch(
            [_item()], provider=FakeProvider(confidence=confidence),
            policy=KevTransactionPolicy(batch_size=1), persist=False,
        )
    )
    assert result.predictions[0].status == expected


def test_model_failure_marks_each_transaction_failed(monkeypatch):
    monkeypatch.setattr(
        "src.services.merchants.resolve_canonical_merchant",
        lambda raw, conn=None: {"canonical_name": raw, "category": None, "confidence": "none", "matched_by": "none"},
    )
    result = asyncio.run(
        classify_transaction_batch(
            [_item(1), _item(2)], provider=FakeProvider(fail=True),
            policy=KevTransactionPolicy(batch_size=8), persist=False,
        )
    )
    assert result.failed == 2
    assert all(prediction.category is None for prediction in result.predictions)


def test_duplicate_rows_are_not_sent_twice(monkeypatch):
    monkeypatch.setattr(
        "src.services.merchants.resolve_canonical_merchant",
        lambda raw, conn=None: {"canonical_name": raw, "category": None, "confidence": "none", "matched_by": "none"},
    )
    provider = FakeProvider()
    result = asyncio.run(
        classify_transaction_batch(
            [_item(1), _item(1)], provider=provider,
            policy=KevTransactionPolicy(batch_size=8, retries=0), persist=False,
        )
    )
    assert len(provider.calls) == 4
    assert result.failed == 1
    assert result.auto_accepted == 1


def test_uncertain_pending_transaction_gets_one_quick_search_retry(monkeypatch):
    db = _ledger_db()
    calls = []

    async def fake_search(query, *, scrape=True):
        calls.append((query, scrape))
        return {"text": "Example Market is a grocery retailer and food market."}

    async def fake_local(items):
        from src.services.local_transaction_classifier import LocalClassification
        return [LocalClassification(str(items[0]["id"]), None, "abstained", reason="test abstention", model="fake-local")]

    monkeypatch.setattr("src.services.search.search_merchant", fake_search)
    monkeypatch.setattr("src.services.local_transaction_classifier.classify_with_local_llm", fake_local)
    result = asyncio.run(
        classify_pending_transactions(
            db,
            user_id="fake-kev-user",
            limit=1,
            provider=FakeProvider(confidence=0.84),
            policy=KevTransactionPolicy(batch_size=1, retries=0, quick_search_enabled=True),
            apply=False,
        )
    )
    assert calls == [("Market", False)]
    assert result.processed == 1
    assert db.execute("SELECT category FROM transactions WHERE id=1").fetchone()[0] is None


def test_uncertain_pending_transaction_routes_to_local_adjudicator_and_review(monkeypatch):
    db = _ledger_db()

    async def fake_search(query, *, scrape=True):
        return {
            "identity_status": "NO_IDENTITY",
            "results": [],
            "text": "[relevance: none]",
        }

    async def fake_local(items):
        from src.services.local_transaction_classifier import LocalClassification
        return [LocalClassification(str(items[0]["id"]), None, "abstained", reason="identity insufficient", model="fake-local")]

    monkeypatch.setattr("src.services.search.search_merchant", fake_search)
    monkeypatch.setattr("src.services.local_transaction_classifier.classify_with_local_llm", fake_local)
    result = asyncio.run(
        classify_pending_transactions(
            db,
            user_id="fake-kev-user",
            limit=1,
            provider=FakeProvider(confidence=0.84),
            policy=KevTransactionPolicy(batch_size=1, retries=0, quick_search_enabled=True),
            apply=False,
        )
    )
    assert result.predictions[0].source == "local_llm"
    assert result.predictions[0].status == "abstained"
    assert db.execute("SELECT COUNT(*) FROM transaction_classification_reviews").fetchone()[0] == 1
    assert db.execute("SELECT category, status FROM transactions WHERE id=1").fetchone() == (None, "Pending Evaluation")


def test_empty_and_invalid_inputs_are_safe():
    result = asyncio.run(classify_transaction_batch([], persist=False))
    assert result.predictions == []
    invalid = asyncio.run(classify_transaction_batch([{"merchant": "no id"}], persist=False))
    assert invalid.failed == 1


def _ledger_db():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, user_id TEXT, transaction_id TEXT,
            date TEXT, merchant TEXT, clean_merchant TEXT, amount REAL,
            account_used TEXT, category TEXT, status TEXT, judgment TEXT,
            is_locked INTEGER DEFAULT 0
        );
        CREATE TABLE transaction_correction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
            transaction_row_id INTEGER, transaction_id TEXT, occurred_at TEXT,
            action TEXT, source TEXT, reason TEXT, changes TEXT,
            before_state TEXT, after_state TEXT
        );
        """
    )
    ensure_kev_transaction_schema(db)
    db.execute(
        """INSERT INTO transactions
        (id, user_id, transaction_id, date, merchant, clean_merchant, amount,
         account_used, category, status, judgment, is_locked)
        VALUES (1, 'fake-kev-user', 'plaid-1', '2026-09-22', 'Market',
                'Market', 42.5, 'checking', NULL, 'Pending Evaluation', NULL, 0)"""
    )
    db.commit()
    return db


def test_apply_changes_only_classification_fields_and_audits():
    db = _ledger_db()
    result = TransactionBatchResult(
        "b1", [TransactionClassification("1", "plaid-1", "Groceries", {"Groceries": 0.99}, 0.99, 0.99, status="auto")],
        processed=1, kev_classified=1, deterministic=0, auto_accepted=1,
        review=0, failed=0, latency_ms=1.0, model="fake",
    )
    counts = apply_auto_predictions(db, result, user_id="fake-kev-user")
    row = db.execute("SELECT transaction_id, date, merchant, amount, category, status FROM transactions WHERE id=1").fetchone()
    assert counts["applied"] == 1
    assert row == ("plaid-1", "2026-09-22", "Market", 42.5, "Groceries", "Evaluated")
    assert db.execute("SELECT source FROM transaction_correction_log WHERE transaction_row_id=1").fetchone()[0] == "kev_auto"


def test_human_correction_overrides_prediction():
    db = _ledger_db()
    db.execute(
        "INSERT INTO transaction_correction_log (user_id, transaction_row_id, source, after_state) VALUES ('fake-kev-user', 1, 'user', ?)" ,
        (json.dumps({"category": "Shopping"}),),
    )
    result = TransactionBatchResult(
        "b2", [TransactionClassification("1", "plaid-1", "Groceries", {"Groceries": 0.99}, 0.99, 0.99, status="auto")],
        processed=1, kev_classified=1, deterministic=0, auto_accepted=1,
        review=0, failed=0, latency_ms=1.0, model="fake",
    )
    counts = apply_auto_predictions(db, result, user_id="fake-kev-user")
    assert counts["human_override"] == 1
    assert db.execute("SELECT category FROM transactions WHERE id=1").fetchone()[0] is None


def test_metrics_include_coverage_and_calibration():
    predictions = [
        TransactionClassification("1", None, "Groceries", {}, .99, .9, status="auto"),
        TransactionClassification("2", None, "Shopping", {}, .81, .2, status="review"),
    ]
    metrics = classification_metrics(predictions, {"1": "Groceries", "2": "Dining Out"})
    assert metrics["labeled"] == 2
    assert "0.95" in metrics["coverage_by_threshold"]
    assert 0 <= metrics["expected_calibration_error"] <= 1
