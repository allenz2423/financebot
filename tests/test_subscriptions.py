import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta

from src.services.subscriptions import (
    set_subscription,
    cancel_subscription,
    get_billing_calendar,
    format_billing_calendar_embed,
    format_billing_calendar_report,
)
from src.db.migrations import apply_all


@pytest.fixture
def subs_db():
    conn = sqlite3.connect(":memory:")
    apply_all(conn)
    yield conn
    conn.close()


def test_set_subscription_and_isolation(subs_db):
    user_a = "user_subs_a"
    user_b = "user_subs_b"

    # User A creates a monthly subscription with explicit due date
    res_a = set_subscription(
        user_id=user_a,
        merchant="TST* NETFLIX.COM",
        amount=15.49,
        cadence="monthly",
        next_due_date="2026-10-01",
        category="Entertainment",
        conn=subs_db,
    )
    assert res_a["status"] == "success"
    assert res_a["merchant"] == "Netflix"  # auto-cleaned descriptor!
    assert res_a["amount"] == 15.49
    assert res_a["cadence"] == "monthly"
    assert res_a["next_due_date"] == "2026-10-01"

    # User B creates a subscription with same merchant
    res_b = set_subscription(
        user_id=user_b,
        merchant="Netflix",
        amount=22.99,
        cadence="monthly",
        conn=subs_db,
    )
    assert res_b["amount"] == 22.99

    # User A updates their subscription
    res_a2 = set_subscription(
        user_id=user_a,
        merchant="Netflix",
        amount=19.99,
        cadence="monthly",
        next_due_date="2026-10-15",
        conn=subs_db,
    )
    assert res_a2["amount"] == 19.99

    # Ensure user B's subscription is unaffected
    cal_b = get_billing_calendar(user_id=user_b, conn=subs_db)
    assert len(cal_b["upcoming_bills"]) == 1
    assert cal_b["upcoming_bills"][0]["amount"] == 22.99


def test_set_subscription_validation(subs_db):
    with pytest.raises(ValueError, match="forced isolation violation"):
        set_subscription(user_id="", merchant="Spotify", amount=10.0, conn=subs_db)

    with pytest.raises(ValueError, match="Merchant name cannot be empty"):
        set_subscription(user_id="user_1", merchant="  ", amount=10.0, conn=subs_db)

    with pytest.raises(ValueError, match="Subscription amount must be a positive number"):
        set_subscription(user_id="user_1", merchant="Spotify", amount=-5.0, conn=subs_db)

    with pytest.raises(ValueError, match="next_due_date must be in YYYY-MM-DD format"):
        set_subscription(user_id="user_1", merchant="Spotify", amount=10.0, next_due_date="not-a-date", conn=subs_db)


def test_cancel_subscription(subs_db):
    user_id = "user_cancel"
    set_subscription(user_id=user_id, merchant="Hulu", amount=12.99, conn=subs_db)

    res = cancel_subscription(user_id=user_id, merchant="Hulu", conn=subs_db)
    assert res["status"] == "success"
    assert "Cancelled" in res["message"]

    # Inactive subscription should not appear in active calendar
    cal = get_billing_calendar(user_id=user_id, conn=subs_db)
    assert cal["metrics"]["active_subscriptions_count"] == 0

    # Nonexistent subscription
    res2 = cancel_subscription(user_id=user_id, merchant="Nonexistent", conn=subs_db)
    assert res2["status"] == "not_found"


