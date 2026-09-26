from datetime import datetime, timezone

import pytest

from src.agent.task_verifier import MAX_CHECKS, verify_task_contract


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def result(contract, evidence=None, **kwargs):
    return verify_task_contract(contract, evidence or {}, now=NOW, **kwargs)


def test_all_check_kinds_pass_with_matching_evidence():
    contract = {
        "required_receipts": [{
            "check_id": "receipt-check", "receipt_id": "r1", "owner_id": "u1",
            "call_id": "c1", "tool_name": "search_gmail",
        }],
        "freshness_checks": [{
            "check_id": "fresh-check", "evidence_ref": "balance", "max_age_seconds": 60,
        }],
        "arithmetic_checks": [{
            "check_id": "math-check", "left": "0.1", "operation": "+",
            "right": "0.2", "expected": "0.3",
        }],
        "policy_checks": [{
            "check_id": "policy-check", "allowed_tools": ["search_gmail"],
            "actual_tools": ["search_gmail"],
        }],
        "required_deliverables": [{"check_id": "deliverable-check", "evidence_ref": "summary-1"}],
    }
    evidence = {
        "receipts": {"r1": {
            "owner_id": "u1", "call_id": "c1", "tool_name": "search_gmail",
            "status": "confirmed", "ok": True, "complete": True,
        }},
        "freshness": {"balance": "2026-09-25T11:59:30Z"},
        "deliverables": {"summary-1"},
    }

    outcome = result(contract, evidence)

    assert outcome["passed"] is True
    assert [check["kind"] for check in outcome["checks"]] == [
        "receipt", "freshness", "arithmetic", "policy", "deliverable",
    ]
    assert all(check["passed"] and check["reason_code"] == "verified" for check in outcome["checks"])


@pytest.mark.parametrize("receipt,reason", [
    (None, "receipt_missing"),
    ({"owner_id": "other", "call_id": "c", "tool_name": "t", "status": "confirmed", "ok": True, "complete": True}, "receipt_mismatch"),
    ({"owner_id": "u", "call_id": "other-call", "tool_name": "t", "status": "confirmed", "ok": True, "complete": True}, "receipt_mismatch"),
    ({"owner_id": "u", "call_id": "c", "tool_name": "other-tool", "status": "confirmed", "ok": True, "complete": True}, "receipt_mismatch"),
    ({"owner_id": "u", "call_id": "c", "tool_name": "t", "status": "unknown", "ok": True, "complete": True}, "receipt_not_confirmed"),
    ({"owner_id": "u", "call_id": "c", "tool_name": "t", "status": "confirmed", "ok": True, "complete": False}, "receipt_not_confirmed"),
])
def test_receipts_require_owner_call_tool_and_confirmed_status(receipt, reason):
    outcome = result(
        {"required_receipts": [{
            "check_id": "r-check", "receipt_id": "r", "owner_id": "u", "call_id": "c", "tool_name": "t",
        }]},
        {"receipts": {} if receipt is None else {"r": receipt}},
    )
    assert outcome["passed"] is False
    assert outcome["checks"][0]["reason_code"] == reason
    assert outcome["checks"][0]["evidence_refs"] == ["r"]


@pytest.mark.parametrize("timestamp,reason", [
    (None, "invalid_timestamp"),
    ("2026-09-25T11:58:59Z", "stale_evidence"),
    ("2026-09-25T12:00:01Z", "timestamp_in_future"),
    ("2026-09-25T12:00:00", "invalid_timestamp"),
    ("not-a-date", "invalid_timestamp"),
])
def test_freshness_fails_closed_for_missing_stale_future_or_bad_timestamps(timestamp, reason):
    evidence = {"freshness": {} if timestamp is None else {"ref": timestamp}}
    outcome = result({"freshness_checks": [{
        "check_id": "fresh", "evidence_ref": "ref", "max_age_seconds": 60,
    }]}, evidence)
    assert outcome["passed"] is False
    assert outcome["checks"][0]["reason_code"] == reason


def test_naive_now_and_invalid_freshness_limits_fail_closed():
    naive = datetime(2026, 9, 25, 12)
    checks = [{
        "check_id": f"fresh-{age}", "evidence_ref": "ref", "max_age_seconds": age,
    } for age in (0, -1, 10**12)]
    outcome = verify_task_contract(
        {"freshness_checks": checks},
        {"freshness": {"ref": "2026-09-25T11:59:00Z"}},
        now=naive,
    )
    assert outcome["passed"] is False
    assert [check["reason_code"] for check in outcome["checks"]] == [
        "invalid_max_age", "invalid_max_age", "invalid_max_age",
    ]

    outcome = verify_task_contract(
        {"freshness_checks": [{
            "check_id": "fresh", "evidence_ref": "ref", "max_age_seconds": 60,
        }]},
        {"freshness": {"ref": "2026-09-25T11:59:00Z"}},
        now=naive,
    )
    assert outcome["checks"][0]["reason_code"] == "invalid_now"


