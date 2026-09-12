import pytest
import sqlite3

from src.services.cash_drag import analyze_cash_drag, format_cash_drag_report


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
            institution_name TEXT,
            current_balance REAL,
            available_balance REAL,
            credit_limit REAL,
            active INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE transactions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            account_id TEXT,
            amount REAL,
            date TEXT,
            name TEXT,
            merchant TEXT,
            category TEXT,
            status TEXT
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def test_analyze_cash_drag_isolation():
    with pytest.raises(ValueError, match="forced isolation violation"):
        analyze_cash_drag(user_id="")

    with pytest.raises(ValueError, match="forced isolation violation"):
        analyze_cash_drag(user_id=None)  # type: ignore


def test_analyze_cash_drag_empty_state(test_db):
    res = analyze_cash_drag(user_id="empty_user", conn=test_db)
    assert res["total_liquid_cash"] == 0.0
    assert res["total_checking_cash"] == 0.0
    assert res["total_savings_cash"] == 0.0
    assert res["excess_idle_cash"] == 0.0
    assert res["annual_lost_interest"] == 0.0
    assert "No active depository bank accounts found" in res["action_plan"][0]

    report = format_cash_drag_report(res)
    assert "No active bank accounts found" in report


def test_analyze_cash_drag_with_idle_checking_cash(test_db):
    user_id = "user_drag_1"
    c = test_db.cursor()

    # User has $15,000 in Chase Checking and $2,000 in Ally HYSA
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, institution_name, current_balance, available_balance, active)
        VALUES ('chk1', ?, 'Chase Total Checking', 'depository', 'checking', 'Chase', 15000.0, 15000.0, 1),
               ('sav1', ?, 'Ally High Yield Savings', 'depository', 'savings', 'Ally', 2000.0, 2000.0, 1)
    """, (user_id, user_id))

    # User spent $3,000 in evaluated expenses over past 30 days
    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('tx1', ?, 1200.0, date('now', '-5 days'), 'Rent', 'Landlord', 'Rent', 'Evaluated'),
               ('tx2', ?, 800.0, date('now', '-10 days'), 'Groceries', 'Kroger', 'Groceries', 'Evaluated'),
               ('tx3', ?, 1000.0, date('now', '-15 days'), 'Dining', 'Various', 'Dining', 'Evaluated')
    """, (user_id, user_id, user_id))
    test_db.commit()

    # 1.5x buffer on $3,000/mo = $4,500 recommended buffer
    # Total checking = $15,000
    # Excess idle cash = $15,000 - $4,500 = $10,500
    # At 4.5% APY benchmark:
    # Annual lost interest = 10,500 * 0.045 = $472.50
    # Monthly lost interest = 472.50 / 12 = $39.38
    res = analyze_cash_drag(user_id=user_id, conn=test_db, benchmark_apy=0.045, buffer_months=1.5)

    assert res["total_liquid_cash"] == 17000.0
    assert res["total_checking_cash"] == 15000.0
    assert res["total_savings_cash"] == 2000.0
    assert res["monthly_burn_rate"] == 3000.0
    assert res["recommended_buffer"] == 4500.0
    assert res["excess_idle_cash"] == 10500.0
    assert res["annual_lost_interest"] == 472.50
    assert res["monthly_lost_interest"] == 39.38

    report = format_cash_drag_report(res)
    assert "High Cash Drag" in report
    assert "$10,500.00" in report
    assert "-$472.50/yr" in report
    assert "Move **$10,500.00** from **Chase Total Checking** to **Ally High Yield Savings**" in report


def test_analyze_cash_drag_optimized_state(test_db):
    user_id = "user_optimized"
    c = test_db.cursor()

    # User has $3,000 in checking, $40,000 in HYSA, spending $2,500/mo
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, institution_name, current_balance, available_balance, active)
        VALUES ('chk2', ?, 'Checking', 'depository', 'checking', 'Schwab', 3000.0, 3000.0, 1),
               ('sav2', ?, 'Marcus MMF', 'depository', 'savings', 'Marcus', 40000.0, 40000.0, 1)
    """, (user_id, user_id))

    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('tx10', ?, 2500.0, date('now', '-5 days'), 'All expenses', 'Various', 'Living', 'Evaluated')
    """, (user_id,))
    test_db.commit()

    # 1.5x on $2,500 = $3,750 recommended buffer.
    # Checking has $3,000, so excess idle cash is $0.
    res = analyze_cash_drag(user_id=user_id, conn=test_db)
    assert res["excess_idle_cash"] == 0.0
    assert res["annual_lost_interest"] == 0.0
    assert "Optimal checking allocation" in res["action_plan"][0]

    report = format_cash_drag_report(res)
    assert "Optimized Liquid Cash" in report
