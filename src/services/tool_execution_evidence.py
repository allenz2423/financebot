"""Fail-closed classification of tool results.

The model's prose is never evidence that a tool succeeded.  This helper only
classifies the value returned by the dispatcher; durable receipts still record
whether dispatch reached a terminal state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_FAILURE_PREFIXES = (
    "REJECTED:",
    "REFUSED:",
    "ERROR",
    "ERROR:",
    "ERROR EXECUTING",
    "FAILED",
    "FAILED:",
    "FAILURE:",
    "DENIED:",
    "BLOCKED:",
    "UNKNOWN:",
    "UNKNOWN TOOL",
    "[TOOL_ERROR]",
)

_FAILURE_PHRASES = (
    "WAS REJECTED",
    "REQUEST REJECTED",
    "PERMISSION DENIED",
    "DATABASE COMMIT FAILED",
    "VERIFICATION FAILED",
    "FETCH FAILED",
    "REQUEST FAILED",
    "OPERATION FAILED",
    "TIMED OUT",
    "TOOL EXECUTION ERROR",
    "TOOL EXECUTION FAILED",
    "PLAID ACCOUNTING WORKFLOW FAILED",
    "LIVE PLAID PULL FAILED",
    "IS LOCKED",
    "IMMUTABLE",
    "MUST BE UNLOCKED",
    "WEB RESEARCH IS REQUIRED",
    "NO SUCH",
)


def tool_result_indicates_failure(result: Any) -> bool:
    """Return true unless the dispatcher returned a non-error result.

    Empty/None results fail closed. Mapping/envelope results honor explicit
    ``ok``/``status``/``error`` fields before falling back to bounded text
    markers used by legacy handlers.
    """

    if result is None or result is False:
        return True

    if isinstance(result, Mapping):
        if result.get("ok") is False:
            return True
        if result.get("error"):
            return True
        if str(result.get("status") or "").casefold() in {
            "failed", "error", "blocked", "unknown", "rejected"
        }:
            return True

    explicit_ok = getattr(result, "ok", None)
    explicit_status = str(getattr(result, "status", "") or "").casefold()
    if explicit_ok is False or explicit_status in {"failed", "error", "blocked", "unknown", "rejected"}:
        return True

    # Do not use ``result or ""`` here: numeric zero is a valid financial
    # result and must not be confused with an empty response.
    text = "" if result is None else str(result).strip()
    if not text:
        return True
    upper = text.upper()
    first_line = upper.splitlines()[0].strip() if upper.splitlines() else upper
    if first_line.startswith(_FAILURE_PREFIXES):
        return True
    return any(phrase in upper for phrase in _FAILURE_PHRASES)


__all__ = ["tool_result_indicates_failure"]
