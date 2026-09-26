"""Acceptance tests for registry-owned semantic tool discovery.

The registry invokes retrieval, intersects candidates with controller scope,
and returns bounded copies of canonical schemas. Discovery is not dispatch
authorization or a source of approval.
"""

from copy import deepcopy

import pytest

import src.services.llm as llm
from src.services.tool_registry import (
    ToolDefinition,
    ToolRegistry,
    search_tool_definitions,
)


def test_search_tools_is_model_callable_control_tool_not_authority():
    search_schema = next(
        schema for schema in llm.BOT_TOOLS_SCHEMA
        if schema["function"]["name"] == "search_tools"
    )
    parameters = search_schema["function"]["parameters"]
    assert parameters["required"] == ["query"]
    assert parameters["additionalProperties"] is False
    assert "does not grant approval or permission" in search_schema["function"]["description"]
    assert "search_tools" in llm.EXPECTED_TOOL_NAMES
    assert "search_tools" in llm._PLAN_CONTROL_TOOLS


def test_search_tools_returns_candidate_workflows_without_granting_or_mutating_scope():
    allowed = {"search_gmail", "read_gmail_message"}

    candidates = llm._workflow_candidates_for_discovery(
        "check new email",
        "summarize recent Gmail",
        allowed,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["workflow_id"] == "gmail.triage"
    assert candidate["version"] == "1.0.0"
    assert len(candidate["digest"]) == 64
    assert candidate["required_tools"] == ["search_gmail", "read_gmail_message"]
    assert [step["tool_name"] for step in candidate["steps"]] == candidate["required_tools"]
    assert candidate["guardrails"]
    assert candidate["result_schema"]["type"] == "object"
    assert allowed == {"search_gmail", "read_gmail_message"}

    # A workflow requiring a hidden tool is not suggested into this scope.
    assert llm._workflow_candidates_for_discovery(
        "check new email", "", {"search_gmail"}
    ) == []


def test_runtime_effect_labeler_never_calls_unclassified_tools_read_only():
    registry, _calls = _registry()

    assert llm._tool_discovery_side_effect_label(
        registry.lookup("opaque_operation"), "read"
    ) == "unknown"
    assert llm._tool_discovery_side_effect_label(
        registry.lookup("opaque_operation"), "mutate"
    ) == "unknown"
    assert llm._tool_discovery_side_effect_label(None, "read") == "unknown"
    assert llm._tool_discovery_side_effect_label(None, "mutate") == "mutation"
    assert llm._tool_discovery_side_effect_label(None, "conditional") == "conditional"
    assert llm._tool_discovery_side_effect_label(
        registry.lookup("account_balance"), "mutate"
    ) == "mutation"
    assert llm._tool_discovery_side_effect_label(
        registry.lookup("account_balance"), "conditional"
    ) == "conditional"


@pytest.mark.asyncio
async def test_registry_search_tools_owns_retrieval_and_keeps_scope_authoritative():
    registry, calls = _registry()
    observed = []

    async def ranker(query):
        observed.append(query)
        return {
            "method": "lexical",
            "fallback_reason": "index_not_ready",
            "raw_topk": [
                {"name": "transfer_funds"},
                {"name": "account_balance"},
            ],
        }

    result = await registry.search_tools(
        "read a balance",
        "monthly financial review",
        canonical_catalog_schemas=registry.schemas(),
        allowed_names={"account_balance"},
        ranker=ranker,
        side_effect_labels={"account_balance": "read_only"},
        availability_reasons={
            "account_balance": {
                "available": None,
                "reason": "Schema is discoverable; live service availability was not checked.",
            }
        },
    )

    assert observed == ["read a balance\nmonthly financial review"]
    assert result["method"] == "lexical"
    assert [tool["name"] for tool in result["tools"]] == ["account_balance"]
    assert result["tools"][0]["availability"] is None
    assert "not checked" in result["tools"][0]["reason"]
    assert calls == []


def _registry(*, handler=None):
    calls = []

    def record(**arguments):
        calls.append(arguments)
        return {"ok": True}

    handler = handler or record
    definitions = [
        ToolDefinition(
            name="account_balance",
            description="Read the current balance for an account.",
            schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "string", "examples": ["checking"]}
                },
                "required": ["account_id"],
                "additionalProperties": False,
                "examples": [{"account_id": "checking"}],
            },
            handler=handler,
            toolset="finance",
            side_effect="read",
        ),
        ToolDefinition(
            name="transfer_funds",
            description="Move funds between accounts.",
            schema={
                "type": "object",
                "properties": {
                    "from_account": {"type": "string"},
                    "to_account": {"type": "string"},
                    "amount": {"type": "number"},
                },
                "required": ["from_account", "to_account", "amount"],
                "additionalProperties": False,
                "examples": [{
                    "from_account": "checking",
                    "to_account": "savings",
                    "amount": 10,
                }],
            },
            handler=handler,
            toolset="finance",
            side_effect="mutate",
        ),
        ToolDefinition(
            name="opaque_operation",
            description="An operation whose effects are not classified.",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler,
            toolset="misc",
            side_effect="future_effect_kind",
        ),
    ]
    return ToolRegistry(definitions), calls


