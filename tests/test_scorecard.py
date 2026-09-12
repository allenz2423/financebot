import pytest
import sqlite3
import discord

from src.services.scorecard import calculate_financial_health_scorecard, format_scorecard_embed


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
    c.execute("""
        CREATE TABLE investment_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            symbol TEXT,
            shares REAL,
            cost_basis REAL,
            current_price REAL,
            asset_class TEXT
        )
    """)
    c.execute("""
        CREATE TABLE savings_buckets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            name TEXT,
            target_amount REAL,
            current_amount REAL,
            target_date TEXT,
            category TEXT,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE user_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            debt_name TEXT,
            balance REAL,
            min_payment REAL,
            apr REAL
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def test_scorecard_isolation():
    with pytest.raises(ValueError, match="forced isolation violation"):
        calculate_financial_health_scorecard(user_id="")

    with pytest.raises(ValueError, match="forced isolation violation"):
        calculate_financial_health_scorecard(user_id=None)  # type: ignore


def test_scorecard_empty_user(test_db):
    res = calculate_financial_health_scorecard(user_id="empty_user", conn=test_db)
    assert 0 <= res["total_score"] <= 100
    assert len(res["pillars"]) == 5
    assert res["grade"] in ["A+", "A", "B", "C", "D", "F"]
    assert res["weakest_pillar"] is not None

    embed = format_scorecard_embed(res)
    assert isinstance(embed, discord.Embed)
    assert "Financial Health Scorecard" in embed.title


def test_scorecard_fortress_profile_a_plus(test_db):
    user_id = "fortress_user"
    c = test_db.cursor()

    # 1. Depository Cash: $30,000 checking/savings
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, current_balance, available_balance, credit_limit, active)
        VALUES ('dep1', ?, 'Fidelity Cash', 'depository', 'checking', 30000.0, 30000.0, 0.0, 1),
               ('cred1', ?, 'Chase Sapphire', 'credit', 'credit card', 150.0, 9850.0, 10000.0, 1)
    """, (user_id, user_id))

    # 2. Monthly burn $2,500, Income $6,000 (Savings rate > 50%)
    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('tx1', ?, -6000.0, date('now', '-5 days'), 'Payroll', 'Employer', 'Income', 'Evaluated'),
               ('tx2', ?, 2500.0, date('now', '-10 days'), 'Living Expenses', 'Various', 'Living', 'Evaluated')
    """, (user_id, user_id))

    # 3. Investment holdings: $85,000
    c.execute("""
        INSERT INTO investment_holdings (user_id, symbol, shares, cost_basis, current_price, asset_class)
        VALUES (?, 'VTI', 200, 200.0, 250.0, 'Equities'),
               (?, 'BND', 400, 75.0, 87.50, 'Fixed Income')
    """, (user_id, user_id))

    # 4. Sinking fund: $10,000 emergency fund
    c.execute("""
        INSERT INTO savings_buckets (user_id, name, target_amount, current_amount, updated_at)
        VALUES (?, 'Emergency Fund', 15000.0, 10000.0, datetime('now'))
    """, (user_id,))
    test_db.commit()

    res = calculate_financial_health_scorecard(user_id=user_id, conn=test_db)
    assert res["total_score"] >= 90
    assert res["grade"] == "A+"
    assert res["badge"] == "🏆"
    assert res["metrics"]["runway_months"] >= 6.0
    assert res["metrics"]["credit_utilization_pct"] < 10.0


def test_scorecard_distressed_profile_grade_f(test_db):
    user_id = "distressed_user"
    c = test_db.cursor()

    # 1. Depository cash: only $200
    c.execute("""
        INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, subtype, current_balance, available_balance, credit_limit, active)
        VALUES ('dep2', ?, 'Checking', 'depository', 'checking', 200.0, 200.0, 0.0, 1),
               ('cred2', ?, 'Maxed Card', 'credit', 'credit card', 4800.0, 200.0, 5000.0, 1)
    """, (user_id, user_id))

    # 2. Deficit burn: $3,000 outflow with only $1,000 inflow
    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('tx_in', ?, -1000.0, date('now', '-4 days'), 'Gig Income', 'App', 'Income', 'Evaluated'),
               ('tx_out', ?, 3000.0, date('now', '-6 days'), 'High Spend', 'Stores', 'Shopping', 'Evaluated')
    """, (user_id, user_id))

    # 3. Debt loan: $15,000
    c.execute("""
        INSERT INTO user_debts (user_id, debt_name, balance, min_payment, apr)
        VALUES (?, 'Personal Loan', 15000.0, 350.0, 18.5)
    """, (user_id,))
    test_db.commit()

    res = calculate_financial_health_scorecard(user_id=user_id, conn=test_db)
    assert res["total_score"] < 50
    assert res["grade"] == "F"
    assert res["badge"] == "🚨"
    assert len(res["recommendations"]) > 0
    assert "Pay down" in res["highest_leverage_action"] or "Build emergency fund" in res["highest_leverage_action"]
