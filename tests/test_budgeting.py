import pytest
from src.services.budgeting import (
    predict_next_paydays,
    calculate_locked_liabilities,
    calculate_credit_float_velocity,
    get_safe_to_spend_metrics,
    calculate_emergency_fund_health
)

def test_predict_next_paydays():
    res = predict_next_paydays('342385739952160769')
    assert "Predicted Cadence" in res or "Insufficient" in res

def test_calculate_locked_liabilities():
    res = calculate_locked_liabilities('342385739952160769', '2026-10-01')
    assert "Total Locked Liabilities" in res

def test_calculate_credit_float_velocity():
    res = calculate_credit_float_velocity('342385739952160769')
    assert "Liquid Cash" in res
    assert "Credit Card Float" in res

def test_get_safe_to_spend_metrics():
    res = get_safe_to_spend_metrics('342385739952160769')
    assert "ZERO-BASED BUDGET: SAFE-TO-SPEND" in res

def test_calculate_emergency_fund_health():
    res = calculate_emergency_fund_health('342385739952160769')
    assert "EMERGENCY FUND HEALTH & RUNWAY AUDIT" in res
    assert "Bare-Bones Survival Runway" in res


def test_render_progress_bar():
    from src.services.budgeting import render_progress_bar
    assert render_progress_bar(0.0) == "[░░░░░░░░░░] 0%"
    assert render_progress_bar(50.0) == "[█████░░░░░] 50%"
    assert render_progress_bar(100.0) == "[██████████] 100%"
    assert render_progress_bar(125.0) == "[██████████] 125%"


def test_category_budgets_crud_and_isolation(tmp_path):
    import sqlite3
    import src.db.queries as q

    test_conn = sqlite3.connect(tmp_path / "budget_test.db")
    test_conn.executescript("""
        CREATE TABLE category_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL,
            monthly_limit REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, category)
        );
    """)

    orig_conn, orig_c = q.conn, q.c
    q.conn = test_conn
    q.c = test_conn.cursor()
    try:
        # Set budget for User 1
        res1 = q.set_category_budget(user_id="user_1", category="Groceries", monthly_limit=600.0)
        assert res1["status"] == "success"
        assert res1["category"] == "Groceries"
        assert res1["monthly_limit"] == 600.0

        # Update budget for User 1
        res1_up = q.set_category_budget(user_id="user_1", category="Groceries", monthly_limit=650.0)
        assert res1_up["monthly_limit"] == 650.0

        # Set budget for Dining Out
        q.set_category_budget(user_id="user_1", category="Dining Out", monthly_limit=250.0)

        # User 2 sets budget for Groceries
        q.set_category_budget(user_id="user_2", category="Groceries", monthly_limit=400.0)

        # Verify User 1 lists only their own budgets
        u1_budgets = q.get_category_budgets(user_id="user_1")
        assert len(u1_budgets) == 2
        cats = {b["category"]: b["monthly_limit"] for b in u1_budgets}
        assert cats["Groceries"] == 650.0
        assert cats["Dining Out"] == 250.0

        # Verify User 2 lists only their own budgets
        u2_budgets = q.get_category_budgets(user_id="user_2")
        assert len(u2_budgets) == 1
        assert u2_budgets[0]["monthly_limit"] == 400.0

        # Delete Dining Out for User 1
        assert q.delete_category_budget(user_id="user_1", category="Dining Out") is True
        assert len(q.get_category_budgets(user_id="user_1")) == 1

        # User 2 cannot delete User 1's Groceries
        q.delete_category_budget(user_id="user_2", category="Groceries")
        assert len(q.get_category_budgets(user_id="user_1")) == 1
    finally:
        q.conn = orig_conn
        q.c = orig_c
        test_conn.close()