def _search(
    registry,
    query,
    *,
    retrieval_ordered_names,
    authorized_tools,
    availability=None,
    side_effect_labels=None,
    task_context=None,
    limit=5,
):
    return search_tool_definitions(
        query,
        registry.schemas(),
        retrieval_ordered_names,
        authorized_tools,
        availability_reasons=availability,
        side_effect_labels=side_effect_labels,
        task_context=task_context,
        limit=limit,
    )


def test_search_filters_unauthorized_candidates_before_returning_definitions():
    registry, _calls = _registry()

    results = _search(
        registry,
        "move funds between accounts",
        retrieval_ordered_names=["transfer_funds", "account_balance"],
        authorized_tools={"account_balance"},
    )

    assert results
    assert {item["name"] for item in results} == {"account_balance"}
    assert "transfer_funds" not in {item["name"] for item in results}


def test_search_is_bounded_and_has_stable_order_for_equal_relevance():
    registry, _calls = _registry()
    # Candidate ranking belongs to the retriever. The helper must preserve its
    # order, enforce the bound, and be independent of set iteration order.
    results = _search(
        registry,
        "account",
        retrieval_ordered_names=["transfer_funds", "account_balance", "opaque_operation"],
        authorized_tools={"account_balance", "transfer_funds", "opaque_operation"},
        limit=2,
    )
    repeated = _search(
        registry,
        "account",
        retrieval_ordered_names=["transfer_funds", "account_balance", "opaque_operation"],
        authorized_tools={"opaque_operation", "transfer_funds", "account_balance"},
        limit=2,
    )

    assert len(results) <= 2
    assert [item["name"] for item in results] == ["transfer_funds", "account_balance"]
    assert [item["name"] for item in results] == [item["name"] for item in repeated]


def test_search_skips_oversized_schema_without_truncating_it():
    registry, _calls = _registry()
    results = _search(
        registry,
        "balance",
        retrieval_ordered_names=["account_balance"],
        authorized_tools={"account_balance"},
    )
    assert results
    from src.services.tool_registry import search_tool_definitions
    skipped = search_tool_definitions(
        "balance",
        registry.schemas(),
        ["account_balance"],
        {"account_balance"},
        max_schema_chars=256,
    )
    assert skipped == []


def test_search_returns_defensive_schema_copies():
    registry, _calls = _registry()
    before = deepcopy(registry.lookup("account_balance").parameters_schema)

    results = _search(
        registry,
        "balance",
        retrieval_ordered_names=["account_balance"],
        authorized_tools={"account_balance"},
    )
    results[0]["schema"]["function"]["parameters"]["properties"]["account_id"]["type"] = "integer"
    results[0]["schema"]["function"]["parameters"]["examples"][0]["account_id"] = "tampered"

    fresh = _search(
        registry,
        "balance",
        retrieval_ordered_names=["account_balance"],
        authorized_tools={"account_balance"},
    )
    assert registry.lookup("account_balance").parameters_schema == before
    assert fresh[0]["schema"] == registry.schemas()[0]


