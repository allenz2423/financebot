import sqlite3
import pytest
from src.services.monitor import compile_natural_language_rule, add_monitor_rule_from_nl, list_monitor_rules
from src.db.migrations import apply_all

TEST_USER = "user-nl-rules-1"

@pytest.fixture
def monitor_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_all(conn)
    yield conn
    conn.close()


def test_compile_low_balance_rule():
    spec = compile_natural_language_rule("Alert if balance drops below $1,000 in 14 days")
    assert spec["kind"] == "projected_balance_low"
    assert spec["config"]["threshold"] == 1000.0
    assert spec["config"]["window_days"] == 14

    spec2 = compile_natural_language_rule("notify when checking falls under 400")
    assert spec2["kind"] == "projected_balance_low"
    assert spec2["config"]["threshold"] == 400.0
    assert spec2["config"]["window_days"] == 14


def test_compile_category_spend_rule():
    spec = compile_natural_language_rule("Alert me if dining exceeds $250 this week")
    assert spec["kind"] == "category_spend_exceeded"
    assert spec["config"]["category"] == "Dining"
    assert spec["config"]["limit"] == 250.0
    assert spec["config"]["window_days"] == 7

    spec2 = compile_natural_language_rule("warn if groceries exceeds $500 this month")
    assert spec2["kind"] == "category_spend_exceeded"
    assert spec2["config"]["category"] == "Groceries"
    assert spec2["config"]["limit"] == 500.0
    assert spec2["config"]["window_days"] == 30


def test_compile_large_deposit_rule():
    spec = compile_natural_language_rule("Alert on deposits over $2,000")
    assert spec["kind"] == "large_deposit"
    assert spec["config"]["threshold"] == 2000.0


def test_compile_subscription_increase_rule():
    spec = compile_natural_language_rule("Warn if Netflix increases by 15%")
    assert spec["kind"] == "subscription_price_changed"
    assert spec["config"]["merchant"] == "Netflix"
    assert spec["config"]["pct_increase"] == 15.0


def test_compile_unusual_spend_rule():
    spec = compile_natural_language_rule("Detect unusual transactions at 2.5 stddev")
    assert spec["kind"] == "unusual_transaction"
    assert spec["config"]["stddev_multiplier"] == 2.5


def test_add_monitor_rule_from_nl_integration(monitor_db):
    res = add_monitor_rule_from_nl(monitor_db, TEST_USER, "Alert me if dining exceeds $150 this week")
    assert res["id"] is not None
    assert res["kind"] == "category_spend_exceeded"

    rules = list_monitor_rules(monitor_db, TEST_USER)
    assert len(rules) == 1
    assert rules[0]["id"] == res["id"]
    assert rules[0]["kind"] == "category_spend_exceeded"
    assert rules[0]["config"]["limit"] == 150.0
