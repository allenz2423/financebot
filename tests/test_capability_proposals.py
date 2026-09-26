"""Pure validation contract for safe, non-runnable capability drafts."""

import copy

import pytest

from src.services.capability_proposals import (
    CAPABILITY_CONTROL_TOOLS,
    validate_capability_proposal,
)


CANONICAL_TOOLS = frozenset({"search_gmail", "read_gmail_message", "get_balance"})


def _adapter_proposal():
    return {
        "proposal_id": "adapter.vendor_status",
        "title": "Vendor order status lookup",
        "capability_kind": "adapter",
        "proposed_tool_name": "lookup_vendor_status",
        "purpose": "Look up the status of a vendor order.",
        "proposed_tool_description": "Read the status of one vendor order.",
        "requested_permissions": ["search_gmail"],
        "acceptance_checks": ["Returns a status for a known synthetic order."],
        "workflow_steps": [],
        "proposed_parameters_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    }


def _workflow_proposal():
    return {
        "proposal_id": "workflow.mail_balance_summary",
        "title": "Mail and balance summary",
        "capability_kind": "workflow",
        "proposed_tool_name": "summarize_mail_and_balance",
        "purpose": "Summarize selected recent mail and the current balance.",
        "proposed_tool_description": "Summarize recent mail and a balance read.",
        "requested_permissions": ["search_gmail", "read_gmail_message", "get_balance"],
        "acceptance_checks": [
            "Summary cites the selected message IDs.",
            "Balance matches the tool result.",
        ],
        "proposed_parameters_schema": {
            "type": "object",
            "properties": {"days_back": {"type": "integer"}},
            "required": ["days_back"],
            "additionalProperties": False,
        },
        "workflow_steps": [
            {
                "tool_name": "search_gmail",
                "description": "Find recent messages.",
                "completion_criteria": "Relevant synthetic message IDs are returned.",
            },
            {
                "tool_name": "get_balance",
                "description": "Read the current balance.",
                "completion_criteria": "The balance is returned from the fake handler.",
            },
        ],
    }


@pytest.mark.parametrize("payload_factory", [_adapter_proposal, _workflow_proposal])
def test_valid_adapter_and_workflow_return_non_runnable_drafts(payload_factory):
    proposal = validate_capability_proposal(payload_factory(), CANONICAL_TOOLS)

    assert proposal["proposal_id"]
    assert proposal["capability_kind"] in {"adapter", "workflow"}
    assert proposal["proposed_tool_name"]
    assert isinstance(proposal["requested_permissions"], list)
    assert isinstance(proposal["acceptance_checks"], list)
    assert isinstance(proposal["workflow_steps"], list)
    assert len(proposal["digest"]) == 64
    assert proposal["status"] == "draft"
    assert proposal["available"] is False
    assert proposal["runnable"] is False


@pytest.mark.parametrize("permission", ["unknown_tool", "delegate_task"])
def test_rejects_unknown_or_control_tool_permission(permission):
    payload = _adapter_proposal()
    payload["requested_permissions"] = [permission]

    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


def test_proposed_tool_must_be_new_and_workflow_must_have_steps():
    payload = _adapter_proposal()
    payload["proposed_tool_name"] = "search_gmail"
    with pytest.raises(ValueError, match="already exists"):
        validate_capability_proposal(payload, CANONICAL_TOOLS)

    payload = _workflow_proposal()
    payload["workflow_steps"] = []
    with pytest.raises(ValueError, match="at least one declarative step"):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


@pytest.mark.parametrize("control_name", sorted(CAPABILITY_CONTROL_TOOLS))
def test_rejects_every_control_tool_as_new_tool_permission_or_workflow_step(control_name):
    canonical = CANONICAL_TOOLS | {control_name}

    payload = _adapter_proposal()
    payload["proposed_tool_name"] = control_name
    with pytest.raises(ValueError, match="control tool"):
        validate_capability_proposal(payload, canonical)

    payload = _adapter_proposal()
    payload["requested_permissions"] = [control_name]
    with pytest.raises(ValueError, match="control tools"):
        validate_capability_proposal(payload, canonical)

    payload = _workflow_proposal()
    payload["workflow_steps"][0]["tool_name"] = control_name
    payload["requested_permissions"] = [control_name, "get_balance"]
    with pytest.raises(ValueError, match="control tool"):
        validate_capability_proposal(payload, canonical)


def test_rejects_history_lookup_as_a_proposed_workflow_capability():
    payload = _workflow_proposal()
    payload["requested_permissions"].append("search_session_history")

    with pytest.raises(ValueError):
        validate_capability_proposal(
            payload, CANONICAL_TOOLS | {"search_session_history"}
        )


@pytest.mark.parametrize("tool_name", ["unknown_tool", "task_plan", "search_tools"])
def test_rejects_unknown_or_control_tool_in_workflow_steps(tool_name):
    payload = _workflow_proposal()
    payload["workflow_steps"][0]["tool_name"] = tool_name

    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


def test_rejects_history_lookup_as_a_workflow_step():
    payload = _workflow_proposal()
    payload["workflow_steps"][0]["tool_name"] = "search_session_history"

    with pytest.raises(ValueError, match="control tool"):
        validate_capability_proposal(
            payload, CANONICAL_TOOLS | {"search_session_history"}
        )


def test_workflow_permissions_must_cover_every_referenced_step_tool():
    payload = _workflow_proposal()
    payload["requested_permissions"] = ["search_gmail"]

    with pytest.raises(ValueError, match="include every workflow step tool"):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


@pytest.mark.parametrize("field", ["code", "source", "implementation", "python"])
def test_rejects_executable_or_code_payload_fields(field):
    payload = _adapter_proposal()
    payload[field] = "print('must not execute')"

    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


def test_rejects_nested_executable_payload_fields():
    payload = _workflow_proposal()
    payload["workflow_steps"][0]["implementation"] = {"language": "python", "source": "pass"}

    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


def test_rejects_unrecognized_top_level_and_step_keys():
    payload = _adapter_proposal()
    payload["operator_approved"] = True
    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)

    payload = _workflow_proposal()
    payload["workflow_steps"][0]["retry_policy"] = "always"
    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


