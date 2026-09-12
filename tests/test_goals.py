import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta

from src.services.goals import (
    set_savings_goal,
    record_goal_contribution,
    delete_savings_goal,
    calculate_goal_milestones_and_deficits,
    format_goals_embed,
    format_goals_report,
)
from src.db.migrations import apply_all


@pytest.fixture
def goals_db():
    conn = sqlite3.connect(":memory:")
    apply_all(conn)
    yield conn
    conn.close()


def test_set_savings_goal_and_isolation(goals_db):
    user_a = "user_goal_a"
    user_b = "user_goal_b"

    # User A creates a goal
    res_a = set_savings_goal(
        user_id=user_a,
        name="Emergency Reserve",
        target_amount=10000.0,
        target_date="2027-01-01",
        category="Safety",
        conn=goals_db,
    )
    assert res_a["status"] == "success"
    assert res_a["name"] == "Emergency Reserve"
    assert res_a["target_amount"] == 10000.0
    assert res_a["target_date"] == "2027-01-01"
    assert res_a["category"] == "Safety"

    # User B creates a goal with same name
    res_b = set_savings_goal(
        user_id=user_b,
        name="Emergency Reserve",
        target_amount=3000.0,
        target_date="2026-11-01",
        category="Buffer",
        conn=goals_db,
    )
    assert res_b["target_amount"] == 3000.0

    # User A updates their goal
    res_a2 = set_savings_goal(
        user_id=user_a,
        name="Emergency Reserve",
        target_amount=12000.0,
        target_date="2027-06-01",
        category="Safety Net",
        conn=goals_db,
    )
    assert res_a2["target_amount"] == 12000.0
    assert res_a2["category"] == "Safety Net"

    # Ensure User B's goal was not altered
    calc_b = calculate_goal_milestones_and_deficits(user_id=user_b, conn=goals_db)
    assert len(calc_b["goals"]) == 1
    assert calc_b["goals"][0]["target_amount"] == 3000.0


def test_set_savings_goal_validation(goals_db):
    with pytest.raises(ValueError, match="forced isolation violation"):
        set_savings_goal(user_id="", name="Test", target_amount=100.0, conn=goals_db)

    with pytest.raises(ValueError, match="Goal name cannot be empty"):
        set_savings_goal(user_id="user_1", name="  ", target_amount=100.0, conn=goals_db)

    with pytest.raises(ValueError, match="Target amount must be a positive number"):
        set_savings_goal(user_id="user_1", name="Test", target_amount=-50.0, conn=goals_db)

    with pytest.raises(ValueError, match="Target deadline must be in YYYY-MM-DD format"):
        set_savings_goal(user_id="user_1", name="Test", target_amount=100.0, target_date="tomorrow", conn=goals_db)