def test_spending_pace_and_forecast_calculation(tmp_path):
    import sqlite3
    from datetime import datetime
    from src.services.budgeting import calculate_spending_pace_and_forecast, format_spending_pace_report

    test_conn = sqlite3.connect(tmp_path / "pace_test.db")
    test_conn.executescript("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            date TEXT,
            merchant TEXT,
            amount REAL,
            category TEXT
        );
        CREATE TABLE category_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            category TEXT,
            monthly_limit REAL,
            updated_at TEXT,
            UNIQUE(user_id, category)
        );
    """)

    cur = test_conn.cursor()
    # Month: September 2026 (30 days). Evaluation on Day 15 (50% elapsed).
    # Budgets:
    #   Groceries: limit $600. Spent: $250. Expected to date: $300. Pace: 0.83x (ON TRACK)
    #   Dining Out: limit $200. Spent: $180. Expected to date: $100. Pace: 1.80x (OVERPACING)
    #   Streaming: limit $50. Spent: $60. Expected to date: $25. Pace: 2.40x (BLOWN)
    cur.execute("INSERT INTO category_budgets (user_id, category, monthly_limit) VALUES ('user_pace', 'Groceries', 600.0)")
    cur.execute("INSERT INTO category_budgets (user_id, category, monthly_limit) VALUES ('user_pace', 'Dining Out', 200.0)")
    cur.execute("INSERT INTO category_budgets (user_id, category, monthly_limit) VALUES ('user_pace', 'Streaming', 50.0)")

    cur.execute("""
        INSERT INTO transactions (user_id, date, merchant, amount, category)
        VALUES 
        ('user_pace', '2026-09-02', 'Trader Joes', 250.0, 'Groceries'),
        ('user_pace', '2026-09-05', 'Sushi Bar', 180.0, 'Dining Out'),
        ('user_pace', '2026-09-08', 'Netflix & Spotify', 60.0, 'Streaming'),
        ('user_pace', '2026-09-10', 'Best Buy', 100.0, 'Electronics'),
        ('user_pace', '2026-09-01', 'Employer Payroll', -4000.0, 'Income')
    """)
    test_conn.commit()

    eval_date = datetime(2026, 9, 15, 12, 0, 0)
    data = calculate_spending_pace_and_forecast("user_pace", now=eval_date, conn=test_conn)

    period = data["period"]
    assert period["current_day"] == 15
    assert period["days_in_month"] == 30
    assert period["remaining_days"] == 15
    assert period["elapsed_pct"] == 50.0

    spend = data["spending_summary"]
    # Total spend: 250 + 180 + 60 + 100 = 590.0
    assert spend["total_spent_to_date"] == 590.0
    assert spend["current_daily_burn"] == round(590.0 / 15, 2)
    assert spend["projected_month_spend"] == 1180.0
    assert spend["realized_income"] == 4000.0
    assert spend["projected_net_savings"] == 4000.0 - 1180.0

    cats = {c["category"]: c for c in data["category_budgets"]}
    assert cats["Groceries"]["status"] == "ON TRACK"
    assert cats["Groceries"]["pace_ratio"] < 1.0
    assert cats["Dining Out"]["status"] == "OVERPACING"
    assert cats["Streaming"]["status"] == "BLOWN"
    assert cats["Streaming"]["remaining_daily_cap"] == 0.0

    # Unbudgeted
    unbudgeted = {u["category"]: u for u in data["unbudgeted_categories"]}
    assert "Electronics" in unbudgeted
    assert unbudgeted["Electronics"]["spent"] == 100.0

    # Format text report
    report = format_spending_pace_report(data)
    assert "MONTHLY BUDGET & PACING: SEPTEMBER 2026" in report
    assert "Groceries" in report
    assert "OVERPACING" in report
    assert "BLOWN" in report
    test_conn.close()


@pytest.mark.asyncio
async def test_discord_budget_commands(tmp_path):
    import sqlite3
    from unittest.mock import AsyncMock, MagicMock, patch
    import src.db.queries as q
    from src.bot.commands import budget_cmd, set_cat_budget_cmd, del_cat_budget_cmd, pacing_cmd

    test_conn = sqlite3.connect(tmp_path / "discord_budget.db")

    test_conn.executescript("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            date TEXT,
            merchant TEXT,
            amount REAL,
            category TEXT
        );
        CREATE TABLE category_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            category TEXT,
            monthly_limit REAL,
            updated_at TEXT,
            UNIQUE(user_id, category)
        );
    """)

    mock_ctx = MagicMock()
    mock_ctx.author.id = 8888
    mock_ctx.invoked_subcommand = None
    mock_ctx.send = AsyncMock()

    with patch("src.core.state.DB_PATH", str(tmp_path / "discord_budget.db")):
        # 1. Set budget command
        orig_conn, orig_c = q.conn, q.c
        q.conn = test_conn
        q.c = test_conn.cursor()
        try:
            await set_cat_budget_cmd(mock_ctx, category="Groceries", monthly_limit=550.0)
            assert "Set monthly budget for **Groceries**" in mock_ctx.send.call_args.args[0]

            # 2. View budget dashboard
            await budget_cmd(mock_ctx)
            embed = mock_ctx.send.call_args.kwargs.get("embed")
            assert embed is not None
            assert "Monthly Budget & Pacing" in embed.title

            # 3. Pacing report
            await pacing_cmd(mock_ctx)
            pacing_text = mock_ctx.send.call_args.args[0]
            assert "MONTHLY BUDGET & PACING" in pacing_text

            # 4. Delete budget command
            await del_cat_budget_cmd(mock_ctx, category="Groceries")
            assert "Removed monthly budget for **Groceries**" in mock_ctx.send.call_args.args[0]
        finally:
            q.conn = orig_conn
            q.c = orig_c
            test_conn.close()

