"""
Delilah Financial OS - Sinking Funds & Savings Goals Engine.

Provides deadline-aware goal tracking, required monthly savings contribution
schedules, and shortfall deficit detection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, date
from typing import Dict, Any, List, Optional

import discord

from src.core.state import DB_PATH
from src.db.migrations import apply_all
from src.services.budgeting import render_progress_bar


def _ensure_tables(conn: sqlite3.Connection) -> None:
    apply_all(conn)


def set_savings_goal(
    *,
    user_id: str,
    name: str,
    target_amount: float,
    target_date: Optional[str] = None,
    category: str = "General",
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Create or update a savings goal with optional deadline and category.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("set_savings_goal: forced isolation violation — user_id is required")
    name = str(name or "").strip()
    if not name:
        raise ValueError("Goal name cannot be empty")
    target_val = float(target_amount)
    if target_val <= 0:
        raise ValueError("Target amount must be a positive number")

    cleaned_date = None
    if target_date:
        cleaned_date = str(target_date).strip()[:10]
        try:
            datetime.strptime(cleaned_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Target deadline must be in YYYY-MM-DD format")

    cleaned_cat = str(category or "General").strip().title()

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        _ensure_tables(db_conn)
        c = db_conn.cursor()
        c.execute("""
            INSERT INTO savings_buckets (user_id, name, target_amount, current_amount, category, target_date, updated_at)
            VALUES (?, ?, ?, 0.0, ?, ?, datetime('now'))
            ON CONFLICT(user_id, name) DO UPDATE SET
                target_amount = excluded.target_amount,
                category = excluded.category,
                target_date = excluded.target_date,
                updated_at = datetime('now')
        """, (user_id, name, target_val, cleaned_cat, cleaned_date))
        db_conn.commit()

        c.execute("""
            SELECT id, name, target_amount, current_amount, category, target_date
            FROM savings_buckets
            WHERE user_id = ? AND LOWER(name) = LOWER(?)
        """, (user_id, name))
        row = c.fetchone()
        return {
            "status": "success",
            "id": row[0],
            "name": row[1],
            "target_amount": float(row[2]),
            "current_amount": float(row[3]),
            "category": row[4],
            "target_date": row[5],
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def record_goal_contribution(
    *,
    user_id: str,
    name: str,
    amount: float,
    source: str = "manual",
    transaction_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Deposit funds into a savings goal envelope and record the audit log.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("record_goal_contribution: forced isolation violation — user_id is required")
    name = str(name or "").strip()
    if not name:
        raise ValueError("Goal name cannot be empty")
    amt_val = float(amount)
    if amt_val == 0:
        raise ValueError("Contribution amount cannot be zero")

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        _ensure_tables(db_conn)
        c = db_conn.cursor()

        c.execute("""
            SELECT id, target_amount, current_amount
            FROM savings_buckets
            WHERE user_id = ? AND LOWER(name) = LOWER(?)
        """, (user_id, name))
        row = c.fetchone()
        if not row:
            # Auto-create envelope if it doesn't exist
            target = max(1000.0, amt_val)
            c.execute("""
                INSERT INTO savings_buckets (user_id, name, target_amount, current_amount, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (user_id, name, target, max(0.0, amt_val)))
            new_balance = max(0.0, amt_val)
            target_amt = target
        else:
            new_balance = max(0.0, float(row[2]) + amt_val)
            target_amt = float(row[1])
            c.execute("""
                UPDATE savings_buckets
                SET current_amount = ?, updated_at = datetime('now')
                WHERE user_id = ? AND LOWER(name) = LOWER(?)
            """, (new_balance, user_id, name))

        # Log contribution
        c.execute("""
            INSERT INTO bucket_contributions (user_id, bucket_name, amount, source, transaction_id, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        """, (user_id, name, amt_val, source, transaction_id))
        db_conn.commit()

        return {
            "status": "success",
            "name": name,
            "amount_deposited": amt_val,
            "new_balance": round(new_balance, 2),
            "target_amount": round(target_amt, 2),
            "remaining": round(max(0.0, target_amt - new_balance), 2),
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def delete_savings_goal(
    *,
    user_id: str,
    name: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Delete a savings goal."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("delete_savings_goal: forced isolation violation — user_id is required")
    name = str(name or "").strip()

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        _ensure_tables(db_conn)
        c = db_conn.cursor()
        c.execute("DELETE FROM savings_buckets WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, name))
        affected = c.rowcount
        db_conn.commit()
        if affected == 0:
            return {"status": "not_found", "message": f"Goal '{name}' not found."}
        return {"status": "success", "message": f"Goal '{name}' deleted."}

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def calculate_goal_milestones_and_deficits(
    *,
    user_id: str,
    now: Optional[datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Evaluates goal progress, monthly required contribution, velocity, and deficit shortfalls.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("calculate_goal_milestones_and_deficits: forced isolation violation — user_id is required")

    dt = now or datetime.now()
    today = dt.date()

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        _ensure_tables(db_conn)
        c = db_conn.cursor()

        c.execute("""
            SELECT id, name, target_amount, current_amount, category, target_date
            FROM savings_buckets
            WHERE user_id = ?
            ORDER BY name ASC
        """, (user_id,))
        rows = c.fetchall()

        goals = []
        total_target = 0.0
        total_saved = 0.0
        total_required_monthly = 0.0
        total_monthly_velocity = 0.0
        total_deficit = 0.0
        deficit_count = 0

        for r in rows:
            gid, name, tgt, cur, cat, tdate_str = r[0], r[1], float(r[2] or 0.0), float(r[3] or 0.0), r[4] or "General", r[5]
            total_target += tgt
            total_saved += cur
            remaining = max(0.0, tgt - cur)
            progress_pct = round((cur / tgt * 100.0) if tgt > 0 else 100.0, 1)

            # Trailing 90-day velocity from bucket_contributions
            c.execute("""
                SELECT SUM(amount)
                FROM bucket_contributions
                WHERE user_id = ? AND LOWER(bucket_name) = LOWER(?)
                  AND created_at >= datetime('now', '-90 days')
            """, (user_id, name))
            c_row = c.fetchone()
            contrib_90d = float(c_row[0] or 0.0) if c_row and c_row[0] else 0.0
            monthly_velocity = round(contrib_90d / 3.0, 2)
            total_monthly_velocity += monthly_velocity

            # Calculate deadline math
            days_left = None
            months_left = None
            required_monthly = 0.0
            monthly_deficit = 0.0
            projected_shortfall = 0.0
            status = "NO_DEADLINE"
            icon = "ℹ️"

            if remaining == 0.0:
                status = "FULLY_FUNDED"
                icon = "🎉"
            elif tdate_str:
                try:
                    tdate = datetime.strptime(tdate_str[:10], "%Y-%m-%d").date()
                    days_left = (tdate - today).days
                    if days_left < 0:
                        status = "OVERDUE"
                        icon = "🚨"
                        months_left = 0.0
                        required_monthly = remaining
                        monthly_deficit = remaining
                        projected_shortfall = remaining
                    else:
                        months_left = max(0.1, round(days_left / 30.44, 1))
                        required_monthly = round(remaining / months_left, 2)
                        total_required_monthly += required_monthly

                        if monthly_velocity < required_monthly:
                            monthly_deficit = round(required_monthly - monthly_velocity, 2)
                            projected_saved = cur + (monthly_velocity * months_left)
                            projected_shortfall = max(0.0, round(tgt - projected_saved, 2))
                            status = "DEFICIT"
                            icon = "⚠️"
                            total_deficit += monthly_deficit
                            deficit_count += 1
                        else:
                            status = "ON_TRACK"
                            icon = "🟢"
                except ValueError:
                    status = "INVALID_DATE"
                    icon = "❓"

            goals.append({
                "id": gid,
                "name": name,
                "category": cat,
                "current_amount": round(cur, 2),
                "target_amount": round(tgt, 2),
                "remaining_amount": round(remaining, 2),
                "progress_pct": progress_pct,
                "progress_bar": render_progress_bar(progress_pct, width=8),
                "target_date": tdate_str,
                "days_remaining": days_left,
                "months_remaining": months_left,
                "required_monthly_deposit": required_monthly,
                "monthly_velocity": monthly_velocity,
                "monthly_deficit": monthly_deficit,
                "projected_shortfall": projected_shortfall,
                "status": status,
                "status_icon": icon,
            })

        overall_pct = round((total_saved / total_target * 100.0) if total_target > 0 else 100.0, 1)

        return {
            "user_id": user_id,
            "calculated_at": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_target": round(total_target, 2),
                "total_saved": round(total_saved, 2),
                "total_remaining": round(max(0.0, total_target - total_saved), 2),
                "overall_progress_pct": overall_pct,
                "progress_bar": render_progress_bar(overall_pct, width=10),
                "total_required_monthly": round(total_required_monthly, 2),
                "total_monthly_velocity": round(total_monthly_velocity, 2),
                "total_monthly_deficit": round(total_deficit, 2),
                "goals_count": len(goals),
                "deficit_count": deficit_count,
            },
            "goals": goals,
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_goals_embed(data: Dict[str, Any], show_deficit_only: bool = False) -> discord.Embed:
    """Format goals calculation into a polished Discord embed."""
    summary = data["summary"]
    goals = data["goals"]

    if show_deficit_only:
        display_goals = [g for g in goals if g["status"] in ("DEFICIT", "OVERDUE")]
        title = "⚠️ Savings Goals Shortfall & Deficit Warning"
        color = discord.Color.orange()
    else:
        display_goals = goals
        title = "🎯 Sinking Funds & Savings Goal Milestones"
        color = discord.Color.green() if summary["deficit_count"] == 0 else discord.Color.gold()

    embed = discord.Embed(
        title=title,
        color=color,
        description=(
            f"**Total Saved:** **${summary['total_saved']:,.2f}** of **${summary['total_target']:,.2f}** "
            f"({summary['overall_progress_pct']}%)\n"
            f"`{summary['progress_bar']}`\n\n"
            f"**Required Monthly:** **${summary['total_required_monthly']:,.2f}/mo** | "
            f"**Current Flow:** **${summary['total_monthly_velocity']:,.2f}/mo**\n"
            f"**Total Deficit:** **${summary['total_monthly_deficit']:,.2f}/mo** across {summary['deficit_count']} goal(s)"
        )
    )

    if not display_goals:
        embed.add_field(
            name="No Goals Found",
            value="No goals match this view. Use `!setgoal <name> <target> [YYYY-MM-DD]` to add one.",
            inline=False
        )
        return embed

    for g in display_goals[:12]:
        deadline_text = f"Deadline: `{g['target_date']}` ({g['days_remaining']}d left)" if g["target_date"] else "No deadline set"
        val_lines = [
            f"Saved: **${g['current_amount']:,.2f}** / ${g['target_amount']:,.2f} ({g['progress_pct']}%)",
            f"`{g['progress_bar']}` {deadline_text}",
        ]
        if g["status"] == "DEFICIT":
            val_lines.append(
                f"⚠️ **Deficit:** Need **${g['required_monthly_deposit']:,.2f}/mo** (current pace: ${g['monthly_velocity']:,.2f}/mo). "
                f"Shortfall: **${g['projected_shortfall']:,.2f}**"
            )
        elif g["status"] == "OVERDUE":
            val_lines.append(f"🚨 **Goal Overdue!** ${g['remaining_amount']:,.2f} remaining past deadline.")
        elif g["status"] == "FULLY_FUNDED":
            val_lines.append("🎉 **100% Fully Funded! Ready to spend.**")
        elif g["status"] == "ON_TRACK":
            val_lines.append(f"🟢 **On Track:** Saving ${g['monthly_velocity']:,.2f}/mo (required: ${g['required_monthly_deposit']:,.2f}/mo)")

        embed.add_field(
            name=f"{g['status_icon']} {g['name']} ({g['category']})",
            value="\n".join(val_lines),
            inline=False
        )

    embed.set_footer(text="Use !fundgoal <name> <amount> to deposit funds · !setgoal to edit")
    return embed


def format_goals_report(data: Dict[str, Any]) -> str:
    """Format goals calculation into a clean text report."""
    summary = data["summary"]
    goals = data["goals"]

    lines = [
        "🎯 SAVINGS GOAL MILESTONES & DEFICIT SCHEDULE",
        f"Generated At: {data['calculated_at']}",
        "=" * 60,
        f"Total Saved:     ${summary['total_saved']:,.2f} / ${summary['total_target']:,.2f} ({summary['overall_progress_pct']}%)",
        f"Required Flow:   ${summary['total_required_monthly']:,.2f}/month",
        f"Actual Velocity: ${summary['total_monthly_velocity']:,.2f}/month",
        f"Monthly Deficit: ${summary['total_monthly_deficit']:,.2f}/month ({summary['deficit_count']} goals behind pace)",
        "-" * 60,
    ]

    for g in goals:
        lines.append(f"[{g['status_icon']} {g['status']}] {g['name']} ({g['category']}):")
        lines.append(f"   Balance: ${g['current_amount']:,.2f} / ${g['target_amount']:,.2f} ({g['progress_pct']}%)")
        if g["target_date"]:
            lines.append(f"   Target Date: {g['target_date']} ({g['days_remaining']} days left)")
            lines.append(f"   Required: ${g['required_monthly_deposit']:,.2f}/mo | Pace: ${g['monthly_velocity']:,.2f}/mo")
            if g["monthly_deficit"] > 0:
                lines.append(f"   DEFICIT: Short by ${g['monthly_deficit']:,.2f}/mo (projected -${g['projected_shortfall']:,.2f})")
        lines.append("")

    return "\n".join(lines)
