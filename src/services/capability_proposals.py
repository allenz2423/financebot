"""Pure-data validation for draft capability proposals.

Validated proposals are descriptive records only. They are not registered,
approved, runnable, or an authorization grant. This module intentionally has
no runtime, persistence, or service dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


_TOP_LEVEL_FIELDS = frozenset({
    "proposal_id",
    "title",
    "purpose",
    "capability_kind",
    "proposed_tool_name",
    "proposed_tool_description",
    "proposed_parameters_schema",
    "requested_permissions",
    "acceptance_checks",
    "workflow_steps",
})
_REQUIRED_FIELDS = _TOP_LEVEL_FIELDS - {"workflow_steps"}
_SCHEMA_FIELDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "description",
})
_SCHEMA_TYPES = frozenset({
    "object", "array", "string", "number", "integer", "boolean", "null",
})
_CONTROL_TOOLS = frozenset({
    "await_user", "delegate_task", "enable_reasoning", "end_turn",
    "explore_domain", "inspect_task", "load_tool_schemas", "search_session_history", "search_tools",
    "steer_task", "task_cancel", "task_list", "task_plan",
    "draft_capability_proposal", "manage_user_profile",
})
CAPABILITY_CONTROL_TOOLS = _CONTROL_TOOLS


@dataclass
class CapabilityProposalGate:
    """Turn-local gate requiring recurrence and an unambiguous catalog miss."""

    request_recurs: bool
    eligible: bool = False
    discovery_conflicted: bool = False
    search_attempts: int = 0

    def begin_search_attempt(self) -> None:
        """Invalidate old miss evidence before any pre-dispatch denial can occur."""
        if self.search_attempts:
            self.discovery_conflicted = True
        self.search_attempts += 1
        self.eligible = False
    def record_search_result(
        self, *, tools_found: bool, workflows_found: bool, unrestricted: bool,
    ) -> None:
        if tools_found or workflows_found or not unrestricted:
            self.discovery_conflicted = True
        self.eligible = bool(
            self.request_recurs
            and not tools_found
            and not workflows_found
            and unrestricted
            and not self.discovery_conflicted
        )

    def record_discovery_error(self) -> None:
        self.eligible = False
        self.discovery_conflicted = True

    def record_catalog_exploration(self) -> None:
        self.eligible = False
        self.discovery_conflicted = True

    def record_schema_load(self, tool_names: Any) -> None:
        self.eligible = False
        self.discovery_conflicted = True
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_MAX_DEPTH = 6
_MAX_SCHEMA_NODES = 256
_MAX_PROPERTIES = 32
_MAX_ENUM_VALUES = 32
_MAX_ACCEPTANCE_CHECKS = 12
_MAX_WORKFLOW_STEPS = 20
_MAX_REQUESTED_PERMISSIONS = 20
_MAX_LIST_ITEMS = 64
_MAX_STRING_LENGTH = 2048
_MAX_TITLE_LENGTH = 120
_MAX_PURPOSE_LENGTH = 800
_MAX_DESCRIPTION_LENGTH = 500
_MAX_CHECK_LENGTH = 500
_MAX_SCHEMA_DESCRIPTION_LENGTH = 500
_FORBIDDEN_KEYS = frozenset({
    "code", "executable", "execution", "command", "commands", "prompt",
    "prompts", "script", "scripts", "shell", "source", "source_code",
})


@dataclass(frozen=True)
class CapabilityProposal:
    """Normalized, draft-only proposal data."""

    proposal_id: str
    title: str
    purpose: str
    capability_kind: str
    proposed_tool_name: str
    proposed_tool_description: str
    proposed_parameters_schema: dict[str, Any]
    requested_permissions: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    workflow_steps: tuple[dict[str, str], ...]

    def normalized_fields(self) -> dict[str, Any]:
        """Return JSON-compatible normalized fields, excluding derived metadata."""
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "purpose": self.purpose,
            "capability_kind": self.capability_kind,
            "proposed_tool_name": self.proposed_tool_name,
            "proposed_tool_description": self.proposed_tool_description,
            "proposed_parameters_schema": self.proposed_parameters_schema,
            "requested_permissions": list(self.requested_permissions),
            "acceptance_checks": list(self.acceptance_checks),
            "workflow_steps": [dict(step) for step in self.workflow_steps],
        }


def _text(value: Any, field: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        raise ValueError(f"{field} must contain {minimum}–{maximum} characters")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in normalized):
        raise ValueError(f"{field} contains control characters")
    return normalized


def _is_forbidden_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return bool(_FORBIDDEN_KEYS.intersection(normalized.split("_")))


def _check_json_value(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    """Reject non-JSON values, non-finite numbers, excessive nesting, and code keys."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > _MAX_SCHEMA_NODES:
        raise ValueError("proposal data exceeds the node limit")
    if depth > _MAX_DEPTH:
        raise ValueError("proposal data exceeds the maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 4096:
            raise ValueError("proposal data contains an oversized integer")
        if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
            raise ValueError("proposal data contains an oversized string")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("proposal data contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > _MAX_LIST_ITEMS:
            raise ValueError("proposal data contains an oversized list")
        for item in value:
            _check_json_value(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        if len(value) > _MAX_PROPERTIES:
            raise ValueError("proposal data contains an oversized object")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("proposal object keys must be strings")
            if _is_forbidden_key(key):
                raise ValueError(f"forbidden executable/code field: {key}")
            _check_json_value(item, depth=depth + 1, counter=counter)
        return
    raise ValueError(f"proposal contains a non-JSON value: {type(value).__name__}")


def _normalize_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise ValueError("proposed_parameters_schema must be a JSON object")
    counter = [0]

    def visit(node: Any, depth: int) -> dict[str, Any]:
        counter[0] += 1
        if counter[0] > _MAX_SCHEMA_NODES:
            raise ValueError("parameter schema exceeds the node limit")
        if depth > _MAX_DEPTH:
            raise ValueError("parameter schema exceeds the maximum nesting depth")
        if not isinstance(node, dict):
            raise ValueError("each schema node must be an object")
        if len(node) > _MAX_PROPERTIES:
            raise ValueError("schema object has too many fields")
        for key in node:
            if not isinstance(key, str):
                raise ValueError("schema keys must be strings")
            if _is_forbidden_key(key):
                raise ValueError(f"forbidden executable/code field: {key}")
            if key not in _SCHEMA_FIELDS:
                raise ValueError(f"unsupported JSON Schema field: {key}")

        result: dict[str, Any] = {}
        kind = node.get("type")
        if kind not in _SCHEMA_TYPES:
            raise ValueError("schema type must be a supported JSON Schema type")
        result["type"] = kind

        if "description" in node:
            result["description"] = _text(
                node["description"], "schema description", _MAX_SCHEMA_DESCRIPTION_LENGTH
            )
        if "properties" in node:
            properties = node["properties"]
            if not isinstance(properties, dict) or len(properties) > _MAX_PROPERTIES:
                raise ValueError("schema properties must be a bounded object")
            normalized_properties: dict[str, Any] = {}
            for name in sorted(properties):
                _text(name, "schema property name", 128)
                normalized_properties[name] = visit(properties[name], depth + 1)
            result["properties"] = normalized_properties
        if "required" in node:
            required = node["required"]
            if not isinstance(required, list) or len(required) > _MAX_PROPERTIES:
                raise ValueError("schema required must be a bounded list")
            normalized_required = [_text(item, "required property", 128) for item in required]
            if len(set(normalized_required)) != len(normalized_required):
                raise ValueError("schema required contains duplicates")
            properties = result.get("properties", {})
            if any(item not in properties for item in normalized_required):
                raise ValueError("schema required names must exist in properties")
            result["required"] = sorted(normalized_required)
        if "additionalProperties" in node:
            additional = node["additionalProperties"]
            if not isinstance(additional, bool):
                raise ValueError("additionalProperties must be a boolean")
            result["additionalProperties"] = additional
        if "items" in node:
            result["items"] = visit(node["items"], depth + 1)
        if "enum" in node:
            enum = node["enum"]
            if not isinstance(enum, list) or not 1 <= len(enum) <= _MAX_ENUM_VALUES:
                raise ValueError("schema enum must contain 1–32 values")
            _check_json_value(enum)
            if len({json.dumps(item, sort_keys=True, separators=(",", ":")) for item in enum}) != len(enum):
                raise ValueError("schema enum values must be unique")
            result["enum"] = enum

        if kind == "object" and "properties" not in result:
            result["properties"] = {}
        if kind == "array" and "items" not in result:
            raise ValueError("array schemas must define items")
        if kind != "object" and any(key in result for key in ("properties", "required", "additionalProperties")):
            raise ValueError("object-only schema fields require type=object")
        if kind != "array" and "items" in result:
            raise ValueError("items requires type=array")
        return result

    normalized = visit(schema, 0)
    if normalized.get("type") != "object":
        raise ValueError("proposed_parameters_schema root type must be object")
    return normalized


def validate_capability_proposal(
    payload: Mapping[str, Any], canonical_tool_names: Any,
) -> dict[str, Any]:
    """Validate and normalize a draft-only proposal; never grants or activates it.

    ``canonical_tool_names`` is an authoritative snapshot supplied by the
    caller. It is used only to validate references; this function never
    mutates a registry or permission store.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    try:
        raw = dict(payload)
        _check_json_value(raw)
        keys = set(raw)
        extra = keys - _TOP_LEVEL_FIELDS
        missing = _REQUIRED_FIELDS - keys
        if extra:
            raise ValueError(f"unexpected proposal fields: {sorted(extra)}")
        if missing:
            raise ValueError(f"missing proposal fields: {sorted(missing)}")

        if isinstance(canonical_tool_names, (str, bytes)):
            raise ValueError("canonical_tool_names must be a collection of tool names")
        try:
            canonical = frozenset(canonical_tool_names)
        except TypeError as exc:
            raise ValueError("canonical_tool_names must be iterable") from exc
        if any(not isinstance(name, str) or not name for name in canonical):
            raise ValueError("canonical tool names must be nonempty strings")

        proposal_id = _text(raw["proposal_id"], "proposal_id", 64)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}", proposal_id):
            raise ValueError("proposal_id has an invalid format")
        title = _text(raw["title"], "title", _MAX_TITLE_LENGTH, minimum=2)
        purpose = _text(raw["purpose"], "purpose", _MAX_PURPOSE_LENGTH, minimum=2)
        kind = raw["capability_kind"]
        if kind not in {"workflow", "adapter"}:
            raise ValueError("capability_kind must be 'workflow' or 'adapter'")
        proposed_name = _text(raw["proposed_tool_name"], "proposed_tool_name", 64, minimum=2)
        if not _SLUG_RE.fullmatch(proposed_name):
            raise ValueError("proposed_tool_name must be a lowercase slug")
        if proposed_name in _CONTROL_TOOLS:
            raise ValueError("proposed_tool_name cannot be a control tool")
        if proposed_name in canonical:
            raise ValueError("proposed_tool_name already exists in the canonical tool catalog")
        description = _text(
            raw["proposed_tool_description"], "proposed_tool_description",
            _MAX_DESCRIPTION_LENGTH, minimum=2,
        )
        schema = _normalize_schema(raw["proposed_parameters_schema"])

        permissions = raw["requested_permissions"]
        if not isinstance(permissions, list) or len(permissions) > _MAX_REQUESTED_PERMISSIONS:
            raise ValueError("requested_permissions must be a bounded list")
        normalized_permissions = [_text(name, "requested permission", 100) for name in permissions]
        if len(set(normalized_permissions)) != len(normalized_permissions):
            raise ValueError("requested_permissions contains duplicates")
        unknown_permissions = set(normalized_permissions) - canonical
        if unknown_permissions:
            raise ValueError(f"unknown requested permissions: {sorted(unknown_permissions)}")
        if set(normalized_permissions) & _CONTROL_TOOLS:
            raise ValueError("requested_permissions cannot contain control tools")
        normalized_permissions.sort()

        checks = raw["acceptance_checks"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= _MAX_ACCEPTANCE_CHECKS:
            raise ValueError("acceptance_checks must contain 1–12 checks")
        normalized_checks = [
            _text(check, "acceptance check", _MAX_CHECK_LENGTH, minimum=2) for check in checks
        ]

        raw_steps = raw.get("workflow_steps", [])
        if not isinstance(raw_steps, list) or len(raw_steps) > _MAX_WORKFLOW_STEPS:
            raise ValueError("workflow_steps must be a bounded list")
        steps: list[dict[str, str]] = []
        for index, step in enumerate(raw_steps):
            if not isinstance(step, dict) or set(step) != {
                "tool_name", "description", "completion_criteria"
            }:
                raise ValueError(
                    f"workflow_steps[{index}] must contain only tool_name, description, "
                    "and completion_criteria"
                )
            tool_name = _text(step["tool_name"], f"workflow_steps[{index}].tool_name", 100)
            if tool_name not in canonical:
                raise ValueError(f"workflow step references unknown tool: {tool_name}")
            if tool_name in _CONTROL_TOOLS:
                raise ValueError(f"workflow step cannot use control tool: {tool_name}")
            steps.append({
                "tool_name": tool_name,
                "description": _text(
                    step["description"], f"workflow_steps[{index}].description", 500
                ),
                "completion_criteria": _text(
                    step["completion_criteria"],
                    f"workflow_steps[{index}].completion_criteria", 500,
                ),
            })
        uncovered_step_tools = {
            step["tool_name"] for step in steps
        } - set(normalized_permissions)
        if uncovered_step_tools:
            raise ValueError(
                "requested_permissions must include every workflow step tool: "
                f"{sorted(uncovered_step_tools)}"
            )
        if kind == "workflow" and not steps:
            raise ValueError("workflow proposals require at least one declarative step")

        proposal = CapabilityProposal(
            proposal_id=proposal_id,
            title=title,
            purpose=purpose,
            capability_kind=kind,
            proposed_tool_name=proposed_name,
            proposed_tool_description=description,
            proposed_parameters_schema=schema,
            requested_permissions=tuple(normalized_permissions),
            acceptance_checks=tuple(normalized_checks),
            workflow_steps=tuple(steps),
        )
        normalized = proposal.normalized_fields()
        encoded = json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return {
            **normalized,
            "status": "draft",
            "available": False,
            "runnable": False,
            "digest": hashlib.sha256(encoded).hexdigest(),
        }
    except (TypeError, OverflowError) as exc:
        raise ValueError(f"invalid capability proposal: {exc}") from exc


__all__ = [
    "CapabilityProposal", "CAPABILITY_CONTROL_TOOLS", "CapabilityProposalGate",
    "validate_capability_proposal",
]
