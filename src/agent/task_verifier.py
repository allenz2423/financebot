"""Pure, fail-closed deterministic checks for task completion contracts.

This module deliberately has no persistence, network, or model dependencies.
Contract and evidence values are treated as untrusted input; failures expose
stable reason codes, never exception messages or evidence values.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Mapping


MAX_CHECKS = 100
MAX_STRING_LENGTH = 256
MAX_NUMBER_LENGTH = 128
MAX_EVIDENCE_ITEMS = 1000
MAX_FRESHNESS_SECONDS = 10 * 365 * 24 * 60 * 60

_CHECK_GROUPS = (
    ("required_receipts", "receipt"),
    ("freshness_checks", "freshness"),
    ("arithmetic_checks", "arithmetic"),
    ("policy_checks", "policy"),
    ("required_deliverables", "deliverable"),
    ("artifact_checks", "artifact"),
    ("numeric_claim_checks", "numeric_claim"),
    ("model_reviews", "model_review"),
)
_SUPPORTED_REQUIRED_KINDS = {kind for _, kind in _CHECK_GROUPS}
MAX_PERSISTED_EVIDENCE_REFS = 100
MAX_PERSISTED_EVENT_BYTES = 32_768


def _valid_string(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_STRING_LENGTH and value.strip() == value


def _result(
    check_id: str, kind: str, passed: bool, reason_code: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "kind": kind,
        "passed": passed,
        "reason_code": reason_code,
        "evidence_refs": evidence_refs or [],
    }


def _invalid_check_id(kind: str, index: int) -> str:
    return f"invalid_{kind}_{index}"


def _persistence_safe_outcome(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Make verifier output satisfy SessionStore's event limits and ID rules."""
    seen: set[str] = set()
    unique_checks: list[dict[str, Any]] = []
    for index, raw in enumerate(checks):
        check = dict(raw)
        check_id = check.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            check_id = f"invalid_check_{index}"
        candidate = check_id
        if candidate in seen:
            candidate = f"duplicate_check_{index}"
        suffix = 1
        while candidate in seen:
            candidate = f"duplicate_check_{index}_{suffix}"
            suffix += 1
        check["check_id"] = candidate
        seen.add(candidate)
        unique_checks.append(check)

    evidence_ref_count = sum(
        len(check.get("evidence_refs", []))
        for check in unique_checks
        if isinstance(check.get("evidence_refs", []), list)
    )
    compact = [{
        "check_id": check["check_id"],
        "reason_code": check["reason_code"],
        "evidence_refs": check.get("evidence_refs", []),
    } for check in unique_checks]
    passed = all(bool(check.get("passed")) for check in unique_checks)
    payload = {
        "passed": passed,
        "checks": compact,
        "check_ids": [check["check_id"] for check in compact],
        "failed_check_ids": [
            check["check_id"] for check in unique_checks if not check.get("passed")
        ],
        "reason_codes": [check["reason_code"] for check in compact],
        "evidence_refs": [
            ref for check in compact for ref in check["evidence_refs"]
        ],
    }
    try:
        payload_size = len(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
    except (TypeError, ValueError):
        payload_size = MAX_PERSISTED_EVENT_BYTES + 1

    if (
        len(unique_checks) > MAX_CHECKS
        or evidence_ref_count > MAX_PERSISTED_EVIDENCE_REFS
        or payload_size > MAX_PERSISTED_EVENT_BYTES
    ):
        failure = _result(
            "verification_result_limit", "contract", False,
            "verification_result_exceeds_persistence_limits",
        )
        return {"passed": False, "checks": [failure]}
    return {"passed": passed, "checks": unique_checks}


def _parse_decimal(value: Any) -> Fraction | None:
    """Parse a bounded decimal string into an exact rational value."""
    if not isinstance(value, str) or not value or len(value) > MAX_NUMBER_LENGTH:
        return None
    # Restrict the grammar to ordinary base-10 notation with an optional
    # exponent. This avoids accepting whitespace, NaN, Infinity, and booleans.
    import re

    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value) is None:
        return None
    try:
        from decimal import Decimal, InvalidOperation

        number = Decimal(value)
        if not number.is_finite() or abs(number.as_tuple().exponent) > 1000:
            return None
        return Fraction(number)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_evidence_map(evidence: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = evidence.get(key, {})
    if not isinstance(value, Mapping) or len(value) > MAX_EVIDENCE_ITEMS:
        return {}
    return value


def _arithmetic_check(item: Mapping[str, Any], check_id: str) -> dict[str, Any]:
    fields = ("left", "right", "expected")
    values = [_parse_decimal(item.get(field)) for field in fields]
    if any(value is None for value in values):
        return _result(check_id, "arithmetic", False, "malformed_number")
    left, right, expected = values
    operation = item.get("operation")
    try:
        if operation == "+":
            actual = left + right
        elif operation == "-":
            actual = left - right
        elif operation == "*":
            actual = left * right
        elif operation == "/":
            if right == 0:
                return _result(check_id, "arithmetic", False, "division_by_zero")
            actual = left / right
        else:
            return _result(check_id, "arithmetic", False, "unsupported_operation")
    except (ArithmeticError, ValueError, OverflowError):
        return _result(check_id, "arithmetic", False, "arithmetic_error")

    tolerance_value = _parse_decimal(item.get("tolerance", "0"))
    if tolerance_value is None or tolerance_value < 0:
        return _result(check_id, "arithmetic", False, "invalid_tolerance")
    if abs(actual - expected) > tolerance_value:
        return _result(check_id, "arithmetic", False, "arithmetic_mismatch")
    return _result(check_id, "arithmetic", True, "verified")


def verify_task_contract(
    contract: dict,
    evidence: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """Evaluate bounded typed checks against supplied evidence only.

    Missing or malformed inputs fail closed. Arithmetic uses exact rational
    values derived from decimal strings, including division; no float rounding
    or ambient Decimal precision is involved.
    """
    if not isinstance(contract, dict) or not isinstance(evidence, dict):
        check = _result("invalid_input", "contract", False, "invalid_input")
        return {"passed": False, "checks": [check]}

    groups: list[tuple[str, list[Any], str, bool]] = []
    total = 0
    for key, kind in _CHECK_GROUPS:
        raw = contract.get(key, [])
        if not isinstance(raw, list):
            groups.append((key, [], kind, True))
            total += 1
            continue
        total += len(raw)
        groups.append((key, raw, kind, False))
    required_kinds_input = contract.get("required_check_kinds", [])
    required_ids_input = contract.get("required_check_ids", [])
    total += len(required_kinds_input) if isinstance(required_kinds_input, list) else 1
    total += len(required_ids_input) if isinstance(required_ids_input, list) else 1
    if total == 0:
        return {
            "passed": False,
            "checks": [_result("empty_contract", "contract", False, "no_checks")],
        }
    if total > MAX_CHECKS:
        return {
            "passed": False,
            "checks": [_result("contract_limit", "contract", False, "too_many_checks")],
        }

    effective_now = now if now is not None else datetime.now(timezone.utc)
    now_valid = (
        isinstance(effective_now, datetime)
        and effective_now.tzinfo is not None
        and effective_now.utcoffset() is not None
    )
    if now_valid:
        effective_now = effective_now.astimezone(timezone.utc)

    receipts = _safe_evidence_map(evidence, "receipts")
    freshness = _safe_evidence_map(evidence, "freshness")
    deliverables_raw = evidence.get("deliverables", ())
    deliverables: set[str] = set()
    deliverables_valid = isinstance(deliverables_raw, (list, tuple, set, frozenset))
    if deliverables_valid and len(deliverables_raw) <= MAX_EVIDENCE_ITEMS:
        deliverables = {
            value for value in deliverables_raw
            if _valid_string(value)
        }
    else:
        deliverables_valid = False

    checks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for key, items, kind, malformed_list in groups:
        if malformed_list:
            checks.append(_result(_invalid_check_id(kind, 0), kind, False, "malformed_check_list"))
            continue
        for index, raw_item in enumerate(items):
            fallback_id = _invalid_check_id(kind, index)
            if not isinstance(raw_item, Mapping):
                checks.append(_result(fallback_id, kind, False, "malformed_check"))
                continue
            check_id = raw_item.get("check_id")
            if not _valid_string(check_id):
                checks.append(_result(fallback_id, kind, False, "invalid_check_id"))
                continue
            if check_id in seen_ids:
                checks.append(_result(check_id, kind, False, "duplicate_check_id"))
                continue
            seen_ids.add(check_id)

            if kind == "receipt":
                receipt_id, owner_id, call_id, tool_name = (
                    raw_item.get("receipt_id"), raw_item.get("owner_id"),
                    raw_item.get("call_id"), raw_item.get("tool_name"),
                )
                if not all(_valid_string(value) for value in (receipt_id, owner_id, call_id, tool_name)):
                    checks.append(_result(check_id, kind, False, "malformed_check"))
                    continue
                receipt = receipts.get(receipt_id)
                if not isinstance(receipt, Mapping):
                    checks.append(_result(check_id, kind, False, "receipt_missing", [receipt_id]))
                elif any(receipt.get(field) != expected for field, expected in (
                    ("owner_id", owner_id), ("call_id", call_id), ("tool_name", tool_name),
                )):
                    checks.append(_result(check_id, kind, False, "receipt_mismatch", [receipt_id]))
                elif (
                    receipt.get("status") != "confirmed"
                    or receipt.get("ok") is not True
                    or receipt.get("complete") is not True
                ):
                    checks.append(_result(check_id, kind, False, "receipt_not_confirmed", [receipt_id]))
                else:
                    checks.append(_result(check_id, kind, True, "verified", [receipt_id]))

            elif kind == "freshness":
                ref, max_age = raw_item.get("evidence_ref"), raw_item.get("max_age_seconds")
                if not _valid_string(ref) or isinstance(max_age, bool) or not isinstance(max_age, int):
                    checks.append(_result(check_id, kind, False, "malformed_check"))
                    continue
                if max_age <= 0 or max_age > MAX_FRESHNESS_SECONDS:
                    checks.append(_result(check_id, kind, False, "invalid_max_age"))
                    continue
                if not now_valid:
                    checks.append(_result(check_id, kind, False, "invalid_now", [ref]))
                    continue
                timestamp = _timestamp(freshness.get(ref))
                if timestamp is None:
                    checks.append(_result(check_id, kind, False, "invalid_timestamp", [ref]))
                    continue
                age = (effective_now - timestamp).total_seconds()
                if age < 0:
                    checks.append(_result(check_id, kind, False, "timestamp_in_future", [ref]))
                elif age > max_age:
                    checks.append(_result(check_id, kind, False, "stale_evidence", [ref]))
                else:
                    checks.append(_result(check_id, kind, True, "verified", [ref]))

            elif kind == "arithmetic":
                refs = raw_item.get("evidence_refs", [])
                if not isinstance(refs, list) or len(refs) > 20 or any(not _valid_string(ref) for ref in refs):
                    checks.append(_result(check_id, kind, False, "malformed_evidence_refs"))
                else:
                    check = _arithmetic_check(raw_item, check_id)
                    check["evidence_refs"] = refs
                    checks.append(check)

            elif kind == "policy":
                allowed, actual = raw_item.get("allowed_tools"), raw_item.get("actual_tools")
                if (
                    not isinstance(allowed, list) or not isinstance(actual, list)
                    or len(allowed) > MAX_EVIDENCE_ITEMS or len(actual) > MAX_EVIDENCE_ITEMS
                    or any(not _valid_string(value) for value in allowed + actual)
                ):
                    checks.append(_result(check_id, kind, False, "malformed_check"))
                elif set(actual).issubset(set(allowed)):
                    checks.append(_result(check_id, kind, True, "verified"))
                else:
                    checks.append(_result(check_id, kind, False, "disallowed_tool"))

            elif kind == "numeric_claim":
                claims = raw_item.get("claims")
                allowed_values = raw_item.get("allowed_values")
                field_claims = raw_item.get("field_claims", [])
                allowed_by_field = raw_item.get("allowed_by_field", {})
                malformed_currency_claims = raw_item.get("malformed_currency_claims", False)
                unsupported_currency_claims = raw_item.get("unsupported_currency_claims", False)
                refs = raw_item.get("evidence_refs", [])
                if (
                    not isinstance(malformed_currency_claims, bool)
                    or not isinstance(unsupported_currency_claims, bool)
                    or not isinstance(claims, list) or len(claims) > 100
                    or not isinstance(allowed_values, list) or len(allowed_values) > 1000
                    or not isinstance(field_claims, list) or len(field_claims) > 100
                    or not isinstance(allowed_by_field, Mapping) or len(allowed_by_field) > 100
                    or not isinstance(refs, list) or len(refs) > 20
                    or any(not _valid_string(ref) for ref in refs)
                ):
                    checks.append(_result(check_id, kind, False, "malformed_check"))
                    continue
                if unsupported_currency_claims:
                    checks.append(_result(check_id, kind, False, "unsupported_currency_claim", refs))
                    continue
                if malformed_currency_claims:
                    checks.append(_result(check_id, kind, False, "malformed_number", refs))
                    continue
                parsed_claims = [_parse_decimal(value) for value in claims]
                parsed_allowed = [_parse_decimal(value) for value in allowed_values]
                parsed_field_claims = []
                fields_valid = True
                for field_claim in field_claims:
                    if not isinstance(field_claim, Mapping) or not all(
                        _valid_string(field_claim.get(key)) for key in ("field", "value")
                    ):
                        fields_valid = False
                        break
                    field = field_claim["field"]
                    field_value = _parse_decimal(field_claim["value"])
                    allowed_field_values = allowed_by_field.get(field, [])
                    if not isinstance(allowed_field_values, list) or len(allowed_field_values) > 100:
                        fields_valid = False
                        break
                    parsed_field_allowed = [
                        _parse_decimal(value) for value in allowed_field_values
                    ]
                    parsed_field_claims.append((
                        field_value, parsed_field_allowed,
                    ))
                if not fields_valid or any(value is None for value in (
                    *parsed_claims, *parsed_allowed,
                    *(claim for claim, _allowed in parsed_field_claims),
                    *(value for _claim, allowed in parsed_field_claims for value in allowed),
                )):
                    checks.append(_result(check_id, kind, False, "malformed_number", refs))
                elif (
                    set(parsed_claims).issubset(set(parsed_allowed))
                    and all(claim in set(allowed) for claim, allowed in parsed_field_claims)
                ):
                    checks.append(_result(check_id, kind, True, "verified", refs))
                else:
                    checks.append(_result(check_id, kind, False, "unsupported_numeric_claim", refs))

            elif kind == "model_review":
                passed = raw_item.get("passed")
                issues = raw_item.get("issues")
                refs = raw_item.get("evidence_refs", [])
                if (
                    not isinstance(passed, bool)
                    or not isinstance(issues, list) or len(issues) > 10
                    or any(
                        not isinstance(issue, str)
                        or not 0 < len(issue) <= MAX_STRING_LENGTH
                        or issue.strip() != issue
                        for issue in issues
                    )
                    or not isinstance(refs, list) or len(refs) > 20
                    or any(not _valid_string(ref) for ref in refs)
                ):
                    checks.append(_result(check_id, kind, False, "malformed_check"))
                elif passed:
                    checks.append(_result(check_id, kind, True, "verified", refs))
                else:
                    checks.append(_result(check_id, kind, False, "model_review_rejected", refs))

            else:  # deliverable
                if kind == "artifact":
                    receipt_id = raw_item.get("receipt_id")
                    owner_id = raw_item.get("owner_id")
                    call_id = raw_item.get("call_id")
                    expected_path = raw_item.get("expected_path")
                    expected_extension = raw_item.get("expected_extension")
                    expected_format = raw_item.get("expected_format")
                    refs = [receipt_id] if _valid_string(receipt_id) else []
                    artifacts = _safe_evidence_map(evidence, "artifacts")
                    artifact = artifacts.get(receipt_id) if _valid_string(receipt_id) else None
                    if not all(_valid_string(value) for value in (
                        receipt_id, owner_id, call_id,
                    )) or (expected_path is not None and not _valid_string(expected_path)) or (
                        expected_extension is not None
                        and (
                            not isinstance(expected_extension, str)
                            or re.fullmatch(r"[a-z0-9]{1,10}", expected_extension) is None
                        )
                    ) or (
                        expected_format is not None
                        and (
                            not isinstance(expected_format, str)
                            or expected_format not in {"json", "csv"}
                        )
                    ):
                        checks.append(_result(check_id, kind, False, "malformed_check", refs))
                    elif not isinstance(artifact, Mapping):
                        checks.append(_result(check_id, kind, False, "artifact_evidence_missing", refs))
                    else:
                        path = artifact.get("path")
                        filename = artifact.get("filename")
                        digest = artifact.get("sha256")
                        size = artifact.get("byte_size")
                        message_id = artifact.get("message_id")
                        if any(artifact.get(field) != value for field, value in (
                            ("owner_id", owner_id), ("call_id", call_id),
                            ("receipt_id", receipt_id), ("status", "sent"),
                        )):
                            checks.append(_result(check_id, kind, False, "artifact_link_mismatch", refs))
                        elif (
                            not _valid_string(path)
                            or path.startswith(("/", "\\"))
                            or "\\" in path
                            or any(part in {"", ".", ".."} for part in path.split("/"))
                            or not _valid_string(filename)
                            or path.rsplit("/", 1)[-1] != filename
                            or (
                                expected_path is not None
                                and (
                                    path.casefold() != expected_path.casefold()
                                    if "/" in expected_path
                                    else path.rsplit("/", 1)[-1].casefold() != expected_path.casefold()
                                )
                            )
                            or (
                                expected_extension is not None
                                and not filename.casefold().endswith(
                                    f".{expected_extension.casefold()}"
                                )
                            )
                            or (
                                expected_format == "json"
                                and artifact.get("valid_json") is not True
                            )
                            or (
                                expected_format == "csv"
                                and artifact.get("valid_csv") is not True
                            )
                            or not isinstance(digest, str)
                            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                            or isinstance(size, bool) or not isinstance(size, int)
                            or size <= 0 or size > 100_000_000
                            or not _valid_string(message_id)
                        ):
                            checks.append(_result(check_id, kind, False, "artifact_integrity_invalid", refs))
                        else:
                            checks.append(_result(check_id, kind, True, "verified", refs))
                    continue

                ref = raw_item.get("evidence_ref")
                if not _valid_string(ref):
                    checks.append(_result(check_id, kind, False, "malformed_check"))
                elif not deliverables_valid:
                    checks.append(_result(check_id, kind, False, "malformed_deliverables", [ref]))
                elif ref in deliverables:
                    checks.append(_result(check_id, kind, True, "verified", [ref]))
                else:
                    checks.append(_result(check_id, kind, False, "deliverable_missing", [ref]))

    required_kinds = contract.get("required_check_kinds", [])
    if (
        not isinstance(required_kinds, list)
        or len(required_kinds) > len(_SUPPORTED_REQUIRED_KINDS)
        or any(not isinstance(kind, str) for kind in required_kinds)
    ):
        checks.append(_result("required_check_kinds", "contract", False, "malformed_required_kinds"))
    else:
        observed_kinds = {check["kind"] for check in checks}
        for kind in dict.fromkeys(required_kinds):
            if kind not in _SUPPORTED_REQUIRED_KINDS:
                checks.append(_result("required_check_kind", "contract", False, "unsupported_required_kind"))
            elif kind not in observed_kinds:
                checks.append(_result(
                    f"required_{kind}", kind, False, "required_check_missing_or_failed"
                ))

    required_ids = contract.get("required_check_ids", [])
    if (
        not isinstance(required_ids, list) or len(required_ids) > MAX_CHECKS
        or any(not _valid_string(check_id) for check_id in required_ids)
    ):
        checks.append(_result("required_check_ids", "contract", False, "malformed_required_check_ids"))
    else:
        observed_ids = {check["check_id"] for check in checks}
        for check_id in dict.fromkeys(required_ids):
            if check_id not in observed_ids:
                checks.append(_result(
                    str(check_id), "contract", False, "required_check_missing_or_failed"
                ))

    return _persistence_safe_outcome(checks)
