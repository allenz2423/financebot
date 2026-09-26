import dataclasses

import pytest

from src.services.workflow_registry import (
    DuplicateWorkflowError,
    WorkflowDefinition,
    WorkflowRegistry,
    WorkflowStep,
    built_in_workflows,
)


def _workflow(*, workflow_id="mail.triage", version="1.0.0", tools=("search_mail", "read_mail"), steps=None):
    if steps is None:
        steps = tuple(
            WorkflowStep(name, f"Perform {name}.", f"Verify {name} completed.")
            for name in tools
        )
    return WorkflowDefinition(
        workflow_id=workflow_id,
        version=version,
        trigger_hints=("summarize recent email", "triage inbox"),
        required_tools=tuple(tools),
        steps=tuple(steps),
        guardrails=("Require normal authorization.",),
        result_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )


def test_registration_validates_tool_names_and_rejects_unknown_tools():
    registry = WorkflowRegistry({"search_mail", "read_mail"})
    registered = registry.register(_workflow())
    assert registered.workflow_id == "mail.triage"

    with pytest.raises(ValueError, match="unknown tools"):
        WorkflowRegistry({"search_mail"}, (_workflow(),))

    control_workflow = _workflow(
        tools=("task_plan", "read_mail"),
        steps=(
            WorkflowStep("task_plan", "Create a task plan.", "Plan is persisted."),
            WorkflowStep("read_mail", "Read selected mail.", "Message is available."),
        ),
    )
    with pytest.raises(ValueError, match="control tools"):
        WorkflowRegistry({"task_plan", "read_mail"}, (control_workflow,))


@pytest.mark.parametrize("control_tool", [
    "await_user", "delegate_task", "enable_reasoning", "end_turn",
    "explore_domain", "load_tool_schemas", "search_tools", "task_cancel",
    "task_list", "task_plan",
])
def test_registration_rejects_every_plan_control_tool(control_tool):
    definition = _workflow(
        tools=(control_tool, "read_mail"),
        steps=(
            WorkflowStep(control_tool, "Run control action.", "Control result exists."),
            WorkflowStep("read_mail", "Read message.", "Message is available."),
        ),
    )
    with pytest.raises(ValueError, match="control tools"):
        WorkflowRegistry({control_tool, "read_mail"}, (definition,))


def test_registration_rejects_duplicate_workflow_id_and_version():
    definition = _workflow()
    with pytest.raises(DuplicateWorkflowError, match="mail.triage@1.0.0"):
        WorkflowRegistry({"search_mail", "read_mail"}, (definition, definition))


@pytest.mark.parametrize("kwargs", [
    {"workflow_id": "Not Valid"},
    {"version": "latest"},
    {"trigger_hints": ()},
    {"guardrails": ()},
    {"steps": (WorkflowStep("search_mail", "Only one step", "Done"),)},
    {"required_tools": ("read_mail", "search_mail")},
])
def test_definition_rejects_malformed_identity_or_contract(kwargs):
    values = {
        "workflow_id": "mail.triage",
        "version": "1.0.0",
        "trigger_hints": ("triage email",),
        "required_tools": ("search_mail", "read_mail"),
        "steps": (
            WorkflowStep("search_mail", "Search", "IDs returned"),
            WorkflowStep("read_mail", "Read", "Bodies returned"),
        ),
        "guardrails": ("Require normal authorization.",),
        "result_schema": {
            "type": "object", "properties": {"summary": {"type": "string"}},
            "required": ["summary"], "additionalProperties": False,
        },
    }
    values.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        WorkflowDefinition(**values)


@pytest.mark.parametrize("schema", [
    {"type": "array", "items": {"type": "string"}},
    {"type": "object", "properties": {}, "additionalProperties": False},
    {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["missing"], "additionalProperties": False},
    {"type": "object", "properties": {"x": {"type": "string", "$ref": "https://example.invalid/schema"}}, "additionalProperties": False},
    {"type": "object", "properties": {"x": {"type": "array"}}, "additionalProperties": False},
    {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True},
])
def test_result_schema_is_a_constrained_json_object(schema):
    definition = _workflow()
    with pytest.raises((ValueError, TypeError)):
        dataclasses.replace(definition, result_schema=schema)


