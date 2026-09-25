import json

import pytest

from src.services.local_transaction_classifier import LocalClassification, _parse, _prompt, classify_with_local_llm
from src.services.transaction_taxonomy import TRANSACTION_CATEGORIES


def _item(row_id="1"):
    return {
        "id": row_id,
        "merchant": "Dragon Restaurant",
        "clean_merchant": "Dragon Restaurant",
        "amount": 300.0,
        "identity_status": "STRONG_IDENTITY",
        "search_evidence": [{"title": "Dragon Restaurant", "url": "https://dragon.example"}],
        "allowed_category_paths": list(TRANSACTION_CATEGORIES),
    }


def _response(**overrides):
    value = {
        "transaction_row_id": "1",
        "decision": "accept",
        "category_path": TRANSACTION_CATEGORIES[0],
        "confidence": 0.95,
        "margin": 0.3,
        "merchant_identity_confidence": 0.95,
        "evidence_sufficient": True,
        "source_ids": ["search-1"],
        "reason": "strong identity",
    }
    value.update(overrides)
    return json.dumps({"items": [value]})


def test_prompt_contains_closed_taxonomy_and_abstention_contract():
    system, user = _prompt([_item()])
    assert "abstain" in system
    assert "allowed_category_paths" in user
    assert "category_path" in system


def test_parse_accepts_valid_category():
    result = _parse(_response(), [_item()], "local")
    assert result[0].status == "accepted"
    assert result[0].category == TRANSACTION_CATEGORIES[0]


def test_parse_rejects_unknown_category():
    result = _parse(_response(category_path="Invented Category"), [_item()], "local")
    assert result[0].status == "abstained"
    assert result[0].category is None


def test_parse_rejects_category_outside_allowed_subset():
    item = _item()
    item["allowed_category_paths"] = [TRANSACTION_CATEGORIES[1]]
    result = _parse(_response(), [item], "local")
    assert result[0].status == "abstained"


@pytest.mark.parametrize("decision", ["abstain", "", "unknown"])
def test_parse_non_accept_decision_abstains(decision):
    result = _parse(_response(decision=decision), [_item()], "local")
    assert result[0].status == "abstained"


@pytest.mark.parametrize("evidence", [False, None, 0, ""])
def test_parse_requires_evidence(evidence):
    result = _parse(_response(evidence_sufficient=evidence), [_item()], "local")
    assert result[0].status == "abstained"


@pytest.mark.parametrize("confidence", [-1, 0, 0.5, 2, "bad"])
def test_parse_bounds_confidence(confidence):
    result = _parse(_response(confidence=confidence), [_item()], "local")
    assert 0.0 <= result[0].confidence <= 1.0


def test_parse_missing_item_is_invalid():
    result = _parse(json.dumps({"items": []}), [_item()], "local")
    assert result[0].status == "invalid_response"


def test_parse_invalid_json_is_invalid():
    result = _parse("not json", [_item()], "local")
    assert result[0].status == "invalid_response"


def test_empty_batch_returns_empty():
    import asyncio
    assert asyncio.run(classify_with_local_llm([])) == []


@pytest.mark.asyncio
async def test_http_error_becomes_error(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            raise RuntimeError("offline")

        def json(self):
            return {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("src.services.local_transaction_classifier.httpx.AsyncClient", lambda **kwargs: FakeClient())
    result = await classify_with_local_llm([_item()])
    assert result[0].status == "error"


def test_local_result_contract_has_explicit_source_fields():
    result = _parse(_response(), [_item()], "classifier-local")
    assert result[0].source_ids == ("search-1",)
    assert result[0].merchant_identity_confidence == 0.95
    assert result[0].evidence_sufficient is True
