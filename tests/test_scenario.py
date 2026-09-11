import sqlite3
import pytest
from datetime import datetime, timedelta
import src.db.queries as q

TEST_USER_1 = "user-scenario-1"
TEST_USER_2 = "user-scenario-2"

@pytest.fixture(autouse=True)
def setup_scenario_db(monkeypatch):
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_c = test_conn.cursor()

    test_c.executescript("""
        CREATE TABLE IF NOT EXISTS plaid_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            plaid_account_id TEXT UNIQUE,
            name TEXT,
            type TEXT,
            subtype TEXT,
            current_balance REAL,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            date TEXT,
            merchant TEXT,
            clean_merchant TEXT,
            category TEXT,
            amount REAL,
            status TEXT DEFAULT 'Evaluated'
        );
        CREATE TABLE IF NOT EXISTS planned_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            direction TEXT NOT NULL,
            expected_amount REAL,
            expected_date TEXT,
            status TEXT DEFAULT 'Expected',
            description TEXT
        );
        CREATE TABLE IF NOT EXISTS cash_inflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            net_expected REAL,
            expected_date TEXT,
            status TEXT DEFAULT 'Pending',
            source TEXT
        );
    """)

    monkeypatch.setattr(q, "conn", test_conn)
    monkeypatch.setattr(q, "c", test_c)
    yield test_conn
    test_conn.close()


def test_simulate_cash_flow_baseline():
    # User 1 has $5,000 checking balance
    q.c.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, current_balance, active)
        VALUES (?, 'acc_1', 'Checking', 'depository', 5000.0, 1)
    """, (TEST_USER_1,))

    # Past 30 days transactions = $600 total ($20/day daily burn)
    past_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    q.c.execute("""
        INSERT INTO transactions (user_id, date, merchant, amount)
        VALUES (?, ?, 'Grocery Store', 600.0)
    """, (TEST_USER_1, past_date))
    q.conn.commit()

    # Simulate 30 days
    res = q.simulate_cash_flow_scenario(days=30, user_id=TEST_USER_1)
    assert res["status"] == "success"
    assert res["starting_liquid"] == 5000.0
    assert res["daily_burn_rate"] == 20.0
    # Over 30 days with $20/day burn = -$600 -> final = 4400
    assert res["scenario"]["final_balance"] == 4400.0
    assert res["scenario"]["runway_days"] is None  # Never runs out


def test_simulate_what_if_expense_and_runway_depletion():
    # Starting balance $1,000
    q.c.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, current_balance, active)
        VALUES (?, 'acc_1', 'Checking', 'depository', 1000.0, 1)
    """, (TEST_USER_1,))
    q.conn.commit()

    # Scenario 1: Buy $400 gadget on day 10
    events = [{"name": "Gadget", "amount": -400.0, "offset_days": 10}]
    res = q.simulate_cash_flow_scenario(days=30, scenario_events=events, user_id=TEST_USER_1)
    assert res["delta"]["final_balance_impact"] == -400.0
    assert res["scenario"]["min_balance"] == 600.0
    assert res["scenario"]["runway_days"] is None

    # Scenario 2: Severe expense of $1,500 on day 5 -> depletes runway!
    severe_events = [{"name": "Emergency Repair", "amount": -1500.0, "offset_days": 5}]
    res2 = q.simulate_cash_flow_scenario(days=30, scenario_events=severe_events, user_id=TEST_USER_1)
    assert res2["scenario"]["min_balance"] < 0
    assert res2["scenario"]["runway_days"] == 5
