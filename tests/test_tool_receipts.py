import sqlite3

import pytest

from src.services.tool_receipts import ReceiptLifecycleError, ReceiptStore


def test_receipt_lifecycle_is_durable_and_correlated():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)

    prepared = store.prepare(
        receipt_id="receipt-1",
        call_id="call-1",
        user_id="user-1",
        turn_id="turn-1",
        round_id=2,
        tool_name="send_push_alert",
        origin="native",
        arguments={"message": "done"},
    )
    assert prepared.status == "prepared"
    assert prepared.ok is None

    started = store.start("receipt-1")
    assert started.status == "started"

    confirmed = store.finish(
        "receipt-1",
        status="confirmed",
        ok=True,
        complete=True,
        result_summary="alert accepted",
    )
    assert confirmed.status == "confirmed"
    assert confirmed.ok is True
    assert confirmed.complete is True
    assert store.get("receipt-1").call_id == "call-1"


def test_unknown_is_terminal_and_cannot_be_marked_successful_later():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)
    store.prepare(
        receipt_id="receipt-2",
        call_id="call-2",
        user_id="user-1",
        turn_id="turn-1",
        round_id=1,
        tool_name="send_push_alert",
        origin="native",
        arguments={},
    )
    store.start("receipt-2")
    store.finish(
        "receipt-2",
        status="unknown",
        ok=False,
        complete=False,
        error="history append failed after dispatch",
    )
    with pytest.raises(ReceiptLifecycleError, match="invalid receipt transition"):
        store.finish("receipt-2", status="confirmed", ok=True, complete=True)


def test_turn_observability_correlates_round_receipt_and_claim_records():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)
    store.record_provider_round({
        "turn_id": "turn-3",
        "provider": "openai",
        "requested_model": "model-a",
        "route_profile": "primary",
        "provider_order": ["ProviderA"],
        "finish_reason": "tool_calls",
        "native_tool_calls": 1,
        "tools_offered": 3,
        "text_chars": 0,
        "latency_ms": 42,
    })
    store.record_claim_decision(
        turn_id="turn-3",
        allowed=False,
        claims=["I sent the alert"],
        unsupported=["I sent the alert"],
    )
    view = store.observability_for_turn("turn-3")
    assert view["provider_rounds"][0]["finish_reason"] == "tool_calls"
    assert view["claim_decisions"][0]["unsupported"] == ["I sent the alert"]


def test_turn_observability_includes_router_authorization():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)
    store.record_router_decision({
        "turn_id": "turn-4",
        "round_id": 1,
        "mode": "ordinary",
        "decision": "authorized",
        "reason": "proposal_topk",
        "allowed_names": {"search_web", "end_turn"},
        "candidate_names": {"search_web"},
        "top1_score": 0.91,
        "latency_ms": 3.5,
    })
    view = store.observability_for_turn("turn-4")
    assert view["router_decisions"][0]["decision"] == "authorized"
    assert view["router_decisions"][0]["allowed_names"] == ["end_turn", "search_web"]
