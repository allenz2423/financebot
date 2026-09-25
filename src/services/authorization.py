"""Small, explicit authorization pipeline for tool execution.

Delilah has three different security decisions that are easy to conflate:

* policy authorization: is this operation allowed in principle?
* human approval: did a user approve this exact operation?
* execution grant: may the executor consume this target-bound capability now?

This module keeps those decisions separate.  It deliberately delegates the
single-use and target-binding mechanics to :mod:`tool_grants` so callers can
adopt the clearer vocabulary without creating a second grant implementation.
It has no persistence dependency; an adapter can persist ``ApprovalRequest``
or ``HumanApproval`` using the application's existing stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Mapping

from src.services.tool_grants import (
    TargetBoundGrant,
    consume_grant,
    issue_grant,
)


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AuthorizationError(PermissionError):
    """Base error raised before a tool is allowed to execute."""


class ApprovalRequiredError(AuthorizationError):
    """Raised when a policy decision needs human approval first."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def request_fingerprint(
    *,
    turn_id: str,
    user_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    target: Any,
) -> str:
    """Return a stable identity for the exact action a user is approving."""

    payload = {
        "turn_id": str(turn_id),
        "user_id": str(user_id),
        "tool_name": str(tool_name),
        "arguments": dict(arguments),
        "target": target,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolPolicy:
    """Static policy metadata for one callable tool."""

    tool_name: str
    risk: RiskTier = RiskTier.LOW
    operation: str = "read"
    approval_required: bool = False
    enabled: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.tool_name).strip():
            raise ValueError("tool policy requires a tool name")
        if not str(self.operation).strip():
            raise ValueError("tool policy requires an operation")
        object.__setattr__(self, "risk", RiskTier(self.risk))


@dataclass(frozen=True)
class AuthorizationRequest:
    """The request evaluated by policy before any grant is issued."""

    turn_id: str
    user_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    target: Any
    policy: ToolPolicy

    def __post_init__(self) -> None:
        if not str(self.turn_id).strip():
            raise ValueError("authorization request requires a turn id")
        if not str(self.user_id).strip():
            raise ValueError("authorization request requires a user id")
        if str(self.tool_name) != self.policy.tool_name:
            raise ValueError("request tool does not match its policy")

    @property
    def fingerprint(self) -> str:
        return request_fingerprint(
            turn_id=self.turn_id,
            user_id=self.user_id,
            tool_name=self.tool_name,
            arguments=self.arguments,
            target=self.target,
        )


