from src.services.claim_evidence import (
    build_claim_repair_prompt,
    detect_action_claims,
    evaluate_claims,
    safe_claim_fallback,
    successful_capabilities,
)


def test_plain_help_text_has_no_completed_action_claim():
    decision = evaluate_claims("I can help you check that next.", [])
    assert decision.allowed
    assert not decision.claims


def test_claim_without_receipt_is_blocked():
    decision = evaluate_claims("I checked your balance and sent an alert.", [])
    assert not decision.allowed
    assert {claim.capability for claim in decision.unsupported} == {"read", "mutation"}


def test_matching_legacy_trace_supports_claim():
    trace = [
        {"name": "get_current_financial_position", "ok": True},
        {"name": "send_push_alert", "ok": True},
    ]
    decision = evaluate_claims("I checked your balance and sent an alert.", trace)
    assert decision.allowed
    assert not decision.unsupported


def test_failed_partial_and_unknown_receipts_do_not_support_claims():
    trace = [
        {"name": "get_current_financial_position", "ok": True, "status": "partial"},
        {"name": "send_push_alert", "ok": True, "status": "unknown"},
        {"name": "send_push_alert", "ok": False, "status": "failed"},
    ]
    decision = evaluate_claims("I checked your balance and sent an alert.", trace)
    assert not decision.allowed
    assert {claim.capability for claim in decision.unsupported} == {"read", "mutation"}


def test_memory_receipt_is_distinct_from_generic_read_receipt():
    assert successful_capabilities([{"name": "save_memory_claim", "ok": True}]) == {
        "read",
        "memory",
        "mutation",
    }
    assert not evaluate_claims("I saved that to memory.", []).allowed
    assert evaluate_claims(
        "I saved that to memory.", [{"name": "save_memory_claim", "ok": True}]
    ).allowed


def test_repair_prompt_and_fallback_are_deterministic():
    decision = evaluate_claims("I sent the email.", [])
    prompt = build_claim_repair_prompt(decision)
    assert "Do not call tools" in prompt
    assert "I sent the email." not in safe_claim_fallback()


def test_truthful_negated_refusal_is_not_misclassified_as_success_claim():
    decision = evaluate_claims(
        "I cannot claim that I sent the push alert because the tool failed.",
        [{"name": "send_push_alert", "status": "failed", "ok": False, "complete": False}],
    )
    assert decision.allowed
    assert not decision.unsupported