def test_get_billing_calendar_cash_outflows_and_burn(subs_db):
    user_id = "user_cal"
    now_dt = datetime(2026, 9, 12, 12, 0, 0)

    # 1. Due in 3 days (falls in 7d, 14d, 30d window)
    due_3d = (now_dt + timedelta(days=3)).strftime("%Y-%m-%d")
    set_subscription(user_id=user_id, merchant="Gym", amount=50.0, cadence="monthly", next_due_date=due_3d, conn=subs_db)

    # 2. Due in 10 days (falls in 14d, 30d window)
    due_10d = (now_dt + timedelta(days=10)).strftime("%Y-%m-%d")
    set_subscription(user_id=user_id, merchant="Internet", amount=80.0, cadence="monthly", next_due_date=due_10d, conn=subs_db)

    # 3. Due in 25 days (falls in 30d window) - Annual sub of $120 ($10/mo)
    due_25d = (now_dt + timedelta(days=25)).strftime("%Y-%m-%d")
    set_subscription(user_id=user_id, merchant="Amazon Prime", amount=120.0, cadence="annual", next_due_date=due_25d, conn=subs_db)

    # 4. Overdue bill (was due 4 days ago)
    due_past = (now_dt - timedelta(days=4)).strftime("%Y-%m-%d")
    set_subscription(user_id=user_id, merchant="Electric", amount=95.0, cadence="monthly", next_due_date=due_past, conn=subs_db)

    # 5. Weekly subscription (due in 5 days, $10/week -> $43.33/mo)
    due_5d = (now_dt + timedelta(days=5)).strftime("%Y-%m-%d")
    set_subscription(user_id=user_id, merchant="Meal Kit", amount=10.0, cadence="weekly", next_due_date=due_5d, conn=subs_db)

    cal = get_billing_calendar(user_id=user_id, days_ahead=30, now=now_dt, conn=subs_db)
    metrics = cal["metrics"]

    assert metrics["active_subscriptions_count"] == 5
    assert metrics["overdue_count"] == 1
    assert metrics["upcoming_count"] == 4

    # Check cash needed:
    # 7d window: Gym ($50) + Meal Kit ($10) = $60
    assert metrics["cash_needed_7d"] == 60.0
    # 14d window: 7d ($60) + Internet ($80) = $140
    assert metrics["cash_needed_14d"] == 140.0
    # 30d window: 14d ($140) + Amazon Prime ($120) = $260
    assert metrics["cash_needed_30d"] == 260.0

    # Overdue list check
    assert cal["overdue_bills"][0]["merchant"] == "Electric"
    assert cal["overdue_bills"][0]["days_until_due"] == -4

    # Embed format check
    embed = format_billing_calendar_embed(cal)
    assert "Upcoming Bills & Subscription Calendar" in embed.title
    assert "$60.00" in embed.description
    assert any("Overdue" in f.name for f in embed.fields)

    # Report format check
    report = format_billing_calendar_report(cal)
    assert "RECURRING BILLS & SUBSCRIPTION CALENDAR" in report
    assert "Electric" in report


@pytest.mark.asyncio
async def test_billing_calendar_discord_commands(subs_db, monkeypatch):
    import src.services.subscriptions as subs_mod
    from src.bot.commands import bills_group, bills_add_subcmd, bills_cancel_subcmd, upcomingbills_cmd

    # Seed
    set_subscription(user_id="ctx_bill_user", merchant="Netflix", amount=15.49, cadence="monthly", conn=subs_db)

    orig_cal = subs_mod.get_billing_calendar
    orig_set = subs_mod.set_subscription
    orig_cancel = subs_mod.cancel_subscription

    monkeypatch.setattr(subs_mod, "get_billing_calendar", lambda user_id, days_ahead=30, now=None, conn=None: orig_cal(user_id=user_id, days_ahead=days_ahead, now=now, conn=subs_db))
    monkeypatch.setattr(subs_mod, "set_subscription", lambda user_id, merchant, amount, cadence="monthly", next_due_date=None, category="Subscriptions", notes=None, auto_renew=True, conn=None: orig_set(user_id=user_id, merchant=merchant, amount=amount, cadence=cadence, next_due_date=next_due_date, category=category, notes=notes, auto_renew=auto_renew, conn=subs_db))
    monkeypatch.setattr(subs_mod, "cancel_subscription", lambda user_id, merchant, conn=None: orig_cancel(user_id=user_id, merchant=merchant, conn=subs_db))

    ctx = MagicMock()
    ctx.author.id = "ctx_bill_user"
    ctx.send = AsyncMock()
    ctx.invoked_subcommand = None

    # Overview
    await bills_group(ctx)
    ctx.send.assert_called_once()
    embed = ctx.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "Upcoming Bills" in embed.title

    # Add bill command
    ctx.send.reset_mock()
    await bills_add_subcmd(ctx, merchant="Spotify", amount=11.99, cadence="monthly")
    ctx.send.assert_called_once()
    assert "Tracked recurring bill" in ctx.send.call_args[0][0]
    assert "Spotify" in ctx.send.call_args[0][0]

    # Upcoming bills command
    ctx.send.reset_mock()
    await upcomingbills_cmd(ctx, days=14)
    ctx.send.assert_called_once()

    # Cancel bill command
    ctx.send.reset_mock()
    await bills_cancel_subcmd(ctx, merchant="Netflix")
    ctx.send.assert_called_once()
    assert "Cancelled" in ctx.send.call_args[0][0]
