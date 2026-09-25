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


__all__ = ["TargetBoundGrant", "consume_grant", "issue_grant"]
