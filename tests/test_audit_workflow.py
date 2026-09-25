import pytest

from src.services.audit_workflow import (
    mark_batch_corrected,
    mark_batch_locked,
    next_required_action,
    normalize_row_ids,
    validate_lock_ids,
)


def test_row_ids_are_normalized_and_deduplicated():
    assert normalize_row_ids(["3", 2, "3", "bad", None]) == [2, 3]


def test_correction_forces_exact_lock_phase():
    state = {"phase": "ready_to_classify", "dirty": False}
    assert mark_batch_corrected(state, ["7", 7, 8]) == [7, 8]
    assert state["pending_lock_ids"] == [7, 8]
    assert state["phase"] == "lock_required"
    assert next_required_action(state, scope="unlocked", require_fresh_verification=False) == "batch_lock_transactions"


def test_lock_requires_the_exact_correction_set():
    state = {"pending_lock_ids": [7, 8]}
    assert validate_lock_ids(state, [8, "7"]) == [7, 8]
    with pytest.raises(ValueError, match="exactly"):
        validate_lock_ids(state, [7])
    with pytest.raises(ValueError, match="no successful batch correction"):
        validate_lock_ids({}, [7])


def test_successful_lock_forces_post_lock_verification():
    state = {"pending_lock_ids": [7], "dirty": True, "verified_this_turn": True}
    mark_batch_locked(state)
    assert state["pending_lock_ids"] == []
    assert state["phase"] == "verify_after_lock"
    assert state["verified_this_turn"] is False
    assert next_required_action(state, scope="unlocked", require_fresh_verification=True) == "get_unlocked_transactions"


def test_research_and_classification_phases_have_one_next_action():
    assert next_required_action({"research_inflight": ["kemet kebab"]}, scope="unlocked", require_fresh_verification=False) == "save_known_merchant"
    assert next_required_action({"research_pending": ["google"]}, scope="unlocked", require_fresh_verification=False) == "search_web"
    assert next_required_action({"remaining_count": 1, "unresolved_ids": []}, scope="unlocked", require_fresh_verification=False) == "batch_correct_transactions"
    assert next_required_action({"remaining_count": 0}, scope="unlocked", require_fresh_verification=False) is None