@dataclass(frozen=True)
class PolicyDecision:
    """Policy output; this is not an execution grant."""

    outcome: PolicyOutcome
    tool_name: str
    risk: RiskTier
    operation: str
    request_fingerprint: str
    reason: str

    @property
    def allowed_without_approval(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.outcome is PolicyOutcome.APPROVAL_REQUIRED


@dataclass(frozen=True)
class ApprovalRequest:
    """A presentation-safe approval request bound to one exact action."""

    approval_id: str
    request_fingerprint: str
    tool_name: str
    operation: str
    risk: RiskTier
    summary: str
    expires_at: float


@dataclass(frozen=True)
class HumanApproval:
    """The user's response to an ``ApprovalRequest``."""

    approval_id: str
    request_fingerprint: str
    status: ApprovalStatus
    approved_by: str | None = None
    decided_at: float | None = None
    expires_at: float | None = None

    @property
    def approved(self) -> bool:
        return self.status is ApprovalStatus.APPROVED


@dataclass(frozen=True)
class ExecutionAuthorization:
    """A policy decision plus the grant that may be consumed by execution."""

    decision: PolicyDecision
    grant: TargetBoundGrant
    approval: HumanApproval | None = None

    @property
    def grant_id(self) -> str:
        return self.grant.grant_id


_HIGH_RISK = frozenset({RiskTier.HIGH, RiskTier.CRITICAL})


def evaluate_policy(request: AuthorizationRequest) -> PolicyDecision:
    """Evaluate policy without issuing a grant or contacting a user.

    Low-risk reads are automatic by default.  A policy can explicitly require
    approval for a medium-risk operation, and high/critical operations always
    require approval even if a caller accidentally omitted the flag.
    """

    policy = request.policy
    if not policy.enabled:
        outcome = PolicyOutcome.DENY
        reason = policy.reason or "tool is disabled by policy"
    elif policy.approval_required or policy.risk in _HIGH_RISK:
        outcome = PolicyOutcome.APPROVAL_REQUIRED
        reason = policy.reason or "human approval is required for this operation"
    else:
        outcome = PolicyOutcome.ALLOW
        reason = policy.reason or "low-risk operation is allowed by policy"

    return PolicyDecision(
        outcome=outcome,
        tool_name=request.tool_name,
        risk=policy.risk,
        operation=policy.operation,
        request_fingerprint=request.fingerprint,
        reason=reason,
    )


def build_approval_request(
    request: AuthorizationRequest,
    decision: PolicyDecision,
    *,
    approval_id: str,
    summary: str,
    expires_at: float,
) -> ApprovalRequest:
    """Create a user-facing approval object without granting execution."""

    if decision.request_fingerprint != request.fingerprint:
        raise AuthorizationError("policy decision does not match request")
    if decision.outcome is not PolicyOutcome.APPROVAL_REQUIRED:
        raise AuthorizationError("approval is not required for this request")
    if not str(approval_id).strip():
        raise ValueError("approval request requires an id")
    return ApprovalRequest(
        approval_id=str(approval_id),
        request_fingerprint=request.fingerprint,
        tool_name=request.tool_name,
        operation=request.policy.operation,
        risk=request.policy.risk,
        summary=str(summary),
        expires_at=float(expires_at),
    )


def approve_request(
    approval_request: ApprovalRequest,
    *,
    approved: bool,
    approved_by: str | None = None,
    now: float | None = None,
) -> HumanApproval:
    """Convert a user's response into a typed approval decision."""

    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if current >= approval_request.expires_at:
        status = ApprovalStatus.EXPIRED
    else:
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    return HumanApproval(
        approval_id=approval_request.approval_id,
        request_fingerprint=approval_request.request_fingerprint,
        status=status,
        approved_by=str(approved_by) if approved_by is not None else None,
        decided_at=current,
        expires_at=approval_request.expires_at,
    )


def issue_execution_grant(
    request: AuthorizationRequest,
    decision: PolicyDecision,
    *,
    approval: HumanApproval | None = None,
    ttl_seconds: float = 120.0,
    now: float | None = None,
) -> ExecutionAuthorization:
    """Issue the existing target-bound grant only after policy/approval gates."""

    if decision.request_fingerprint != request.fingerprint:
        raise AuthorizationError("policy decision does not match request")
    if decision.outcome is PolicyOutcome.DENY:
        raise AuthorizationError(decision.reason)
    if decision.requires_approval:
        if approval is None:
            raise ApprovalRequiredError("human approval is required before issuing a grant")
        if approval.request_fingerprint != request.fingerprint:
            raise AuthorizationError("approval is bound to another request")
        if not approval.approved:
            raise AuthorizationError(f"approval is not valid: {approval.status.value}")
        current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        if approval.expires_at is not None and current >= approval.expires_at:
            raise AuthorizationError("approval has expired")

    grant = issue_grant(
        turn_id=request.turn_id,
        tool_name=request.tool_name,
        target=request.target,
        operation=request.policy.operation,
        allowed_arguments=request.arguments,
        risk=request.policy.risk.value,
        approval_required=decision.requires_approval,
        ttl_seconds=ttl_seconds,
        now=now,
    )
    return ExecutionAuthorization(decision=decision, grant=grant, approval=approval)


def consume_execution_grant(
    authorization: ExecutionAuthorization,
    *,
    turn_id: str,
    tool_name: str,
    target: Any,
    arguments: Mapping[str, Any],
    now: float | None = None,
) -> TargetBoundGrant:
    """Consume a grant while deriving approval from the typed approval object."""

    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if current >= authorization.grant.expires_at:
        raise AuthorizationError("execution grant has expired")
    approved = bool(authorization.approval and authorization.approval.approved)
    return consume_grant(
        authorization.grant,
        turn_id=turn_id,
        tool_name=tool_name,
        target=target,
        arguments=arguments,
        approved=approved,
        now=current,
    )


__all__ = [
    "ApprovalRequest",
    "ApprovalRequiredError",
    "ApprovalStatus",
    "AuthorizationError",
    "AuthorizationRequest",
    "ExecutionAuthorization",
    "HumanApproval",
    "PolicyDecision",
    "PolicyOutcome",
    "RiskTier",
    "ToolPolicy",
    "approve_request",
    "build_approval_request",
    "consume_execution_grant",
    "evaluate_policy",
    "issue_execution_grant",
    "request_fingerprint",
]
