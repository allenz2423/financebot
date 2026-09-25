"""Pure state transitions for the transaction audit controller.

The advisor loop owns I/O and policy, while this module owns the small pieces
of audit state that must never depend on model prose.
"""

from __future__ import annotations

from typing import Any, Mapping


def normalize_row_ids(values: Any) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(result))


def mark_batch_corrected(state: dict[str, Any], row_ids: Any) -> list[int]:
    """Record the exact rows whose successful correction must be locked next."""
    ids = normalize_row_ids(row_ids)
    if not ids:
        raise ValueError("AUDIT CORRECTION TRANSITION: no valid corrected transaction IDs")
    state["pending_lock_ids"] = ids
    state["phase"] = "lock_required"
    state["dirty"] = True
    return ids


def validate_lock_ids(state: Mapping[str, Any], row_ids: Any) -> list[int]:
    expected = normalize_row_ids(state.get("pending_lock_ids"))
    requested = normalize_row_ids(row_ids)
    if not expected:
        raise ValueError(
            "AUDIT LOCK GUARD: no successful batch correction is pending; classify the verified rows first."
        )
    if requested != expected:
        raise ValueError(
            "AUDIT LOCK GUARD: lock exactly the transaction IDs returned by the preceding batch correction: "
            + ", ".join(str(row_id) for row_id in expected)
        )
    return expected


def mark_batch_locked(state: dict[str, Any]) -> None:
    """Move the controller into post-lock verification."""
    state["pending_lock_ids"] = []
    state["phase"] = "verify_after_lock"
    state["verified_this_turn"] = False
    state["dirty"] = True


def next_required_action(
    state: Mapping[str, Any],
    *,
    scope: str,
    require_fresh_verification: bool,
) -> str | None:
    """Return the only safe next action, or ``None`` when the audit is terminal."""
    if require_fresh_verification:
        return "get_locked_transactions" if scope == "locked" else "get_unlocked_transactions"
    if normalize_row_ids(state.get("pending_lock_ids")):
        return "batch_lock_transactions"
    if state.get("research_inflight"):
        return "save_known_merchant"
    if state.get("research_pending"):
        return "search_web"
    remaining = state.get("remaining_count")
    unresolved = normalize_row_ids(state.get("unresolved_ids"))
    if remaining not in (None, 0) and len(unresolved) != int(remaining):
        return "batch_correct_transactions"
    return None


__all__ = [
    "mark_batch_corrected", "mark_batch_locked", "next_required_action",
    "normalize_row_ids", "validate_lock_ids",
]