@pytest.mark.parametrize("check,reason", [
    ({"left": "1", "operation": "+", "right": "2", "expected": "3"}, "invalid_check_id"),
    ({"check_id": "x", "left": "NaN", "operation": "+", "right": "2", "expected": "3"}, "malformed_number"),
    ({"check_id": "x", "left": "1", "operation": "^", "right": "2", "expected": "3"}, "unsupported_operation"),
    ({"check_id": "x", "left": "1", "operation": "/", "right": "0", "expected": "0"}, "division_by_zero"),
    ({"check_id": "x", "left": "1", "operation": "+", "right": "2", "expected": "4"}, "arithmetic_mismatch"),
    ({"check_id": "x", "left": "1", "operation": "+", "right": "2", "expected": "3", "tolerance": "-1"}, "invalid_tolerance"),
])
def test_arithmetic_malformed_inputs_and_failures_are_structured(check, reason):
    outcome = result({"arithmetic_checks": [check]})
    assert outcome["passed"] is False
    assert outcome["checks"][0]["reason_code"] == reason
    assert set(outcome["checks"][0]) == {"check_id", "kind", "passed", "reason_code", "evidence_refs"}


def test_decimal_arithmetic_is_exact_and_tolerance_is_inclusive():
    checks = [
        {"check_id": "sum", "left": "0.1", "operation": "+", "right": "0.2", "expected": "0.3"},
        {"check_id": "div", "left": "1", "operation": "/", "right": "3", "expected": "0.333", "tolerance": "0.00034"},
        {"check_id": "mul", "left": "12.5", "operation": "*", "right": "4", "expected": "50"},
    ]
    outcome = result({"arithmetic_checks": checks})
    assert outcome["passed"] is True
    assert [check["reason_code"] for check in outcome["checks"]] == ["verified"] * 3


def test_policy_requires_actual_tools_to_be_subset_of_allowed_tools():
    outcome = result({"policy_checks": [{
        "check_id": "policy", "allowed_tools": ["read"], "actual_tools": ["read", "write"],
    }]})
    assert outcome["passed"] is False
    assert outcome["checks"][0]["reason_code"] == "disallowed_tool"


def test_model_review_accepts_well_formed_pass_and_rejection():
    passed = result({"model_reviews": [{
        "check_id": "review-pass", "passed": True, "issues": [],
        "evidence_refs": ["artifact-1", "receipt-1"],
    }]})
    assert passed["passed"] is True
    assert passed["checks"][0] == {
        "check_id": "review-pass", "kind": "model_review", "passed": True,
        "reason_code": "verified", "evidence_refs": ["artifact-1", "receipt-1"],
    }

    rejected = result({"model_reviews": [{
        "check_id": "review-rejected", "passed": False,
        "issues": ["The summary omits a requested section."],
    }]})
    assert rejected["passed"] is False
    assert rejected["checks"][0]["reason_code"] == "model_review_rejected"


@pytest.mark.parametrize("review", [
    {"check_id": "missing-passed", "issues": []},
    {"check_id": "truthy-passed", "passed": 1, "issues": []},
    {"check_id": "missing-issues", "passed": True},
    {"check_id": "non-list-issues", "passed": True, "issues": "none"},
    {"check_id": "non-string-issue", "passed": True, "issues": [None]},
    {"check_id": "empty-issue", "passed": False, "issues": [""]},
    {"check_id": "oversized-issue", "passed": False, "issues": ["x" * 257]},
    {"check_id": "too-many-issues", "passed": False, "issues": ["issue"] * 11},
    {"check_id": "non-list-refs", "passed": True, "issues": [], "evidence_refs": "ref"},
    {"check_id": "invalid-ref", "passed": True, "issues": [], "evidence_refs": [""]},
    {"check_id": "too-many-refs", "passed": True, "issues": [], "evidence_refs": [f"r{i}" for i in range(21)]},
])
def test_model_review_malformed_input_fails_closed(review):
    outcome = result({"model_reviews": [review]})
    assert outcome["passed"] is False
    assert outcome["checks"][0]["reason_code"] == "malformed_check"


def test_model_review_accepts_maximum_bounded_issues_and_evidence_refs():
    outcome = result({"model_reviews": [{
        "check_id": "bounded-review", "passed": False,
        "issues": ["x" * 256 for _ in range(10)],
        "evidence_refs": [f"ref-{index}" for index in range(20)],
    }]})
    assert outcome["passed"] is False
    check = outcome["checks"][0]
    assert check["reason_code"] == "model_review_rejected"
    assert len(check["evidence_refs"]) == 20


