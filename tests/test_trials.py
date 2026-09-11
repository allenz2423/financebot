import sqlite3
import pytest
from datetime import datetime, timedelta
import src.db.queries as q
from src.services.monitor import evaluate_rule
from src.db.migrations import apply_all

TEST_USER_1 = "user-trials-1"
TEST_USER_2 = "user-trials-2"

@pytest.fixture(autouse=True)
def setup_trials_db(monkeypatch):
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_c = test_conn.cursor()

    apply_all(test_conn)

    test_c.executescript("""
        CREATE TABLE IF NOT EXISTS subscription_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            service_name TEXT NOT NULL,
            trial_end_date TEXT NOT NULL,
            projected_cost REAL NOT NULL DEFAULT 0.0,
            billing_cycle TEXT DEFAULT 'monthly',
            cancellation_url TEXT,
            notes TEXT,
            status TEXT DEFAULT 'active',
            notified INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            merchant TEXT,
            last_amount REAL,
            last_date TEXT,
            status TEXT DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            merchant TEXT,
            clean_merchant TEXT,
            amount REAL,
            date TEXT
        );
    """)

    monkeypatch.setattr(q, "conn", test_conn)
    monkeypatch.setattr(q, "c", test_c)
    yield test_conn
    test_conn.close()


def test_trials_crud_and_isolation():
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    t1 = q.add_subscription_trial(
        user_id=TEST_USER_1,
        service_name="Audible",
        trial_end_date=tomorrow,
        projected_cost=14.99,
        cancellation_url="https://audible.com/cancel"
    )
    assert t1["id"] is not None
    assert t1["service_name"] == "Audible"

    # User 2 sees nothing
    u2_trials = q.list_subscription_trials(user_id=TEST_USER_2)
    assert len(u2_trials) == 0

    # User 1 sees 1 trial with 1 day remaining
    u1_trials = q.list_subscription_trials(user_id=TEST_USER_1)
    assert len(u1_trials) == 1
    assert u1_trials[0]["days_remaining"] == 1

    # Update status to cancelled
    q.update_trial_status(t1["id"], "cancelled", user_id=TEST_USER_1)
    active_trials = q.list_subscription_trials(user_id=TEST_USER_1, active_only=True)
    assert len(active_trials) == 0


def test_trial_expiring_soon_monitor_evaluator(setup_trials_db):
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    q.add_subscription_trial(
        user_id=TEST_USER_1,
        service_name="Duolingo Super",
        trial_end_date=tomorrow,
        projected_cost=9.99
    )

    rule_row = {
        "kind": "trial_expiring_soon",
        "config": {"days_notice": 2}
    }
    alert = evaluate_rule(setup_trials_db, rule_row, TEST_USER_1)
    assert alert is not None
    assert "Duolingo Super" in alert
    assert "expires on" in alert
    assert "$9.99" in alert


def test_zombie_subscriptions():
    q.c.execute("""
        INSERT INTO subscriptions (user_id, merchant, last_amount, last_date, status)
        VALUES (?, 'Gym Membership', 45.00, '2026-08-01', 'Active')
    """, (TEST_USER_1,))
    q.conn.commit()

    zombies = q.detect_zombie_subscriptions(inactivity_days=30, user_id=TEST_USER_1)
    assert len(zombies) == 1
    assert zombies[0]["merchant"] == "Gym Membership"
    assert zombies[0]["annual_cost"] == 540.00
