import os
import sqlite3
import pytest
from datetime import datetime
import src.db.queries as q
from src.services.report_generator import (
    latex_escape,
    build_latex_digest,
    export_financial_digest,
)
from src.db.migrations import apply_all

TEST_USER_1 = "user-digest-1"
TEST_USER_2 = "user-digest-2"

@pytest.fixture(autouse=True)
def setup_digest_db(monkeypatch):
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_c = test_conn.cursor()

    apply_all(test_conn)

    test_c.executescript("""
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
        CREATE TABLE IF NOT EXISTS roundup_settings (
            user_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            target_bucket_name TEXT NOT NULL DEFAULT 'Emergency Fund',
            multiplier REAL NOT NULL DEFAULT 1.0,
            whole_dollar_roundup REAL NOT NULL DEFAULT 1.0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)

    monkeypatch.setattr(q, "conn", test_conn)
    monkeypatch.setattr(q, "c", test_c)
    yield test_conn
    test_conn.close()


def test_generate_financial_digest_calculation_and_isolation():
    # User 1: Inflow $5,000 (Paycheck), Outflows $2,000 across categories
    # Inflow is negative amount in Delilah ledger
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date, category) VALUES (?, 'Acme Corp Payroll', -5000.0, date('now'), 'Income')",
        (TEST_USER_1,)
    )
    # Expenses
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date, category) VALUES (?, 'Supermarket & Deli', 600.0, date('now'), 'Groceries')",
        (TEST_USER_1,)
    )
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date, category) VALUES (?, 'Electric Utility', 400.0, date('now'), 'Utilities')",
        (TEST_USER_1,)
    )
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date, category) VALUES (?, 'Tech Gadget Store', 1000.0, date('now'), 'Electronics')",
        (TEST_USER_1,)
    )

    # Debt: $2000 balance, $100 min payment
    q.c.execute(
        "INSERT INTO user_debts (user_id, name, balance, apr, min_payment) VALUES (?, 'Visa Card', 2000.0, 18.5, 100.0)",
        (TEST_USER_1,)
    )

    # Sinking fund: $1,500 / $3,000 (50%)
    q.c.execute(
        "INSERT INTO savings_buckets (user_id, name, target_amount, current_amount) VALUES (?, 'Emergency Fund', 3000.0, 1500.0)",
        (TEST_USER_1,)
    )

    # User 2 transaction
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date, category) VALUES (?, 'User2 Store', 300.0, date('now'), 'Shopping')",
        (TEST_USER_2,)
    )
    q.safe_commit()

    # Generate monthly digest for User 1
    d1 = q.generate_financial_digest(period="monthly", user_id=TEST_USER_1)
    assert d1["status"] == "success"
    assert d1["period_label"] == "Monthly"
    assert d1["inflow"] == 5000.0
    assert d1["outflow"] == 2000.0
    assert d1["net_cash_flow"] == 3000.0
    assert d1["savings_rate_pct"] == 60.0
    assert d1["transaction_count"] == 4

    # Top categories
    cats = d1["category_spending"]
    assert len(cats) == 3
    assert cats[0]["category"] == "Electronics"
    assert cats[0]["amount"] == 1000.0
    assert cats[0]["percentage"] == 50.0

    # Top transactions
    top_tx = d1["top_transactions"]
    assert len(top_tx) == 3
    assert top_tx[0]["merchant"] == "Tech Gadget Store"
    assert top_tx[0]["amount"] == 1000.0

    # Debt & Sinking fund summary
    assert d1["debt_summary"]["total_debt"] == 2000.0
    assert d1["sinking_funds_summary"]["total_saved"] == 1500.0
    assert d1["sinking_funds_summary"]["progress_pct"] == 50.0

    # Financial health score & Grade
    assert d1["financial_health_score"] >= 70
    assert d1["grade"] in ("A+", "A", "B")

    # User 2 isolation check
    d2 = q.generate_financial_digest(period="monthly", user_id=TEST_USER_2)
    assert d2["inflow"] == 0.0
    assert d2["outflow"] == 300.0
    assert d2["debt_summary"]["total_debt"] == 0.0

    # Forced isolation check
    with pytest.raises(ValueError):
        q.generate_financial_digest(period="monthly", user_id="")


def test_latex_digest_formatting_and_escaping():
    # Test escaping special LaTeX characters
    raw = "AT&T 10% Off #1 {Gadgets} & Co. $50_Bill ~100^2 \\test"
    escaped = latex_escape(raw)
    assert r"\&" in escaped
    assert r"\%" in escaped
    assert r"\#" in escaped
    assert r"\{" in escaped
    assert r"\}" in escaped
    assert r"\$" in escaped
    assert r"\_" in escaped

    # Build dummy digest
    digest = {
        "period_label": "Monthly",
        "financial_health_score": 88,
        "grade": "A",
        "inflow": 6500.0,
        "outflow": 3200.0,
        "net_cash_flow": 3300.0,
        "savings_rate_pct": 50.8,
        "transaction_count": 42,
        "debt_summary": {"debt_count": 1, "total_debt": 1500.0},
        "sinking_funds_summary": {"total_saved": 4000.0, "total_target": 5000.0, "progress_pct": 80.0},
        "active_trials_count": 2,
        "category_spending": [
            {"category": "Rent & Housing", "amount": 1800.0, "percentage": 56.2, "count": 1},
            {"category": "Groceries_&_Food", "amount": 600.0, "percentage": 18.8, "count": 15},
        ],
        "top_transactions": [
            {"date": "2026-09-01", "merchant": "Landlord & Property LLC", "category": "Rent & Housing", "amount": 1800.0}
        ]
    }

    tex = build_latex_digest(digest, user_name="Alice & Bob")
    assert "\\documentclass" in tex
    assert "Financial Health Scorecard" in tex
    assert "Alice \\& Bob" in tex
    assert "Rent \\& Housing" in tex
    assert "\\end{document}" in tex


def test_export_financial_digest_file_generation(tmp_path):
    # Setup transactions
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date, category) VALUES (?, 'Employer Inc', -4000.0, date('now'), 'Salary')",
        (TEST_USER_1,)
    )
    q.c.execute(
        "INSERT INTO transactions (user_id, merchant, amount, date, category) VALUES (?, 'Grocery Mart', 350.0, date('now'), 'Food')",
        (TEST_USER_1,)
    )
    q.safe_commit()

    res = export_financial_digest(
        period="weekly",
        user_id=TEST_USER_1,
        user_name="Tester",
        output_dir=str(tmp_path)
    )
    assert res["digest"] is not None
    assert res["tex_path"] is not None
    assert os.path.exists(res["tex_path"])
    assert res["tex_path"].endswith(".tex")

    with open(res["tex_path"], "r", encoding="utf-8") as f:
        content = f.read()
    assert "\\documentclass" in content
    assert "Tester" in content
    assert "Weekly" in content
