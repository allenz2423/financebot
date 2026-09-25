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
