import pytest

from src.services.result_contracts import ResultContract
from src.services.tool_registry import (
    DuplicateToolError,
    ToolDefinition,
    ToolRegistry,
    normalize_tool_call,
)


def _definition(handler=lambda **arguments: arguments):
    return ToolDefinition(
        name="lookup_balance",
        description="Look up a user's balance.",
        schema={
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
        handler=handler,
        toolset="finance",
        side_effect="read",
        risk="low",
        user_scope="user",
        result_contract=ResultContract(
            "lookup_balance", ("balance", "currency"), "read"
        ),
    )


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry([_definition()])

    with pytest.raises(DuplicateToolError):
        registry.register(_definition())


def test_schema_generation_lists_function_and_delilah_metadata():
    registry = ToolRegistry([_definition()])

    schema = registry.schemas()[0]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "lookup_balance"
    assert schema["function"]["parameters"]["required"] == ["account_id"]
    assert schema["x-delilah"]["toolset"] == "finance"
    assert schema["x-delilah"]["result_contract"]["fact_keys"] == [
        "balance",
        "currency",
    ]
    assert registry.lookup("lookup_balance") is registry.get("lookup_balance")


def test_dispatch_normalizes_provider_arguments_and_returns_bounded_envelope():
    registry = ToolRegistry([
        _definition(lambda **arguments: {
            "balance": 125,
            "currency": "USD",
            "internal_detail": "not part of the contract",
            "received": arguments["account_id"],
        })
    ])

    result = registry.dispatch({
        "id": "provider-call-1",
        "function": {
            "name": "lookup_balance",
            "arguments": '{"account_id":"checking"}',
        },
    }, receipt_id="receipt-1")

    assert result.ok is True
    assert result.complete is True
    assert result.tool_name == "lookup_balance"
    assert result.call_id == "provider-call-1"
    assert result.receipt_id == "receipt-1"
    assert result.facts == {"balance": 125, "currency": "USD"}


def test_dispatch_rejects_invalid_arguments_before_handler_runs():
    called = False

    def handler(**_arguments):
        nonlocal called
        called = True
        return {"balance": 1}

    result = ToolRegistry([_definition(handler)]).dispatch(
        "lookup_balance", {"account_id": 3}
    )

    assert result.ok is False
    assert "type string" in result.error
    assert called is False


def test_normalize_tool_call_accepts_direct_json_and_generates_an_id():
    call = normalize_tool_call("lookup_balance", '{"account_id":"savings"}')

    assert call.name == "lookup_balance"
    assert call.arguments == {"account_id": "savings"}
    assert call.call_id.startswith("call_")


def test_dispatch_fails_closed_for_empty_handler_results():
    registry = ToolRegistry([_definition(lambda **_arguments: None)])

    result = registry.dispatch("lookup_balance", {"account_id": "checking"})

    assert result.ok is False
    assert result.status == "failed"


def test_dispatch_fails_closed_for_legacy_error_text():
    registry = ToolRegistry([
        _definition(lambda **_arguments: "ERROR: provider rejected the request")
    ])

    result = registry.dispatch("lookup_balance", {"account_id": "checking"})

    assert result.ok is False
    assert result.status == "failed"


def test_dispatch_preserves_zero_as_a_valid_scalar_result():
    definition = ToolDefinition(
        name="zero_value",
        description="A valid zero result.",
        schema={"type": "object", "properties": {}},
        handler=lambda **_arguments: 0,
    )

    result = ToolRegistry([definition]).dispatch("zero_value", {})

    assert result.ok is True
    assert result.summary == "zero_value completed."
