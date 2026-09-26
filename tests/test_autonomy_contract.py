from dataclasses import FrozenInstanceError

import pytest

from src.services.autonomy_contract import (
    AutonomyContract,
    build_autonomy_contract,
    mutation_allowed,
    next_required_tool,
    tool_call_allowed_by_contract,
)


PULL_ONLY_ARGS = {
    "force_refresh": True,
    "reconcile": False,
    "post_summary": False,
}


@pytest.mark.parametrize(
    "message",
    [
        "just do whatever",
        "take care of this",
        "Take care of my finances",
        "What should I focus on with my finances?",
        "Can you do whatever is best with my finances?",
        "Review my financial situation and suggest next steps",
        "What should I do with my finances?",
        "Help me with my finances",
        "Could you help me manage my finances?",
        "review everything",
        "review my situation",
        "Review my financial situation",
    ],
)
def test_vague_financial_review_selects_bounded_workflow(message):
    contract = build_autonomy_contract(message, financial_context=True)

    assert contract.mode == "investigate_report"
    assert contract.required_tools == frozenset(
        {
            "sync_plaid_accounting",
            "get_current_financial_position",
            "get_debt_overview",
            "get_upcoming_bills_calendar",
        }
    )
    assert len(contract.mutation_permissions) == 1
    assert contract.mutation_permissions[0].tool_name == "sync_plaid_accounting"
    assert set(dict(contract.mutation_permissions[0].exact_arguments).items()) == set(
        PULL_ONLY_ARGS.items()
    )
    assert any("recommend" in criterion.lower() for criterion in contract.completion_criteria)


def test_vague_request_without_financial_context_does_not_start_financial_workflow():
    contract = build_autonomy_contract("just do whatever")

    assert contract.mode == "investigate_report"
    assert contract.required_tools == frozenset()
    assert contract.mutation_permissions == ()
    assert "financial" not in contract.objective.lower()


def test_vague_financial_followup_uses_only_explicitly_supplied_user_context():
    contract = build_autonomy_contract(
        "just do whatever",
        recent_user_context="Can you run a Plaid sync?",
    )

    assert "get_debt_overview" in contract.required_tools
    assert not build_autonomy_contract(
        "just do whatever",
        recent_user_context="Can you check my email for anything new?",
    ).required_tools


@pytest.mark.parametrize(
    "message",
    [
        "What should I focus on with my finances?",
        "Take care of my finances",
        "review everything",
    ],
)
def test_open_ended_financial_requests_select_the_bounded_review(message):
    contract = build_autonomy_contract(message)

    assert contract.required_tools == frozenset(
        {
            "sync_plaid_accounting",
            "get_current_financial_position",
            "get_debt_overview",
            "get_upcoming_bills_calendar",
        }
    )
    # Only the exact, pull-only refresh; the workflow remains advisory.
    assert len(contract.mutation_permissions) == 1
    assert contract.mutation_permissions[0].tool_name == "sync_plaid_accounting"


@pytest.mark.parametrize(
    ("message", "kwargs", "expected"),
    [
        ("Can you run a Plaid sync", {}, "execute"),
        ("What is my checking account balance?", {}, "investigate_report"),
        ("Make a debt payoff plan", {}, "plan"),
        ("Monitor my balance and alert me if it drops", {}, "monitor"),
        ("Continue where we left off", {"has_active_task": True}, "continue"),
        ("Should I proceed with this choice?", {}, "await_decision"),
    ],
)
def test_classifies_every_contract_mode(message, kwargs, expected):
    assert build_autonomy_contract(message, **kwargs).mode == expected


def test_explicit_task_cancel_is_scoped_to_visible_conversation_tasks():
    contract = build_autonomy_contract("Please cancel my task")

    assert contract.mode == "execute"
    assert contract.explicit_mutation_intent == "task_cancel"
    assert contract.required_tools == frozenset({"task_cancel"})
    assert tool_call_allowed_by_contract(
        contract,
        "task_cancel",
        {"task_id": "task-1234"},
        side_effect="mutation",
    )
    assert not tool_call_allowed_by_contract(
        contract,
        "task_cancel",
        {"task_id": "task-1234", "user_id": "other-owner"},
        side_effect="mutation",
    )


