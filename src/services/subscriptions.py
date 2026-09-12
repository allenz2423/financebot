"""
Delilah Financial OS - Subscriptions & Billing Cycle Calendar Engine.

Tracks recurring charges, calculates next billing dates, forecasts upcoming
cash outflow schedules (7d/14d/30d), and identifies overdue bills.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

import discord

from src.core.state import DB_PATH
from src.db.migrations import apply_all
from src.services.merchants import clean_raw_merchant_descriptor


_CADENCE_DAYS = {
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 91,
    "annual": 365,
    "yearly": 365,
}

_CADENCE_MONTHLY_FACTOR = {
    "weekly": 4.333,
    "biweekly": 2.167,
    "monthly": 1.0,
    "quarterly": 1.0 / 3.0,
    "annual": 1.0 / 12.0,
    "yearly": 1.0 / 12.0,
}


def _advance_date_by_cadence(start_date: date, cadence: str) -> date:
    c = cadence.lower()
    days = _CADENCE_DAYS.get(c, 30)
    return start_date + timedelta(days=days)


def set_subscription(
    *,
    user_id: str,
    merchant: str,
    amount: float,
    cadence: str = "monthly",
    next_due_date: Optional[str] = None,
    category: str = "Subscriptions",
    notes: Optional[str] = None,
    auto_renew: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Create or update a recurring subscription with billing cycle cadence.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("set_subscription: forced isolation violation — user_id is required")

    merchant_name = clean_raw_merchant_descriptor(merchant) or str(merchant or "").strip()
    if not merchant_name:
        raise ValueError("Merchant name cannot be empty")

    amt_val = float(amount)
    if amt_val <= 0:
        raise ValueError("Subscription amount must be a positive number")

    cadence_norm = str(cadence or "monthly").strip().lower()
    if cadence_norm not in _CADENCE_DAYS:
        cadence_norm = "monthly"

    cat_norm = str(category or "Subscriptions").strip().title()

    cleaned_due_date = None
    if next_due_date:
        cleaned_due_date = str(next_due_date).strip()[:10]
        try:
            datetime.strptime(cleaned_due_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("next_due_date must be in YYYY-MM-DD format")
    else:
        # Auto-compute next due date as 1 cycle from today
        cleaned_due_date = _advance_date_by_cadence(datetime.now().date(), cadence_norm).strftime("%Y-%m-%d")

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        apply_all(db_conn)
        c = db_conn.cursor()

        c.execute("""
            INSERT INTO subscriptions (
                user_id, merchant, last_amount, status, cadence, next_due_date, category, notes, auto_renew
            )
            VALUES (?, ?, ?, 'Active', ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, merchant) DO UPDATE SET
                last_amount = excluded.last_amount,
                status = 'Active',
                cadence = excluded.cadence,
                next_due_date = excluded.next_due_date,
                category = excluded.category,
                notes = COALESCE(excluded.notes, subscriptions.notes),
                auto_renew = excluded.auto_renew
        """, (user_id, merchant_name, amt_val, cadence_norm, cleaned_due_date, cat_norm, notes, 1 if auto_renew else 0))
        db_conn.commit()

        c.execute("""
            SELECT id, merchant, last_amount, cadence, next_due_date, category, status
            FROM subscriptions
            WHERE user_id = ? AND LOWER(merchant) = LOWER(?)
        """, (user_id, merchant_name))
        row = c.fetchone()
        return {
            "status": "success",
            "id": row[0],
            "merchant": row[1],
            "amount": float(row[2]),
            "cadence": row[3],
            "next_due_date": row[4],
            "category": row[5],
            "subscription_status": row[6],
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def cancel_subscription(
    *,
    user_id: str,
    merchant: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Mark a subscription as Cancelled.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("cancel_subscription: forced isolation violation — user_id is required")
    m_name = str(merchant or "").strip()

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        apply_all(db_conn)
        c = db_conn.cursor()
        c.execute("""
            UPDATE subscriptions
            SET status = 'Cancelled'
            WHERE user_id = ? AND (LOWER(merchant) = LOWER(?) OR LOWER(merchant) LIKE LOWER(?))
        """, (user_id, m_name, f"%{m_name}%"))
        affected = c.rowcount
        db_conn.commit()
        if affected == 0:
            return {"status": "not_found", "message": f"Subscription '{m_name}' not found."}
        return {"status": "success", "message": f"Subscription '{m_name}' marked as Cancelled."}

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def get_billing_calendar(
    *,
    user_id: str,
    days_ahead: int = 30,
    now: Optional[datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Computes upcoming recurring bills calendar, cash requirements (7d/14d/30d),
    monthly burn, and identifies overdue bills.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("get_billing_calendar: forced isolation violation — user_id is required")

    dt = now or datetime.now()
    today = dt.date()
    window_end = today + timedelta(days=days_ahead)

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        apply_all(db_conn)
        c = db_conn.cursor()

        c.execute("""
            SELECT id, merchant, last_amount, last_date, status, cadence, next_due_date, category, auto_renew
            FROM subscriptions
            WHERE user_id = ? AND status = 'Active'
            ORDER BY next_due_date ASC
        """, (user_id,))
        rows = c.fetchall()

        upcoming_bills = []
        overdue_bills = []
        total_monthly_burn = 0.0
        cash_7d = 0.0
        cash_14d = 0.0
        cash_30d = 0.0

        for r in rows:
            sid, merch, amt, last_dt_str, stat, cad, due_dt_str, cat, renew = (
                r[0], r[1], float(r[2] or 0.0), r[3], r[4], r[5] or "monthly", r[6], r[7] or "Subscriptions", bool(r[8])
            )
            cad = cad.lower()
            factor = _CADENCE_MONTHLY_FACTOR.get(cad, 1.0)
            monthly_equiv = amt * factor
            total_monthly_burn += monthly_equiv

            # Determine due date
            due_date = None
            if due_dt_str:
                try:
                    due_date = datetime.strptime(due_dt_str[:10], "%Y-%m-%d").date()
                except ValueError:
                    due_date = None

            if not due_date and last_dt_str:
                try:
                    last_d = datetime.strptime(last_dt_str[:10], "%Y-%m-%d").date()
                    due_date = _advance_date_by_cadence(last_d, cad)
                except ValueError:
                    due_date = None

            if not due_date:
                # Default to today + 30
                due_date = today + timedelta(days=30)

            days_left = (due_date - today).days

            bill_item = {
                "id": sid,
                "merchant": merch,
                "amount": round(amt, 2),
                "monthly_equivalent": round(monthly_equiv, 2),
                "cadence": cad,
                "category": cat,
                "next_due_date": due_date.strftime("%Y-%m-%d"),
                "days_until_due": days_left,
                "auto_renew": renew,
            }

            if days_left < 0:
                bill_item["status"] = "OVERDUE"
                overdue_bills.append(bill_item)
            elif days_left <= days_ahead:
                bill_item["status"] = "UPCOMING"
                upcoming_bills.append(bill_item)

            # Accumulate cash needs
            if 0 <= days_left <= 7:
                cash_7d += amt
            if 0 <= days_left <= 14:
                cash_14d += amt
            if 0 <= days_left <= 30:
                cash_30d += amt

        # Sort upcoming bills by date
        upcoming_bills.sort(key=lambda b: b["days_until_due"])
        overdue_bills.sort(key=lambda b: b["days_until_due"])

        return {
            "user_id": user_id,
            "calculated_at": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "days_ahead": days_ahead,
            "metrics": {
                "active_subscriptions_count": len(rows),
                "monthly_recurring_burn": round(total_monthly_burn, 2),
                "annual_recurring_cost": round(total_monthly_burn * 12.0, 2),
                "cash_needed_7d": round(cash_7d, 2),
                "cash_needed_14d": round(cash_14d, 2),
                "cash_needed_30d": round(cash_30d, 2),
                "overdue_count": len(overdue_bills),
                "upcoming_count": len(upcoming_bills),
            },
            "upcoming_bills": upcoming_bills,
            "overdue_bills": overdue_bills,
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_billing_calendar_embed(data: Dict[str, Any]) -> discord.Embed:
    """Format upcoming bills calendar into a clean Discord embed."""
    metrics = data["metrics"]
    upcoming = data["upcoming_bills"]
    overdue = data["overdue_bills"]

    color = discord.Color.red() if metrics["overdue_count"] > 0 else discord.Color.blue()

    embed = discord.Embed(
        title="📅 Upcoming Bills & Subscription Calendar",
        color=color,
        description=(
            f"**Monthly Recurring Burn:** **${metrics['monthly_recurring_burn']:,.2f}/mo** "
            f"(${metrics['annual_recurring_cost']:,.2f}/yr across {metrics['active_subscriptions_count']} subs)\n\n"
            f"💵 **Cash Outflow Required:**\n"
            f"• Next 7 Days: **${metrics['cash_needed_7d']:,.2f}**\n"
            f"• Next 14 Days: **${metrics['cash_needed_14d']:,.2f}**\n"
            f"• Next 30 Days: **${metrics['cash_needed_30d']:,.2f}**"
        )
    )

    if overdue:
        od_lines = [
            f"• 🚨 **{b['merchant']}**: **${b['amount']:,.2f}** (was due {b['next_due_date']}, {abs(b['days_until_due'])}d overdue)"
            for b in overdue[:5]
        ]
        embed.add_field(name="⚠️ Overdue / Unconfirmed Charges", value="\n".join(od_lines), inline=False)

    if not upcoming:
        embed.add_field(name="Upcoming Charges", value="No upcoming bills scheduled in this window.", inline=False)
    else:
        up_lines = []
        for b in upcoming[:10]:
            when = f"in {b['days_until_due']}d (`{b['next_due_date']}`)" if b["days_until_due"] > 0 else "TODAY"
            cadence_tag = f"/{b['cadence'][:2]}" if b["cadence"] != "monthly" else ""
            up_lines.append(
                f"• **{b['merchant']}**: **${b['amount']:,.2f}{cadence_tag}** · {when} ({b['category']})"
            )
        embed.add_field(name="⏳ Schedule (Next 30 Days)", value="\n".join(up_lines), inline=False)

    embed.set_footer(text="Use !addbill <name> <amount> to track · !cancelbill <name> to remove")
    return embed


def format_billing_calendar_report(data: Dict[str, Any]) -> str:
    """Format upcoming bills into a text report."""
    metrics = data["metrics"]
    upcoming = data["upcoming_bills"]
    overdue = data["overdue_bills"]

    lines = [
        "📅 RECURRING BILLS & SUBSCRIPTION CALENDAR",
        f"Generated At: {data['calculated_at']}",
        "=" * 60,
        f"Monthly Recurring Burn:   ${metrics['monthly_recurring_burn']:,.2f}/month",
        f"Annualized Run Rate:      ${metrics['annual_recurring_cost']:,.2f}/year",
        f"Cash Outflow (7 Days):    ${metrics['cash_needed_7d']:,.2f}",
        f"Cash Outflow (14 Days):   ${metrics['cash_needed_14d']:,.2f}",
        f"Cash Outflow (30 Days):   ${metrics['cash_needed_30d']:,.2f}",
        "-" * 60,
    ]

    if overdue:
        lines.append("OVERDUE CHARGES:")
        for b in overdue:
            lines.append(f"  [!] {b['merchant']}: ${b['amount']:,.2f} due {b['next_due_date']} ({abs(b['days_until_due'])} days ago)")
        lines.append("")

    lines.append("UPCOMING CHARGES:")
    for b in upcoming:
        lines.append(f"  • {b['next_due_date']} ({b['days_until_due']:2d}d): {b['merchant']:<20} ${b['amount']:>8,.2f} ({b['cadence']})")

    return "\n".join(lines)
