"""Tests for the persistent financial monitor engine."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services import monitor as mon
from src.db.migrations import apply_all


def _fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_all(conn)
    # Minimal transactions table so the monitor engine can read the ledger.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            transaction_id TEXT,
            date TEXT,
            merchant TEXT,
            clean_merchant TEXT,
            category TEXT,
            amount REAL,
            override_amount REAL,
            account_used TEXT,
            status TEXT,
            judgment TEXT,
            pending_transaction_id TEXT,
            context TEXT,
            is_locked INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            liquid_balance REAL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cash_inflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            source TEXT,
            gross_amount REAL,
            deductions REAL,
            net_expected REAL,
            expected_date TEXT,
            status TEXT,
            recurrence TEXT,
            notes TEXT
        )
        """
    )
    conn.commit()
    return conn


def test_validate_rule_unknown_kind():
    ok, err, _ = mon._validate_rule("bogus", {})
    assert ok is False
    assert "Unknown rule kind" in err


def test_validate_rule_projected_balance_low():
    ok, err, cfg = mon._validate_rule(
        "projected_balance_low", {"threshold": 1000, "window_days": 14}
    )
    assert ok is True
    assert cfg["threshold"] == 1000.0
    assert cfg["window_days"] == 14


def test_validate_rule_clamps_window_days():
    ok, _, cfg = mon._validate_rule(
        "projected_balance_low", {"threshold": 1, "window_days": 99999}
    )
    assert ok is True
    assert cfg["window_days"] == 365


def test_validate_rule_category_requires_category():
    ok, err, _ = mon._validate_rule("category_spend_exceeded", {})
    assert ok is False
    assert "category" in err


def test_add_and_list_rule():
    conn = _fresh_conn()
    ok, msg, rid = mon.add_monitor_rule(
        conn, "1", "low balance", "projected_balance_low",
        {"threshold": 1000, "window_days": 14}, severity="warning",
    )
    assert ok is True
    assert rid is not None
    rules = mon.list_monitor_rules(conn, "1", include_disabled=True)
    assert len(rules) == 1
    assert rules[0]["kind"] == "projected_balance_low"
    assert rules[0]["severity"] == "warning"


def test_toggle_and_delete_rule():
    conn = _fresh_conn()
    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "r", "large_deposit", {"min_amount": 1000}
    )
    assert ok
    assert "disabled" in mon.toggle_monitor_rule(conn, rid, "1", False)
    assert "enabled" in mon.toggle_monitor_rule(conn, rid, "1", True)
    assert "Deleted" in mon.delete_monitor_rule(conn, rid, "1")
    assert mon.list_monitor_rules(conn, "1") == []


def test_record_and_list_alerts():
    conn = _fresh_conn()
    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "r", "large_deposit", {"min_amount": 1000}
    )
    assert ok
    aid = mon.record_alert(
        conn, "1", rid, "large_deposit", "warning",
        "t", "detail text",
    )
    alerts = mon.list_alerts(conn, "1", include_acked=False)
    assert len(alerts) == 1
    assert alerts[0]["id"] == aid
    assert "Acked" in mon.ack_alert(conn, aid, "1")
    assert mon.list_alerts(conn, "1", include_acked=False) == []
    assert mon.list_alerts(conn, "1", include_acked=True)


def test_run_pass_no_rules():
    conn = _fresh_conn()
    res = mon.run_monitor_pass(conn, "1", deliver=False)
    assert res["rules_evaluated"] == 0
    assert res["rules_fired"] == 0


def test_run_pass_fires_large_deposit():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO transactions (user_id, merchant, amount, status, date) "
        "VALUES ('1', 'ACME', -2500.0, 'Evaluated', '2026-09-01')"
    )
    conn.commit()
    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "big deposit", "large_deposit", {"min_amount": 1000.0}
    )
    assert ok
    res = mon.run_monitor_pass(conn, "1", deliver=False)
    assert res["rules_fired"] == 1
    assert res["alert_ids"]


def test_cooldown_prevents_refire():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO transactions (user_id, merchant, amount, status, date) "
        "VALUES ('1', 'ACME', -2500.0, 'Evaluated', '2026-09-01')"
    )
    conn.commit()
    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "big deposit", "large_deposit",
        {"min_amount": 1000.0}, cooldown_hours=24,
    )
    assert ok
    r1 = mon.run_monitor_pass(conn, "1", deliver=False)
    assert r1["rules_fired"] == 1
    r2 = mon.run_monitor_pass(conn, "1", deliver=False)
    assert r2["rules_fired"] == 0


def test_user_isolation():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO transactions (user_id, merchant, amount, status, date) "
        "VALUES ('1', 'ACME', -2500.0, 'Evaluated', '2026-09-01')"
    )
    conn.commit()
    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "big deposit", "large_deposit", {"min_amount": 1000.0}
    )
    assert ok
    res = mon.run_monitor_pass(conn, "2", deliver=False)
    assert res["rules_evaluated"] == 0


def test_invalid_kind_rejected():
    conn = _fresh_conn()
    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "bad", "not_a_kind", {}
    )
    assert ok is False
    assert rid is None


