from __future__ import annotations

import src.services.llm as llm
from src.services.autonomy_contract import build_autonomy_contract, mutation_allowed


def test_profile_set_contract_binds_exact_field_and_value_from_current_message():
    contract = build_autonomy_contract(
        "Set my profile field risk_tolerance to cautious."
    )
    exact = {
        "action": "set",
        "values": {"risk_tolerance": "cautious"},
    }

    assert contract.explicit_mutation_intent == "manage_user_profile"
    assert contract.required_tools == frozenset({"manage_user_profile"})
    assert mutation_allowed(contract, "manage_user_profile", exact)
    assert not mutation_allowed(
        contract,
        "manage_user_profile",
        {"action": "set", "values": {"risk_tolerance": "growth"}},
    )
    assert not mutation_allowed(
        contract,
        "manage_user_profile",
        {**exact, "owner_id": "another-user"},
    )


def test_profile_contract_parses_exact_nested_json_and_forget_scope():
    contract = build_autonomy_contract(
        'Set my profile field autonomy_limits to {"require_mutation_confirmation": true}'
    )
    assert mutation_allowed(
        contract,
        "manage_user_profile",
        {
            "action": "set",
            "values": {"autonomy_limits": {"require_mutation_confirmation": True}},
        },
    )

    forget_field = build_autonomy_contract("Forget my profile field risk_tolerance.")
    assert mutation_allowed(
        forget_field,
        "manage_user_profile",
        {"action": "forget", "fields": ["risk_tolerance"]},
    )
    assert not mutation_allowed(
        forget_field, "manage_user_profile", {"action": "forget"}
    )

    forget_all = build_autonomy_contract("Forget my operating profile.")
    assert mutation_allowed(forget_all, "manage_user_profile", {"action": "forget"})


def test_quoted_or_vague_profile_text_does_not_authorize_profile_mutation():
    for message in (
        'The user said "set my profile field risk_tolerance to cautious."',
        "Please update my profile.",
        "Forget that preference.",
    ):
        contract = build_autonomy_contract(message)
        assert not mutation_allowed(
            contract,
            "manage_user_profile",
            {"action": "forget"},
        )


def test_profile_confirmation_preference_only_restricts_mutations(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_operating_profile",
        lambda _user_id: {
            "values": {"autonomy_limits": {"require_mutation_confirmation": True}},
            "provenance": {},
        },
    )
    unconstrained_contract = build_autonomy_contract("What's my account balance?")
    assert llm._profile_additional_confirmation_denial(
        "user-a", unconstrained_contract, "add_transaction", {"amount": 10}
    ) == (
        "PROFILE_CONFIRMATION_REQUIRED: this profile requires an exact, current-turn "
        "user request for the proposed mutation; it cannot be authorized by prior context."
    )

    explicit_sync = build_autonomy_contract("Run a Plaid sync.")
    assert llm._profile_additional_confirmation_denial(
        "user-a",
        explicit_sync,
        "sync_plaid_accounting",
        {"force_refresh": True, "reconcile": False, "post_summary": False},
    ) is None


def test_profile_tool_effect_is_action_specific():
    assert llm._autonomy_contract_effect("manage_user_profile", {"action": "inspect"}) == "read"
    assert llm._autonomy_contract_effect(
        "manage_user_profile",
        {"action": "set", "values": {"risk_tolerance": "cautious"}},
    ) == "mutation"
    assert llm._autonomy_contract_effect(
        "manage_user_profile", {"action": "inspect", "values": {}}
    ) == "unknown"


def test_delegated_children_cannot_change_the_owner_global_profile():
    assert "manage_user_profile" in llm._DELEGATION_CONTROL_TOOLS
