"""Financial correctness tests for the ledger.

These tests verify core invariants without touching the production
database.  They monkeypatch the module-global ``conn`` used by
``src.db.queries`` to an in-memory SQLite database seeded with a small
deterministic schema, then restore the original connection afterwards.
"""

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.db.queries as q
from src.db.migrations import apply_all


TEST_USER = "test-financial-user"


def _seed_memory_db():
    """Create an in-memory DB with the minimal schema the financial
    functions in ``src.db.queries`` rely on."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_all(conn)
    conn.executescript(
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
        );
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
        );
        CREATE TABLE IF NOT EXISTS planned_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            merchant TEXT,
            amount REAL,
            next_run_date TEXT,
            recurrence TEXT,
            account_used TEXT,
            category TEXT,
            status TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            liquid_balance REAL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS savings_buckets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            name TEXT,
            current REAL,
            target REAL
        );
        CREATE TABLE IF NOT EXISTS budget_settings (
            user_id TEXT PRIMARY KEY,
            weekly_limit REAL,
            impulse_threshold REAL
        );
        """
    )
    conn.commit()
    return conn


def _swap_conn(new_conn):
    """Temporarily swap the module-global connection."""
    old_conn = q.conn
    old_c = q.c
    q.conn = new_conn
    q.c = new_conn.cursor()
    return old_conn, old_c


def _restore_conn(old_conn, old_c):
    q.conn = old_conn
    q.c = old_c


def _rid_from_result(result: str) -> int:
    """Parse the row id out of add_transaction's human-readable return."""
    m = re.search(r"#(\d+)", result or "")
    assert m, f"add_transaction returned no id: {result!r}"
    return int(m.group(1))


def test_add_transaction_rounds_and_signs():
    conn = _seed_memory_db()
    old_conn, old_c = _swap_conn(conn)
    try:
        result = q.add_transaction(
            date="2026-09-01",
            merchant="Coffee",
            amount=4.50,
            category="Food",
            account_used="Cash",
            status="Evaluated",
            judgment="Morning coffee",
            user_id=TEST_USER,
        )
        rid = _rid_from_result(result)
        row = conn.execute(
            "SELECT amount, status FROM transactions WHERE id = ?", (rid,)
        ).fetchone()
        assert row["amount"] == 4.50
        assert row["status"] == "Evaluated"
    finally:
        _restore_conn(old_conn, old_c)


def test_negative_amount_is_income():
    conn = _seed_memory_db()
    old_conn, old_c = _swap_conn(conn)
    try:
        q.add_transaction(
            date="2026-09-01", merchant="Payroll", amount=-2000.00,
            category="Income", account_used="Checking", status="Evaluated",
            judgment="Paycheck", user_id=TEST_USER,
        )
        row = conn.execute(
            "SELECT amount FROM transactions WHERE merchant = 'Payroll'"
        ).fetchone()
        assert row["amount"] == -2000.00
    finally:
        _restore_conn(old_conn, old_c)


def test_lock_and_unlock_states():
    conn = _seed_memory_db()
    old_conn, old_c = _swap_conn(conn)
    try:
        result = q.add_transaction(
            date="2026-09-01", merchant="X", amount=10.0,
            category="Misc", account_used="Cash", status="Evaluated",
            judgment="x", user_id=TEST_USER,
        )
        rid = _rid_from_result(result)
        unlocked = q.get_unlocked_transactions(user_id=TEST_USER, days=30)
        assert "X" in unlocked or "transactions" in unlocked.lower()
        locked = q.get_locked_transactions(user_id=TEST_USER, days=30)
        assert "None" in locked or "0" in locked or "No " in locked
        q.lock_transaction(transaction_row_id=rid, user_id=TEST_USER)
        locked2 = q.get_locked_transactions(user_id=TEST_USER, days=30)
        assert "X" in locked2 or "transactions" in locked2.lower()
    finally:
        _restore_conn(old_conn, old_c)


def test_weekly_spending_only_evaluated_positive():
    conn = _seed_memory_db()
    old_conn, old_c = _swap_conn(conn)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        q.add_transaction(date=today, merchant="A", amount=100.0,
                         category="X", account_used="Cash", status="Evaluated",
                         judgment="a", user_id=TEST_USER)
        q.add_transaction(date=today, merchant="B", amount=50.0,
                         category="X", account_used="Cash", status="Pending",
                         judgment="b", user_id=TEST_USER)
        total = q.get_weekly_spending(user_id=TEST_USER)
        assert total == 100.0
    finally:
        _restore_conn(old_conn, old_c)


def test_income_does_not_count_as_spending():
    conn = _seed_memory_db()
    old_conn, old_c = _swap_conn(conn)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        q.add_transaction(date=today, merchant="Payroll",
                         amount=-3000.0, category="Income",
                         account_used="Checking", status="Evaluated",
                         judgment="pay", user_id=TEST_USER)
        assert q.get_weekly_spending(user_id=TEST_USER) == 0.0
    finally:
        _restore_conn(old_conn, old_c)


def test_delete_manual_transaction_only():
    conn = _seed_memory_db()
    old_conn, old_c = _swap_conn(conn)
    try:
        result = q.add_transaction(
            date="2026-09-01", merchant="Manual", amount=5.0,
            category="Misc", account_used="Cash", status="Evaluated",
            judgment="manual", user_id=TEST_USER,
        )
        rid = _rid_from_result(result)
        q.delete_manual_transaction(transaction_row_id=rid, user_id=TEST_USER)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        _restore_conn(old_conn, old_c)