def test_model_review_does_not_substitute_for_required_deterministic_checks():
    outcome = result({
        "required_check_kinds": ["freshness"],
        "model_reviews": [{
            "check_id": "review", "passed": True, "issues": [],
        }],
    })
    assert outcome["passed"] is False
    assert [(check["kind"], check["reason_code"]) for check in outcome["checks"]] == [
        ("model_review", "verified"),
        ("freshness", "required_check_missing_or_failed"),
    ]


def test_financial_numeric_claims_must_match_trusted_typed_values():
    contract = {"numeric_claim_checks": [{
        "check_id": "financial_numeric_claims",
        "claims": ["80.00", "1000"],
        "allowed_values": ["80", "1000.00"],
        "evidence_refs": ["receipt-1"],
    }]}
    assert result(contract)["passed"] is True

    contract["numeric_claim_checks"][0]["claims"] = ["999.00"]
    outcome = result(contract)
    assert outcome["passed"] is False
    assert outcome["checks"][0]["reason_code"] == "unsupported_numeric_claim"

    wrong_label = result({"numeric_claim_checks": [{
        "check_id": "labelled_claim",
        "claims": ["100"],
        "allowed_values": ["100", "80"],
        "field_claims": [{"field": "net_cash", "value": "100"}],
        "allowed_by_field": {"net_cash": ["80"]},
    }]})
    assert wrong_label["passed"] is False
    assert wrong_label["checks"][0]["reason_code"] == "unsupported_numeric_claim"


@pytest.mark.parametrize(("flag", "reason"), [
    ("malformed_currency_claims", "malformed_number"),
    ("unsupported_currency_claims", "unsupported_currency_claim"),
])
def test_numeric_claim_currency_scanner_failures_are_structured(flag, reason):
    outcome = result({"numeric_claim_checks": [{
        "check_id": "currency-claims", "claims": [], "allowed_values": [], flag: True,
        "evidence_refs": ["receipt-1"],
    }]})
    assert outcome["passed"] is False
    assert outcome["checks"][0]["reason_code"] == reason
    assert outcome["checks"][0]["evidence_refs"] == ["receipt-1"]


def test_required_deliverable_must_be_present_in_bounded_evidence_set():
    contract = {"required_deliverables": [{"check_id": "d", "evidence_ref": "artifact"}]}
    assert result(contract, {"deliverables": ["other"]})["checks"][0]["reason_code"] == "deliverable_missing"
    assert result(contract, {"deliverables": "artifact"})["checks"][0]["reason_code"] == "malformed_deliverables"


def test_artifact_check_binds_owner_path_receipt_bytes_and_delivery_message():
    contract = {"artifact_checks": [{
        "check_id": "requested_artifact_validation", "receipt_id": "r1",
        "owner_id": "u1", "call_id": "c1", "expected_path": "report.csv",
    }]}
    evidence = {"artifacts": {"r1": {
        "status": "sent", "receipt_id": "r1", "owner_id": "u1", "call_id": "c1",
        "path": "reports/report.csv", "filename": "report.csv",
        "sha256": "a" * 64, "byte_size": 128, "message_id": "discord-123",
    }}}
    assert result(contract, evidence)["passed"] is True

    exact_directory_contract = {"artifact_checks": [{
        **contract["artifact_checks"][0], "expected_path": "reports/report.csv",
    }]}
    assert result(exact_directory_contract, evidence)["passed"] is True
    nested_mismatch = {"artifacts": {"r1": {
        **evidence["artifacts"]["r1"], "path": "other/report.csv",
    }}}
    assert result(exact_directory_contract, nested_mismatch)["passed"] is False

    for field, value in (
        ("owner_id", "u2"), ("path", "reports/other.csv"),
        ("sha256", "not-a-hash"), ("byte_size", 0),
        ("message_id", ""), ("status", "unknown"),
    ):
        mismatched = {"artifacts": {"r1": {**evidence["artifacts"]["r1"], field: value}}}
        outcome = result(contract, mismatched)
        assert outcome["passed"] is False, field


def test_required_check_kinds_and_ids_fail_closed_when_typed_proofs_are_absent():
    contract = {
        "required_check_kinds": ["freshness", "arithmetic"],
        "required_check_ids": ["requested_artifact"],
        "required_deliverables": [{
            "check_id": "final_response", "evidence_ref": "response",
        }],
    }
    missing = result(contract, {"deliverables": ["response"]})
    assert missing["passed"] is False
    assert {(item["check_id"], item["reason_code"]) for item in missing["checks"]} >= {
        ("required_freshness", "required_check_missing_or_failed"),
        ("required_arithmetic", "required_check_missing_or_failed"),
        ("requested_artifact", "required_check_missing_or_failed"),
    }


