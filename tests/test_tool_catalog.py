import ast
from pathlib import Path

import pytest

from src.services.tool_catalog import ToolCatalogError, validate_tool_catalog


def _schema(name, parameters=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


def test_catalog_rejects_duplicate_names():
    with pytest.raises(ToolCatalogError, match="duplicate"):
        validate_tool_catalog([_schema("one"), _schema("one")])


def test_catalog_requires_registered_handlers_to_be_advertised():
    with pytest.raises(ToolCatalogError, match="not advertised"):
        validate_tool_catalog([_schema("one")], registered_names={"one", "two"})


def test_catalog_checks_expected_parity_and_returns_names():
    assert validate_tool_catalog(
        [_schema("one"), _schema("two")], expected_names={"one", "two"}
    ) == {"one", "two"}


def test_catalog_rejects_malformed_schema():
    with pytest.raises(ToolCatalogError, match="parameters"):
        validate_tool_catalog([_schema("one", parameters=None) | {
            "function": {"name": "one"}
        }])


def test_live_delilah_catalog_has_unique_schema_and_registered_advisor_tools():
    import src.services.llm as llm

    names = validate_tool_catalog(
        llm.BOT_TOOLS_SCHEMA,
        expected_names=llm.EXPECTED_TOOL_NAMES,
        registered_names=llm.ADVISOR_TOOLS_DISPATCH,
    )
    assert names == llm.KNOWN_TOOLS
    assert len(llm.ADVISOR_TOOL_REGISTRY) == len(llm.ADVISOR_TOOLS_DISPATCH)


def test_await_user_is_registered_as_a_bounded_durable_question_tool():
    import src.services.llm as llm

    assert "await_user" in llm.KNOWN_TOOLS
    schema = next(
        tool["function"] for tool in llm.BOT_TOOLS_SCHEMA
        if tool["function"]["name"] == "await_user"
    )
    assert schema["parameters"]["required"] == ["question", "choices"]
    assert schema["parameters"]["properties"]["choices"]["minItems"] == 2
    assert schema["parameters"]["properties"]["choices"]["maxItems"] == 8
    assert "only tool call" in schema["description"]


def test_task_list_is_registered_as_read_only_durable_progress_lookup():
    import src.services.llm as llm

    assert "task_list" in llm.KNOWN_TOOLS
    assert "task_list" in llm.EXPECTED_TOOL_NAMES
    schema = next(
        tool["function"] for tool in llm.BOT_TOOLS_SCHEMA
        if tool["function"]["name"] == "task_list"
    )
    assert schema["parameters"]["properties"]["limit"]["maximum"] == 20
    assert "does not resume tasks" in schema["description"]
    assert "prove that an external action happened" in schema["description"]
    assert "task_list" in llm._INPUT_ONLY_REPLY_ALLOWED_TOOLS
    assert "task_list" not in llm.MUTATION_TOOLS


def test_task_plan_is_bounded_and_multi_action_policy_is_deterministic():
    import src.services.llm as llm

    schema = next(
        tool["function"] for tool in llm.BOT_TOOLS_SCHEMA
        if tool["function"]["name"] == "task_plan"
    )
    steps = schema["parameters"]["properties"]["steps"]
    assert steps["minItems"] == 2
    assert steps["maxItems"] == 20
    assert {"tool_name", "description", "completion_criteria"} <= set(
        steps["items"]["required"]
    )
    assert "task_plan" in llm.EXPECTED_TOOL_NAMES
    parameters = schema["parameters"]
    assert {
        "workflow_id", "workflow_version", "workflow_digest", "steps",
    } <= set(parameters["properties"])
    assert parameters["additionalProperties"] is False
    assert "never both" in schema["description"]
    assert "pins the exact version/digest" in schema["description"]
    assert llm._requires_durable_plan("1. Search\n2. Read\n3. Summarize", set())
    assert llm._requires_durable_plan("Please create a PDF report", set())


def test_task_plan_selects_either_manual_steps_or_an_exact_workflow_pin():
    import src.services.llm as llm

    workflow_id, version = "gmail.triage", "1.0.0"
    digest = llm.WORKFLOW_REGISTRY.digest(workflow_id, version)
    selected_steps, pin = llm._resolve_task_plan_selection(
        {
            "workflow_id": workflow_id,
            "workflow_version": version,
            "workflow_digest": digest,
        },
        llm.WORKFLOW_REGISTRY,
    )
    assert [step["tool_name"] for step in selected_steps] == [
        "search_gmail", "read_gmail_message",
    ]
    assert pin == {
        "workflow_id": workflow_id, "version": version, "digest": digest,
    }

    manual_steps = [
        {"tool_name": "search_gmail", "description": "Search", "completion_criteria": "IDs"},
        {"tool_name": "read_gmail_message", "description": "Read", "completion_criteria": "Bodies"},
    ]
    selected_manual, manual_pin = llm._resolve_task_plan_selection(
        {"steps": manual_steps}, llm.WORKFLOW_REGISTRY
    )
    assert selected_manual == manual_steps
    assert manual_pin is None

    with pytest.raises(ValueError, match="requires only"):
        llm._resolve_task_plan_selection(
            {"steps": manual_steps, "workflow_id": workflow_id},
            llm.WORKFLOW_REGISTRY,
        )
    with pytest.raises(ValueError, match="digest"):
        llm._resolve_task_plan_selection(
            {
                "workflow_id": workflow_id,
                "workflow_version": version,
                "workflow_digest": "0" * 64,
            },
            llm.WORKFLOW_REGISTRY,
        )

    # Even a registry injected after startup cannot turn control tools into
    # executable workflow steps.
    from src.services.workflow_registry import WorkflowDefinition, WorkflowRegistry, WorkflowStep
    control = WorkflowDefinition(
        workflow_id="test.control", version="1.0.0",
        trigger_hints=("test control selection",),
        steps=(
            WorkflowStep("explore_domain", "Explore", "Discovery completes."),
            WorkflowStep("search_gmail", "Search", "Messages are found."),
        ),
        guardrails=("Normal authorization still applies.",),
        result_schema={
            "type": "object", "properties": {"summary": {"type": "string"}},
            "required": ["summary"], "additionalProperties": False,
        },
    )
    injected_registry = WorkflowRegistry(
        {"explore_domain", "search_gmail"},
    )
    # Bypass normal registration to simulate stale/tampered in-memory state;
    # selection must retain its own action-tool boundary.
    injected_registry._definitions[(control.workflow_id, control.version)] = control
    with pytest.raises(ValueError, match="plan-control tools"):
        llm._resolve_task_plan_selection(
            {
                "workflow_id": control.workflow_id,
                "workflow_version": control.version,
                "workflow_digest": injected_registry.digest(control.workflow_id, control.version),
            },
            injected_registry,
        )
    assert not llm._requires_durable_plan("What is my current balance?", set())
    assert llm._tool_requires_durable_plan("monitor_add_rule")
    assert llm._tool_requires_durable_plan("set_savings_goal")
    assert llm._tool_requires_durable_plan("monitor_ack_alert")
    assert not llm._tool_requires_durable_plan("get_financial_dashboard")
    assert llm._tool_call_requires_durable_plan({
        "function": {"name": "fetch_webpage", "arguments": {"save_only": True}}
    })
    assert llm._tool_call_requires_durable_plan({
        "function": {"name": "fetch_webpage", "arguments": '{"save_only": true}'}
    })
    assert llm._durable_plan_batch_error(
        ["search_gmail"], required=True, has_plan=False, audit_active=False
    )
    assert llm._durable_plan_batch_error(
        ["task_plan", "search_gmail"], required=False, has_plan=False,
        audit_active=False,
    )
    assert llm._durable_plan_batch_error(
        ["task_plan"], required=True, has_plan=False, audit_active=False
    ) is None
    assert llm._durable_plan_batch_error(
        ["search_gmail"], required=True, has_plan=True, audit_active=False
    ) is None
    assert llm._durable_plan_batch_error(
        ["search_gmail", "set_savings_goal"], required=False,
        has_plan=False, audit_active=False,
    )
    assert llm._durable_plan_batch_error(
        ["search_gmail", "set_savings_goal"], required=False,
        has_plan=False, audit_active=True,
    ) is None
    llm._validate_plan_tool_names([{"tool_name": "search_gmail"}])
    with pytest.raises(ValueError, match="non-action tool"):
        llm._validate_plan_tool_names([{"tool_name": "task_list"}])


def test_every_advertised_tool_has_a_dispatch_branch_or_registry_handler():
    import src.services.llm as llm

    source_path = Path(llm.__file__)
    tree = ast.parse(source_path.read_text())
    explicit_routes = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "func_name":
            continue
        for operator, comparator in zip(node.ops, node.comparators):
            if isinstance(operator, ast.Eq) and isinstance(comparator, ast.Constant):
                if isinstance(comparator.value, str):
                    explicit_routes.add(comparator.value)
            elif isinstance(operator, ast.In) and isinstance(
                comparator, (ast.Tuple, ast.List, ast.Set)
            ):
                explicit_routes.update(
                    item.value
                    for item in comparator.elts
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                )

    routed = explicit_routes | set(llm.ADVISOR_TOOLS_DISPATCH)
    assert llm.KNOWN_TOOLS - routed == set()