def test_search_reports_unavailable_tools_with_reason_without_making_them_callable():
    registry, _calls = _registry()

    results = _search(
        registry,
        "current balance",
        retrieval_ordered_names=["account_balance"],
        authorized_tools={"account_balance"},
        availability={
            "account_balance": {"available": False, "reason": "Bank connection is missing."}
        },
    )

    assert len(results) == 1
    assert results[0]["availability"] is False
    assert results[0]["reason"] == "Bank connection is missing."


def test_unknown_effect_classification_fails_closed_in_discovery_metadata():
    registry, _calls = _registry()

    results = _search(
        registry,
        "opaque operation",
        retrieval_ordered_names=["opaque_operation"],
        authorized_tools={"opaque_operation"},
        side_effect_labels={"opaque_operation": "future_effect_kind"},
    )

    assert len(results) == 1
    assert results[0]["side_effect"] == "unknown"
    assert results[0]["side_effect"] not in {"none", "read"}


def test_discovery_preserves_explicit_effect_labels_and_defaults_unknown_to_unknown():
    registry, _calls = _registry()

    results = _search(
        registry,
        "tool",
        retrieval_ordered_names=["account_balance", "transfer_funds", "opaque_operation"],
        authorized_tools={"account_balance", "transfer_funds", "opaque_operation"},
        side_effect_labels={
            "account_balance": "read",
            "transfer_funds": "mutation",
            # Deliberately no label for opaque_operation.
        },
    )

    assert [item["side_effect"] for item in results] == ["read", "mutation", "unknown"]


def test_examples_only_come_from_registered_schema_declarations():
    registry, _calls = _registry()
    registered_schema = registry.lookup("account_balance").parameters_schema

    results = _search(
        registry,
        "balance",
        retrieval_ordered_names=["account_balance"],
        authorized_tools={"account_balance"},
        side_effect_labels={"account_balance": "read"},
    )

    assert results[0]["examples"] == registry.schemas()[0]["function"]["parameters"]["examples"]
    assert results[0]["examples"] != [{"account_id": "invented"}]
    assert registered_schema["examples"] == [{"account_id": "checking"}]

    no_examples = _search(
        registry,
        "opaque operation",
        retrieval_ordered_names=["opaque_operation"],
        authorized_tools={"opaque_operation"},
    )
    assert "examples" not in no_examples[0]


@pytest.mark.parametrize("query", ["", "   ", None, 42, [], {"query": "balance"}])
def test_empty_or_malformed_query_fails_safely(query):
    registry, _calls = _registry()

    with pytest.raises((TypeError, ValueError)):
        _search(
            registry,
            query,
            retrieval_ordered_names=["account_balance", "transfer_funds", "opaque_operation"],
            authorized_tools={"account_balance", "transfer_funds", "opaque_operation"},
        )


def test_discovery_neither_dispatches_tools_nor_mints_authority():
    registry, calls = _registry()

    results = _search(
        registry,
        "transfer funds",
        retrieval_ordered_names=["transfer_funds"],
        authorized_tools={"transfer_funds"},
        side_effect_labels={"transfer_funds": "mutation"},
    )

    assert results[0]["name"] == "transfer_funds"
    assert calls == []
    # A search result is descriptive only; it does not add a grant or alter the
    # explicitly supplied authorization set used by a later search.
    read_only = _search(
        registry,
        "transfer funds",
        retrieval_ordered_names=["transfer_funds", "account_balance"],
        authorized_tools={"account_balance"},
        task_context={"authorized_tools": ["transfer_funds"]},
    )
    assert {item["name"] for item in read_only} == {"account_balance"}
    assert calls == []