def test_required_check_kinds_and_ids_pass_when_exact_declared_checks_are_supplied():
    contract = {
        "required_check_kinds": ["freshness", "arithmetic"],
        "required_check_ids": ["requested_artifact"],
        "freshness_checks": [{
            "check_id": "fresh", "evidence_ref": "source", "max_age_seconds": 60,
        }],
        "arithmetic_checks": [{
            "check_id": "math", "left": "1", "operation": "+", "right": "2", "expected": "3",
        }],
        "required_deliverables": [
            {"check_id": "final_response", "evidence_ref": "response"},
            {"check_id": "requested_artifact", "evidence_ref": "artifact-path"},
        ],
    }
    passed = result(contract, {
        "freshness": {"source": "2026-09-25T11:59:30Z"},
        "deliverables": ["response", "artifact-path"],
    })
    assert passed["passed"] is True
    assert all(check["passed"] for check in passed["checks"])
    assert {check["check_id"] for check in passed["checks"]} >= {
        "fresh", "math", "final_response", "requested_artifact",
    }


def test_required_check_kind_and_id_do_not_accept_a_failing_declared_check():
    outcome = result({
        "required_check_kinds": ["freshness"],
        "required_check_ids": ["requested_artifact"],
        "freshness_checks": [{
            "check_id": "fresh", "evidence_ref": "source", "max_age_seconds": 60,
        }],
        "required_deliverables": [{
            "check_id": "requested_artifact", "evidence_ref": "artifact-path",
        }],
    }, {
        "freshness": {"source": "2026-09-25T11:58:00Z"},
        "deliverables": [],
    })

    assert outcome["passed"] is False
    assert [(check["check_id"], check["reason_code"]) for check in outcome["checks"]] == [
        ("fresh", "stale_evidence"),
        ("requested_artifact", "deliverable_missing"),
    ]


def test_contract_and_evidence_size_limits_fail_closed():
    assert result({})["checks"][0]["reason_code"] == "no_checks"
    too_many = {"policy_checks": [{
        "check_id": f"c{i}", "allowed_tools": [], "actual_tools": [],
    } for i in range(MAX_CHECKS + 1)]}
    result_overflow = result(too_many)
    assert result_overflow == {
        "passed": False,
        "checks": [{
            "check_id": "contract_limit", "kind": "contract", "passed": False,
            "reason_code": "too_many_checks", "evidence_refs": [],
        }],
    }

    oversized_map = {str(i): "2026-09-25T11:59:00Z" for i in range(1001)}
    stale = result({"freshness_checks": [{
        "check_id": "fresh", "evidence_ref": "0", "max_age_seconds": 60,
    }]}, {"freshness": oversized_map})
    assert stale["checks"][0]["reason_code"] == "invalid_timestamp"


def test_invalid_top_level_and_duplicate_check_ids_fail_closed_without_raw_values():
    assert verify_task_contract([], {})["checks"][0]["reason_code"] == "invalid_input"
    outcome = result({"required_deliverables": [
        {"check_id": "same", "evidence_ref": "secret-reference"},
        {"check_id": "same", "evidence_ref": "another-secret"},
    ]}, {"deliverables": []})
    assert outcome["checks"][1]["reason_code"] == "duplicate_check_id"
    assert len({check["check_id"] for check in outcome["checks"]}) == len(outcome["checks"])
    assert outcome["checks"][0]["reason_code"] == "deliverable_missing"
    assert all("secret" not in check["reason_code"] for check in outcome["checks"])


def test_verifier_replaces_event_payloads_that_exceed_persistence_bounds():
    too_many_refs = result({"arithmetic_checks": [{
        "check_id": f"math-{index}", "left": "1", "operation": "+",
        "right": "1", "expected": "2",
        "evidence_refs": [f"r{index}-{ref}" for ref in range(20)],
    } for index in range(6)]})
    assert too_many_refs["passed"] is False
    assert too_many_refs["checks"][0]["reason_code"] == "verification_result_exceeds_persistence_limits"

    huge_event = result({"required_deliverables": [{
        "check_id": "c" * (256 - len(str(index))) + str(index), "evidence_ref": "ref",
    } for index in range(100)]}, {"deliverables": ["ref"]})
    assert huge_event["passed"] is False
    assert huge_event["checks"][0]["reason_code"] == "verification_result_exceeds_persistence_limits"