def test_task_cancel_contract_binds_an_explicit_id_when_user_provides_one():
    contract = build_autonomy_contract("Cancel task task-1234")

    assert contract.mutation_permissions[0].target_value == "task-1234"
    assert tool_call_allowed_by_contract(
        contract,
        "task_cancel",
        {"task_id": "task-1234"},
        side_effect="mutation",
    )
    assert not tool_call_allowed_by_contract(
        contract,
        "task_cancel",
        {"task_id": "task-5678"},
        side_effect="mutation",
    )
    assert not tool_call_allowed_by_contract(
        build_autonomy_contract("What is my balance?"),
        "task_cancel",
        {"task_id": "task-1234"},
        side_effect="mutation",
    )


def test_task_steering_requires_exact_user_correction_and_step_scope():
    contract = build_autonomy_contract(
        "Please correct step step_abc123: Compare only transactions posted since yesterday."
    )
    assert contract.mode == "execute"
    assert contract.explicit_mutation_intent == "steer_task"
    assert contract.required_tools == frozenset({"steer_task"})
    assert mutation_allowed(
        contract,
        "steer_task",
        {
            "task_id": "task-1234",
            "step_id": "step_abc123",
            "correction": "Compare only transactions posted since yesterday.",
        },
    )
    assert not mutation_allowed(
        contract,
        "steer_task",
        {
            "task_id": "task-1234",
            "step_id": "step_other",
            "correction": "Compare only transactions posted since yesterday.",
        },
    )
    assert not mutation_allowed(
        contract,
        "steer_task",
        {
            "task_id": "task-1234",
            "step_id": "step_abc123",
            "correction": "Also transfer the remaining money.",
        },
    )


@pytest.mark.parametrize("message", [
    "What does 'correct step step_abc123: Do something' mean?",
    "Don't redirect step step_abc123: Change the transfer amount.",
    "Correct the next step: summarize differently.",
])
def test_nonexact_task_steering_mentions_do_not_authorize_steering(message):
    contract = build_autonomy_contract(message)
    assert contract.explicit_mutation_intent != "steer_task"
    assert not mutation_allowed(
        contract,
        "steer_task",
        {"task_id": "task", "step_id": "step_abc123", "correction": "Do something."},
    )


def test_monitor_contract_does_not_claim_permission_to_create_rules():
    contract = build_autonomy_contract("Monitor my balance")

    assert contract.mode == "monitor"
    assert "existing monitoring only" in contract.objective
    assert "do not create or change rules" in contract.completion_criteria[0]
    assert contract.mutation_permissions == ()
    assert not tool_call_allowed_by_contract(
        contract,
        "monitor_create_natural_rule",
        {"instruction": "alert me if balance drops"},
        side_effect="mutation",
    )


def test_explicit_monitor_request_permits_only_exact_natural_rule_creation():
    request = "Monitor my balance and alert me if it drops"
    contract = build_autonomy_contract(request)

    assert contract.required_tools == frozenset({"monitor_create_natural_rule"})
    assert tool_call_allowed_by_contract(
        contract,
        "monitor_create_natural_rule",
        {"instruction": request},
        side_effect="mutation",
    )
    assert not tool_call_allowed_by_contract(
        contract,
        "monitor_create_natural_rule",
        {"instruction": "Monitor my balance and send me a daily report"},
        side_effect="mutation",
    )
    assert not tool_call_allowed_by_contract(
        contract,
        "monitor_add_rule",
        {"name": "Balance", "kind": "projected_balance_low"},
        side_effect="mutation",
    )


@pytest.mark.parametrize(
    "message",
    [
        "What does 'monitor my balance and alert me if it drops' mean?",
        "Explain how to notify me when a balance falls.",
    ],
)
def test_explanatory_monitor_mentions_do_not_create_rules(message):
    contract = build_autonomy_contract(message)

    assert contract.mode == "monitor"
    assert contract.required_tools == frozenset()
    assert contract.mutation_permissions == ()