@pytest.mark.parametrize(("kind", "value"), [
    ("object", []),
    ("array", {}),
    ("string", 1),
    ("number", True),
    ("number", float("inf")),
    ("integer", True),
    ("integer", 1.5),
    ("boolean", 0),
    ("null", "null"),
])
def test_enum_values_must_match_their_declared_json_type(kind, value):
    definition = _workflow()
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "array",
                "items": {"type": kind, "enum": [value]},
            },
        },
        "additionalProperties": False,
    }
    with pytest.raises(ValueError, match=r"enum.*(must be|JSON values)"):
        dataclasses.replace(definition, result_schema=schema)


def test_valid_enums_use_json_types_and_recurse_through_step_schemas():
    definition = _workflow(steps=(
        WorkflowStep("search_mail", "Search", "IDs", {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "enum": [1, 2.0]},
                "include_read": {"type": "boolean", "enum": [False, True]},
                "query": {"type": "string", "enum": ["inbox", "sent"]},
            },
            "required": ["count", "include_read", "query"],
            "additionalProperties": False,
        }),
        WorkflowStep("read_mail", "Read", "Bodies"),
    ))
    definition = dataclasses.replace(definition, result_schema={
        "type": "object",
        "properties": {
            "score": {"type": "number", "enum": [1, 1.5]},
            "optional": {"type": "null", "enum": [None]},
            "tags": {"type": "array", "items": {"type": "string", "enum": ["new"]}},
        },
        "additionalProperties": False,
    })
    registry = WorkflowRegistry({"search_mail", "read_mail"}, (definition,))
    assert registry.validate_step_arguments("mail.triage", "1.0.0", 0, {
        "count": 2.0, "include_read": False, "query": "inbox",
    })


def test_required_tools_are_derived_from_unique_step_tools_when_omitted():
    definition = _workflow(
        tools=(),
        steps=(
            WorkflowStep("search_mail", "Search", "IDs"),
            WorkflowStep("search_mail", "Search again", "More IDs"),
            WorkflowStep("read_mail", "Read", "Bodies"),
        ),
    )
    assert definition.required_tools == ("search_mail", "read_mail")
    WorkflowRegistry({"search_mail", "read_mail"}, (definition,))


def test_digest_is_stable_and_covers_ordered_steps():
    definition = _workflow()
    registry = WorkflowRegistry({"search_mail", "read_mail"}, (definition,))
    first = registry.digest("mail.triage", "1.0.0")
    rebuilt = WorkflowRegistry({"read_mail", "search_mail"}, (_workflow(),))
    assert first == rebuilt.digest("mail.triage", "1.0.0")
    assert len(first) == 64

    reordered = _workflow(steps=(
        WorkflowStep("read_mail", "Read first", "Bodies"),
        WorkflowStep("search_mail", "Search second", "IDs"),
    ), tools=("read_mail", "search_mail"))
    assert first != WorkflowRegistry({"search_mail", "read_mail"}, (reordered,)).digest("mail.triage", "1.0.0")


def test_digest_changes_when_step_arguments_schema_changes():
    base = _workflow(steps=(
        WorkflowStep("search_mail", "Search", "IDs", {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        }),
        WorkflowStep("read_mail", "Read", "Bodies"),
    ))
    changed = _workflow(steps=(
        WorkflowStep("search_mail", "Search", "IDs", {
            "type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"], "additionalProperties": False,
        }),
        WorkflowStep("read_mail", "Read", "Bodies"),
    ))
    assert WorkflowRegistry({"search_mail", "read_mail"}, (base,)).digest("mail.triage", "1.0.0") != \
        WorkflowRegistry({"search_mail", "read_mail"}, (changed,)).digest("mail.triage", "1.0.0")


