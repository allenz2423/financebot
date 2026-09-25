from src.services.tool_contract import (
    ensure_tool_call_id,
    tool_call_repeat_key,
    validate_tool_arguments,
)


def _schema(required=None):
    return {
        "function": {
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": required or ["query"],
            }
        }
    }


def test_valid_arguments_pass():
    assert validate_tool_arguments(_schema(), {"query": "internships", "limit": 5}) is None


def test_missing_and_wrong_arguments_are_rejected_before_execution():
    assert "missing required" in validate_tool_arguments(_schema(), {})
    assert "type integer" in validate_tool_arguments(_schema(), {"query": "x", "limit": "5"})
    assert ">=" in validate_tool_arguments(_schema(), {"query": "x", "limit": 0})


def test_repeat_key_is_stable_across_object_key_order():
    assert tool_call_repeat_key("tool", {"a": 1, "b": 2}) == tool_call_repeat_key(
        "tool", {"b": 2, "a": 1}
    )


def test_missing_tool_call_id_gets_delilah_owned_id_without_changing_payload():
    call = {"type": "function", "function": {"name": "search_gmail", "arguments": {}}}
    normalized = ensure_tool_call_id(
        call,
        origin="fallback",
        turn_id="user-1",
        round_id=3,
        ordinal=2,
    )
    assert normalized["id"].startswith("delilah_fallback_")
    assert "_3_2_" in normalized["id"]
    assert normalized["function"] == call["function"]
    assert "id" not in call


def test_existing_provider_id_is_preserved():
    call = {"id": "provider-call-1", "function": {"name": "search_web"}}
    assert ensure_tool_call_id(call, origin="fallback")["id"] == "provider-call-1"
