import pytest
import sqlite3

from src.services.credit import calculate_credit_utilization, format_credit_utilization_report
from src.services.monitor import (
    _eval_credit_utilization_high,
    _validate_rule,
    compile_natural_language_rule,
)


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE plaid_accounts (
            plaid_account_id TEXT PRIMARY KEY,
            user_id TEXT,
            name TEXT,
            type TEXT,
            subtype TEXT,
            current_balance REAL,
            available_balance REAL,
            credit_limit REAL,
            active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def test_calculate_credit_utilization_isolation():
    with pytest.raises(ValueError, match="forced isolation violation"):
        calculate_credit_utilization(user_id="")

    with pytest.raises(ValueError, match="forced isolation violation"):
        calculate_credit_utilization(user_id=None)  # type: ignore


def test_calculate_credit_utilization_empty(test_db):
    res = calculate_credit_utilization(user_id="empty_user", conn=test_db)
    assert res["card_count"] == 0
    assert res["total_balance"] == 0.0
    assert res["total_limit"] == 0.0
    assert res["aggregate_utilization_pct"] == 0.0
    assert res["overall_tier"] == "Optimal"
    assert "No active credit card accounts found" in res["recommendations"][0]

    report = format_credit_utilization_report(res)
    assert "No active credit card accounts found" in report


def test_calculate_credit_utilization_single_card_and_paydown(test_db):
    user_id = "user_card_1"
    c = test_db.cursor()
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, current_balance, available_balance, credit_limit, active)
        VALUES ('acc1', ?, 'Chase Sapphire Preferred', 'credit', 'credit card', 4500.0, 500.0, 5000.0, 1)
    """, (user_id,))
    test_db.commit()

    res = calculate_credit_utilization(user_id=user_id, conn=test_db)
    assert res["card_count"] == 1
    card = res["cards"][0]
    assert card["name"] == "Chase Sapphire Preferred"
    assert card["balance"] == 4500.0
    assert card["limit"] == 5000.0
    assert card["utilization_pct"] == 90.0
    assert card["tier"] == "Critical"
    assert card["badge"] == "🔴"

    # Paydown to reach <30%: 4500 - (5000 * 0.299) = 4500 - 1495 = 3005.00
    assert card["paydown_for_30_pct"] == 3005.00
    # Paydown to reach <10%: 4500 - (5000 * 0.099) = 4500 - 495 = 4005.00
    assert card["paydown_for_10_pct"] == 4005.00

    assert res["aggregate_utilization_pct"] == 90.0
    assert res["overall_tier"] == "Critical"
    assert len(res["cards_over_50_pct"]) == 1

    report = format_credit_utilization_report(res)
    assert "Chase Sapphire Preferred" in report
    assert "90.0%" in report
    assert "Pay **$3,005.00** for <30%" in report


def test_calculate_credit_utilization_multi_card_and_isolation(test_db):
    user_a = "user_a"
    user_b = "user_b"

    c = test_db.cursor()
    # User A: 2 cards
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, current_balance, available_balance, credit_limit, active)
        VALUES ('a1', ?, 'Amex Gold', 'credit', 'credit card', 200.0, 4800.0, 5000.0, 1),
               ('a2', ?, 'Capital One Venture', 'credit', 'credit card', 1200.0, 3800.0, 5000.0, 1)
    """, (user_a, user_a))

    # User B: 1 high card
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, current_balance, available_balance, credit_limit, active)
        VALUES ('b1', ?, 'Citi Custom Cash', 'credit', 'credit card', 4800.0, 200.0, 5000.0, 1)
    """, (user_b,))
    test_db.commit()

    # User A check
    res_a = calculate_credit_utilization(user_id=user_a, conn=test_db)
    assert res_a["card_count"] == 2
    assert res_a["total_balance"] == 1400.0
    assert res_a["total_limit"] == 10000.0
    assert res_a["aggregate_utilization_pct"] == 14.0
    assert res_a["overall_tier"] == "Good"
    assert res_a["overall_badge"] == "🟡"
    # Neither card over 30%
    assert len(res_a["cards_over_30_pct"]) == 0

    # User B check
    res_b = calculate_credit_utilization(user_id=user_b, conn=test_db)
    assert res_b["card_count"] == 1
    assert res_b["total_balance"] == 4800.0
    assert res_b["aggregate_utilization_pct"] == 96.0


def test_monitor_credit_utilization_rule_evaluation(test_db):
    user_id = "user_mon"
    c = test_db.cursor()
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, current_balance, available_balance, credit_limit, active)
        VALUES ('c1', ?, 'Apple Card', 'credit', 'credit card', 2500.0, 2500.0, 5000.0, 1)
    """, (user_id,))
    test_db.commit()

    # 50% utilization breaches 30% threshold
    cfg_trigger = {"threshold_pct": 30.0}
    msg = _eval_credit_utilization_high(test_db, cfg_trigger, user_id)
    assert msg is not None
    assert "Apple Card is at 50.0% utilization" in msg

    # 50% does not breach 60% threshold
    cfg_safe = {"threshold_pct": 60.0}
    msg_safe = _eval_credit_utilization_high(test_db, cfg_safe, user_id)
    assert msg_safe is None


def test_monitor_credit_utilization_validation_and_nl_parsing():
    # Validation ok
    ok, err, norm = _validate_rule("credit_utilization_high", {"threshold_pct": 35.0})
    assert ok is True
    assert norm["threshold_pct"] == 35.0

    # Validation invalid type
    ok, err, norm = _validate_rule("credit_utilization_high", {"threshold_pct": "not_a_number"})
    assert ok is False
    assert "requires numeric" in err

    # NL Parsing
    parsed1 = compile_natural_language_rule("alert if credit card utilization exceeds 30%")
    assert parsed1["kind"] == "credit_utilization_high"
    assert parsed1["config"]["threshold_pct"] == 30.0

    parsed2 = compile_natural_language_rule("warn if card utilization over 25%")
    assert parsed2["kind"] == "credit_utilization_high"
    assert parsed2["config"]["threshold_pct"] == 25.0