def test_step_argument_validation_is_closed_and_type_strict():
    definition = _workflow(steps=(
        WorkflowStep("search_mail", "Search", "IDs", {
            "type": "object",
            "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            "required": ["query"], "additionalProperties": False,
        }),
        WorkflowStep("read_mail", "Read", "Bodies", {
            "type": "object", "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"], "additionalProperties": False,
        }),
    ))
    registry = WorkflowRegistry({"search_mail", "read_mail"}, (definition,))
    assert registry.validate_arguments("mail.triage", "1.0.0", 0, {
        "query": "newer_than:1d", "max_results": 10,
    }) is True
    assert registry.validate_step_arguments("mail.triage", "1.0.0", 1, {"message_id": "m-1"}) is True

    for invalid in (
        {"query": "inbox", "unexpected": True},
        {"max_results": 10},
        {"query": 42},
        {"query": "inbox", "max_results": True},
    ):
        with pytest.raises(ValueError):
            registry.validate_step_arguments("mail.triage", "1.0.0", 0, invalid)


def test_step_argument_schema_must_be_closed_json_object():
    for schema in (
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "properties": {"x": {"type": "string"}}},
        {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True},
    ):
        with pytest.raises((ValueError, TypeError)):
            WorkflowStep("search_mail", "Search", "IDs", schema)


def test_suggestions_are_candidates_only_and_plan_projection_preserves_order():
    registry = WorkflowRegistry({"search_mail", "read_mail"}, (_workflow(),))
    candidates = registry.suggest("Please triage my inbox and summarize recent email")
    assert [item.workflow_id for item in candidates] == ["mail.triage"]
    assert registry.suggest("transfer money to savings") == ()
    assert candidates[0].summary() == {
        "workflow_id": "mail.triage", "version": "1.0.0",
        "summary": "summarize recent email", "tool_names": ["search_mail", "read_mail"],
    }
    candidate_dict = candidates[0].to_dict()
    assert candidate_dict["steps"][0]["arguments_schema"] == {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    assert isinstance(candidate_dict, dict)
    assert registry.suggest("transfer money to savings") == ()
    assert registry.plan_steps("mail.triage", "1.0.0") == [
        {"tool_name": "search_mail", "description": "Perform search_mail.", "completion_criteria": "Verify search_mail completed."},
        {"tool_name": "read_mail", "description": "Perform read_mail.", "completion_criteria": "Verify read_mail completed."},
    ]


def test_get_requires_exact_version_and_versions_are_sorted():
    one = _workflow(version="1.0.0")
    two = _workflow(version="2.0.0")
    registry = WorkflowRegistry({"search_mail", "read_mail"}, (two, one))
    assert registry.get("mail.triage", "1.0.0") is one
    assert registry.versions("mail.triage") == ("1.0.0", "2.0.0")
    with pytest.raises(KeyError, match="mail.triage@3.0.0"):
        registry.get("mail.triage", "3.0.0")


def test_builtin_catalog_is_only_loaded_after_injected_tool_validation():
    names = {"search_gmail", "read_gmail_message", "monitor_create_natural_rule", "monitor_list_rules"}
    registry = built_in_workflows(names)
    assert len(registry.suggest("check new email")) == 1
    assert registry.get("gmail.triage", "1.0.0").required_tools == (
        "search_gmail", "read_gmail_message"
    )
    assert "Never replay an ambiguous create" in " ".join(
        registry.get("monitor.create_readback", "1.0.0").guardrails
    )
    monitor = registry.get("monitor.create_readback", "1.0.0")
    assert [step.tool_name for step in monitor.steps] == [
        "monitor_create_natural_rule", "monitor_list_rules",
    ]
    assert registry.validate_step_arguments(
        "monitor.create_readback", "1.0.0", 0,
        {"instruction": "alert me if dining exceeds $200 this week"},
    )
    with pytest.raises(ValueError):
        registry.validate_step_arguments("monitor.create_readback", "1.0.0", 0, {"instruction": "rule", "rule_id": 5})
    with pytest.raises(ValueError, match="unknown tools"):
        built_in_workflows({"search_gmail", "read_gmail_message", "monitor_add_rule", "monitor_list_rules"})
    with pytest.raises(ValueError, match="unknown tools"):
        built_in_workflows({"search_gmail"})


def test_definition_and_nested_result_schema_are_immutable():
    definition = _workflow()
    with pytest.raises(dataclasses.FrozenInstanceError):
        definition.version = "2.0.0"
    with pytest.raises(TypeError):
        definition.result_schema["properties"]["new"] = {"type": "string"}
    with pytest.raises(TypeError):
        definition.steps[0].arguments_schema["properties"]["new"] = {"type": "string"}
