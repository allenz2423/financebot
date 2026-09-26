"""Isolated tests for the operating-profile mutation-confirmation boundary.

These tests exercise only the pure dispatch guards and a temporary preferences
database. They never dispatch a tool or contact an external service.
"""

import pytest

from src.db import prefs
from src.services import llm
from src.services.autonomy_contract import build_autonomy_contract


@pytest.fixture
def profile_db(tmp_path, monkeypatch):
    db_path = tmp_path / "prefs.sqlite3"
    monkeypatch.setattr(prefs, "DB_PATH", str(db_path))
    prefs.init_prefs_schema()
    return db_path


def _plaid_sync_args():
    return {
        "force_refresh": True,
        "reconcile": False,
        "post_summary": False,
    }


@pytest.mark.parametrize("failure_mode", ["missing_schema", "corrupt_database"])
def test_unavailable_or_corrupt_profile_store_fails_closed_for_mutation(
    tmp_path, monkeypatch, failure_mode
):
    db_path = tmp_path / "unavailable-prefs.sqlite3"
    monkeypatch.setattr(prefs, "DB_PATH", str(db_path))

    if failure_mode == "corrupt_database":
        db_path.write_bytes(b"not a SQLite database")
    # The missing-schema case leaves a valid SQLite file without the required
    # preferences table. Both cases make the real profile getter raise.
    denial = llm._profile_additional_confirmation_denial(
        "user-a",
        build_autonomy_contract("Run a Plaid sync."),
        "sync_plaid_accounting",
        _plaid_sync_args(),
    )

    assert denial is not None
    assert denial.startswith("PROFILE_POLICY_UNAVAILABLE:")


def test_enabled_confirmation_limit_denies_without_exact_current_turn_scope(
    profile_db,
):
    prefs.set_operating_profile(
        "user-a",
        {"autonomy_limits": {"require_mutation_confirmation": True}},
        source_turn_id="turn-setting",
    )
    unrelated_contract = build_autonomy_contract("What's my account balance?")

    denial = llm._profile_additional_confirmation_denial(
        "user-a",
        unrelated_contract,
        "sync_plaid_accounting",
        _plaid_sync_args(),
    )

    assert denial is not None
    assert denial.startswith("PROFILE_CONFIRMATION_REQUIRED:")


def test_exact_current_turn_scope_still_passes_through_normal_contract_gate(
    profile_db,
):
    prefs.set_operating_profile(
        "user-a",
        {"autonomy_limits": {"require_mutation_confirmation": True}},
        source_turn_id="turn-setting",
    )
    exact_contract = build_autonomy_contract("Run a Plaid sync.")
    tool_args = _plaid_sync_args()

    # The profile preference is an additional restriction; the exact matching
    # scope clears it but does not replace the ordinary autonomy-contract gate.
    assert (
        llm._profile_additional_confirmation_denial(
            "user-a", exact_contract, "sync_plaid_accounting", tool_args
        )
        is None
    )
    assert (
        llm._autonomy_contract_dispatch_denial(
            exact_contract,
            "sync_plaid_accounting",
            tool_args,
            completed_tool_names=set(),
        )
        is None
    )

    # A mismatch is rejected by both the additional profile limit and the
    # normal contract gate.
    mismatched_args = {**tool_args, "reconcile": True}
    assert llm._profile_additional_confirmation_denial(
        "user-a", exact_contract, "sync_plaid_accounting", mismatched_args
    ).startswith("PROFILE_CONFIRMATION_REQUIRED:")
    assert llm._autonomy_contract_dispatch_denial(
        exact_contract,
        "sync_plaid_accounting",
        mismatched_args,
        completed_tool_names=set(),
    ) is not None


def test_read_only_profile_inspection_is_unaffected_when_confirmation_is_enabled(
    profile_db,
):
    prefs.set_operating_profile(
        "user-a",
        {"autonomy_limits": {"require_mutation_confirmation": True}},
        source_turn_id="turn-setting",
    )

    assert (
        llm._profile_additional_confirmation_denial(
            "user-a",
            build_autonomy_contract("Show my profile."),
            "manage_user_profile",
            {"action": "inspect"},
        )
        is None
    )