def test_bounded_delegation_is_internal_control_not_financial_authority():
    ordinary_task = build_autonomy_contract("Research options and summarize them")
    finance_review = build_autonomy_contract("review my financial situation")
    monitor_turn = build_autonomy_contract("Monitor my balance")
    decision_turn = build_autonomy_contract("Should I proceed with the transfer?")

    assert tool_call_allowed_by_contract(
        ordinary_task,
        "delegate_task",
        {"objective": "Research the requested options"},
        side_effect="mutation",
        control_tool=True,
    )
    assert not tool_call_allowed_by_contract(
        finance_review,
        "delegate_task",
        {"objective": "Research the requested options"},
        side_effect="mutation",
        control_tool=True,
    )
    for narrow_turn in (monitor_turn, decision_turn):
        assert not tool_call_allowed_by_contract(
            narrow_turn,
            "delegate_task",
            {"objective": "Research the requested options"},
            side_effect="mutation",
            control_tool=True,
        )


def test_explicit_sync_keeps_existing_required_tool_inference_compatible():
    contract = build_autonomy_contract("please sync my Plaid accounts now")

    assert contract.mode == "execute"
    assert contract.required_tools == frozenset({"sync_plaid_accounting"})
    assert mutation_allowed(contract, "sync_plaid_accounting", PULL_ONLY_ARGS)


def test_await_decision_takes_precedence_over_other_action_language():
    contract = build_autonomy_contract(
        "Please continue the task; should I approve the transfer?",
        has_active_task=True,
    )

    assert contract.mode == "await_decision"
    assert contract.mutation_permissions == ()


def test_negated_sync_is_not_required_or_permitted():
    contract = build_autonomy_contract("Don't run a Plaid sync; just review my cash")

    assert "sync_plaid_accounting" not in contract.required_tools
    assert not mutation_allowed(contract, "sync_plaid_accounting", PULL_ONLY_ARGS)


@pytest.mark.parametrize(
    "message",
    [
        "What does 'run a Plaid sync' mean?",
        "Explain how a Plaid sync works",
        "I saw the phrase run a Plaid sync in an email",
    ],
)
def test_quoted_or_explanatory_sync_mention_is_not_execution_intent(message):
    contract = build_autonomy_contract(message)

    assert contract.mode == "investigate_report"
    assert "sync_plaid_accounting" not in contract.required_tools
    assert not contract.mutation_permissions


@pytest.mark.parametrize(
    "message",
    [
        "What does 'review my situation' mean for my finances?",
        'Explain the phrase "review everything" in a financial plan.',
        "I am asking what 'take care of my finances' means, not asking you to do it.",
        "Explain what review my financial situation means, please.",
    ],
)
def test_explanatory_or_quoted_review_language_does_not_start_financial_refresh(message):
    contract = build_autonomy_contract(message)

    assert contract.required_tools == frozenset()
    assert contract.mutation_permissions == ()


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("Pay my electric bill", "unmapped:pay"),
        ("Could you pay my electric bill?", "unmapped:pay"),
        ("Could you please delete transaction 123?", "unmapped:delete"),
        ("Delete transaction 12345", "unmapped:delete"),
        ("Transfer $50 to savings", "unmapped:transfer"),
        ("Add a reminder for rent", "unmapped:add"),
    ],
)
def test_direct_mutation_intent_is_distinguished_but_not_authorized(message, intent):
    contract = build_autonomy_contract(message)

    assert contract.mode == "execute"
    assert contract.explicit_mutation_intent == intent
    assert contract.mutation_permissions == ()
    assert "do not dispatch" in contract.objective
    assert not mutation_allowed(contract, "pay_bill", {"amount": 50, "target": "electric"})
    assert not tool_call_allowed_by_contract(
        contract,
        "pay_bill",
        {"amount": 50, "target": "electric"},
        side_effect="mutation",
    )


def test_unmapped_mutation_contract_does_not_echo_arbitrary_user_target():
    contract = build_autonomy_contract("Delete transaction with private memo: rent 123")

    assert contract.explicit_mutation_intent == "unmapped:delete"
    assert "private memo" not in str(contract.to_dict())


