import sqlite3
import pytest
import src.db.queries as q

TEST_USER_1 = "user-debt-test-1"
TEST_USER_2 = "user-debt-test-2"

@pytest.fixture(autouse=True)
def setup_debt_db(monkeypatch):
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_c = test_conn.cursor()

    test_c.executescript("""
        CREATE TABLE IF NOT EXISTS user_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            balance REAL NOT NULL,
            apr REAL NOT NULL DEFAULT 0.0,
            min_payment REAL NOT NULL DEFAULT 0.0,
            plaid_account_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS plaid_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            plaid_account_id TEXT UNIQUE,
            name TEXT,
            official_name TEXT,
            mask TEXT,
            type TEXT,
            subtype TEXT,
            institution_name TEXT,
            currency TEXT DEFAULT 'USD',
            current_balance REAL,
            available_balance REAL,
            credit_limit REAL,
            last_synced_at TEXT,
            active INTEGER DEFAULT 1
        );
    """)

    monkeypatch.setattr(q, "conn", test_conn)
    monkeypatch.setattr(q, "c", test_c)
    yield test_conn
    test_conn.close()


def test_debt_crud_and_isolation():
    d1 = q.add_or_update_debt(user_id=TEST_USER_1, name="Credit Card A", balance=1500.0, apr=24.99, min_payment=50.0)
    assert d1["id"] is not None
    assert d1["name"] == "Credit Card A"

    user2_debts = q.list_user_debts(user_id=TEST_USER_2, auto_sync=False)
    assert len(user2_debts) == 0

    user1_debts = q.list_user_debts(user_id=TEST_USER_1, auto_sync=False)
    assert len(user1_debts) == 1
    assert user1_debts[0]["balance"] == 1500.0

    q.add_or_update_debt(user_id=TEST_USER_1, name="Credit Card A", balance=1200.0, apr=22.0, min_payment=45.0, debt_id=d1["id"])
    updated = q.list_user_debts(user_id=TEST_USER_1, auto_sync=False)
    assert updated[0]["balance"] == 1200.0
    assert updated[0]["apr"] == 22.0

    assert not q.delete_user_debt(d1["id"], user_id=TEST_USER_2)
    assert len(q.list_user_debts(user_id=TEST_USER_1, auto_sync=False)) == 1

    assert q.delete_user_debt(d1["id"], user_id=TEST_USER_1)
    assert len(q.list_user_debts(user_id=TEST_USER_1, auto_sync=False)) == 0


def test_debt_sync_from_plaid():
    q.c.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, subtype, current_balance, active)
        VALUES
        (?, 'plaid_cc_1', 'Chase Sapphire', 'credit', 'credit card', 2500.0, 1),
        (?, 'plaid_loan_1', 'SoFi Student Loan', 'loan', 'student', 8000.0, 1),
        (?, 'plaid_dep_1', 'Checking', 'depository', 'checking', 3000.0, 1)
    """, (TEST_USER_1, TEST_USER_1, TEST_USER_1))
    q.conn.commit()

    synced = q.sync_debts_from_plaid(user_id=TEST_USER_1)
    assert synced == 2

    debts = q.list_user_debts(user_id=TEST_USER_1, auto_sync=False)
    assert len(debts) == 2
    names = [d["name"] for d in debts]
    assert any("Chase Sapphire" in n for n in names)
    assert any("SoFi Student Loan" in n for n in names)


def test_snowball_vs_avalanche_payoff():
    q.add_or_update_debt(user_id=TEST_USER_1, name="Small Loan", balance=1000.0, apr=12.0, min_payment=30.0)
    q.add_or_update_debt(user_id=TEST_USER_1, name="High APR Card", balance=4000.0, apr=26.0, min_payment=120.0)
    q.add_or_update_debt(user_id=TEST_USER_1, name="Student Loan", balance=10000.0, apr=6.0, min_payment=200.0)

    avalanche_res = q.calculate_debt_payoff(strategy="avalanche", extra_monthly=200.0, user_id=TEST_USER_1)
    assert avalanche_res["status"] == "success"
    assert avalanche_res["strategy"] == "avalanche"

    snowball_res = q.calculate_debt_payoff(strategy="snowball", extra_monthly=200.0, user_id=TEST_USER_1)
    assert snowball_res["status"] == "success"
    assert snowball_res["strategy"] == "snowball"

    first_snowball_milestone = snowball_res["milestones"][0]["name"]
    assert first_snowball_milestone == "Small Loan"

    assert avalanche_res["total_interest_paid"] <= snowball_res["total_interest_paid"]
    assert "comparison" in avalanche_res
    assert avalanche_res["comparison"]["alt_strategy"] == "snowball"