def test_record_goal_contribution_and_delete(goals_db):
    user_id = "user_contrib"

    # Contribution creates envelope if missing
    res1 = record_goal_contribution(
        user_id=user_id,
        name="Car Down Payment",
        amount=500.0,
        source="transfer",
        conn=goals_db
    )
    assert res1["status"] == "success"
    assert res1["new_balance"] == 500.0

    # Second contribution
    res2 = record_goal_contribution(
        user_id=user_id,
        name="Car Down Payment",
        amount=250.0,
        source="roundup",
        conn=goals_db
    )
    assert res2["new_balance"] == 750.0

    # Check contributions log
    cur = goals_db.execute("SELECT amount, source FROM bucket_contributions WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    assert len(rows) == 2

    # Delete goal
    del_res = delete_savings_goal(user_id=user_id, name="Car Down Payment", conn=goals_db)
    assert del_res["status"] == "success"

    del_res2 = delete_savings_goal(user_id=user_id, name="Nonexistent", conn=goals_db)
    assert del_res2["status"] == "not_found"


def test_calculate_goal_milestones_and_deficits(goals_db):
    user_id = "user_math"
    # Reference date: Sept 12, 2026
    ref_dt = datetime(2026, 9, 12, 12, 0, 0)

    # 1. Goal A: On Track (Target $3,000, Saved $1,000, Deadline in 10 months -> $200/mo req. Pace: $250/mo)
    deadline_a = (ref_dt + timedelta(days=304)).strftime("%Y-%m-%d")  # ~10 months
    set_savings_goal(user_id=user_id, name="Vacation", target_amount=3000.0, target_date=deadline_a, conn=goals_db)
    # Deposit $1000 total balance
    record_goal_contribution(user_id=user_id, name="Vacation", amount=1000.0, conn=goals_db)
    # Add recent contributions to simulate $250/mo velocity (3 months * 250 = 750)
    # Two contributions of 375.0 in recent 30 days
    goals_db.execute("INSERT INTO bucket_contributions (user_id, bucket_name, amount, created_at) VALUES (?, ?, ?, datetime('now'))", (user_id, "Vacation", 375.0))
    goals_db.execute("INSERT INTO bucket_contributions (user_id, bucket_name, amount, created_at) VALUES (?, ?, ?, datetime('now'))", (user_id, "Vacation", 375.0))

    # 2. Goal B: In Deficit (Target $6,000, Saved $1,000, Deadline in 5 months -> $1,000/mo req. Pace: $100/mo)
    deadline_b = (ref_dt + timedelta(days=152)).strftime("%Y-%m-%d")  # ~5 months
    set_savings_goal(user_id=user_id, name="Car Repair", target_amount=6000.0, target_date=deadline_b, conn=goals_db)
    record_goal_contribution(user_id=user_id, name="Car Repair", amount=1000.0, conn=goals_db)
    # Recent contributions: $300 in last 90 days -> $100/mo velocity
    goals_db.execute("INSERT INTO bucket_contributions (user_id, bucket_name, amount, created_at) VALUES (?, ?, ?, datetime('now'))", (user_id, "Car Repair", 300.0))

    # 3. Goal C: Fully Funded (Target $500, Saved $500)
    set_savings_goal(user_id=user_id, name="Holiday Gifts", target_amount=500.0, conn=goals_db)
    record_goal_contribution(user_id=user_id, name="Holiday Gifts", amount=500.0, conn=goals_db)

    # 4. Goal D: Overdue (Deadline was 10 days ago)
    deadline_d = (ref_dt - timedelta(days=10)).strftime("%Y-%m-%d")
    set_savings_goal(user_id=user_id, name="Tax Bill", target_amount=1200.0, target_date=deadline_d, conn=goals_db)
    record_goal_contribution(user_id=user_id, name="Tax Bill", amount=200.0, conn=goals_db)

    goals_db.commit()

    res = calculate_goal_milestones_and_deficits(user_id=user_id, now=ref_dt, conn=goals_db)
    summary = res["summary"]
    goals = {g["name"]: g for g in res["goals"]}

    assert summary["goals_count"] == 4
    assert summary["deficit_count"] >= 1  # Car Repair is in deficit

    # Verify Goal C: Fully funded
    assert goals["Holiday Gifts"]["status"] == "FULLY_FUNDED"
    assert goals["Holiday Gifts"]["remaining_amount"] == 0.0

    # Verify Goal D: Overdue
    assert goals["Tax Bill"]["status"] == "OVERDUE"
    assert goals["Tax Bill"]["days_remaining"] < 0

    # Verify Goal B: Deficit
    assert goals["Car Repair"]["status"] == "DEFICIT"
    assert goals["Car Repair"]["monthly_deficit"] > 0
    assert goals["Car Repair"]["projected_shortfall"] > 0

    # Test embed formatting
    embed = format_goals_embed(res, show_deficit_only=False)
    assert "Sinking Funds & Savings Goal Milestones" in embed.title

    deficit_embed = format_goals_embed(res, show_deficit_only=True)
    assert "Shortfall & Deficit Warning" in deficit_embed.title
    assert any("Car Repair" in f.name for f in deficit_embed.fields)

    # Test report formatting
    report = format_goals_report(res)
    assert "SAVINGS GOAL MILESTONES & DEFICIT SCHEDULE" in report
    assert "Car Repair" in report


@pytest.mark.asyncio
async def test_goals_discord_commands(goals_db, monkeypatch):
    import src.services.goals as goals_mod
    from src.bot.commands import goals_group, setgoal_cmd, fundgoal_cmd, delgoal_cmd

    # Seed data
    set_savings_goal(user_id="ctx_goal_user", name="Laptop", target_amount=1500.0, conn=goals_db)

    orig_calc = goals_mod.calculate_goal_milestones_and_deficits
    orig_set = goals_mod.set_savings_goal
    orig_fund = goals_mod.record_goal_contribution
    orig_del = goals_mod.delete_savings_goal

    monkeypatch.setattr(goals_mod, "calculate_goal_milestones_and_deficits", lambda user_id, now=None, conn=None: orig_calc(user_id=user_id, now=now, conn=goals_db))
    monkeypatch.setattr(goals_mod, "set_savings_goal", lambda user_id, name, target_amount, target_date=None, category="General", conn=None: orig_set(user_id=user_id, name=name, target_amount=target_amount, target_date=target_date, category=category, conn=goals_db))
    monkeypatch.setattr(goals_mod, "record_goal_contribution", lambda user_id, name, amount, source="manual", transaction_id=None, conn=None: orig_fund(user_id=user_id, name=name, amount=amount, source=source, transaction_id=transaction_id, conn=goals_db))
    monkeypatch.setattr(goals_mod, "delete_savings_goal", lambda user_id, name, conn=None: orig_del(user_id=user_id, name=name, conn=goals_db))

    ctx = MagicMock()
    ctx.author.id = "ctx_goal_user"
    ctx.send = AsyncMock()
    ctx.invoked_subcommand = None

    # Test goals overview
    await goals_group(ctx)
    ctx.send.assert_called_once()
    embed = ctx.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "Laptop" in str([f.name for f in embed.fields])

    # Test setgoal_cmd
    ctx.send.reset_mock()
    await setgoal_cmd(ctx, "Ski Trip", 800.0, "2027-02-15", "Travel")
    assert "Ski Trip" in ctx.send.call_args[0][0]

    # Test fundgoal_cmd
    ctx.send.reset_mock()
    await fundgoal_cmd(ctx, "Ski Trip", 200.0)
    assert "Deposited" in ctx.send.call_args[0][0]

    # Test delgoal_cmd
    ctx.send.reset_mock()
    await delgoal_cmd(ctx, "Ski Trip")
    assert "deleted" in ctx.send.call_args[0][0]