@pytest.mark.parametrize(
    "arguments",
    [
        {"force_refresh": True, "reconcile": True, "post_summary": False},
        {"force_refresh": True, "reconcile": False},
        {**PULL_ONLY_ARGS, "unexpected": False},
        {"force_refresh": 1, "reconcile": False, "post_summary": False},
        {"force_refresh": True, "reconcile": False, "post_summary": True},
    ],
)
def test_refresh_permission_rejects_non_exact_or_unsafe_arguments(arguments):
    contract = build_autonomy_contract("review my financial situation", financial_context=True)

    assert not mutation_allowed(contract, "sync_plaid_accounting", arguments)


@pytest.mark.parametrize(
    "tool_name",
    [
        "transfer_funds",
        "pay_bill",
        "delete_transaction",
        "cancel_subscription",
        "reconcile_expected_and_planned_transactions",
        "monitor_create_natural_rule",
        "send_email",
    ],
)
def test_contract_never_permits_destructive_or_unlisted_mutations(tool_name):
    contract = build_autonomy_contract("review my financial situation", financial_context=True)

    assert not mutation_allowed(contract, tool_name, PULL_ONLY_ARGS)


def test_dispatch_policy_preserves_reads_and_fails_closed_on_unknown_effects():
    contract = build_autonomy_contract("just do whatever", financial_context=True)

    assert tool_call_allowed_by_contract(
        contract, "get_current_financial_position", {}, side_effect="read"
    )
    assert not tool_call_allowed_by_contract(
        contract, "sync_plaid_accounting", PULL_ONLY_ARGS, side_effect="unknown"
    )
    assert not tool_call_allowed_by_contract(
        contract, "transfer_funds", {}, side_effect="mutation"
    )
    assert tool_call_allowed_by_contract(
        contract, "sync_plaid_accounting", PULL_ONLY_ARGS, side_effect="mutation"
    )


def test_default_financial_review_enforces_deterministic_evidence_order():
    contract = build_autonomy_contract("review my financial situation")

    assert next_required_tool(contract, set()) == "sync_plaid_accounting"
    assert next_required_tool(
        contract, {"sync_plaid_accounting"}
    ) == "get_current_financial_position"
    assert next_required_tool(
        contract,
        {"sync_plaid_accounting", "get_current_financial_position"},
    ) == "get_debt_overview"
    assert next_required_tool(
        contract,
        {
            "sync_plaid_accounting",
            "get_current_financial_position",
            "get_debt_overview",
        },
    ) == "get_upcoming_bills_calendar"
    assert next_required_tool(contract, set(contract.required_tools)) is None


def test_active_task_does_not_expand_mutation_permissions():
    contract = build_autonomy_contract("Continue where we left off", has_active_task=True)

    assert contract.mode == "continue"
    assert not mutation_allowed(contract, "sync_plaid_accounting", PULL_ONLY_ARGS, True)


def test_plan_contract_cannot_be_given_refresh_permission_by_call_arguments():
    contract = build_autonomy_contract("Make a plan for my finances")

    assert not mutation_allowed(contract, "sync_plaid_accounting", PULL_ONLY_ARGS)


def test_contract_is_frozen_and_exposes_serializable_fields_and_status():
    contract = build_autonomy_contract("Can you run a Plaid sync")
    serialized = contract.to_dict()

    assert isinstance(contract, AutonomyContract)
    assert set(serialized) == {
        "mode",
        "objective",
        "allowed_initiative",
        "required_evidence",
        "completion_criteria",
        "mutation_permissions",
        "required_tools",
        "explicit_mutation_intent",
    }
    assert serialized["required_tools"] == ["sync_plaid_accounting"]
    assert "Plaid refresh only" in contract.status_summary
    with pytest.raises(FrozenInstanceError):
        contract.mode = "plan"


def test_financial_review_status_states_that_mutations_are_out_of_scope():
    contract = build_autonomy_contract("just do whatever", financial_context=True)

    assert "will not pay bills" in contract.status_summary
    assert "or make other account changes" in contract.status_summary
