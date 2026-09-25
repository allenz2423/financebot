"""Prompt-level invariants for the failure modes that motivated the review.

These tests deliberately exercise the final evidence boundary instead of
trusting the model's prose. The local-model test is separate because it is
network/model dependent; this battery stays fast and deterministic.
"""

import pytest

from src.services.claim_evidence import evaluate_claims
from src.services.tool_results import ToolResultEnvelope
from src.services.tool_router import authorize_live_proposal


@pytest.mark.parametrize(
    ("text", "trace", "allowed"),
    [
        ("I sent the alert successfully.", [], False),
        ("I sent the alert successfully.", [{"name": "send_push_alert", "ok": False}], False),
        ("I sent the alert successfully.", [{"name": "send_push_alert", "status": "unknown", "complete": False}], False),
        ("I sent the alert successfully.", [{"name": "send_push_alert", "status": "confirmed", "ok": True, "complete": True}], True),
        ("I searched your email.", [{"name": "search_gmail", "status": "confirmed", "ok": True, "complete": True}], True),
        ("I deleted the saved claim.", [{"name": "delete_memory", "status": "failed", "ok": False, "complete": False}], False),
    ],
)
def test_final_claim_boundary_catches_behavioral_failures(text, trace, allowed):
    assert evaluate_claims(text, trace).allowed is allowed


def test_failed_typed_result_cannot_become_a_success_claim():
    envelope = ToolResultEnvelope(
        schema_version=1,
        tool_name="send_push_alert",
        call_id="delilah_fallback_test",
        receipt_id="receipt-test",
        ok=False,
        complete=False,
        status="failed",
        summary="request rejected",
        error="provider rejected request",
        side_effect="mutate",
    )
    decision = evaluate_claims(
        "The alert was sent.",
        [envelope.trace_fields()],
    )
    assert decision.allowed is False


def test_live_router_catches_wrong_route_without_expanding_authority():
    decision = authorize_live_proposal(
        {
            "raw_topk": [{"name": "search_gmail", "score": 1.0}],
            "read_topk": ["search_gmail"],
            "write_topk": [],
            "domain_topk": ["search_gmail"],
        },
        offered_names={"search_gmail", "send_push_alert", "end_turn"},
    )
    assert "search_gmail" in decision["allowed_names"]
    assert "send_push_alert" not in decision["allowed_names"]
    assert "delete_everything" not in decision["allowed_names"]
