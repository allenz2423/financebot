import sqlite3
import pytest
import src.db.queries as q
from src.db.migrations import apply_all

TEST_USER_1 = "user-sinking-1"
TEST_USER_2 = "user-sinking-2"

@pytest.fixture(autouse=True)
def setup_sinking_db(monkeypatch):
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_c = test_conn.cursor()

    apply_all(test_conn)

    test_c.executescript("""
        CREATE TABLE IF NOT EXISTS savings_buckets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target_amount REAL NOT NULL DEFAULT 0.0,
            current_amount REAL NOT NULL DEFAULT 0.0,
            category TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS roundup_settings (
            user_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            target_bucket_name TEXT NOT NULL DEFAULT 'Emergency Fund',
            multiplier REAL NOT NULL DEFAULT 1.0,
            whole_dollar_roundup REAL NOT NULL DEFAULT 1.0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bucket_contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            bucket_name TEXT NOT NULL,
            amount REAL NOT NULL,
            source TEXT DEFAULT 'manual',
            transaction_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            merchant TEXT,
            clean_merchant TEXT,
            amount REAL NOT NULL,
            date TEXT NOT NULL,
            category TEXT,
            status TEXT DEFAULT 'Evaluated',
            is_locked INTEGER DEFAULT 0
        );
    """)

    monkeypatch.setattr(q, "conn", test_conn)
    monkeypatch.setattr(q, "c", test_c)
    yield test_conn
    test_conn.close()


def test_roundup_calculation_and_settings():
    # User 1 sets up 2x multiplier into Vacation fund
    settings = q.set_roundup_settings(
        user_id=TEST_USER_1,
        enabled=True,
        target_bucket_name="Vacation",
        multiplier=2.0,
        whole_dollar_roundup=1.0
    )
    assert settings["enabled"] is True
    assert settings["target_bucket_name"] == "Vacation"
    assert settings["multiplier"] == 2.0

    # Test round up math
    # 4.25 -> spare is 0.75 * 2.0 = 1.50
    spare1 = q.calculate_transaction_roundup(4.25, multiplier=2.0)
    assert spare1 == 1.50

    # 10.00 -> whole dollar round up: 1.00 * 2.0 = 2.00
    spare2 = q.calculate_transaction_roundup(10.00, multiplier=2.0, whole_dollar_roundup=1.0)
    assert spare2 == 2.00

    # Whole dollar round up set to 0.0 -> flat $10.00 gives $0.00
    spare3 = q.calculate_transaction_roundup(10.00, multiplier=1.0, whole_dollar_roundup=0.0)
    assert spare3 == 0.0

    # User 2 has default 1x multiplier and Emergency Fund
    cfg2 = q.get_roundup_settings(user_id=TEST_USER_2)
    assert cfg2["multiplier"] == 1.0
    assert cfg2["target_bucket_name"] == "Emergency Fund"
    spare_u2 = q.calculate_transaction_roundup(4.25, multiplier=cfg2["multiplier"])
    assert spare_u2 == 0.75


def test_apply_transaction_roundups_and_isolation():
    # Setup bucket for user 1
    q.adjust_savings_bucket(name="Vacation", target=1000.0, delta=100.0, user_id=TEST_USER_1)

    # Insert transactions for user 1
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date) VALUES (?, 'Coffee Shop', 4.35, date('now'))",
        (TEST_USER_1,)
    )
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date) VALUES (?, 'Grocery Market', 52.80, date('now'))",
        (TEST_USER_1,)
    )
    # Insert transaction for user 2
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date) VALUES (?, 'Bookstore', 12.10, date('now'))",
        (TEST_USER_2,)
    )
    q.safe_commit()

    # User 1 sets target bucket to Vacation
    q.set_roundup_settings(user_id=TEST_USER_1, target_bucket_name="Vacation", multiplier=1.0)

    # Sweep User 1
    # 4.35 -> 0.65, 52.80 -> 0.20. Total = 0.85
    res1 = q.apply_transaction_roundups(user_id=TEST_USER_1)
    assert res1["status"] == "success"
    assert res1["roundups_count"] == 2
    assert res1["total_swept"] == 0.85
    assert res1["current_bucket_amount"] == 100.85

    # Check that contributions were logged for user 1
    q.c.execute("SELECT COUNT(*) FROM bucket_contributions WHERE user_id = ?", (TEST_USER_1,))
    assert q.c.fetchone()[0] == 2

    # Check that User 2 had 0 contributions
    q.c.execute("SELECT COUNT(*) FROM bucket_contributions WHERE user_id = ?", (TEST_USER_2,))
    assert q.c.fetchone()[0] == 0

    # Forced isolation check
    with pytest.raises(ValueError):
        q.apply_transaction_roundups(user_id="")


def test_sinking_funds_overview_projections():
    # Empty case
    empty = q.get_sinking_funds_overview(user_id=TEST_USER_1)
    assert empty["status"] == "empty"

    # Create bucket: Car Repair, target 1200, current 600 (50%)
    q.adjust_savings_bucket(name="Car Repair", target=1200.0, delta=600.0, user_id=TEST_USER_1)

    # Add historical contributions to simulate velocity: $300 over last 90 days = $100/mo
    q.c.execute(
        "INSERT INTO bucket_contributions (user_id, bucket_name, amount, created_at) VALUES (?, 'Car Repair', 150.0, datetime('now', '-20 days'))",
        (TEST_USER_1,)
    )
    q.c.execute(
        "INSERT INTO bucket_contributions (user_id, bucket_name, amount, created_at) VALUES (?, 'Car Repair', 150.0, datetime('now', '-40 days'))",
        (TEST_USER_1,)
    )
    q.safe_commit()

    overview = q.get_sinking_funds_overview(user_id=TEST_USER_1)
    assert overview["status"] == "success"
    assert overview["total_saved"] == 600.0
    assert overview["total_target"] == 1200.0
    assert overview["overall_progress_pct"] == 50.0

    b = overview["buckets"][0]
    assert b["name"] == "Car Repair"
    assert b["remaining_amount"] == 600.0
    assert b["monthly_velocity"] == 100.0
    assert b["months_remaining"] == 6.0  # 600 remaining / 100 per month = 6 months
    assert "⭐" in b["milestone"]
