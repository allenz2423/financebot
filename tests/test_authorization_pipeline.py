import pytest

from src.services.authorization import (
    ApprovalRequiredError,
    ApprovalStatus,
    AuthorizationRequest,
    PolicyOutcome,
    RiskTier,
    ToolPolicy,
    approve_request,
    build_approval_request,
    consume_execution_grant,
    evaluate_policy,
    issue_execution_grant,
)


def _request(policy: ToolPolicy, *, arguments=None, target=None):
    return AuthorizationRequest(
        turn_id="turn-1",
        user_id="user-1",
        tool_name=policy.tool_name,
        arguments=arguments or {"query": "coffee"},
        target=target if target is not None else {"scope": "user-1"},
        policy=policy,
    )


def test_low_risk_read_is_automatic_but_still_gets_a_single_use_grant():
    request = _request(ToolPolicy("search_web", risk=RiskTier.LOW, operation="read"))
    decision = evaluate_policy(request)

    assert decision.outcome is PolicyOutcome.ALLOW
    authorization = issue_execution_grant(request, decision, now=100)
    consumed = consume_execution_grant(
        authorization,
        turn_id="turn-1",
        tool_name="search_web",
        target={"scope": "user-1"},
        arguments={"query": "coffee", "limit": 3},
        now=101,
    )
    assert consumed.used


def test_high_risk_operation_requires_human_approval_before_grant():
    policy = ToolPolicy(
        "delete_transaction",
        risk=RiskTier.HIGH,
        operation="delete",
    )
    request = _request(policy, arguments={"transaction_id": 42}, target={"transaction_id": 42})
    decision = evaluate_policy(request)

    assert decision.outcome is PolicyOutcome.APPROVAL_REQUIRED
    with pytest.raises(ApprovalRequiredError):
        issue_execution_grant(request, decision, now=100)

    approval_request = build_approval_request(
        request,
        decision,
        approval_id="approval-1",
        summary="Delete transaction 42",
        expires_at=200,
    )
    rejected = approve_request(approval_request, approved=False, approved_by="user-1", now=101)
    assert rejected.status is ApprovalStatus.REJECTED
    with pytest.raises(PermissionError, match="not valid"):
        issue_execution_grant(request, decision, approval=rejected, now=101)

    approved = approve_request(approval_request, approved=True, approved_by="user-1", now=102)
    authorization = issue_execution_grant(request, decision, approval=approved, now=102)
    assert authorization.grant.approval_required
    assert consume_execution_grant(
        authorization,
        turn_id="turn-1",
        tool_name="delete_transaction",
        target={"transaction_id": 42},
        arguments={"transaction_id": 42, "reason": "duplicate"},
        now=103,
    ).used


def test_approval_cannot_be_replayed_for_a_different_target():
    policy = ToolPolicy("delete_transaction", risk=RiskTier.CRITICAL, operation="delete")
    request = _request(policy, arguments={"transaction_id": 42}, target={"transaction_id": 42})
    decision = evaluate_policy(request)
    pending = build_approval_request(
        request,
        decision,
        approval_id="approval-2",
        summary="Delete transaction 42",
        expires_at=200,
    )
    approval = approve_request(pending, approved=True, approved_by="user-1", now=101)

    changed = _request(policy, arguments={"transaction_id": 43}, target={"transaction_id": 43})
    changed_decision = evaluate_policy(changed)
    with pytest.raises(PermissionError, match="another request"):
        issue_execution_grant(changed, changed_decision, approval=approval, now=101)


def test_expired_approval_is_not_accepted():
    policy = ToolPolicy("send_push_alert", risk=RiskTier.HIGH, operation="send")
    request = _request(policy, arguments={"message": "alert"}, target={"channel": "user-1"})
    decision = evaluate_policy(request)
    pending = build_approval_request(
        request,
        decision,
        approval_id="approval-3",
        summary="Send alert",
        expires_at=100,
    )
    approval = approve_request(pending, approved=True, approved_by="user-1", now=101)
    assert approval.status is ApprovalStatus.EXPIRED
    with pytest.raises(PermissionError, match="not valid"):
        issue_execution_grant(request, decision, approval=approval, now=101)


def test_approval_cannot_be_used_after_its_expiry_even_if_it_was_approved_early():
    policy = ToolPolicy("send_push_alert", risk=RiskTier.HIGH, operation="send")
    request = _request(policy, arguments={"message": "alert"}, target={"channel": "user-1"})
    decision = evaluate_policy(request)
    pending = build_approval_request(
        request,
        decision,
        approval_id="approval-4",
        summary="Send alert",
        expires_at=110,
    )
    approval = approve_request(pending, approved=True, approved_by="user-1", now=101)
    with pytest.raises(PermissionError, match="expired"):
        issue_execution_grant(request, decision, approval=approval, now=111)
