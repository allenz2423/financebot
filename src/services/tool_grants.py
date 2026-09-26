"""Single-use, target-bound authorization grants for routed tool calls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import uuid
from typing import Any, Mapping


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _target_key(target: Any) -> str:
    return hashlib.sha256(_canonical(target).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TargetBoundGrant:
    grant_id: str
    turn_id: str
    tool_name: str
    target_key: str
    operation: str
    allowed_arguments: Mapping[str, Any]
    risk: str
    approval_required: bool
    expires_at: float
    used: bool = False


@dataclass(frozen=True)
class DelegationCapabilityGrant:
    """Parent-issued tool scope for one child task; not a replacement for action receipts."""

    grant_id: str
    user_id: str
    parent_task_id: str
    child_task_id: str
    allowed_tools: frozenset[str]


def issue_delegation_capability_grant(
    *,
    user_id: str,
    parent_task_id: str,
    child_task_id: str,
    allowed_tools: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    grant_id: str | None = None,
) -> DelegationCapabilityGrant:
    tools = frozenset(str(name) for name in allowed_tools)
    if not str(user_id).strip() or not str(parent_task_id).strip() or not str(child_task_id).strip():
        raise ValueError("delegation grant requires owner, parent, and child task IDs")
    if not tools or tools.intersection({"delegate_task", "await_user", "task_cancel"}):
        raise PermissionError("delegation grant has an empty or control-capable child scope")
    return DelegationCapabilityGrant(
        grant_id=(
            str(grant_id) if grant_id is not None
            else f"delegation_grant_{uuid.uuid4().hex}"
        ),
        user_id=str(user_id),
        parent_task_id=str(parent_task_id),
        child_task_id=str(child_task_id),
        allowed_tools=tools,
    )


def validate_delegation_capability_grant(
    grant: DelegationCapabilityGrant,
    *,
    user_id: str,
    parent_task_id: str,
    task_id: str,
    tool_name: str,
) -> None:
    """Enforce that child execution remains within the parent's durable grant."""
    if (
        str(user_id) != grant.user_id
        or str(parent_task_id) != grant.parent_task_id
        or str(task_id) != grant.child_task_id
    ):
        raise PermissionError("delegation grant belongs to another owner or child task")
    if str(tool_name) not in grant.allowed_tools:
        raise PermissionError("delegated action exceeds the parent-issued child tool grant")


def issue_grant(
    *,
    turn_id: str,
    tool_name: str,
    target: Any,
    operation: str,
    allowed_arguments: Mapping[str, Any] | None,
    risk: str,
    approval_required: bool,
    ttl_seconds: float = 120.0,
    now: float | None = None,
) -> TargetBoundGrant:
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    return TargetBoundGrant(
        grant_id=f"grant_{uuid.uuid4().hex}",
        turn_id=str(turn_id),
        tool_name=str(tool_name),
        target_key=_target_key(target),
        operation=str(operation),
        allowed_arguments=dict(allowed_arguments or {}),
        risk=str(risk),
        approval_required=bool(approval_required),
        expires_at=current + max(1.0, float(ttl_seconds)),
    )


def consume_grant(
    grant: TargetBoundGrant,
    *,
    turn_id: str,
    tool_name: str,
    target: Any,
    arguments: Mapping[str, Any],
    approved: bool,
    now: float | None = None,
) -> TargetBoundGrant:
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if grant.used:
        raise PermissionError("tool grant has already been consumed")
    if current > grant.expires_at:
        raise PermissionError("tool grant has expired")
    if str(turn_id) != grant.turn_id:
        raise PermissionError("tool grant belongs to another turn")
    if str(tool_name) != grant.tool_name:
        raise PermissionError("tool grant targets another tool")
    if _target_key(target) != grant.target_key:
        raise PermissionError("tool grant target mismatch")
    if grant.approval_required and not approved:
        raise PermissionError("tool grant requires approval")
    for key, value in grant.allowed_arguments.items():
        if arguments.get(key) != value:
            raise PermissionError(f"tool grant argument constraint failed: {key}")
    return replace(grant, used=True)


__all__ = [
    "DelegationCapabilityGrant", "TargetBoundGrant", "consume_grant", "issue_grant",
    "issue_delegation_capability_grant", "validate_delegation_capability_grant",
]
