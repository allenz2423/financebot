import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta

from src.services.net_worth import (
    calculate_unified_net_worth,
    format_net_worth_embed,
    format_net_worth_report,
)
from src.db.migrations import apply_all


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    apply_all(conn)
    # Ensure plaid_accounts table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plaid_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            plaid_account_id TEXT UNIQUE,
            name TEXT,
            official_name TEXT,
            type TEXT,
            subtype TEXT,
            current_balance REAL DEFAULT 0.0,
            available_balance REAL DEFAULT 0.0,
            credit_limit REAL DEFAULT 0.0,
            iso_currency_code TEXT DEFAULT 'USD',
            active INTEGER DEFAULT 1,
            last_synced TEXT
        )
    """)
    # Ensure savings_buckets table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS savings_buckets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target_amount REAL DEFAULT 0.0,
            current_amount REAL DEFAULT 0.0,
            category TEXT DEFAULT 'General',
            target_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Ensure user_debts table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            balance REAL NOT NULL,
            apr REAL DEFAULT 0.0,
            min_payment REAL DEFAULT 0.0,
            plaid_account_id TEXT
        )
    """)
    # Ensure financial_snapshots table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS financial_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            net_worth REAL NOT NULL,
            liquid_cash REAL DEFAULT 0.0,
            total_debt REAL DEFAULT 0.0,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def test_calculate_unified_net_worth_empty(memory_db):
    res = calculate_unified_net_worth(user_id="user_empty", conn=memory_db)
    assert res["user_id"] == "user_empty"
    assert res["assets"]["total_assets"] == 0.0
    assert res["liabilities"]["total_liabilities"] == 0.0
    assert res["net_worth"] == 0.0
    assert res["solvency"]["debt_to_asset_ratio"] == 0.0
    assert "Fortress Solvency" in res["solvency"]["status"]
    assert res["history"]["delta_30d"] is None