def test_delete_rule_cascade_and_user_isolation():
    conn = _fresh_conn()
    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "temp", "large_deposit", {"min_amount": 1.0}
    )
    assert ok
    mon.record_alert(conn, "1", rid, "large_deposit", "warning", "t", "d")
    assert len(mon.list_alerts(conn, "1", include_acked=False)) == 1
    assert "Deleted" in mon.delete_monitor_rule(conn, rid, "1")
    assert mon.list_monitor_rules(conn, "1") == []
    # Cascade: alerts for the deleted rule are gone.
    assert mon.list_alerts(conn, "1", include_acked=True) == []
    # Cannot delete another user's rule.
    ok2, _, rid2 = mon.add_monitor_rule(
        conn, "2", "other", "large_deposit", {"min_amount": 1.0}
    )
    assert ok2
    assert "not found" in mon.delete_monitor_rule(conn, rid2, "1")


def test_duplicate_charge_detected_evaluator():
    conn = _fresh_conn()
    today = mon.datetime.now().strftime("%Y-%m-%d")
    # Insert two identical charges for Starbucks
    conn.execute(
        "INSERT INTO transactions (user_id, merchant, clean_merchant, amount, status, date) "
        "VALUES ('1', 'STARBUCKS #1234', 'Starbucks', 6.75, 'Evaluated', ?)",
        (today,)
    )
    conn.execute(
        "INSERT INTO transactions (user_id, merchant, clean_merchant, amount, status, date) "
        "VALUES ('1', 'STARBUCKS #1234', 'Starbucks', 6.75, 'Evaluated', ?)",
        (today,)
    )
    conn.commit()

    ok, _, rid = mon.add_monitor_rule(
        conn, "1", "dupe check", "duplicate_charge_detected",
        {"window_days": 3, "min_amount": 5.0, "merchant": "Starbucks"}
    )
    assert ok

    res = mon.run_monitor_pass(conn, "1", deliver=False)
    assert res["rules_fired"] == 1
    alerts = mon.list_alerts(conn, "1")
    assert len(alerts) == 1
    assert "Potential duplicate charge" in alerts[0]["detail"]
    assert "$6.75" in alerts[0]["detail"]


def test_validate_rule_category_budget_overpacing():
    ok, _, cfg = mon._validate_rule(
        "category_budget_overpacing",
        {"category": "Dining Out", "pace_threshold_pct": 135.0, "min_spent": 30.0}
    )
    assert ok is True
    assert cfg["category"] == "Dining Out"
    assert cfg["pace_threshold_pct"] == 135.0
    assert cfg["min_spent"] == 30.0

    # Default values
    ok2, _, cfg2 = mon._validate_rule("category_budget_overpacing", {})
    assert ok2 is True
    assert cfg2["category"] == "*"
    assert cfg2["pace_threshold_pct"] == 120.0
    assert cfg2["min_spent"] == 25.0


def test_category_budget_overpacing_evaluator(monkeypatch):
    conn = _fresh_conn()
    user_id = "user_pacing_test"

    # Set up category budget of $500 for Dining Out
    conn.execute(
        "INSERT INTO category_budgets (user_id, category, monthly_limit) VALUES (?, ?, ?)",
        (user_id, "Dining Out", 500.0)
    )
    conn.commit()

    # Insert transactions in current month
    # We fix the date to day 10 of current month
    from datetime import datetime
    now_dt = datetime.now()
    tx_date = f"{now_dt.year:04d}-{now_dt.month:02d}-05"

    conn.execute(
        "INSERT INTO transactions (user_id, merchant, category, amount, status, date) "
        "VALUES (?, ?, ?, ?, 'Evaluated', ?)",
        (user_id, "Fancy Bistro", "Dining Out", 350.0, tx_date)
    )
    conn.commit()

    # Add pacing monitor rule for Dining Out
    ok, _, rid = mon.add_monitor_rule(
        conn, user_id, "Dining pacing alert", "category_budget_overpacing",
        {"category": "Dining Out", "pace_threshold_pct": 120.0, "min_spent": 25.0}
    )
    assert ok is True

    # Run monitor pass
    res = mon.run_monitor_pass(conn, user_id, deliver=False)
    assert res["rules_fired"] == 1
    alerts = mon.list_alerts(conn, user_id)
    assert len(alerts) == 1
    assert "Dining Out budget" in alerts[0]["detail"]
    assert "$350.00" in alerts[0]["detail"]

    # Other user isolation: user_2 should not fire any alerts
    res_other = mon.run_monitor_pass(conn, "user_isolated_other", deliver=False)
    assert res_other["rules_fired"] == 0


def test_category_budget_overpacing_not_triggered_when_on_track():
    conn = _fresh_conn()
    user_id = "user_on_track"

    conn.execute(
        "INSERT INTO category_budgets (user_id, category, monthly_limit) VALUES (?, ?, ?)",
        (user_id, "Groceries", 1000.0)
    )
    conn.commit()

    from datetime import datetime
    now_dt = datetime.now()
    tx_date = f"{now_dt.year:04d}-{now_dt.month:02d}-02"

    conn.execute(
        "INSERT INTO transactions (user_id, merchant, category, amount, status, date) "
        "VALUES (?, ?, ?, ?, 'Evaluated', ?)",
        (user_id, "Supermarket", "Groceries", 20.0, tx_date)
    )
    conn.commit()

    # Rule with min_spent 25.0 should not fire
    ok, _, rid = mon.add_monitor_rule(
        conn, user_id, "Groceries pacing alert", "category_budget_overpacing",
        {"category": "Groceries", "pace_threshold_pct": 120.0, "min_spent": 25.0}
    )
    assert ok is True

    res = mon.run_monitor_pass(conn, user_id, deliver=False)
    assert res["rules_fired"] == 0
    assert mon.list_alerts(conn, user_id) == []

