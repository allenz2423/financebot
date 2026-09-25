"""Small, dependency-free tool-call contract checks.

Tool arguments are model output and must be treated as untrusted input.  This
implements the JSON-Schema subset used by Delilah's built-in declarations and
returns human-readable errors for the next model turn, following Qwen Code's
validate-before-execute pattern without adding a new runtime dependency.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate(schema: Any, value: Any, path: str) -> str | None:
    if not isinstance(schema, dict):
        return None

    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']!r}"

    if "const" in schema and value != schema["const"]:
        return f"{path} must equal {schema['const']!r}"

    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_type_matches(value, item) for item in expected):
            return f"{path} must have type one of {expected!r}"
    elif isinstance(expected, str) and not _type_matches(value, expected):
        return f"{path} must have type {expected}"

    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [name for name in required if name not in value]
            if missing:
                return f"missing required parameter(s): {', '.join(map(str, missing))}"
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, child in properties.items():
                if name in value:
                    error = _validate(child, value[name], f"{path}.{name}")
                    if error:
                        return error
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            extras = sorted(set(value) - set(properties))
            if extras:
                return f"unexpected parameter(s): {', '.join(extras)}"

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _validate(item_schema, item, f"{path}[{index}]")
                if error:
                    return error
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            return f"{path} must contain at least {schema['minItems']} item(s)"
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            return f"{path} must contain at most {schema['maxItems']} item(s)"

    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            return f"{path} must be at least {schema['minLength']} characters"
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            return f"{path} must be at most {schema['maxLength']} characters"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            return f"{path} must be >= {schema['minimum']}"
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            return f"{path} must be <= {schema['maximum']}"

    return None


def validate_tool_arguments(tool_schema: dict[str, Any] | None, args: Any) -> str | None:
    """Return a concise validation error, or ``None`` when args are valid."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    if not tool_schema:
        return None
    parameters = tool_schema.get("function", {}).get("parameters", {})
    return _validate(parameters, args, "arguments")


def tool_call_repeat_key(tool_name: str, args: Any) -> str:
    """Stable identity for a tool call, independent of object key ordering."""
    canonical = json.dumps(
        {"name": tool_name, "arguments": args},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_tool_call_id(
    tool_call: dict[str, Any],
    *,
    origin: str = "native",
    turn_id: str = "turn",
    round_id: int = 0,
    ordinal: int = 0,
) -> dict[str, Any]:
    """Return a tool call with a Delilah-owned ID when the provider omitted one.

    Provider IDs are preserved when present.  Fallback calls and malformed
    provider calls receive an opaque ID before they can enter conversation
    history or execution.  The random suffix prevents collisions across
    retries while the stable prefix makes logs easy to correlate.
    """

    normalized = dict(tool_call or {})
    existing = str(normalized.get("id") or "").strip()
    if existing:
        return normalized

    safe_origin = "fallback" if str(origin).casefold() == "fallback" else "native"
    turn_digest = hashlib.sha256(str(turn_id).encode("utf-8")).hexdigest()[:10]
    normalized["id"] = (
        f"delilah_{safe_origin}_{turn_digest}_{round_id}_{ordinal}_"
        f"{uuid.uuid4().hex}"
    )
    return normalized


__all__ = ["ensure_tool_call_id", "tool_call_repeat_key", "validate_tool_arguments"]