def test_rejects_deep_and_oversized_schemas():
    payload = _workflow_proposal()
    schema = {"type": "string"}
    for _ in range(20):
        schema = {"type": "array", "items": schema}
    payload["workflow_steps"][0]["arguments_schema"] = {
        "type": "object",
        "properties": {"nested": schema},
        "additionalProperties": False,
    }
    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


def test_tool_schema_limits_are_also_enforced_by_the_validator():
    payload = _adapter_proposal()
    payload["title"] = "x"
    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)

    payload = _adapter_proposal()
    payload["proposed_tool_name"] = "x"
    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)

    payload = _adapter_proposal()
    permissions = [f"tool_{index:02d}" for index in range(21)]
    payload["requested_permissions"] = permissions
    with pytest.raises(ValueError, match="bounded list"):
        validate_capability_proposal(payload, CANONICAL_TOOLS | set(permissions))

    payload = _workflow_proposal()
    payload["workflow_steps"][0]["arguments_schema"] = {
        "type": "object",
        "properties": {f"field_{index}": {"type": "string"} for index in range(100)},
        "additionalProperties": False,
    }
    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


@pytest.mark.parametrize("checks", [[], ["  "], [{"id": "", "description": "check"}], [{"id": "x"}]])
def test_rejects_malformed_or_empty_acceptance_checks(checks):
    payload = _adapter_proposal()
    payload["acceptance_checks"] = checks

    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_json_values(bad_value):
    payload = _adapter_proposal()
    payload["proposed_parameters_schema"]["properties"]["order_id"]["enum"] = [bad_value]

    with pytest.raises(ValueError):
        validate_capability_proposal(payload, CANONICAL_TOOLS)


def test_digest_is_stable_across_mapping_key_order():
    payload = _adapter_proposal()
    reordered = dict(reversed(list(payload.items())))
    reordered["proposed_parameters_schema"] = dict(
        reversed(list(payload["proposed_parameters_schema"].items()))
    )

    first = validate_capability_proposal(payload, CANONICAL_TOOLS)
    second = validate_capability_proposal(reordered, CANONICAL_TOOLS)

    assert first["digest"] == second["digest"]


@pytest.mark.parametrize("change", [
    lambda payload: payload.update(purpose="A different purpose."),
    lambda payload: payload["requested_permissions"].append("get_balance"),
    lambda payload: payload["acceptance_checks"].__setitem__(0, "A changed acceptance check."),
    lambda payload: payload["proposed_parameters_schema"]["properties"].update(currency={"type": "string"}),
])
def test_digest_changes_when_proposal_contract_changes(change):
    original = _adapter_proposal()
    changed = copy.deepcopy(original)
    change(changed)

    assert validate_capability_proposal(original, CANONICAL_TOOLS)["digest"] != \
        validate_capability_proposal(changed, CANONICAL_TOOLS)["digest"]
