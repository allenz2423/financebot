import pytest

from src.services.tool_grants import consume_grant, issue_grant


def test_grant_is_target_bound_and_single_use():
    grant = issue_grant(
        turn_id="turn-1",
        tool_name="send_push_alert",
        target={"user_id": "u1"},
        operation="send",
        allowed_arguments={"priority": "high"},
        risk="mutation",
        approval_required=False,
        now=100,
    )
    consumed = consume_grant(
        grant,
        turn_id="turn-1",
        tool_name="send_push_alert",
        target={"user_id": "u1"},
        arguments={"priority": "high", "message": "done"},
        approved=False,
        now=101,
    )
    assert consumed.used
    with pytest.raises(PermissionError, match="already"):
        consume_grant(
            consumed,
            turn_id="turn-1",
            tool_name="send_push_alert",
            target={"user_id": "u1"},
            arguments={"priority": "high"},
            approved=False,
            now=102,
        )


def test_grant_rejects_target_and_approval_mismatch():
    grant = issue_grant(
        turn_id="turn-1",
        tool_name="delete_transaction",
        target={"transaction_id": 4},
        operation="delete",
        allowed_arguments={},
        risk="high",
        approval_required=True,
        now=100,
    )
    with pytest.raises(PermissionError, match="target"):
        consume_grant(
            grant,
            turn_id="turn-1",
            tool_name="delete_transaction",
            target={"transaction_id": 5},
            arguments={},
            approved=True,
            now=101,
        )
    with pytest.raises(PermissionError, match="approval"):
        consume_grant(
            grant,
            turn_id="turn-1",
            tool_name="delete_transaction",
            target={"transaction_id": 4},
            arguments={},
            approved=False,
            now=101,
        )
