"""Bounded, provider-independent helpers for optional task review.

This module only prepares untrusted text for a reviewer and validates its
structured response. It deliberately does not call a model or any network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any


MAX_OBJECTIVE_CHARS = 2_000
MAX_RESPONSE_CHARS = 8_000
MAX_CHECKS = 20
MAX_CHECK_FIELD_CHARS = 80
MAX_PROMPT_CHARS = 40_000
MAX_REVIEW_RESPONSE_CHARS = 4_096
MAX_ISSUES = 10
MAX_ISSUE_CHARS = 256

_PROMPT = (
    "Review the task outcome using only the supplied objective, final response, "
    "and deterministic check summaries. This is a read-only assessment: do not "
    "request or perform actions. Return exactly one JSON object with this schema: "
    '{"passed": boolean, "issues": [string, ...]}. Keep issues concise.\n'
    "Input:\n"
)


def reviewer_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether optional model review is enabled; default is safely off."""
    values = os.environ if env is None else env
    value = values.get("DELILAH_TASK_MODEL_REVIEWER", "")
    if not isinstance(value, str):
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Avoid control-character expansion when JSON-encoding untrusted text.
    return "".join(char if char >= " " else " " for char in value[:limit])


def _check_summaries(checks: Any) -> list[dict[str, Any]]:
    """Project arbitrary check records onto a bounded, non-executable summary."""
    if isinstance(checks, (str, bytes, Mapping)) or not isinstance(checks, Sequence):
        return []
    summaries: list[dict[str, Any]] = []
    for check in checks[:MAX_CHECKS]:
        if not isinstance(check, Mapping):
            continue
        passed = check.get("passed")
        summaries.append({
            "check_id": _bounded_text(check.get("check_id"), MAX_CHECK_FIELD_CHARS),
            "kind": _bounded_text(check.get("kind"), MAX_CHECK_FIELD_CHARS),
            "passed": passed if type(passed) is bool else None,
            "reason_code": _bounded_text(check.get("reason_code"), MAX_CHECK_FIELD_CHARS),
        })
    return summaries


def build_review_prompt(
    objective: Any,
    final_response: Any,
    deterministic_checks: Any,
) -> str:
    """Build compact reviewer input containing no tool definitions or call data."""
    payload = {
        "objective": _bounded_text(objective, MAX_OBJECTIVE_CHARS),
        "final_response": _bounded_text(final_response, MAX_RESPONSE_CHARS),
        "deterministic_checks": _check_summaries(deterministic_checks),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    prompt = _PROMPT + encoded
    # Fixed fields and limits keep this comfortably below the ceiling. Retain a
    # final fail-safe in case those limits are changed independently later.
    return prompt[:MAX_PROMPT_CHARS]


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_review_response(raw: Any) -> dict[str, Any] | None:
    """Parse strict bounded JSON, returning ``None`` for every invalid result."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_REVIEW_RESPONSE_CHARS:
        return None
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"passed", "issues"}:
        return None
    if type(parsed["passed"]) is not bool or not isinstance(parsed["issues"], list):
        return None
    if len(parsed["issues"]) > MAX_ISSUES:
        return None
    if any(
        not isinstance(issue, str)
        or not issue.strip()
        or issue.strip() != issue
        or len(issue) > MAX_ISSUE_CHARS
        for issue in parsed["issues"]
    ):
        return None
    if (parsed["passed"] and parsed["issues"]) or (
        not parsed["passed"] and not parsed["issues"]
    ):
        return None
    return {"passed": parsed["passed"], "issues": list(parsed["issues"])}