def test_calculate_unified_net_worth_with_assets_and_debts(memory_db):
    user_a = "user_100"
    user_b = "user_200"

    # Insert Plaid depository cash for user_a
    memory_db.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, subtype, current_balance, available_balance)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_a, "acc_dep_1", "Chase Premier Checking", "depository", "checking", 5000.0, 4800.0))

    # Insert investment holdings for user_a
    memory_db.execute("""
        INSERT INTO investment_holdings (user_id, symbol, shares, cost_basis, current_price, asset_class)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_a, "VOO", 10.0, 400.0, 500.0, "Equities"))

    # Insert sinking funds for user_a
    memory_db.execute("""
        INSERT INTO savings_buckets (user_id, name, current_amount, target_amount, category)
        VALUES (?, ?, ?, ?, ?)
    """, (user_a, "Emergency Fund", 3000.0, 10000.0, "Emergency"))

    # Insert Plaid credit debt for user_a
    memory_db.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, subtype, current_balance, credit_limit)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_a, "acc_cred_1", "Amex Gold", "credit", "credit card", 1500.0, 10000.0))

    # Insert user debt / loan for user_a
    memory_db.execute("""
        INSERT INTO user_debts (user_id, name, balance, apr, min_payment)
        VALUES (?, ?, ?, ?, ?)
    """, (user_a, "Auto Loan", 4500.0, 4.5, 250.0))

    # Insert user_b data to verify strict isolation
    memory_db.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, subtype, current_balance)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_b, "acc_b_1", "Offshore Vault", "depository", "savings", 1000000.0))
    memory_db.commit()

    now = datetime(2026, 9, 12, 12, 0, 0)
    data = calculate_unified_net_worth(user_id=user_a, conn=memory_db, now=now)

    # Assets: cash 5000 + investments 5000 (10 * 500) + sinking 3000 = 13000
    assert data["assets"]["depository_cash"] == 5000.0
    assert data["assets"]["investments_value"] == 5000.0
    assert data["assets"]["investments_cost_basis"] == 4000.0
    assert data["assets"]["investments_unrealized_pnl"] == 1000.0
    assert data["assets"]["sinking_funds"] == 3000.0
    assert data["assets"]["total_assets"] == 13000.0

    # Liabilities: credit 1500 + loan 4500 = 6000
    assert data["liabilities"]["credit_card_debt"] == 1500.0
    assert data["liabilities"]["term_loans_debt"] == 4500.0
    assert data["liabilities"]["total_liabilities"] == 6000.0

    # Net Worth: 13000 - 6000 = 7000
    assert data["net_worth"] == 7000.0
    # Leverage: 6000 / 13000 = 0.4615 (Moderate Leverage)
    assert round(data["solvency"]["debt_to_asset_ratio"], 2) == 0.46
    assert "Moderate Leverage" in data["solvency"]["status"]

    # Verify user_b is untouched by user_a
    data_b = calculate_unified_net_worth(user_id=user_b, conn=memory_db, now=now)
    assert data_b["assets"]["total_assets"] == 1000000.0
    assert data_b["liabilities"]["total_liabilities"] == 0.0
    assert data_b["net_worth"] == 1000000.0


def test_calculate_unified_net_worth_historical_deltas(memory_db):
    user_id = "user_hist"
    now = datetime(2026, 9, 12, 12, 0, 0)

    # Insert current cash
    memory_db.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, subtype, current_balance)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, "acc_hist_1", "Checking", "depository", "checking", 20000.0))

    # Insert snapshots 30 days ago and 90 days ago
    ts_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    ts_90d = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    memory_db.execute("""
        INSERT INTO financial_snapshots (user_id, net_worth, captured_at)
        VALUES (?, ?, ?)
    """, (user_id, 16000.0, ts_30d))
    memory_db.execute("""
        INSERT INTO financial_snapshots (user_id, net_worth, captured_at)
        VALUES (?, ?, ?)
    """, (user_id, 10000.0, ts_90d))
    memory_db.commit()

    data = calculate_unified_net_worth(user_id=user_id, conn=memory_db, now=now)
    assert data["net_worth"] == 20000.0
    assert data["history"]["delta_30d"] is not None
    # 20000 - 16000 = 4000 (+25.0%)
    assert data["history"]["delta_30d"]["diff"] == 4000.0
    assert data["history"]["delta_30d"]["pct_change"] == 25.0

    # 90d delta: 20000 - 10000 = 10000 (+100.0%)
    assert data["history"]["delta_90d"]["diff"] == 10000.0
    assert data["history"]["delta_90d"]["pct_change"] == 100.0


def test_insolvent_status_and_formatting(memory_db):
    user_id = "user_broke"
    # Assets: 1000, Debt: 5000 -> Net Worth: -4000
    memory_db.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, subtype, current_balance)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, "acc_broke_1", "Checking", "depository", "checking", 1000.0))
    memory_db.execute("""
        INSERT INTO user_debts (user_id, name, balance)
        VALUES (?, ?, ?)
    """, (user_id, "Maxed Card", 5000.0))
    memory_db.commit()

    data = calculate_unified_net_worth(user_id=user_id, conn=memory_db)
    assert data["net_worth"] == -4000.0
    assert "Negative Equity" in data["solvency"]["status"]

    embed = format_net_worth_embed(data)
    assert "Balance Sheet & Net Worth Statement" in embed.title
    assert "-$4,000.00" in embed.description
    assert any("Liabilities" in f.name for f in embed.fields)

    report = format_net_worth_report(data)
    assert "BALANCE SHEET" in report
    assert "NET WORTH:                  -$4,000.00" in report


def test_forced_isolation_error(memory_db):
    with pytest.raises(ValueError, match="forced isolation violation"):
        calculate_unified_net_worth(user_id="")

    with pytest.raises(ValueError, match="forced isolation violation"):
        calculate_unified_net_worth(user_id=None)


@pytest.mark.asyncio
async def test_networth_discord_command(memory_db, monkeypatch):
    import src.services.net_worth as nw_mod
    from src.bot.commands import networth_cmd

    # Seed some data
    memory_db.execute("""
        INSERT INTO plaid_accounts (user_id, plaid_account_id, name, type, subtype, current_balance)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("ctx_user_1", "acc_ctx_1", "Checking", "depository", "checking", 7500.0))
    memory_db.commit()

    orig_calc = nw_mod.calculate_unified_net_worth
    def mock_calc(user_id, conn=None, now=None):
        return orig_calc(user_id=user_id, conn=memory_db, now=now)
    monkeypatch.setattr(nw_mod, "calculate_unified_net_worth", mock_calc)

    ctx = MagicMock()
    ctx.author.id = "ctx_user_1"
    ctx.send = AsyncMock()

    await networth_cmd(ctx)
    ctx.send.assert_called_once()
    embed = ctx.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "$7,500.00" in embed.description
