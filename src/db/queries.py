import json
import os
import re
import mimetypes
import sqlite3
import asyncio
import contextvars
import hashlib
import ipaddress
import random
import socket
import time
import math
from collections import Counter, defaultdict
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode
from html import unescape
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from dotenv import load_dotenv
import discord
from discord.ext import commands
from typing import Optional, List, Dict, Any, Tuple
import base64
from io import BytesIO
from pathlib import Path
from PIL import Image

# Lazy imports to avoid circular dependencies
# from src.core.state import *
# from src.core.state import CURRENT_USER_ID
from src.utils.helpers import sanitize_merchant_name
# from src.services.search import *

# Import configuration constants needed at module load time
from src.core.state import CHAT_HISTORY_TURNS, DB_PATH

# Import database connection from state module (single shared connection)
from src.core.state import conn, c, bot


# ============================================================
# Financial Helpers
# ============================================================

import time
def get_db_path() -> str:
    """Return the absolute path to the active SQLite database file."""
    return DB_PATH


def safe_commit():
    '''Retry wrapper for SQLite locks under heavy concurrent load.'''
    for _ in range(5):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower():
                time.sleep(0.5)
            else:
                raise
    conn.commit()

def get_weekly_spending(*, user_id: str) -> float:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_weekly_spending: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute("""
        SELECT SUM(amount) FROM transactions
        WHERE user_id = ? AND status = 'Evaluated'
        AND amount > 0
        AND date >= datetime('now', '-7 days')
        AND merchant NOT LIKE '%System Balance Sync%'
        AND COALESCE(category, '') NOT LIKE 'Credit Card Bill Payment%'
        AND COALESCE(category, '') NOT LIKE 'P2P Transfer%'
    """, (user_id,))
    row = c.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0

def get_current_financial_position(*, user_id: str) -> dict:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_current_financial_position: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute("""
        SELECT total_liquid, net_cash, all_balances, date
        FROM transactions
        WHERE user_id = ? AND merchant = 'System Balance Sync'
        ORDER BY id DESC LIMIT 1
    """, (user_id,))
    row = c.fetchone()
    if not row:
        return {
            "available": False,
            "total_liquid": None,
            "net_cash": None,
            "all_balances": None,
            "date": None,
        }
    sync_date_str = row[3]
    is_stale = False
    warning = None

    if sync_date_str:
        try:
            from datetime import datetime
            sync_dt = datetime.strptime(sync_date_str, "%Y-%m-%d %H:%M:%S")
            hours_old = (datetime.now() - sync_dt).total_seconds() / 3600.0
            if hours_old > 12.0:
                is_stale = True
                warning = f" Data may be stale (last Plaid sync was {hours_old:.1f} hours ago)."
        except Exception:
            pass

    return {
        "available": True,
        "total_liquid": row[0],
        "net_cash": row[1],
        "all_balances": row[2],
        "date": row[3],
        "data_stale": is_stale,
        "warning": warning,
    }

def get_transaction_context(*, transaction_row_id: int, user_id: str) -> list[dict]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_transaction_context: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        """SELECT tag, note, created_at FROM transaction_context
        WHERE user_id = ? AND transaction_row_id = ? ORDER BY datetime(created_at) ASC, id ASC""",
        (user_id, int(transaction_row_id)),
    )
    return [{"tag": r[0], "note": r[1], "created_at": r[2]} for r in c.fetchall()]

def format_transaction_context(context_rows: list[dict]) -> str:
    if not context_rows:
        return "No behavioral context recorded."
    parts = []
    for item in context_rows:
        tag = item.get("tag") or "untagged"
        note = item.get("note")
        parts.append(f"{tag}: {note}" if note else tag)
    return " | ".join(parts)

def get_recent_financial_activity(*, user_id: str, days: int = 30, limit: int = 12, offset: int = 0) -> list[dict]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_recent_financial_activity: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        """SELECT id, date, clean_merchant, merchant, category, amount, account_used
        FROM transactions
        WHERE user_id = ? AND status = 'Evaluated'
        AND merchant NOT LIKE '%System Balance Sync%'
        AND date >= datetime('now', ?)
        ORDER BY date DESC LIMIT ? OFFSET ?""",
        (user_id, f"-{days} days", limit, offset),
    )
    return [
        {
            "transaction_row_id": row[0],
            "date": row[1],
            "merchant": row[2] or row[3],
            "category": row[4] or "Uncategorized",
            "amount": float(row[5] or 0),
            "account": row[6] or "Unknown",
            "context": get_transaction_context(transaction_row_id=row[0], user_id=user_id),
        }
        for row in c.fetchall()
    ]

def get_recent_income(*, user_id: str, limit: int = 8, offset: int = 0) -> list[dict]:
    """Return actual recent income transactions from Plaid.

    Plaid convention:
      amount < 0 = money received / income
      amount > 0 = money spent / expense

    Expected/future income belongs in cash_inflows and is handled by
    get_expected_income(), not this function.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_recent_income: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )

    c.execute(
        """
        SELECT
            id,
            date,
            merchant,
            amount,
            category,
            account_used,
            status,
            transaction_id
        FROM transactions
        WHERE user_id = ?
          AND amount < 0
        ORDER BY date DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (user_id, limit, offset),
    )

    return [
        {
            "id": r[0],
            "date": r[1],
            "merchant": r[2],
            "amount": float(r[3]),
            "income_amount": abs(float(r[3])),
            "direction": "Income",
            "category": r[4] or "Uncategorized",
            "account": r[5] or "Unknown",
            "status": r[6],
            "transaction_id": r[7],
        }
        for r in c.fetchall()
    ]


def get_savings_buckets(*, user_id: str) -> list[dict]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_savings_buckets: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        "SELECT id, name, target_amount, current_amount FROM savings_buckets WHERE user_id = ? ORDER BY name, id",
        (user_id,)
    )
    return [{"id": r[0], "name": r[1], "target": r[2], "current": r[3]} for r in c.fetchall()]

def delete_savings_bucket(*, user_id: str, name: str | None = None, bucket_id: int | None = None) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "delete_savings_bucket: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if (name is None) == (bucket_id is None):
        return " Provide exactly one savings bucket identifier: name or bucket_id."
    if bucket_id is not None:
        try:
            bucket_id = int(bucket_id)
        except (TypeError, ValueError):
            return " bucket_id must be an integer."
        if bucket_id <= 0:
            return " bucket_id must be a positive integer."
        c.execute(
            "SELECT id, name, target_amount, current_amount FROM savings_buckets WHERE user_id = ? AND id = ?",
            (user_id, bucket_id),
        )
    else:
        if not isinstance(name, str) or not name.strip():
            return " A non-empty savings bucket name is required."
        c.execute(
            "SELECT id, name, target_amount, current_amount FROM savings_buckets WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, name.strip()),
        )
    row = c.fetchone()
    if not row:
        identifier = f"ID {bucket_id}" if bucket_id is not None else f"named **{name}**"
        return f" No savings bucket {identifier} exists — nothing to delete."
    found_id, found_name, target_amount, current_amount = row
    display_name = (
        found_name
        if found_name is not None and str(found_name).strip()
        else f"blank bucket (ID {found_id})"
    )
    c.execute("DELETE FROM savings_buckets WHERE user_id = ? AND id = ?", (user_id, found_id))
    safe_commit()
    if current_amount and current_amount > 0:
        return (
            f" Deleted **{display_name}** (ID {found_id}; had ${current_amount:,.2f} "
            f"saved toward a ${target_amount:,.2f} target). That money is not moved anywhere."
        )
    return f" Deleted **{display_name}** (ID {found_id})."


def get_roundup_settings(*, user_id: str) -> dict:
    """Retrieve round-up configuration for the user."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("get_roundup_settings: forced isolation violation — user_id is required")
    c.execute(
        "SELECT enabled, target_bucket_name, multiplier, whole_dollar_roundup FROM roundup_settings WHERE user_id = ?",
        (user_id,)
    )
    row = c.fetchone()
    if not row:
        return {
            "enabled": True,
            "target_bucket_name": "Emergency Fund",
            "multiplier": 1.0,
            "whole_dollar_roundup": 1.0,
        }
    return {
        "enabled": bool(row[0]),
        "target_bucket_name": row[1],
        "multiplier": float(row[2]),
        "whole_dollar_roundup": float(row[3]),
    }


def set_roundup_settings(
    *,
    user_id: str,
    enabled: bool = True,
    target_bucket_name: str = "Emergency Fund",
    multiplier: float = 1.0,
    whole_dollar_roundup: float = 1.0,
) -> dict:
    """Update round-up settings for the user."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("set_roundup_settings: forced isolation violation — user_id is required")
    target_bucket_name = (target_bucket_name or "Emergency Fund").strip()
    multiplier = max(0.1, min(float(multiplier), 10.0))
    whole_dollar = max(0.0, float(whole_dollar_roundup))

    c.execute(
        """INSERT INTO roundup_settings (user_id, enabled, target_bucket_name, multiplier, whole_dollar_roundup, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            enabled = excluded.enabled,
            target_bucket_name = excluded.target_bucket_name,
            multiplier = excluded.multiplier,
            whole_dollar_roundup = excluded.whole_dollar_roundup,
            updated_at = datetime('now')""",
        (user_id, 1 if enabled else 0, target_bucket_name, multiplier, whole_dollar)
    )
    safe_commit()
    return {
        "enabled": enabled,
        "target_bucket_name": target_bucket_name,
        "multiplier": multiplier,
        "whole_dollar_roundup": whole_dollar,
    }


def calculate_transaction_roundup(amount: float, multiplier: float = 1.0, whole_dollar_roundup: float = 1.0) -> float:
    """Calculate spare change round-up for a transaction amount."""
    amt = float(amount or 0.0)
    if amt <= 0:
        return 0.0
    cents = round(amt % 1.0, 2)
    if cents == 0.0:
        roundup = whole_dollar_roundup
    else:
        roundup = round(1.0 - cents, 2)
    return round(roundup * multiplier, 2)


def apply_transaction_roundups(days: int = 7, *, user_id: str) -> dict:
    """
    Scan recent purchases, calculate spare change round-ups, and allocate to the target savings bucket.
    Idempotent: will not double-count transactions that have already been rounded up.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("apply_transaction_roundups: forced isolation violation — user_id is required")

    settings = get_roundup_settings(user_id=user_id)
    if not settings["enabled"]:
        return {"status": "disabled", "message": "Round-ups are currently disabled for this user."}

    target_bucket = settings["target_bucket_name"]
    mult = settings["multiplier"]
    whole_dollar = settings["whole_dollar_roundup"]

    # Ensure target bucket exists
    c.execute("SELECT id, current_amount FROM savings_buckets WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, target_bucket))
    b_row = c.fetchone()
    if not b_row:
        c.execute("INSERT INTO savings_buckets (user_id, name, target_amount, current_amount) VALUES (?, ?, 1000.0, 0.0)", (user_id, target_bucket))
        safe_commit()

    # Find candidate transactions in the last N days not yet recorded in bucket_contributions
    c.execute(
        """SELECT t.id, t.date, t.clean_merchant, t.merchant, t.amount
        FROM transactions t
        WHERE t.user_id = ? AND t.amount > 0
          AND t.date >= datetime('now', ?)
          AND t.merchant NOT LIKE '%System Balance Sync%'
          AND COALESCE(t.category, '') NOT LIKE 'Credit Card Bill Payment%'
          AND COALESCE(t.category, '') NOT LIKE 'P2P Transfer%'
          AND NOT EXISTS (
              SELECT 1 FROM bucket_contributions bc
              WHERE bc.user_id = ? AND bc.transaction_id = CAST(t.id AS TEXT)
          )
        ORDER BY datetime(t.date) ASC""",
        (user_id, f"-{days} days", user_id)
    )
    rows = c.fetchall()

    if not rows:
        return {
            "status": "success",
            "roundups_count": 0,
            "total_swept": 0.0,
            "target_bucket": target_bucket,
            "message": "No new transactions found to round up.",
        }

    total_roundup = 0.0
    records = []
    for r in rows:
        tx_id, dt, cmerch, merch, amt = r
        amt_f = float(amt)
        spare = calculate_transaction_roundup(amt_f, multiplier=mult, whole_dollar_roundup=whole_dollar)
        if spare > 0:
            total_roundup += spare
            c.execute(
                """INSERT INTO bucket_contributions (user_id, bucket_name, amount, source, transaction_id, created_at)
                VALUES (?, ?, ?, 'roundup', ?, datetime('now'))""",
                (user_id, target_bucket, spare, str(tx_id))
            )
            records.append({
                "transaction_id": tx_id,
                "merchant": cmerch or merch,
                "amount": amt_f,
                "roundup": spare,
            })

    total_roundup = round(total_roundup, 2)
    # Credit the savings bucket
    c.execute(
        "UPDATE savings_buckets SET current_amount = current_amount + ? WHERE user_id = ? AND LOWER(name) = LOWER(?)",
        (total_roundup, user_id, target_bucket)
    )
    safe_commit()

    # Get updated bucket balance
    c.execute("SELECT current_amount, target_amount FROM savings_buckets WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, target_bucket))
    up_row = c.fetchone()
    cur_bal = float(up_row[0]) if up_row else total_roundup
    tar_bal = float(up_row[1]) if up_row else 1000.0

    return {
        "status": "success",
        "roundups_count": len(records),
        "total_swept": total_roundup,
        "target_bucket": target_bucket,
        "current_bucket_amount": cur_bal,
        "target_amount": tar_bal,
        "progress_pct": round((cur_bal / tar_bal * 100) if tar_bal > 0 else 100.0, 1),
        "transactions_swept": records[:10],
    }


def get_sinking_funds_overview(*, user_id: str) -> dict:
    """
    Compute goal progress, monthly funding velocity, and completion projections for all sinking funds.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("get_sinking_funds_overview: forced isolation violation — user_id is required")

    c.execute(
        "SELECT id, name, target_amount, current_amount FROM savings_buckets WHERE user_id = ? ORDER BY name",
        (user_id,)
    )
    rows = c.fetchall()
    if not rows:
        return {"status": "empty", "message": "No sinking funds or savings buckets configured.", "buckets": []}

    buckets = []
    total_saved = 0.0
    total_target = 0.0

    for bid, name, target_amt, cur_amt in rows:
        target = float(target_amt or 0.0)
        current = float(cur_amt or 0.0)
        total_saved += current
        total_target += target

        pct = round((current / target * 100) if target > 0 else 100.0, 1)

        # Monthly savings velocity over trailing 90 days from contributions
        c.execute(
            """SELECT SUM(amount) FROM bucket_contributions
            WHERE user_id = ? AND LOWER(bucket_name) = LOWER(?)
              AND created_at >= datetime('now', '-90 days')""",
            (user_id, name)
        )
        v_row = c.fetchone()
        three_month_contrib = float(v_row[0] or 0.0)
        monthly_velocity = round(three_month_contrib / 3.0, 2)

        # Remaining & projection
        remaining = max(0.0, target - current)
        months_to_complete = None
        target_date = None
        if remaining > 0 and monthly_velocity > 0:
            months_to_complete = round(remaining / monthly_velocity, 1)
            target_date = (datetime.now() + timedelta(days=int(months_to_complete * 30.44))).strftime("%B %Y")
        elif remaining == 0:
            months_to_complete = 0.0
            target_date = "Completed!"

        # Milestone
        if pct >= 100.0:
            milestone = "🎉 Fully Funded"
        elif pct >= 75.0:
            milestone = "🔥 75% Milestone Reached"
        elif pct >= 50.0:
            milestone = "⭐ 50% Halfway Mark"
        elif pct >= 25.0:
            milestone = "🌱 25% Seed Milestone"
        else:
            milestone = "🚀 In Progress"

        buckets.append({
            "id": bid,
            "name": name,
            "current_amount": current,
            "target_amount": target,
            "remaining_amount": round(remaining, 2),
            "progress_pct": pct,
            "monthly_velocity": monthly_velocity,
            "months_remaining": months_to_complete,
            "projected_completion_date": target_date,
            "milestone": milestone,
        })

    roundup_cfg = get_roundup_settings(user_id=user_id)

    return {
        "status": "success",
        "total_saved": round(total_saved, 2),
        "total_target": round(total_target, 2),
        "overall_progress_pct": round((total_saved / total_target * 100) if total_target > 0 else 100.0, 1),
        "roundup_settings": roundup_cfg,
        "buckets": buckets,
    }


def generate_financial_digest(period: str = "monthly", days: int = None, *, user_id: str) -> dict:
    """
    Compile a deterministic financial scorecard and executive digest for weekly or monthly review.
    Calculates cash flow, savings rate, financial health grade (A+ to F), top expense categories,
    outlier transactions, sinking fund trajectory, and debt metrics.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("generate_financial_digest: forced isolation violation — user_id is required")

    p_norm = (period or "monthly").lower().strip()
    if days is None:
        if p_norm in ("weekly", "week", "7d"):
            days = 7
            period_label = "Weekly"
        else:
            days = 30
            period_label = "Monthly"
    else:
        days = max(1, min(int(days), 365))
        period_label = f"{days}-Day"

    # 1. Settled Cash Flow (Inflow & Outflow)
    c.execute(
        """SELECT
            SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END),
            SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END),
            COUNT(*)
        FROM transactions
        WHERE user_id = ?
          AND merchant NOT LIKE '%System Balance Sync%'
          AND date >= datetime('now', ?)""",
        (user_id, f"-{days} days")
    )
    inflow_raw, outflow_raw, tx_count = c.fetchone() or (0.0, 0.0, 0)
    inflow = float(inflow_raw or 0.0)
    outflow = float(outflow_raw or 0.0)
    net_cash_flow = round(inflow - outflow, 2)

    # Savings Rate
    if inflow > 0:
        savings_rate = round((net_cash_flow / inflow) * 100, 1)
    else:
        savings_rate = 0.0 if outflow == 0 else -100.0

    # 2. Category Spending Breakdown
    c.execute(
        """SELECT COALESCE(category, 'Uncategorized'), SUM(amount), COUNT(*)
        FROM transactions
        WHERE user_id = ? AND amount > 0
          AND merchant NOT LIKE '%System Balance Sync%'
          AND date >= datetime('now', ?)
        GROUP BY COALESCE(category, 'Uncategorized')
        ORDER BY SUM(amount) DESC""",
        (user_id, f"-{days} days")
    )
    cat_rows = c.fetchall()
    categories = []
    for cat_name, cat_amt, cat_cnt in cat_rows:
        amt = float(cat_amt or 0.0)
        pct = round((amt / outflow * 100) if outflow > 0 else 0.0, 1)
        categories.append({
            "category": cat_name,
            "amount": round(amt, 2),
            "count": cat_cnt,
            "percentage": pct,
        })

    # 3. Top Outlier / Largest Purchases
    c.execute(
        """SELECT id, date, COALESCE(clean_merchant, merchant), amount, COALESCE(category, 'Uncategorized')
        FROM transactions
        WHERE user_id = ? AND amount > 0
          AND merchant NOT LIKE '%System Balance Sync%'
          AND date >= datetime('now', ?)
        ORDER BY amount DESC LIMIT 5""",
        (user_id, f"-{days} days")
    )
    top_txs = [
        {"id": r[0], "date": r[1], "merchant": r[2], "amount": float(r[3]), "category": r[4]}
        for r in c.fetchall()
    ]

    # 4. Debt Status
    c.execute(
        "SELECT COUNT(*), SUM(balance), SUM(min_payment) FROM user_debts WHERE user_id = ?",
        (user_id,)
    )
    debt_cnt, debt_bal, debt_min = c.fetchone() or (0, 0.0, 0.0)
    total_debt = float(debt_bal or 0.0)

    # 5. Sinking Funds & Savings Envelopes
    c.execute(
        "SELECT COUNT(*), SUM(current_amount), SUM(target_amount) FROM savings_buckets WHERE user_id = ?",
        (user_id,)
    )
    sf_cnt, sf_cur, sf_tar = c.fetchone() or (0, 0.0, 0.0)
    total_saved = float(sf_cur or 0.0)
    total_target = float(sf_tar or 0.0)
    sf_pct = round((total_saved / total_target * 100) if total_target > 0 else 100.0, 1)

    # 6. Active Subscriptions & Free Trials
    c.execute(
        "SELECT COUNT(*) FROM subscription_trials WHERE user_id = ? AND status = 'active' AND date(trial_end_date) >= date('now')",
        (user_id,)
    )
    active_trials_cnt = (c.fetchone() or [0])[0]

    # 7. Financial Health Score (0 - 100) & Letter Grade
    score = 0.0
    if savings_rate >= 20.0:
        score += 40.0
    elif savings_rate > 0:
        score += (savings_rate / 20.0) * 40.0

    if total_debt == 0.0:
        score += 30.0
    else:
        if inflow > 0 and debt_min:
            coverage = inflow / float(debt_min)
            score += min(25.0, coverage * 3.0)
        else:
            score += 10.0

    score += min(20.0, (sf_pct / 100.0) * 20.0)

    if net_cash_flow >= 0:
        score += 10.0

    health_score = int(round(max(0.0, min(100.0, score))))

    if health_score >= 90:
        grade = "A+"
    elif health_score >= 80:
        grade = "A"
    elif health_score >= 70:
        grade = "B"
    elif health_score >= 60:
        grade = "C"
    elif health_score >= 50:
        grade = "D"
    else:
        grade = "F"

    return {
        "status": "success",
        "period_label": period_label,
        "days": days,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "financial_health_score": health_score,
        "grade": grade,
        "inflow": round(inflow, 2),
        "outflow": round(outflow, 2),
        "net_cash_flow": net_cash_flow,
        "savings_rate_pct": savings_rate,
        "transaction_count": tx_count or 0,
        "category_spending": categories,
        "top_transactions": top_txs,
        "debt_summary": {
            "debt_count": debt_cnt or 0,
            "total_debt": round(total_debt, 2),
        },
        "sinking_funds_summary": {
            "bucket_count": sf_cnt or 0,
            "total_saved": round(total_saved, 2),
            "total_target": round(total_target, 2),
            "progress_pct": sf_pct,
        },
        "active_trials_count": active_trials_cnt or 0,
    }


def get_subscriptions(*, user_id: str) -> list[dict]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_subscriptions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        "SELECT merchant, last_amount, last_date, status FROM subscriptions WHERE user_id = ? ORDER BY merchant",
        (user_id,)
    )
    return [{"merchant": r[0], "amount": r[1], "last_date": r[2], "status": r[3]} for r in c.fetchall()]


def add_subscription_trial(
    *,
    user_id: str,
    service_name: str,
    trial_end_date: str,
    projected_cost: float = 0.0,
    billing_cycle: str = "monthly",
    cancellation_url: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Track a free trial with renewal date and projected cost."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("add_subscription_trial: forced isolation violation — user_id is required")
    service_name = (service_name or "").strip()
    if not service_name:
        raise ValueError("Service name cannot be empty")
    trial_end_date = (trial_end_date or "").strip()
    if not trial_end_date:
        raise ValueError("trial_end_date is required (YYYY-MM-DD)")

    c.execute(
        """INSERT INTO subscription_trials
        (user_id, service_name, trial_end_date, projected_cost, billing_cycle, cancellation_url, notes, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))""",
        (user_id, service_name, trial_end_date, float(projected_cost or 0.0), billing_cycle, cancellation_url, notes)
    )
    new_id = c.lastrowid
    safe_commit()
    return {
        "id": new_id,
        "service_name": service_name,
        "trial_end_date": trial_end_date,
        "projected_cost": float(projected_cost or 0.0),
        "status": "active",
    }


def list_subscription_trials(*, user_id: str, active_only: bool = True) -> list:
    """Return all tracked subscription trials."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("list_subscription_trials: forced isolation violation — user_id is required")
    cond = "AND status = 'active'" if active_only else ""
    c.execute(
        f"""SELECT id, service_name, trial_end_date, projected_cost, billing_cycle, cancellation_url, notes, status
        FROM subscription_trials
        WHERE user_id = ? {cond}
        ORDER BY date(trial_end_date) ASC""",
        (user_id,)
    )
    results = []
    today = datetime.now().date()
    for r in c.fetchall():
        end_dt_str = str(r[2])[:10]
        try:
            end_d = datetime.strptime(end_dt_str, "%Y-%m-%d").date()
            days_left = (end_d - today).days
        except ValueError:
            days_left = None

        results.append({
            "id": r[0],
            "service_name": r[1],
            "trial_end_date": r[2],
            "projected_cost": float(r[3] or 0.0),
            "billing_cycle": r[4],
            "cancellation_url": r[5],
            "notes": r[6],
            "status": r[7],
            "days_remaining": days_left,
        })
    return results


def update_trial_status(trial_id: int, status: str, *, user_id: str) -> bool:
    """Update status of a trial ('active', 'cancelled', 'converted')."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("update_trial_status: forced isolation violation — user_id is required")
    status = status.lower().strip()
    c.execute(
        "UPDATE subscription_trials SET status = ?, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
        (status, int(trial_id), user_id)
    )
    affected = c.rowcount > 0
    safe_commit()
    return affected


def detect_zombie_subscriptions(inactivity_days: int = 45, *, user_id: str) -> list:
    """
    Detect recurring subscriptions that have billed recently, tracking annual cost
    and highlighting potential candidates for cancellation.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("detect_zombie_subscriptions: forced isolation violation — user_id is required")

    c.execute(
        """SELECT s.merchant, s.last_amount, s.last_date, s.status
        FROM subscriptions s
        WHERE s.user_id = ? AND s.status = 'Active'
        ORDER BY s.last_amount DESC""",
        (user_id,)
    )
    subs = c.fetchall()
    zombies = []

    for merch, amt, l_date, st in subs:
        c.execute(
            """SELECT COUNT(*) FROM transactions
            WHERE user_id = ? AND clean_merchant = ?
              AND date >= datetime('now', ?)""",
            (user_id, merch, f"-{inactivity_days} days")
        )
        recent_count = c.fetchone()[0]

        c.execute(
            """SELECT SUM(amount), COUNT(*) FROM transactions
            WHERE user_id = ? AND (merchant LIKE ? OR clean_merchant = ?)
              AND date >= datetime('now', '-365 days')""",
            (user_id, f"%{merch}%", merch)
        )
        annual_row = c.fetchone()
        annual_spend = float(annual_row[0] or (float(amt or 0.0) * 12))

        zombies.append({
            "merchant": merch,
            "monthly_amount": float(amt or 0.0),
            "annual_cost": round(annual_spend, 2),
            "last_billed": l_date,
            "recent_activity_count": recent_count,
            "recommendation": f"Review potential cancellation. Saving: ${annual_spend:,.2f}/yr."
        })

    return zombies

def get_budget_settings(*, user_id: str) -> tuple[float, float]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_budget_settings: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        "SELECT weekly_discretionary_limit, impulse_threshold FROM budget_settings WHERE user_id = ? LIMIT 1",
        (user_id,)
    )
    row = c.fetchone()
    return (float(row[0]), float(row[1])) if row else (150.00, 50.00)

# ============================================================
# Income Reconciliation & Subscriptions
# ============================================================
def check_and_reconcile_income(merchant: str, amount: float, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "check_and_reconcile_income: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        "SELECT id, type, net_expected FROM cash_inflows WHERE user_id = ? AND status = 'Pending'",
        (user_id,)
    )
    candidates = []
    for inflow_id, inflow_type, expected_amt in c.fetchall():
        if expected_amt is None:
            continue
        # Income posts as a NEGATIVE amount; expected income is stored positive.
        amount_close = abs(abs(amount) - abs(expected_amt)) < 0.01
        type_match = inflow_type and inflow_type.lower() in merchant.lower()
        if amount_close:
            candidates.append((inflow_id, inflow_type, type_match))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: not x[2])
    inflow_id, inflow_type, _ = candidates[0]
    c.execute("UPDATE cash_inflows SET status = 'Received' WHERE user_id = ? AND id = ?", (user_id, inflow_id))
    safe_commit()
    return f" **Income Reconciled:** Match found for {inflow_type} (+${abs(amount):.2f})."

def detect_and_update_subscriptions(merchant: str, amount: float, date_str: str, *, user_id: str):
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "detect_and_update_subscriptions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    clean = sanitize_merchant_name(merchant)
    c.execute("SELECT id FROM subscriptions WHERE user_id = ? AND merchant = ?", (user_id, clean))
    row = c.fetchone()
    if row:
        c.execute(
            "UPDATE subscriptions SET last_amount = ?, last_date = ? WHERE user_id = ? AND id = ?",
            (amount, date_str, user_id, row[0]),
        )
    else:
        # Do not embed a merchant-specific subscription whitelist in source code.
        # New subscriptions must be discovered by application logic/research
        # and then stored in the database.
        pass
    safe_commit()

# ============================================================
# Manual Add & Transaction Locking
# ============================================================
def add_transaction(date: str,
    merchant: str,
    amount: float,
    category: str = "Uncategorized ",
    account_used: str = "Manual Entry",
    status: str = "Evaluated",
    judgment: str | None = None,
    notes: str | None = None, *, user_id: str) -> str:
    """Manually add a cash transaction to the ledger that wasn't imported via Plaid."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "add_transaction: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return " amount must be numeric."

    if not judgment:
        judgment = f" **{category}** • **{merchant}** (${abs(amount):,.2f})\n {notes or 'Manual entry'}"

    clean_merchant = sanitize_merchant_name(merchant)

    c.execute(
        """INSERT INTO transactions
        (user_id, date, merchant, clean_merchant, amount, category, account_used, status, judgment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, date, merchant, clean_merchant, amount, category, account_used, status, judgment)
    )
    safe_commit()
    rid = c.lastrowid
    return f" Manually added transaction #{rid}: {merchant} ${amount:,.2f} on {date}."

def lock_transaction(*, transaction_row_id: int, locked: bool = True, auto_commit: bool = True, user_id: str,) -> str:
    """Lock or unlock a transaction to make it immutable."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "lock_transaction: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        transaction_row_id = int(transaction_row_id)
    except (TypeError, ValueError):
        return " transaction_row_id must be an integer."

    c.execute("SELECT id FROM transactions WHERE user_id = ? AND id = ?", (user_id, transaction_row_id))
    if not c.fetchone():
        return f" No transaction #{transaction_row_id} exists."

    with conn:
        conn.execute("UPDATE transactions SET is_locked = ? WHERE user_id = ? AND id = ?", (1 if locked else 0, user_id, transaction_row_id))
    if auto_commit: safe_commit()
    state = " Locked (immutable)" if locked else " Unlocked (editable)"
    return f" Transaction #{transaction_row_id} is now {state}."

# ============================================================
# Settled Transaction Corrections — FULL CLASSIFICATION CONTROL
# ============================================================
def correct_transaction(*, auto_commit: bool = True, 
    transaction_id: str | None = None,
    transaction_row_id: int | None = None,
    corrected_amount: float | None = None,
    reason: str = "",
    source: str = "user",
    context_tag: str | None = None,
    context_note: str | None = None,
    # Only mutable classification/accounting fields. Transaction identity is immutable.
    category: str | None = None,
    status: str | None = None,
    judgment: str | None = None,user_id: str,) -> str:
    """Correct only permitted local accounting/classification fields.
    Transaction identity fields (merchant, clean_merchant, account_used, date,
    transaction_id) are immutable and cannot be modified by this tool.
    The original Plaid transaction_id is never modified. At least one of the optional
    parameters must be provided. A reason is always required for the audit trail.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "correct_transaction: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if transaction_row_id is None and not transaction_id:
        return " Provide transaction_row_id or transaction_id."
    if not reason.strip():
        return " A correction reason is required so the ledger keeps an audit trail."

    c.execute(
        """SELECT id, transaction_id, date, merchant, amount, account_used,
        override_amount, override_reason, is_locked
        FROM transactions WHERE user_id = ? AND (id = ? OR transaction_id = ?) LIMIT 1""",
        (
            user_id,
            int(transaction_row_id) if transaction_row_id is not None else -1,
            transaction_id or "",
        ),
    )
    row = c.fetchone()
    if not row:
        return " I couldn't find that transaction."
    (
        row_id,
        plaid_tx_id,
        date,
        orig_merchant,
        original_amount,
        orig_account,
        old_override,
        old_reason,
        is_locked,
    ) = row

    # HARD GATE: Prevent modifications to locked/immutable transactions
    if is_locked:
        return f" REJECTED: Transaction #{row_id} is locked (immutable). Unlock it first if you absolutely must change it."

    resolved_merchant = _resolve_known_merchant(orig_merchant)
    if resolved_merchant is None and orig_merchant:
        researched = _research_set()
        orig_key = _merchant_key(orig_merchant)
        if orig_key not in researched:
            return (
                f" REJECTED: Merchant '{orig_merchant}' is not in the known merchant registry. "
                "Web research is REQUIRED before this transaction can be corrected. "
                f"Call search_web with the merchant name '{orig_merchant}', then retry the correction."
            )

    # HARD GATE (validated BEFORE any writes): category changes REQUIRE a judgment.
    if category is not None and judgment is None:
        return (
            " REJECTED: You changed `category` without providing `judgment`. "
            "Every category correction MUST include a judgment that explains what this "
            "transaction actually is. Retry with BOTH parameters:\n"
            f'  category="{category}"\n'
            f'  judgment=" **{category}** • **{orig_merchant or "Unknown"}** '
            f'(${abs(original_amount or 0):,.2f})\n <your one-line explanation>"\n'
            "The judgment is Delilah's opinion on what the transaction was for. "
            "Never leave it out when reclassifying."
        )

    if status is not None and status not in {"Pending", "Evaluated", "Ignored"}:
        return f" Invalid status. Must be one of: Pending, Evaluated, Ignored"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changes = []

    if corrected_amount is not None:
        try:
            corrected_amount = float(corrected_amount)
        except (TypeError, ValueError):
            return " corrected_amount must be numeric."
        c.execute(
            "UPDATE transactions SET override_amount = ?, override_reason = ?, override_at = ?, override_source = ? WHERE user_id = ? AND id = ?",
            (corrected_amount, reason.strip(), now, source or "user", user_id, row_id),
        )
        changes.append(f"amount → ${abs(corrected_amount):,.2f}")

    if category is not None:
        c.execute(
            "UPDATE transactions SET category = ? WHERE user_id = ? AND id = ?",
            (category.strip(), user_id, row_id),
        )
        if resolved_merchant is None and orig_merchant:
            c.execute("INSERT OR IGNORE INTO known_merchants (merchant_key, canonical_name, category, confidence, source, updated_at) VALUES (?, ?, ?, 'medium', 'auto_cache', ?)", (re.sub(r"\s+", " ", str(orig_merchant).strip().lower()), orig_merchant, category.strip(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        changes.append(f"category → {category.strip()}")


    if status is not None:
        with conn:
            conn.execute("UPDATE transactions SET status = ? WHERE user_id = ? AND id = ?", (status, user_id, row_id))
        changes.append(f"status → {status}")

    if judgment is not None:
        # HARD SAFEGUARD: Prevent the LLM from writing trigger words that break the audit loop
        safe_judgment = judgment.strip()
        safe_judgment = safe_judgment.replace(
            "Model Unavailable flag cleared", "previously unresolved flag cleared"
        )
        safe_judgment = safe_judgment.replace("Model Unavailable", "Unresolved")
        c.execute(
            "UPDATE transactions SET judgment = ? WHERE user_id = ? AND id = ?", (safe_judgment, user_id, row_id)
        )
        changes.append(f"judgment → {safe_judgment[:200]}")

    if context_tag is not None:
        if not isinstance(context_tag, str) or not context_tag.strip():
            return " context_tag must be a non-empty string when supplied."
        c.execute(
            "INSERT INTO transaction_context (user_id, transaction_row_id, tag, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, row_id, context_tag.strip(), context_note, now),
        )
        changes.append(f"context: {context_tag.strip()}")

    if not changes:
        return " No changes supplied. Provide at least one of: corrected_amount, category, status, judgment, context_tag."

    # Automatic immutable audit record: every successful correction is logged with
    # the exact before/after values and the reason/source supplied to the tool.

    c.execute(
        """
        INSERT INTO transaction_correction_log
        (user_id, transaction_row_id, transaction_id, occurred_at, action, source, reason,
        changes, before_state, after_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, 
            row_id,
            plaid_tx_id,
            now,
            "correct",
            source or "user",
            reason.strip(),
            json.dumps(changes, ensure_ascii=False),
            json.dumps(
                {
                    "date": date,
                    "merchant": orig_merchant,
                    "amount": original_amount,
                    "account_used": orig_account,
                    "override_amount": old_override,
                    "override_reason": old_reason,
                },
                ensure_ascii=False,
                default=str,
            ),
            json.dumps(
                {
                    "date": date,
                    "merchant": orig_merchant,
                    "amount": original_amount,
                    "account_used": orig_account,
                    "override_amount": corrected_amount if corrected_amount is not None else old_override,
                    "override_reason": reason.strip() if corrected_amount is not None else old_reason,
                    "category": category,
                    "status": status,
                    "judgment": judgment,
                    "context_tag": context_tag,
                },
                ensure_ascii=False,
                default=str,
            ),
        ),
    )

    if auto_commit: safe_commit()
    context_text = format_transaction_context(get_transaction_context(transaction_row_id=row_id, user_id=user_id))
    return f" #{row_id} updated: {'; '.join(changes)} — audit logged."

def log_lifestyle_context(state: str,
    note: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None, *, user_id: str) -> str:

    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "log_lifestyle_context: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not isinstance(state, str) or not state.strip():
        return " A non-empty state is required."
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT INTO lifestyle_context_log (user_id, state, note, starts_at, ends_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, state.strip(), note, starts_at, ends_at, now),
    )
    safe_commit()
    window = f" ({starts_at} to {ends_at})" if starts_at or ends_at else ""
    return f" Logged lifestyle context: **{state.strip()}**{window}."

def get_lifestyle_context(*, days: int = 90, limit: int = 20, offset: int = 0, user_id: str,) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_lifestyle_context: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    days = max(1, min(int(days), 3650))
    limit = max(1, min(int(limit), 100))
    c.execute(
        """SELECT state, note, starts_at, ends_at, created_at FROM lifestyle_context_log
        WHERE user_id = ? AND created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (user_id, f"-{days} days", limit, offset),
    )
    rows = c.fetchall()
    if not rows:
        return f" No lifestyle context logged in the last {days} days."
    lines = [f" **Lifestyle context — last {days} days**"]
    for state, note, starts, ends, created in rows:
        window = f" [{starts} → {ends}]" if starts or ends else ""
        line = f"- `{str(created)[:16]}` {state}{window}"
        if note:
            line += f" — {note}"
        lines.append(line)
    return "\n".join(lines)

def tag_transaction_context(transaction_row_id: int, tag: str, note: str | None = None, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "tag_transaction_context: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        transaction_row_id = int(transaction_row_id)
    except (TypeError, ValueError):
        return " transaction_row_id must be an integer."
    if not isinstance(tag, str) or not tag.strip():
        return " A non-empty tag is required."
    c.execute(
        "SELECT id, clean_merchant, merchant, amount FROM transactions WHERE user_id = ? AND id = ?",
        (user_id, transaction_row_id),
    )
    row = c.fetchone()
    if not row:
        return f" No transaction #{transaction_row_id} exists."
    c.execute("SELECT id FROM transaction_context WHERE user_id=? AND transaction_row_id=? AND LOWER(tag)=LOWER(?)", (user_id, transaction_row_id, tag.strip()))
    if c.fetchone():
        return f" Tag '{tag.strip()}' already exists on #{transaction_row_id}."
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT INTO transaction_context (user_id, transaction_row_id, tag, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, transaction_row_id, tag.strip(), note, now),
    )
    safe_commit()
    label = row[1] or row[2]
    return f" Tagged #{transaction_row_id} ({label}, ${abs(float(row[3] or 0)):,.2f}) as **{tag.strip()}**."

def get_transactions_by_context(*, tag: str, days: int = 90, limit: int = 50, user_id: str,) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_transactions_by_context: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not isinstance(tag, str) or not tag.strip():
        return " A tag is required."
    days = max(1, min(int(days), 3650))
    limit = max(1, int(limit))
    c.execute(
        """SELECT t.id, t.date, COALESCE(t.clean_merchant, t.merchant), t.amount, tc.tag, tc.note, tc.created_at
        FROM transaction_context tc JOIN transactions t ON t.id = tc.transaction_row_id
        WHERE t.user_id = ?
        AND LOWER(tc.tag) LIKE LOWER(?) AND tc.created_at >= datetime('now', ?)
        ORDER BY tc.created_at DESC LIMIT ? OFFSET ?""",
        (user_id, f"%{tag.strip()}%", f"-{days} days", limit, 0),
    )
    rows = c.fetchall()
    if not rows:
        return f" No transactions tagged **{tag}** in the last {days} days."
    total = sum(abs(float(r[3] or 0)) for r in rows)
    lines = [
        f" **Transactions tagged '{tag}'** — {len(rows)} found, ${total:,.2f} total"
    ]
    for rid, date, merchant, amount, t, note, created in rows:
        line = f"- #{rid} `{str(date)[:10]}` {merchant} — ${abs(float(amount or 0)):,.2f} [{t}]"
        if note:
            compact_note = re.sub(r"\s+", " ", str(note)).strip()
            if len(compact_note) > 240:
                compact_note = compact_note[:240].rstrip() + "…"
            line += f" — {compact_note}"
        lines.append(line)
    return "\n".join(lines)

def clear_transaction_correction(transaction_row_id: int | None = None,
    transaction_id: str | None = None, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "clear_transaction_correction: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if transaction_row_id is None and not transaction_id:
        return " Provide transaction_row_id or transaction_id."

    c.execute(
        """SELECT id, transaction_id, date, merchant, amount, account_used,
        override_amount, override_reason, override_at, override_source
        FROM transactions
        WHERE user_id = ? AND (id = ? OR transaction_id = ?) LIMIT 1""",
        (
            user_id,
            int(transaction_row_id) if transaction_row_id is not None else -1,
            transaction_id or "",
        ),
    )
    row = c.fetchone()
    if not row:
        return " No matching transaction found."

    (
        row_id,
        plaid_tx_id,
        date,
        merchant,
        amount,
        account_used,
        old_override,
        old_reason,
        old_override_at,
        old_override_source,
    ) = row

    if old_override is None and old_reason is None and old_override_at is None and old_override_source is None:
        return f" Transaction #{row_id} has no local correction to clear."

    c.execute(
        """UPDATE transactions SET override_amount = NULL, override_reason = NULL,
        override_at = NULL, override_source = NULL WHERE user_id = ? AND id = ?""",
        (user_id, row_id),
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        """
        INSERT INTO transaction_correction_log
        (user_id, transaction_row_id, transaction_id, occurred_at, action, source, reason,
         changes, before_state, after_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            row_id,
            plaid_tx_id,
            now,
            "clear",
            "user",
            "Local transaction correction cleared",
            json.dumps(["override_amount → NULL", "override_reason → NULL", "override_at → NULL", "override_source → NULL"], ensure_ascii=False),
            json.dumps(
                {
                    "date": date,
                    "merchant": merchant,
                    "amount": amount,
                    "account_used": account_used,
                    "override_amount": old_override,
                    "override_reason": old_reason,
                    "override_at": old_override_at,
                    "override_source": old_override_source,
                },
                ensure_ascii=False,
                default=str,
            ),
            json.dumps(
                {
                    "date": date,
                    "merchant": merchant,
                    "amount": amount,
                    "account_used": account_used,
                    "override_amount": None,
                    "override_reason": None,
                    "override_at": None,
                    "override_source": None,
                },
                ensure_ascii=False,
                default=str,
            ),
        ),
    )

    safe_commit()
    return f" Local transaction correction cleared for #{row_id} — audit logged."


def get_known_merchant(*, merchant: str) -> str:
    """Read-only lookup in the curated known-merchant registry, including aliases."""
    if not isinstance(merchant, str) or not merchant.strip():
        return " merchant is required."
    resolved = _resolve_known_merchant(merchant)
    if not resolved:
        return f" UNKNOWN MERCHANT: {merchant}. Web research is REQUIRED before classifying it."
    matched = resolved["matched_by"]
    return (
        f" KNOWN MERCHANT: {resolved['canonical_name']}\n"
        f"matched_by={matched} category={resolved['category'] or 'unspecified'}\n"
        f"confidence={resolved['confidence']} source={resolved['source']} updated={resolved['updated_at']}\n"
        f"notes={resolved['notes'] or '(none)'}\n"
        f"evidence_url={resolved['evidence_url'] or '(none)'}"
    )

def get_unique_unregistered_merchants(*, user_id: str,
    scope: str = "unlocked",
    days: int | None = None,
    limit: int = 50,
    status: str = "all",
) -> str:
    """Return a compact deduplicated worklist of ledger merchants missing from the registry.

    This is intentionally merchant-level rather than transaction-level so an audit can
    research each merchant once and then apply the result to all matching transactions.
    System-generated balance rows are excluded.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_unique_unregistered_merchants: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    scope = str(scope or "unlocked").strip().lower()
    if scope not in {"unlocked", "locked", "all"}:
        return " scope must be unlocked, locked, or all."
    safe_limit = max(1, min(int(limit or 50), 100))
    params: list[object] = []
    where = ["merchant IS NOT NULL", "TRIM(merchant) <> ''",
             "LOWER(TRIM(merchant)) <> 'system balance sync'"]
    if scope == "unlocked":
        where.append("is_locked = 0")
    elif scope == "locked":
        where.append("is_locked = 1")
    if days is not None:
        try:
            safe_days = max(1, min(int(days), 3650))
            where.append("date >= date('now', ?)")
            params.append(f"-{safe_days} days")
        except (TypeError, ValueError):
            return " days must be a valid integer."
    if status and str(status).lower() != "all":
        where.append("LOWER(COALESCE(status,'')) = LOWER(?)")
        params.append(str(status))

    c.execute(
        f"""SELECT merchant, COUNT(*) AS tx_count,
                   GROUP_CONCAT(id, ',') AS tx_ids,
                   MIN(date) AS first_seen, MAX(date) AS last_seen
              FROM transactions
             WHERE user_id = ? AND {' AND '.join(where)}
             GROUP BY LOWER(TRIM(merchant))
             ORDER BY tx_count DESC, MAX(datetime(date)) DESC, merchant""",
        [user_id] + params,
    )
    rows = c.fetchall()

    unmatched = []
    for merchant, tx_count, tx_ids, first_seen, last_seen in rows:
        resolved = _resolve_known_merchant(str(merchant))
        if resolved:
            continue
        ids = [x for x in str(tx_ids or '').split(',') if x]
        unmatched.append(
            {
                "merchant": str(merchant),
                "transaction_count": int(tx_count or 0),
                "example_transaction_ids": [int(x) for x in ids[:8] if x.isdigit()],
                "first_seen": first_seen,
                "last_seen": last_seen,
            }
        )
        if len(unmatched) >= safe_limit:
            break

    return json.dumps(
        {
            "scope": scope,
            "count": len(unmatched),
            "merchants": unmatched,
            "note": "Research each merchant once, save reliable results to known_merchants, then classify matching transactions."
        },
        ensure_ascii=False,
    )


def save_known_merchant(
    merchant: str,
    canonical_name: str,
    category: str | None = None,
    confidence: str = "high",
    notes: str = "",
    evidence_url: str = "",
    source: str = "research",
) -> str:
    """Promote a reliably researched merchant into the curated registry."""
    key = _merchant_key(merchant)
    if not key or not str(canonical_name or '').strip():
        return " merchant and canonical_name are required."
    confidence = str(confidence or "high").strip().lower()
    if confidence not in {"high", "medium"}:
        return " confidence must be 'high' or 'medium'."

    source_key = str(source or "research").strip().lower() or "research"
    if source_key != "user":
        merchant_key_val = _merchant_key(merchant)
        researched = _research_set()
        if not any(merchant_key_val in r or r in merchant_key_val for r in researched):
            return (
                f" REJECTED: Cannot save '{merchant}' to known_merchants without web research. "
                "Call search_web for the merchant first, then retry save_known_merchant."
            )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        """INSERT INTO known_merchants
        (merchant_key, canonical_name, category, confidence, notes, evidence_url, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(merchant_key) DO UPDATE SET
            canonical_name=excluded.canonical_name, category=excluded.category,
            confidence=excluded.confidence, notes=excluded.notes,
            evidence_url=excluded.evidence_url, source=excluded.source,
            updated_at=excluded.updated_at""",
        (key, str(canonical_name).strip(), category.strip() if category else None, confidence, str(notes or '').strip(), str(evidence_url or '').strip(), str(source or 'research').strip() or 'research', now),
    )
    safe_commit()
    c.execute("SELECT id, canonical_name, category FROM known_merchants WHERE merchant_key = ? LIMIT 1", (key,))
    verified = c.fetchone()
    if not verified:
        return f" Known merchant write did not persist: {str(canonical_name).strip()}"
    print(f" [REGISTRY VERIFY] merchant_key={key} id={verified[0]} canonical={verified[1]} category={verified[2] or '—'}")
    return f" Known merchant saved and verified: {str(canonical_name).strip()} [{category or 'no category'}]."


def get_recent_corrections(*, user_id: str,
    days: int = 90,
    limit: int = 50,
    transaction_row_id: int | None = None,
    offset: int = 0,
) -> str:
    """Read-only audit log of transaction corrections/clears."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_recent_corrections: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    days = max(1, min(int(days), 3650))
    limit = max(1, int(limit))

    conditions = ["user_id = ?", "occurred_at >= datetime('now', ?)"]
    params: list = [f"-{days} days"]
    if transaction_row_id is not None:
        try:
            transaction_row_id = int(transaction_row_id)
        except (TypeError, ValueError):
            return " transaction_row_id must be an integer."
        conditions.append("transaction_row_id = ?")
        params.append(transaction_row_id)
    params.append(limit)
    params.append(offset)

    c.execute(
        f"""
        SELECT id, transaction_row_id, transaction_id, occurred_at, action,
               source, reason, changes, before_state, after_state
        FROM transaction_correction_log
        WHERE {' AND '.join(conditions)}
        ORDER BY datetime(occurred_at) DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [user_id] + params,
    )
    rows = c.fetchall()
    if not rows:
        if transaction_row_id is not None:
            return f" No correction history found for transaction #{transaction_row_id} in the last {days} days."
        return f" No correction history found in the last {days} days."

    lines = [f" **Recent transaction corrections** — {len(rows)} entries"]
    for log_id, row_id, plaid_id, occurred_at, action, source, reason, changes_json, before_json, after_json in rows:
        try:
            changes = json.loads(changes_json) if changes_json else []
        except Exception:
            changes = [str(changes_json)] if changes_json else []
        change_text = "; ".join(str(x) for x in changes) or "(no listed field changes)"
        line = (
            f"- **log #{log_id}** tx **#{row_id}** `{str(occurred_at)[:19]}` "
            f"**{action}** source={source or 'unknown'} — {change_text}"
        )
        if reason:
            line += f" — reason: {str(reason)[:240]}"
        lines.append(line)

    return "\n".join(lines)


# ============================================================
# Transaction Deletion
# ============================================================

def delete_transaction(
    *,
    transaction_row_id: int,
    reason: str,
    source: str = "user",
    user_id: str,
) -> str:
    """
    Permanently delete ONLY a manually/local transaction.

    HARD SAFETY CONTRACT:
      - current user's row only
      - transaction must exist
      - transaction must be unlocked
      - transaction_id MUST be NULL/empty
      - Plaid-backed transactions can NEVER be deleted
      - transactions_original is NEVER modified
      - deletion is recorded in transaction_correction_log first
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "delete_transaction: user_id is required and must be a non-empty string."
        )

    try:
        rid = int(transaction_row_id)
    except (TypeError, ValueError):
        return " transaction_row_id must be an integer."

    reason = str(reason or "").strip()
    if not reason:
        return " A deletion reason is required."

    c.execute(
        """
        SELECT
            id,
            transaction_id,
            message_id,
            date,
            merchant,
            clean_merchant,
            category,
            amount,
            account_used,
            total_liquid,
            net_cash,
            all_balances,
            status,
            judgment,
            pending_transaction_id,
            override_amount,
            override_reason,
            override_at,
            override_source,
            is_locked,
            user_id
        FROM transactions
        WHERE id = ? AND user_id = ?
        """,
        (rid, user_id),
    )
    row = c.fetchone()

    if not row:
        return f" Transaction #{rid} not found for this user."

    (
        db_id,
        transaction_id,
        message_id,
        date,
        merchant,
        clean_merchant,
        category,
        amount,
        account_used,
        total_liquid,
        net_cash,
        all_balances,
        status,
        judgment,
        pending_transaction_id,
        override_amount,
        override_reason,
        override_at,
        override_source,
        is_locked,
        db_user_id,
    ) = row

    if int(is_locked or 0) != 0:
        return f" Transaction #{rid} is locked and cannot be deleted."

    # THIS IS THE CRITICAL PLAID SAFETY GATE.
    # A real Plaid transaction has a transaction_id.
    # Only locally/manual rows with NULL/empty transaction_id may be deleted.
    if transaction_id is not None and str(transaction_id).strip():
        return (
            f" Transaction #{rid} cannot be deleted: "
            "it is linked to a Plaid transaction."
        )

    before_state = {
        "id": db_id,
        "transaction_id": transaction_id,
        "message_id": message_id,
        "date": date,
        "merchant": merchant,
        "clean_merchant": clean_merchant,
        "category": category,
        "amount": amount,
        "account_used": account_used,
        "total_liquid": total_liquid,
        "net_cash": net_cash,
        "all_balances": all_balances,
        "status": status,
        "judgment": judgment,
        "pending_transaction_id": pending_transaction_id,
        "override_amount": override_amount,
        "override_reason": override_reason,
        "override_at": override_at,
        "override_source": override_source,
        "is_locked": is_locked,
        "user_id": db_user_id,
    }

    import json

    # Record the deletion BEFORE removing the row.
    c.execute(
        """
        INSERT INTO transaction_correction_log (
            transaction_row_id,
            transaction_id,
            occurred_at,
            action,
            source,
            reason,
            changes,
            before_state,
            after_state,
            user_id
        )
        VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rid,
            None,
            "delete",
            str(source or "user"),
            reason,
            json.dumps(["DELETE"]),
            json.dumps(before_state, default=str),
            json.dumps(None),
            user_id,
        ),
    )

    c.execute(
        """
        DELETE FROM transactions
        WHERE id = ?
          AND user_id = ?
          AND is_locked = 0
          AND (transaction_id IS NULL OR TRIM(transaction_id) = '')
        """,
        (rid, user_id),
    )

    if c.rowcount != 1:
        # Defensive rollback of the audit insert if the final DELETE
        # somehow did not affect exactly one row.
        conn.rollback()
        return f" Transaction #{rid} was not deleted."

    conn.commit()

    return f" DELETED TRANSACTION #{rid}"

# ============================================================
# General-Purpose Memories
# ============================================================
def save_memory(content: str, category: str = "general", importance: str = "normal",
    pinned: bool = False, *, user_id: str) -> str:
    """Save a durable memory/note for future reference. Use this liberally —
    memories are cheap, forgetting is expensive. Categories help retrieval."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "save_memory: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not content or not content.strip():
        return " Memory content is required."
    import difflib
    normalized_new = content.strip().lower()
    effective_category = (category.strip().lower() if category else "general")

    # LAYER 1: Exact case-insensitive match
    c.execute(
        "SELECT id FROM delilah_memories WHERE user_id = ? AND LOWER(content) = LOWER(?) LIMIT 1",
        (user_id, content.strip()),
    )
    if c.fetchone():
        return (
            f" Duplicate memory blocked. I already know this: "
            f"{content.strip()[:80]}..."
        )

    # LAYER 2: Fuzzy similarity against recent memories in same category
    c.execute(
        "SELECT id, content FROM delilah_memories "
        "WHERE user_id = ? AND LOWER(category) = LOWER(?) ORDER BY id DESC LIMIT 50",
        (user_id, effective_category),
    )
    for _rid, existing_content in c.fetchall():
        similarity = difflib.SequenceMatcher(
            None, normalized_new, existing_content.strip().lower()
        ).ratio()
        if similarity > 0.80:
            return (
                f" Duplicate memory blocked (similarity {similarity:.0%}). "
                f"I already know this: {existing_content.strip()[:80]}..."
            )

    # LAYER 3: Merchant-specific keyword match
    if effective_category == "merchant":
        merchant_match = re.search(
            r"merchant:\s*(.+?)\s+is\s+", content.strip(), re.IGNORECASE
        )
        if merchant_match:
            merchant_name = merchant_match.group(1).strip().lower()
            if merchant_name:
                c.execute(
                    "SELECT id FROM delilah_memories "
                    "WHERE user_id = ? AND LOWER(category) = 'merchant' "
                    "AND LOWER(content) LIKE ? LIMIT 1",
                    (user_id, f"%{merchant_name}%"),
                )
                if c.fetchone():
                    return (
                        f" Duplicate merchant memory blocked. "
                        f"I already have a memory for '{merchant_name}'."
                    )

    # ── ACTUAL INSERT ──
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        """INSERT INTO delilah_memories (user_id, category, content, importance, created_at, is_pinned)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            category.strip() if category else "general",
            content.strip(),
            importance.strip() if importance else "normal",
            now,
            1 if pinned else 0,
        ),
    )
    rid = c.lastrowid
    safe_commit()
    pin_note = "  (pinned — always shown)" if pinned else ""
    return (
        f" Saved memory #{rid} [{category}]{pin_note}: "
        f"{content.strip()[:120]}{'...' if len(content.strip()) > 120 else ''}"
    )

# Patched get_memories bindings
def get_memories(*, category: str | None = None, days: int = 90, limit: int = 20, offset: int = 0, user_id: str,) -> str:
    """Retrieve saved memories, optionally filtered by category.
    Pinned memories (is_pinned=1) are ALWAYS included regardless of the days
    window or pagination offset, since they represent durable facts (APRs,
    core preferences, identity) that should never silently age out."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_memories: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    days = max(1, min(int(days), 3650))
    limit = max(1, min(int(limit), 100))

    # Pinned memories: always fetched in full, independent of days/offset.
    if category and category.strip():
        c.execute(
            """SELECT id, category, content, importance, created_at
            FROM delilah_memories
            WHERE user_id = ? AND is_active = 1 AND is_pinned = 1
            AND (expires_at IS NULL OR expires_at > datetime('now'))
            AND LOWER(category) = LOWER(?)
            ORDER BY datetime(created_at) DESC""",
            (user_id, category.strip(),),
        )
    else:
        c.execute(
            """SELECT id, category, content, importance, created_at
            FROM delilah_memories
            WHERE user_id = ? AND is_active = 1 AND is_pinned = 1
            AND (expires_at IS NULL OR expires_at > datetime('now'))
            ORDER BY datetime(created_at) DESC""",
            (user_id,),
        )
    pinned_rows = c.fetchall()
    pinned_ids = {r[0] for r in pinned_rows}

    # Regular windowed/paginated memories, excluding anything already pinned
    # so nothing is shown twice.
    if category and category.strip():
        c.execute(
            """SELECT id, category, content, importance, created_at
            FROM delilah_memories
            WHERE user_id = ? AND is_active = 1 AND is_pinned = 0 AND (expires_at IS NULL OR expires_at > datetime('now')) AND LOWER(category) = LOWER(?) AND created_at >= datetime('now', ?)
            ORDER BY datetime(created_at) DESC LIMIT ? OFFSET ?""",
            (user_id, category.strip(), f"-{days} days", limit, offset),
        )
    else:
        c.execute(
            """SELECT id, category, content, importance, created_at
            FROM delilah_memories
            WHERE user_id = ? AND is_active = 1 AND is_pinned = 0 AND (expires_at IS NULL OR expires_at > datetime('now')) AND created_at >= datetime('now', ?)
            ORDER BY datetime(created_at) DESC LIMIT ? OFFSET ?""",
            (user_id, f"-{days} days", limit, offset),
        )
    rows = c.fetchall()

    if not rows and not pinned_rows:
        return f" No memories found in the last {days} days."

    lines = []
    if pinned_rows:
        lines.append(f" **Pinned memories (always shown, {len(pinned_rows)})**")
        for rid, cat, content_, importance, created in pinned_rows:
            compact = re.sub(r"\s+", " ", str(content_ or "")).strip()
            if len(compact) > 320:
                compact = compact[:320].rstrip() + "…"
            lines.append(
                f"- #{rid} [{cat}] ({importance}) `{str(created)[:16]}` — {compact}"
            )
        lines.append("")

    if rows:
        lines.append(f" **Saved memories — last {days} days**")
        for rid, cat, content_, importance, created in rows:
            compact = re.sub(r"\s+", " ", str(content_ or "")).strip()
            if len(compact) > 320:
                compact = compact[:320].rstrip() + "…"
            lines.append(
                f"- #{rid} [{cat}] ({importance}) `{str(created)[:16]}` — {compact}"
            )
    elif pinned_rows:
        lines.append(f" No additional memories in the last {days} days (pinned ones shown above).")

    return "\n".join(lines)

def delete_memory(memory_id: int | None = None,
    content_match: str | None = None,
    category: str | None = None, *, user_id: str) -> str:
    """Delete one or more memories. Provide memory_id for a single precise
    deletion, or content_match (with optional category filter) to delete
    all memories whose content contains that substring."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "delete_memory: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if memory_id is None and not content_match:
        return " Provide either memory_id or content_match."

    if memory_id is not None:
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return " memory_id must be an integer."
        c.execute(
            """SELECT id, category, content FROM delilah_memories
            WHERE user_id = ? AND id = ?""",
            (user_id, memory_id),
        )
        row = c.fetchone()
        if not row:
            return f" No memory with ID {memory_id} exists."
        c.execute("DELETE FROM delilah_memories WHERE user_id = ? AND id = ?", (user_id, memory_id))
        safe_commit()
        return (
            f" Deleted memory #{memory_id} [{row[1]}]: "
            f"{str(row[2])[:100]}{'...' if len(str(row[2])) > 100 else ''}"
        )

    # content_match path
    like = f"%{content_match.strip().lower()}%"
    if category and category.strip():
        c.execute(
            "SELECT id, category, content FROM delilah_memories "
            "WHERE user_id = ? AND LOWER(content) LIKE ? AND LOWER(category) = LOWER(?)",
            (user_id, like, category.strip()),
        )
    else:
        c.execute(
            "SELECT id, category, content FROM delilah_memories "
            "WHERE user_id = ? AND LOWER(content) LIKE ?",
            (user_id, like),
        )
    rows = c.fetchall()
    if not rows:
        return f" No memories matched \"{content_match}\"."
    ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(ids))
    c.execute(f"DELETE FROM delilah_memories WHERE user_id = ? AND id IN ({placeholders})", [user_id] + ids)
    safe_commit()
    return f" Deleted {len(ids)} memory(ies) matching \"{content_match}\"."

# ============================================================
# Financial Visibility / Read Models
# ============================================================
def _num(value) -> float:
    if isinstance(value, str):
        value = value.replace('$', '').replace(',', '')
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

def get_accounts_overview(*, include_inactive: bool = False, user_id: str,) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_accounts_overview: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    where = (
        "WHERE user_id = ?"
        if include_inactive
        else "WHERE user_id = ? AND active = 1"
    )
    c.execute(f"""SELECT plaid_account_id, name, official_name, mask, type, subtype,
        institution_name, current_balance, available_balance,
        credit_limit, last_synced_at, active
        FROM plaid_accounts {where}
        ORDER BY type, subtype, name""", (user_id,))
    rows = c.fetchall()
    if not rows:
        return " No synced account registry records yet. Run a Plaid sync first."
    lines = [" **Accounts overview**"]
    for (
        aid,
        name,
        official,
        mask,
        typ,
        subtype,
        inst,
        current,
        available,
        limit_amt,
        synced,
        active,
    ) in rows:
        balance = f"current ${_num(current):,.2f}"
        if available is not None:
            balance += f", available ${_num(available):,.2f}"
        if limit_amt is not None:
            balance += f", limit ${_num(limit_amt):,.2f}"
        lines.append(
            f"- **{name or official or 'Unnamed account'}**"
            f"{' ••••'+str(mask) if mask else ''} — "
            f"{typ or 'unknown'}/{subtype or 'unknown'} — {balance} — "
            f"{inst or 'institution unknown'}"
            f"{' — synced '+str(synced) if synced else ''}"
        )
        if not active:
            lines[-1] += " — inactive"
    return "\n".join(lines)

# Available fields for transaction queries — SELECT * vs SELECT specific

def _format_compact_tx_line(rid, date, merchant, amount, category, status, locked=None, registry=None):
    """Small model-facing transaction line. Detailed fields are opt-in via `fields`."""
    bits = [
        f"#{rid}",
        str(date)[:10],
        str(merchant or "Unknown"),
        f"${abs(_num(amount)):.2f}",
        str(category or "Uncat"),
        str(status or "unknown"),
    ]
    if locked is not None:
        bits.append("" if locked else "")
    if registry:
        bits.append(registry)
    return " | ".join(bits)

_TX_FIELDS = {
    "id",
    "transaction_id",
    "date",
    "merchant",
    "clean_merchant",
    "category",
    "amount",
    "override_amount",
    "account_used",
    "status",
    "judgment",
    "pending_transaction_id",
    "context",
    "is_locked",
}

def get_recent_transactions(*, user_id: str,
    days: int = 30,
    limit: int = 50,
    status: str = "all",
    fields: str | None = None,
    offset: int = 0,
) -> str:
    """Show transaction history. If `fields` is None, returns ALL fields (SELECT *).
    If fields is a comma-separated string, returns only those fields.
    Available fields: id, transaction_id, date, merchant, clean_merchant, category,
    amount, override_amount, account_used, status, judgment, pending_transaction_id, context
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_recent_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    days = max(1, min(int(days), 3650))
    limit = max(1, int(limit))
    conditions = [
        "user_id = ?",
        "merchant NOT LIKE '%System Balance Sync%'",
        "date >= datetime('now', ?)",
        "date <= datetime('now')",
    ]
    params: list = [user_id, f"-{days} days"]
    if status != "all":
        conditions.append("status = ?")
        params.append(status)
    params.append(limit)
    params.append(offset)

    # Determine which fields to return
    selected_fields = None
    if fields and fields.strip():
        requested = {f.strip().lower() for f in fields.split(",")}
        invalid = requested - _TX_FIELDS
        if invalid:
            return f" Unknown field(s): {', '.join(invalid)}. Available: {', '.join(sorted(_TX_FIELDS))}"
        selected_fields = requested

    c.execute(
        f"""SELECT id, transaction_id, pending_transaction_id, date, merchant,
        clean_merchant, category, amount, override_amount, account_used, status, judgment, is_locked
        FROM transactions
        WHERE {' AND '.join(conditions)}
        ORDER BY datetime(date) DESC, id DESC LIMIT ? OFFSET ?""",
        params,
    )
    rows = c.fetchall()
    if not rows:
        return (
            f" No transactions found in the last {days} days for status **{status}**."
        )

    # Build field name list for display
    all_field_names = [
        "id",
        "transaction_id",
        "pending_id",
        "date",
        "merchant",
        "clean_merchant",
        "category",
        "amount",
        "override_amount",
        "account_used",
        "status",
        "judgment",
        "is_locked",
    ]

    lines = [f" {len(rows)} txs (id|date|merchant|amt|cat|status|ctx)"]
    if len(rows) >= limit:
        lines.append(" [WARNING: Result limit reached. There may be more transactions. Use date/status filters to narrow down.]")
    for row in rows:
        row_dict = dict(zip(all_field_names, row))
        rid = row_dict["id"]
        _merchant = row_dict.get("merchant") or row_dict.get("clean_merchant") or ""
        _known = _resolve_known_merchant(_merchant)
        row_dict["known_merchant"] = bool(_known)
        row_dict["known_merchant_category"] = _known["category"] if _known else None
        row_dict["known_merchant_confidence"] = _known["confidence"] if _known else None
        row_dict["known_merchant_canonical"] = _known["canonical_name"] if _known else None
        row_dict["known_merchant_match"] = _known["matched_by"] if _known else None
        context_text = (
            format_transaction_context(get_transaction_context(transaction_row_id=rid, user_id=user_id))
            if selected_fields is not None and "context" in selected_fields
            else ""
        )
        effective_amount = _num(
            row_dict.get("override_amount")
            if row_dict.get("override_amount") is not None
            else row_dict.get("amount")
        )
        if selected_fields is None:
            registry = "KNOWN" if row_dict.get("known_merchant") else "UNKNOWN→SEARCH"
            line = _format_compact_tx_line(
                rid, row_dict.get("date"),
                row_dict.get("merchant") or row_dict.get("clean_merchant"),
                effective_amount, row_dict.get("category"), row_dict.get("status"),
                registry=registry,
            )
        else:
            # SELECT specific fields
            parts = []
            for fname in sorted(selected_fields):
                if fname == "context":
                    parts.append(f"context={context_text}")
                elif fname == "amount":
                    parts.append(f"amount=${abs(effective_amount):,.2f}")
                elif fname == "override_amount":
                    ov = row_dict.get("override_amount")
                    parts.append(
                        f"override_amount=${abs(_num(ov)):,.2f}"
                        if ov is not None
                        else "override_amount=NULL"
                    )
                else:
                    val = row_dict.get(fname)
                    if val is not None: parts.append(f"{fname}={val}")
            line = f"- #{rid} " + " | ".join(parts)
        lines.append(line)
    return "\n".join(lines)

def _get_transactions_by_lock_state(
    user_id: str,
    locked: bool,
    days: int | None = None,
    limit: int = 50,
    status: str = "all",
    fields: str | None = None,
    offset: int = 0,
) -> str:
    """Read-only transaction review filtered strictly by is_locked state.

    By default, searches the entire ledger. Pass ``days`` to constrain the
    query to a recent time window.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_get_transactions_by_lock_state: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if days is not None:
        days = max(1, min(int(days), 3650))
    limit = max(1, int(limit))
    conditions = [
        "user_id = ?",
        "merchant NOT LIKE '%System Balance Sync%'",
        "is_locked = ?",
    ]
    params: list = [user_id, 1 if locked else 0]
    if days is not None:
        conditions.insert(1, "date >= datetime('now', ?)")
        conditions.insert(2, "date <= datetime('now')")
        params = [user_id, f"-{days} days", 1 if locked else 0]
    if status != "all":
        conditions.append("status = ?")
        params.append(status)

    # GROUND TRUTH: count matching rows BEFORE limit/offset are applied so the
    # returned text can never disagree with the real number of matching rows.
    where_clause = " AND ".join(conditions)
    c.execute(f"SELECT COUNT(*) FROM transactions WHERE {where_clause}", params)
    total_count = int(c.fetchone()[0] or 0)

    params.append(limit)
    params.append(offset)

    selected_fields = None
    if fields and fields.strip():
        requested = {f.strip().lower() for f in fields.split(",")}
        invalid = requested - _TX_FIELDS
        if invalid:
            return (
                f" Unknown field(s): {', '.join(sorted(invalid))}. "
                f"Available: {', '.join(sorted(_TX_FIELDS))}"
            )
        selected_fields = requested

    c.execute(
        f"""SELECT id, transaction_id, pending_transaction_id, date, merchant,
        clean_merchant, category, amount, override_amount, account_used, status, judgment, is_locked
        FROM transactions
        WHERE {where_clause}
        ORDER BY datetime(date) DESC, id DESC LIMIT ? OFFSET ?""",
        params,
    )
    rows = c.fetchall()
    state_label = "locked" if locked else "unlocked"
    if not rows:
        scope = f"in the last {days} days" if days is not None else "in the entire ledger"
        return f" No {state_label} transactions found {scope} for status **{status}** (total matching: {total_count})."

    all_field_names = [
        "id",
        "transaction_id",
        "pending_id",
        "date",
        "merchant",
        "clean_merchant",
        "category",
        "amount",
        "override_amount",
        "account_used",
        "status",
        "judgment",
        "is_locked",
    ]

    remaining_after_page = max(0, total_count - offset - len(rows))
    lines = [
        f"{'' if locked else ''} TOTAL MATCHING: {total_count} {state_label} transaction(s) "
        f"— showing {len(rows)} (offset {offset}, {remaining_after_page} more not shown in this page)"
    ]
    for row in rows:
        row_dict = dict(zip(all_field_names, row))
        rid = row_dict["id"]
        context_text = (
            format_transaction_context(get_transaction_context(transaction_row_id=rid, user_id=user_id))
            if selected_fields is not None and "context" in selected_fields
            else ""
        )
        effective_amount = _num(
            row_dict.get("override_amount")
            if row_dict.get("override_amount") is not None
            else row_dict.get("amount")
        )
        if selected_fields is None:
            line = _format_compact_tx_line(
                rid, row_dict.get("date"),
                row_dict.get("merchant") or row_dict.get("clean_merchant"),
                effective_amount, row_dict.get("category"), row_dict.get("status"),
                locked=bool(row_dict.get("is_locked")),
            )
        else:
            parts = []
            for fname in sorted(selected_fields):
                if fname == "context":
                    parts.append(f"context={context_text}")
                elif fname == "amount":
                    parts.append(f"amount=${abs(effective_amount):,.2f}")
                elif fname == "override_amount":
                    ov = row_dict.get("override_amount")
                    parts.append(
                        f"override_amount=${abs(_num(ov)):,.2f}"
                        if ov is not None
                        else "override_amount=NULL"
                    )
                elif fname == "is_locked":
                    parts.append(f"is_locked={int(bool(row_dict.get('is_locked')))}")
                else:
                    val = row_dict.get(fname)
                    if val is not None: parts.append(f"{fname}={val}")
            line = f"- #{rid} " + " | ".join(parts)
        lines.append(line)
    return "\n".join(lines)

def get_unlocked_transactions(*, user_id: str,
    days: int | None = None,
    limit: int = 50,
    status: str = "all",
    fields: str | None = None,
) -> str:
    """List only editable transactions where is_locked = 0."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_unlocked_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    return _get_transactions_by_lock_state(user_id, False, days=days, limit=limit, status=status, fields=fields)

def get_locked_transactions(*, user_id: str,
    days: int | None = None,
    limit: int = 50,
    status: str = "all",
    fields: str | None = None,
) -> str:
    """List only immutable transactions where is_locked = 1."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_locked_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    return _get_transactions_by_lock_state(user_id, True, days=days, limit=limit, status=status, fields=fields)

def search_transactions(*, user_id: str,
    query: str,
    days: int = 3650,
    limit: int = 50,
    fields: str | None = None,
) -> str:
    """Search historical transactions. If `fields` is None, returns ALL fields (SELECT *).
    If fields is specified (comma-separated), returns only those fields.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "search_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    q = str(query or "").strip()
    if not q:
        return " query is required."
    days = max(1, min(int(days), 3650))
    limit = max(1, int(limit))
    like = f"%{q.lower().replace('%', '\\%').replace('_', '\\_')}%"

    selected_fields = None
    if fields and fields.strip():
        requested = {f.strip().lower() for f in fields.split(",")}
        invalid = requested - _TX_FIELDS
        if invalid:
            return f" Unknown field(s): {', '.join(invalid)}. Available: {', '.join(sorted(_TX_FIELDS))}"
        selected_fields = requested

    c.execute(
        """SELECT id, date, merchant, clean_merchant, category, amount, override_amount, account_used, status, transaction_id
        FROM transactions
        WHERE user_id = ? AND merchant NOT LIKE '%System Balance Sync%'
        AND date >= datetime('now', ?)
        AND date <= datetime('now')
        AND (LOWER(COALESCE(merchant,'')) LIKE ?
            OR LOWER(COALESCE(clean_merchant,'')) LIKE ?
            OR LOWER(COALESCE(category,'')) LIKE ?
            OR LOWER(COALESCE(transaction_id,'')) LIKE ?
            OR LOWER(COALESCE(account_used,'')) LIKE ?
            OR EXISTS (SELECT 1 FROM transaction_context tc WHERE tc.transaction_row_id = transactions.id
                AND (LOWER(COALESCE(tc.tag,'')) LIKE ? OR LOWER(COALESCE(tc.note,'')) LIKE ?)))
        ORDER BY datetime(date) DESC, id DESC LIMIT ? OFFSET ?""",
        (user_id, f"-{days} days", like, like, like, like, like, like, like, limit, 0),
    )
    rows = c.fetchall()
    if not rows:
        return f" No transactions matched **{q}** in the last {days} days."

    all_field_names = [
        "id",
        "date",
        "merchant",
        "clean_merchant",
        "category",
        "amount",
        "override_amount",
        "account_used",
        "status",
        "transaction_id",
    ]

    lines = [f" {len(rows)} results for '{q}' (id|date|merchant|amt|cat|status|ctx)"]
    for row in rows:
        row_dict = dict(zip(all_field_names, row))
        rid = row_dict["id"]
        context_text = (
            format_transaction_context(get_transaction_context(transaction_row_id=rid, user_id=user_id))
            if selected_fields is not None and "context" in selected_fields
            else ""
        )
        effective_amount = _num(
            row_dict.get("override_amount")
            if row_dict.get("override_amount") is not None
            else row_dict.get("amount")
        )
        if selected_fields is None:
            line = _format_compact_tx_line(
                rid, row_dict.get("date"),
                row_dict.get("merchant") or row_dict.get("clean_merchant"),
                effective_amount, row_dict.get("category"), row_dict.get("status"),
            )
        else:
            parts = []
            for fname in sorted(selected_fields):
                if fname == "context":
                    parts.append(f"context={context_text}")
                elif fname == "amount":
                    parts.append(f"amount=${abs(effective_amount):,.2f}")
                elif fname == "override_amount":
                    ov = row_dict.get("override_amount")
                    parts.append(
                        f"override_amount=${abs(_num(ov)):,.2f}"
                        if ov is not None
                        else "override_amount=NULL"
                    )
                else:
                    val = row_dict.get(fname)
                    if val is not None: parts.append(f"{fname}={val}")
            line = f"- #{rid} " + " | ".join(parts)
        lines.append(line)
    return "\n".join(lines)

def get_debt_overview(*, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_debt_overview: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        """SELECT name, current_balance, available_balance, credit_limit, subtype, type
        FROM plaid_accounts WHERE user_id = ? AND active=1 AND type IN ('credit','loan')
        ORDER BY type, name""",
        (user_id,)
    )
    rows = c.fetchall()
    if not rows:
        return " No synced credit-card or loan accounts found."
    credit = loan = 0.0
    lines = [" **Debt overview**"]
    for name, current, available, limit_amt, subtype, typ in rows:
        bal = _num(current)
        if typ == "loan":
            loan += bal
        else:
            credit += bal
        extra = f" / limit ${_num(limit_amt):,.2f}" if limit_amt is not None else ""
        lines.append(f"- {name or 'Unnamed'} ({subtype or typ}) — ${bal:,.2f}{extra}")
    lines.append(f"**Credit debt:** ${credit:,.2f}")
    lines.append(f"**Loan balances:** ${loan:,.2f}")
    return "\n".join(lines)


def sync_debts_from_plaid(*, user_id: str) -> int:
    """Sync active credit and loan accounts from plaid_accounts into user_debts."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("sync_debts_from_plaid: forced isolation violation — user_id is required")
    c.execute(
        """SELECT plaid_account_id, name, current_balance, subtype, type
        FROM plaid_accounts WHERE user_id = ? AND active = 1 AND type IN ('credit', 'loan')""",
        (user_id,)
    )
    rows = c.fetchall()
    synced_count = 0
    for paid, name, bal, subtype, typ in rows:
        balance = float(bal or 0.0)
        if balance <= 0:
            continue
        c.execute(
            "SELECT id, apr, min_payment FROM user_debts WHERE user_id = ? AND plaid_account_id = ?",
            (user_id, paid)
        )
        existing = c.fetchone()
        if existing:
            c.execute(
                "UPDATE user_debts SET balance = ?, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
                (balance, existing[0], user_id)
            )
        else:
            default_apr = 24.99 if typ == "credit" else 8.5
            default_min = max(25.0, round(balance * 0.025, 2))
            c.execute(
                """INSERT INTO user_debts (user_id, name, balance, apr, min_payment, plaid_account_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                (user_id, f"{name or 'Account'} ({subtype or typ})", balance, default_apr, default_min, paid)
            )
        synced_count += 1
    safe_commit()
    return synced_count


def add_or_update_debt(*, user_id: str, name: str, balance: float, apr: float = 0.0, min_payment: float = 0.0, debt_id: Optional[int] = None, plaid_account_id: Optional[str] = None) -> dict:
    """Create or update a tracked debt."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("add_or_update_debt: forced isolation violation — user_id is required")
    name = (name or "").strip()
    if not name:
        raise ValueError("Debt name cannot be empty")
    balance = float(balance)
    apr = max(0.0, float(apr))
    min_payment = max(0.0, float(min_payment))

    if min_payment <= 0.0 and balance > 0:
        min_payment = max(25.0, round(balance * 0.025, 2))

    if debt_id is not None:
        c.execute(
            """UPDATE user_debts
            SET name = ?, balance = ?, apr = ?, min_payment = ?, updated_at = datetime('now')
            WHERE id = ? AND user_id = ?""",
            (name, balance, apr, min_payment, debt_id, user_id)
        )
        safe_commit()
        return {"id": debt_id, "name": name, "balance": balance, "apr": apr, "min_payment": min_payment}

    c.execute(
        """INSERT INTO user_debts (user_id, name, balance, apr, min_payment, plaid_account_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (user_id, name, balance, apr, min_payment, plaid_account_id)
    )
    new_id = c.lastrowid
    safe_commit()
    return {"id": new_id, "name": name, "balance": balance, "apr": apr, "min_payment": min_payment}


def list_user_debts(*, user_id: str, auto_sync: bool = True) -> list:
    """Return all active debts for the user."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("list_user_debts: forced isolation violation — user_id is required")
    if auto_sync:
        sync_debts_from_plaid(user_id=user_id)

    c.execute(
        """SELECT id, name, balance, apr, min_payment, plaid_account_id, updated_at
        FROM user_debts WHERE user_id = ? AND balance > 0
        ORDER BY balance ASC""",
        (user_id,)
    )
    debts = []
    for r in c.fetchall():
        debts.append({
            "id": r[0],
            "name": r[1],
            "balance": float(r[2]),
            "apr": float(r[3]),
            "min_payment": float(r[4]),
            "plaid_account_id": r[5],
            "updated_at": r[6],
        })
    return debts


def delete_user_debt(debt_id: int, *, user_id: str) -> bool:
    """Delete a tracked debt."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("delete_user_debt: forced isolation violation — user_id is required")
    c.execute("DELETE FROM user_debts WHERE id = ? AND user_id = ?", (debt_id, user_id))
    affected = c.rowcount > 0
    safe_commit()
    return affected


def calculate_debt_payoff(
    strategy: str = "avalanche",
    extra_monthly: float = 0.0,
    *,
    user_id: str,
    debts_override: Optional[list] = None
) -> dict:
    """
    Deterministic debt snowball and avalanche simulation engine.
    Strategy: 'avalanche' (highest APR first) or 'snowball' (lowest balance first).
    Simulates monthly payment cascades including rollover of minimum payments.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("calculate_debt_payoff: forced isolation violation — user_id is required")

    strategy = strategy.lower().strip()
    if strategy not in ("avalanche", "snowball"):
        raise ValueError("Strategy must be either 'avalanche' or 'snowball'")

    debts = [dict(d) for d in debts_override] if debts_override is not None else list_user_debts(user_id=user_id)
    active_debts = [d for d in debts if d.get("balance", 0) > 0]

    if not active_debts:
        return {
            "status": "empty",
            "message": "No active debts found to simulate.",
            "total_debt": 0.0,
        }

    total_initial_balance = sum(d["balance"] for d in active_debts)
    total_min_payment = sum(d["min_payment"] for d in active_debts)

    def _simulate(strat: str, extra: float):
        import copy
        current_debts = copy.deepcopy(active_debts)
        months = 0
        total_interest = 0.0
        total_paid = 0.0
        milestones = []
        monthly_schedule = []

        now = datetime.now()

        while current_debts and months < 360:
            months += 1
            month_date = (now + timedelta(days=months * 30.44)).strftime("%Y-%m")
            interest_month = 0.0
            paid_month = 0.0

            # 1. Accrue monthly interest
            for d in current_debts:
                m_interest = round(d["balance"] * (d["apr"] / 100.0 / 12.0), 2)
                d["balance"] += m_interest
                interest_month += m_interest
                total_interest += m_interest

            # 2. Base payment pool: all minimum payments + extra
            # Minimum payments on debts that are already eliminated roll into the extra pool!
            unallocated_funds = extra

            # Pay minimums first
            paid_off_this_turn = []
            for d in current_debts:
                pmt = min(d["min_payment"], d["balance"])
                d["balance"] -= pmt
                paid_month += pmt
                total_paid += pmt
                if d["balance"] <= 0.001:
                    paid_off_this_turn.append(d)

            # Rollover minimum payments of paid off debts
            for d in paid_off_this_turn:
                milestones.append({
                    "name": d["name"],
                    "month": months,
                    "date": month_date,
                    "freed_payment": d["min_payment"],
                })
                current_debts.remove(d)

            # 3. Sort active debts according to strategy for extra payments
            if current_debts:
                if strat == "avalanche":
                    current_debts.sort(key=lambda x: (-x["apr"], x["balance"]))
                else:  # snowball
                    current_debts.sort(key=lambda x: (x["balance"], -x["apr"]))

                # Add freed min payments to extra roll
                freed_minimums = sum(m["freed_payment"] for m in milestones)
                snowball_pool = unallocated_funds + freed_minimums

                # Cascade extra funds to priority debt(s)
                idx = 0
                while snowball_pool > 0.001 and idx < len(current_debts):
                    target = current_debts[idx]
                    alloc = min(snowball_pool, target["balance"])
                    target["balance"] -= alloc
                    paid_month += alloc
                    total_paid += alloc
                    snowball_pool -= alloc

                    if target["balance"] <= 0.001:
                        milestones.append({
                            "name": target["name"],
                            "month": months,
                            "date": month_date,
                            "freed_payment": target["min_payment"],
                        })
                        current_debts.pop(idx)
                    else:
                        idx += 1

            remaining_balance = sum(d["balance"] for d in current_debts)
            monthly_schedule.append({
                "month": months,
                "date": month_date,
                "remaining_balance": round(remaining_balance, 2),
                "interest_paid": round(interest_month, 2),
                "principal_paid": round(paid_month - interest_month, 2),
            })

            if not current_debts:
                break

        return {
            "strategy": strat,
            "months": months,
            "total_interest": round(total_interest, 2),
            "total_paid": round(total_paid, 2),
            "milestones": milestones,
            "monthly_schedule": monthly_schedule,
            "debt_free_date": (now + timedelta(days=months * 30.44)).strftime("%B %Y"),
        }

    chosen_result = _simulate(strategy, extra_monthly)
    alt_strategy = "snowball" if strategy == "avalanche" else "avalanche"
    alt_result = _simulate(alt_strategy, extra_monthly)

    interest_diff = round(alt_result["total_interest"] - chosen_result["total_interest"], 2)
    months_diff = alt_result["months"] - chosen_result["months"]

    return {
        "status": "success",
        "strategy": strategy,
        "total_initial_balance": round(total_initial_balance, 2),
        "total_min_payment": round(total_min_payment, 2),
        "extra_monthly": round(extra_monthly, 2),
        "months_to_payoff": chosen_result["months"],
        "debt_free_date": chosen_result["debt_free_date"],
        "total_interest_paid": chosen_result["total_interest"],
        "total_paid": chosen_result["total_paid"],
        "milestones": chosen_result["milestones"],
        "schedule": chosen_result["monthly_schedule"],
        "comparison": {
            "alt_strategy": alt_strategy,
            "alt_months": alt_result["months"],
            "alt_debt_free_date": alt_result["debt_free_date"],
            "alt_interest": alt_result["total_interest"],
            "interest_saved_with_avalanche": abs(round(alt_result["total_interest"] - chosen_result["total_interest"], 2)) if strategy == "avalanche" else -round(chosen_result["total_interest"] - alt_result["total_interest"], 2),
            "months_saved_with_avalanche": alt_result["months"] - chosen_result["months"] if strategy == "avalanche" else -(chosen_result["months"] - alt_result["months"]),
        }
    }


def add_transaction_item(
    *,
    user_id: str,
    transaction_row_id: int,
    item_name: str,
    total_price: float,
    quantity: float = 1.0,
    unit_price: Optional[float] = None,
    category: Optional[str] = None,
    source: str = "manual",
    raw_receipt_id: Optional[str] = None,
) -> dict:
    """Attach an itemized line item to a transaction."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("add_transaction_item: forced isolation violation — user_id is required")
    item_name = (item_name or "").strip()
    if not item_name:
        raise ValueError("item_name cannot be empty")
    quantity = float(quantity or 1.0)
    total_price = float(total_price)
    if unit_price is None:
        unit_price = round(total_price / quantity, 2) if quantity > 0 else total_price

    c.execute(
        """INSERT INTO transaction_items
        (user_id, transaction_row_id, item_name, quantity, unit_price, total_price, category, source, raw_receipt_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (user_id, transaction_row_id, item_name, quantity, unit_price, total_price, category, source, raw_receipt_id)
    )
    new_id = c.lastrowid
    safe_commit()
    return {
        "id": new_id,
        "transaction_row_id": transaction_row_id,
        "item_name": item_name,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
        "category": category,
        "source": source,
    }


def get_transaction_items(transaction_row_id: int, *, user_id: str) -> list:
    """Retrieve all itemized line items attached to a transaction."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("get_transaction_items: forced isolation violation — user_id is required")
    c.execute(
        """SELECT id, transaction_row_id, item_name, quantity, unit_price, total_price, category, source, raw_receipt_id, created_at
        FROM transaction_items
        WHERE transaction_row_id = ? AND user_id = ?
        ORDER BY id ASC""",
        (transaction_row_id, user_id)
    )
    items = []
    for r in c.fetchall():
        items.append({
            "id": r[0],
            "transaction_row_id": r[1],
            "item_name": r[2],
            "quantity": float(r[3]),
            "unit_price": float(r[4]),
            "total_price": float(r[5]),
            "category": r[6],
            "source": r[7],
            "raw_receipt_id": r[8],
            "created_at": r[9],
        })
    return items


def delete_transaction_item(item_id: int, *, user_id: str) -> bool:
    """Delete an itemized line item."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("delete_transaction_item: forced isolation violation — user_id is required")
    c.execute("DELETE FROM transaction_items WHERE id = ? AND user_id = ?", (item_id, user_id))
    affected = c.rowcount > 0
    safe_commit()
    return affected


def reconcile_receipts_for_user(days: int = 14, *, user_id: str) -> dict:
    """
    Search connected Gmail for receipts matching recent transactions and itemize them.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("reconcile_receipts_for_user: forced isolation violation — user_id is required")

    from src.services.receipt_parser import find_matching_gmail_receipt

    # Query recent evaluated transactions without existing items
    c.execute(
        """SELECT t.id, t.date, t.merchant, t.clean_merchant, t.amount
        FROM transactions t
        WHERE t.user_id = ? AND t.amount > 0
          AND t.date >= datetime('now', ?)
          AND NOT EXISTS (
              SELECT 1 FROM transaction_items ti WHERE ti.transaction_row_id = t.id AND ti.user_id = ?
          )
        ORDER BY t.date DESC LIMIT 20""",
        (user_id, f"-{days} days", user_id)
    )
    unmatched_txs = c.fetchall()

    matched_count = 0
    total_items_added = 0
    reconciled_details = []

    for row in unmatched_txs:
        tx = {
            "id": row[0],
            "date": row[1],
            "merchant": row[2],
            "clean_merchant": row[3],
            "amount": float(row[4]),
        }

        match = find_matching_gmail_receipt(tx, user_id=user_id)
        if match:
            matched_count += 1
            msg_id = match.get("message_id")
            items = match.get("items", [])
            for item in items:
                add_transaction_item(
                    user_id=user_id,
                    transaction_row_id=tx["id"],
                    item_name=item["item_name"],
                    total_price=item["total_price"],
                    quantity=item.get("quantity", 1.0),
                    unit_price=item.get("unit_price"),
                    source="gmail_receipt",
                    raw_receipt_id=msg_id,
                )
                total_items_added += 1

            reconciled_details.append({
                "transaction_id": tx["id"],
                "merchant": tx["clean_merchant"] or tx["merchant"],
                "amount": tx["amount"],
                "subject": match.get("subject"),
                "item_count": len(items),
            })

    return {
        "status": "success",
        "scanned_transactions": len(unmatched_txs),
        "matched_transactions": matched_count,
        "items_extracted": total_items_added,
        "details": reconciled_details,
    }


def tag_transaction_tax(
    transaction_row_id: int,
    is_deductible: bool = True,
    tax_category: Optional[str] = None,
    *,
    user_id: str,
) -> dict:
    """Tag or untag a transaction as a tax deductible business or personal expense."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("tag_transaction_tax: forced isolation violation — user_id is required")
    try:
        transaction_row_id = int(transaction_row_id)
    except (TypeError, ValueError):
        raise ValueError("transaction_row_id must be an integer")

    c.execute(
        "SELECT id, merchant, clean_merchant, amount, date FROM transactions WHERE id = ? AND user_id = ?",
        (transaction_row_id, user_id)
    )
    row = c.fetchone()
    if not row:
        return {"status": "not_found", "message": f"Transaction #{transaction_row_id} not found."}

    tax_cat = (tax_category or "").strip() if is_deductible else None
    deductible_val = 1 if is_deductible else 0

    c.execute(
        "UPDATE transactions SET tax_deductible = ?, tax_category = ? WHERE id = ? AND user_id = ?",
        (deductible_val, tax_cat, transaction_row_id, user_id)
    )
    safe_commit()

    return {
        "status": "success",
        "transaction_id": transaction_row_id,
        "merchant": row[2] or row[1],
        "amount": float(row[3]),
        "date": row[4],
        "tax_deductible": bool(deductible_val),
        "tax_category": tax_cat,
    }


def get_tax_deductions_summary(year: Optional[int] = None, *, user_id: str) -> dict:
    """
    Summarize tax-deductible expenses grouped by Schedule C / tax category.
    Applies the standard 50% deduction rule for business meals.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("get_tax_deductions_summary: forced isolation violation — user_id is required")

    target_year = year or datetime.now().year
    year_str = f"{target_year}%"

    c.execute(
        """SELECT id, date, merchant, clean_merchant, amount, category, tax_category
        FROM transactions
        WHERE user_id = ? AND tax_deductible = 1 AND date LIKE ?
        ORDER BY datetime(date) ASC, id ASC""",
        (user_id, year_str)
    )
    rows = c.fetchall()

    by_category = defaultdict(lambda: {"gross": 0.0, "deductible": 0.0, "count": 0})
    total_gross = 0.0
    total_deductible = 0.0
    transactions_list = []

    for r in rows:
        rid, dt, merch, cmerch, amt, gen_cat, tax_cat = r
        amount = float(amt or 0.0)
        display_cat = tax_cat or gen_cat or "Uncategorized Deductible"
        
        # Apply 50% meal deduction rule if categorized under meals/dining
        is_meal = "meal" in display_cat.lower() or "dining" in display_cat.lower()
        deductible_amount = round(amount * 0.5, 2) if is_meal else amount

        by_category[display_cat]["gross"] += amount
        by_category[display_cat]["deductible"] += deductible_amount
        by_category[display_cat]["count"] += 1

        total_gross += amount
        total_deductible += deductible_amount

        transactions_list.append({
            "id": rid,
            "date": dt,
            "merchant": cmerch or merch,
            "gross_amount": amount,
            "deductible_amount": deductible_amount,
            "tax_category": display_cat,
        })

    categories_summary = [
        {
            "category": cat,
            "count": data["count"],
            "gross": round(data["gross"], 2),
            "deductible": round(data["deductible"], 2),
        }
        for cat, data in sorted(by_category.items(), key=lambda x: -x[1]["deductible"])
    ]

    return {
        "status": "success",
        "year": target_year,
        "total_transactions": len(rows),
        "total_gross_spent": round(total_gross, 2),
        "total_deductible": round(total_deductible, 2),
        "categories": categories_summary,
        "transactions": transactions_list,
    }


def export_tax_csv(year: Optional[int] = None, *, user_id: str) -> str:
    """Generate a clean CSV report of tax deductions."""
    summary = get_tax_deductions_summary(year, user_id=user_id)
    txs = summary.get("transactions", [])

    import io
    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Transaction ID", "Date", "Merchant", "Tax Category", "Gross Amount", "Deductible Amount"])
    for tx in txs:
        writer.writerow([
            tx["id"],
            tx["date"],
            tx["merchant"],
            tx["tax_category"],
            f"{tx['gross_amount']:.2f}",
            f"{tx['deductible_amount']:.2f}",
        ])
    return output.getvalue()


def get_net_worth_history(days: int = 365, limit: int = 100, offset: int = 0, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_net_worth_history: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    days = max(1, min(int(days), 3650))
    limit = max(1, min(int(limit), 500))
    c.execute(
        """SELECT captured_at, total_liquid, total_credit_debt, net_cash, net_worth
        FROM financial_snapshots
        WHERE user_id = ? AND captured_at >= datetime('now', ?)
        ORDER BY datetime(captured_at) DESC LIMIT ? OFFSET ?""",
        (user_id, f"-{days} days", limit, offset),
    )
    rows = c.fetchall()
    if not rows:
        return f" No financial snapshots in the last {days} days."
    lines = [f" **Financial history — last {days} days**"]
    for captured, liquid, debt, net_cash, net_worth in rows:
        lines.append(
            f"- `{str(captured)[:19]}` — liquid ${_num(liquid):,.2f} | "
            f"debt ${_num(debt):,.2f} | net cash ${_num(net_cash):,.2f} | "
            f"net worth ${_num(net_worth):,.2f}"
        )
    return "\n".join(lines)

def get_cash_flow_summary(*, user_id: str, days: int = 30) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_cash_flow_summary: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    days = max(1, min(int(days), 3650))
    c.execute(
        """SELECT SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END),
        SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END),
        COUNT(*)
        FROM transactions
        WHERE user_id = ? AND status='Evaluated'
        AND merchant NOT LIKE '%System Balance Sync%'
        AND date >= datetime('now', ?)""",
        (user_id, f"-{days} days",),
    )
    inflow, outflow, count = c.fetchone() or (0, 0, 0)
    inflow = inflow or 0
    outflow = outflow or 0

    c.execute(
        """SELECT tc.tag, SUM(ABS(t.amount)), COUNT(DISTINCT t.id)
        FROM transactions t JOIN transaction_context tc ON tc.transaction_row_id=t.id
        WHERE t.user_id = ? AND t.status='Evaluated' AND t.amount > 0
        AND t.merchant NOT LIKE '%System Balance Sync%'
        AND t.date >= datetime('now', ?)
        GROUP BY tc.tag ORDER BY SUM(ABS(t.amount)) DESC""",
        (user_id, f"-{days} days",),
    )
    context_rows = c.fetchall()
    context_text = ""
    if context_rows:
        context_text = "\n- Behavioral context (tag totals may overlap): " + "; ".join(
            f"{tag}: ${_num(amt):,.2f} ({count_} tx)"
            for tag, amt, count_ in context_rows
        )

    return (
        f" **Settled cash flow — last {days} days**\n"
        f"- Money in: ${_num(inflow):,.2f}\n"
        f"- Money out: ${_num(outflow):,.2f}\n"
        f"- Net settled flow: ${_num(inflow)-_num(outflow):,.2f}\n"
        f"- Evaluated transactions: {int(count or 0)}"
        f"{context_text}\n"
        f"{get_upcoming_cash_flow(days=days, user_id=user_id)}"
    )

def get_financial_dashboard(*, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_financial_dashboard: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    position = get_current_financial_position(user_id=user_id)
    weekly = get_weekly_spending(user_id=user_id)
    weekly_limit, impulse = get_budget_settings(user_id=user_id)
    weekly_limit = weekly_limit or 0
    impulse = impulse or 0
    c.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND status='Pending' AND merchant NOT LIKE '%System Balance Sync%'",
        (user_id,)
    )
    pending = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM cash_inflows WHERE user_id = ? AND status='Pending'", (user_id,))
    expected_income = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM planned_transactions WHERE user_id = ? AND status='Expected'", (user_id,))
    planned = int(c.fetchone()[0] or 0)
    return (
        " **Delilah financial dashboard**\n"
        f"- Liquid cash: ${_num(position.get('total_liquid')):,.2f}\n"
        f"- Net cash: {position.get('net_cash')}\n"
        f"- Weekly settled spend: ${weekly:,.2f} / ${weekly_limit:,.2f}\n"
        f"- Impulse threshold: ${impulse:,.2f}\n"
        f"- Pending bank transactions: {pending}\n"
        f"- Expected income records: {expected_income}\n"
        f"- Planned future transactions: {planned}\n"
        f"- {get_upcoming_cash_flow(days=30, user_id=user_id)}"
    )

# ============================================================
# Expected Income + Planned Transactions
# ============================================================
def add_expected_income(source: str,
    amount: float,
    expected_date: str | None = None,
    account: str | None = None,
    income_type: str = "Paycheck",
    recurrence: str | None = None,
    gross_amount: float | None = None,
    deductions: float | None = None,
    notes: str | None = None, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "add_expected_income: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        amount = float(amount)
        gross_amount = float(gross_amount) if gross_amount is not None else None
        deductions = float(deductions) if deductions is not None else None
    except (TypeError, ValueError):
        return " Expected income amounts must be numeric."
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        """INSERT INTO cash_inflows
        (user_id, type, source, gross_amount, deductions, net_expected, status, notes,
        created_at, expected_date, account, recurrence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            income_type or "Income",
            source,
            gross_amount,
            deductions,
            amount,
            "Pending",
            notes,
            now,
            expected_date,
            account,
            recurrence,
        ),
    )
    row_id = c.lastrowid
    safe_commit()
    when = f" around {expected_date}" if expected_date else ""
    return f" Logged expected income #{row_id}: **{source}** ${amount:,.2f}{when}."

def get_expected_income(*, user_id: str, status: str = "Pending", days: int = 60) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_expected_income: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        days = max(1, min(int(days), 3650))
    except (TypeError, ValueError):
        days = 60
    if status == "all":
        where = "(expected_date IS NULL OR expected_date <= date('now', '+' || ? || ' days'))"
        params: list = [days]
    else:
        where = "status = ? AND (expected_date IS NULL OR expected_date <= date('now', '+' || ? || ' days'))"
        params = [status or "Pending", days]
    c.execute(
        f"""SELECT id, type, source, net_expected, expected_date, account,
        recurrence, status, matched_transaction_row_id
        FROM cash_inflows WHERE user_id = ? AND {where}
        ORDER BY COALESCE(expected_date, '9999-12-31'), id""",
        [user_id] + params,
    )
    rows = c.fetchall()
    if not rows:
        return (
            f" No expected income with status **{status}** in the next {days} days."
        )
    lines = [f" **Expected income — {status}, next {days} days**"]
    for row in rows:
        (
            rid,
            typ,
            source,
            net,
            expected_date,
            account,
            recurrence,
            row_status,
            matched_id,
        ) = row
        match = f" → tx#{matched_id}" if matched_id else ""
        lines.append(
            f"- #{rid} {typ}: {source} | ${float(net or 0):,.2f} | "
            f"{expected_date or 'date unknown'} | {account or 'account unknown'} | "
            f"{row_status}{match}{' | ' + recurrence if recurrence else ''}"
        )
    return "\n".join(lines)

def update_expected_income(income_id: int,
    source: str | None = None,
    amount: float | None = None,
    expected_date: str | None = None,
    account: str | None = None,
    recurrence: str | None = None,
    status: str | None = None,
    notes: str | None = None, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "update_expected_income: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        income_id = int(income_id)
    except (TypeError, ValueError):
        return " income_id must be an integer."
    fields, params = [], []
    mapping = {
        "source": source,
        "net_expected": amount,
        "expected_date": expected_date,
        "account": account,
        "recurrence": recurrence,
        "status": status,
        "notes": notes,
    }
    for field, value in mapping.items():
        if value is None:
            continue
        if field == "net_expected":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return " amount must be numeric."
        if field == "status" and value not in {"Pending", "Received", "Cancelled"}:
            return " Invalid expected-income status."
        fields.append(f"{field} = ?")
        params.append(value)
    if not fields:
        return " No changes supplied."
    params.extend([user_id, income_id])
    c.execute(f"UPDATE cash_inflows SET {', '.join(fields)} WHERE user_id = ? AND id = ?", params)
    if c.rowcount == 0:
        return f" No expected income record #{income_id} exists."
    safe_commit()
    return f" Updated expected income #{income_id}."

def cancel_expected_income( income_id: int, user_id: str,) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "cancel_expected_income: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        income_id = int(income_id)
    except (TypeError, ValueError):
        return " income_id must be an integer."
    c.execute(
        "UPDATE cash_inflows SET status='Cancelled' WHERE user_id = ? AND id=? AND status='Pending'",
        (user_id, income_id),
    )
    if c.rowcount == 0:
        return f" Expected income #{income_id} was not found or is no longer pending."
    safe_commit()
    return f" Cancelled expected income #{income_id}."

def add_planned_transaction(direction: str,
    merchant: str | None = None,
    description: str | None = None,
    expected_amount: float | None = None,
    expected_date: str | None = None,
    account_used: str | None = None,
    notes: str | None = None,
    source: str = "user",
    context_tag: str | None = None,
    context_note: str | None = None, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "add_planned_transaction: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    direction = str(direction or "").lower().strip()
    if direction not in {"expense", "income", "transfer"}:
        return " direction must be expense, income, or transfer."
    if expected_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(expected_date)):
        return " expected_date must be in YYYY-MM-DD format (e.g., 2026-08-25)."
    try:
        amount = None if expected_amount is None else abs(float(expected_amount))
    except (TypeError, ValueError):
        return " expected_amount must be numeric."
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Prevent duplicate live planned events. A duplicate is defined by the
    # stable financial identity of the event, not its description/notes:
    # same user + direction + merchant + expected amount + expected date.
    # Only live Expected rows participate; historical Cancelled/Matched rows
    # must remain available for audit/reconciliation.
    if merchant and amount is not None and expected_date:
        c.execute(
            """SELECT id
            FROM planned_transactions
            WHERE user_id = ?
              AND status = 'Expected'
              AND direction = ?
              AND LOWER(TRIM(COALESCE(merchant, ''))) = LOWER(TRIM(?))
              AND expected_amount = ?
              AND expected_date = ?
            ORDER BY id
            LIMIT 1""",
            (user_id, direction, merchant, amount, expected_date),
        )
        existing = c.fetchone()

        if existing:
            rid = existing[0]
            return (
                f" Planned {direction} already exists as #{rid}: "
                f"**{merchant or description or 'unlabeled event'}** "
                f"{'$' + format(amount, ',.2f')}"
                f"{' around ' + expected_date if expected_date else ''}. "
                f"No duplicate was created."
            )

    c.execute(
        """INSERT INTO planned_transactions
        (user_id, direction, merchant, description, expected_amount, expected_date,
        account_used, source, notes, context_tag, context_note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            direction,
            merchant,
            description,
            amount,
            expected_date,
            account_used,
            source or "user",
            notes,
            context_tag,
            context_note,
            now,
        ),
    )
    rid = c.lastrowid
    safe_commit()
    return (
        f" Logged planned {direction} #{rid}: "
        f"**{merchant or description or 'unlabeled event'}** "
        f"{'$' + format(amount, ',.2f') if amount is not None else 'amount unknown'}"
        f"{' around ' + expected_date if expected_date else ''}."
    )

def get_planned_transactions(*, user_id: str, status: str = "Expected", days: int = 30) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_planned_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        days = max(1, min(int(days), 3650))
    except (TypeError, ValueError):
        days = 30
    # Planned-transaction statuses are intentionally distinct from the
    # statuses used by transactions/cash_inflows. Do not silently accept
    # LLM-invented values such as "active", "pending", or "scheduled".
    valid_statuses = {"Expected", "Cancelled", "all"}
    status = status or "Expected"
    if status not in valid_statuses:
        return (
            f" Invalid status **{status}** for planned transactions. "
            f"Valid statuses are: **Expected**, **Cancelled**, or **all**. "
            f"For a combined upcoming cash-flow view, use **get_upcoming_cash_flow**."
        )

    if status == "all":
        where = "(expected_date IS NULL OR expected_date <= date('now', '+' || ? || ' days'))"
        params: list = [days]
    else:
        where = "status=? AND (expected_date IS NULL OR expected_date <= date('now', '+' || ? || ' days'))"
        params = [status, days]
    c.execute(
        f"""SELECT id, direction, merchant, description, expected_amount, actual_amount,
        expected_date, posted_date, account_used, status, matched_transaction_row_id, context_tag, context_note
        FROM planned_transactions WHERE user_id = ? AND {where}
        ORDER BY COALESCE(expected_date, '9999-12-31'), id""",
        [user_id] + params,
    )
    rows = c.fetchall()
    if not rows:
        return f" No planned transactions with status **{status}** in the next {days} days."
    lines = [f" **Planned transactions — {status}, next {days} days**"]
    for row in rows:
        (
            rid,
            direction,
            merchant,
            desc,
            exp_amt,
            actual_amt,
            exp_date,
            posted_date,
            account,
            row_status,
            matched_id,
            context_tag,
            context_note,
        ) = row
        amount = actual_amt if actual_amt is not None else exp_amt
        match = f" → tx#{matched_id}" if matched_id else ""
        line = f"- #{rid} {direction}: {merchant or desc or 'unlabeled'} | "
        line += f"${float(amount):,.2f}" if amount is not None else "amount unknown"
        if exp_date or posted_date or account or match:
            line += f" | {exp_date or posted_date or 'date unknown'} | {account or 'account unknown'} | {row_status}{match}"
        line += f" | Context: {context_tag or 'none'}" + (
            f" — {context_note}" if context_note else ""
        )
        lines.append(line)
    return "\n".join(lines)

def update_planned_transaction(planned_id: int,
    merchant: str | None = None,
    description: str | None = None,
    expected_amount: float | None = None,
    expected_date: str | None = None,
    account_used: str | None = None,
    notes: str | None = None,
    status: str | None = None,
    context_tag: str | None = None,
    context_note: str | None = None, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "update_planned_transaction: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        planned_id = int(planned_id)
    except (TypeError, ValueError):
        return " planned_id must be an integer."
    mapping = {
        "merchant": merchant,
        "description": description,
        "expected_amount": expected_amount,
        "expected_date": expected_date,
        "account_used": account_used,
        "notes": notes,
        "status": status,
        "context_tag": context_tag,
        "context_note": context_note,
    }
    fields, params = [], []
    for field, value in mapping.items():
        if value is None:
            continue
        if field == "expected_amount":
            try:
                value = abs(float(value))
            except (TypeError, ValueError):
                return " expected_amount must be numeric."
        if field == "status" and value not in {
            "Expected",
            "Matched",
            "Posted",
            "Cancelled",
            "Expired",
        }:
            return " Invalid planned transaction status."
        fields.append(f"{field}=?")
        params.append(value)
    if not fields:
        return " No changes supplied."
    params.extend([user_id, planned_id])
    c.execute(f"UPDATE planned_transactions SET {', '.join(fields)} WHERE user_id = ? AND id=?", params)
    if c.rowcount == 0:
        return f" Planned transaction #{planned_id} does not exist."
    safe_commit()
    return f" Updated planned transaction #{planned_id}."

def cancel_planned_transaction( planned_id: int, user_id: str,) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "cancel_planned_transaction: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        planned_id = int(planned_id)
    except (TypeError, ValueError):
        return " planned_id must be an integer."
    c.execute(
        "UPDATE planned_transactions SET status='Cancelled', completed_at=? WHERE user_id = ? AND id=? AND status='Expected'",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, planned_id),
    )
    if c.rowcount == 0:
        return f" Planned transaction #{planned_id} was not found or is no longer expected."
    safe_commit()
    return f" Cancelled planned transaction #{planned_id}."

def _date_distance_days(expected_date, actual_date):
    if not expected_date or not actual_date:
        return None
    try:
        e = datetime.strptime(str(expected_date)[:10], "%Y-%m-%d").date()
        a = datetime.strptime(str(actual_date)[:10], "%Y-%m-%d").date()
        return abs((a - e).days)
    except Exception:
        return None

def reconcile_expected_and_planned_transactions(auto_apply: bool = True, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "reconcile_expected_and_planned_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        "SELECT id, type, source, net_expected, expected_date, account, status FROM cash_inflows WHERE user_id = ? AND status='Pending'",
        (user_id,)
    )
    expected_income = c.fetchall()
    c.execute("""SELECT id, direction, merchant, description, expected_amount,
        expected_date, account_used, status, context_tag, context_note
        FROM planned_transactions WHERE user_id = ? AND status='Expected'""", (user_id,))
    planned = c.fetchall()
    c.execute("""SELECT id, merchant, clean_merchant, amount, account_used, date, status
        FROM transactions WHERE user_id = ? AND status IN ('Pending', 'Evaluated')
        AND merchant NOT LIKE '%System Balance Sync%' ORDER BY id DESC""", (user_id,))
    txs = c.fetchall()
    used_tx_ids = set()
    matched = []

    for row in expected_income:
        rid, typ, source, expected_amt, expected_date, account, status = row
        best = None
        for tx in txs:
            if tx[0] in used_tx_ids or float(tx[3] or 0) >= 0:
                continue
            amount_diff = abs(abs(float(tx[3])) - float(expected_amt or 0))
            score = 0
            if expected_amt is not None and amount_diff <= max(
                1.0, abs(float(expected_amt)) * 0.02
            ):
                score += 45
            elif expected_amt is not None and amount_diff <= max(
                5.0, abs(float(expected_amt)) * 0.05
            ):
                score += 20
            source_text = " ".join(str(x or "").lower() for x in (source, tx[1], tx[2]))
            source_tokens = [
                t
                for t in re.findall(r"[a-z0-9]+", str(source or "").lower())
                if len(t) >= 3
            ]
            if source_tokens and any(t in source_text for t in source_tokens):
                score += 30
            d = _date_distance_days(expected_date, tx[5])
            if d is not None and d <= 3:
                score += 20
            if score >= 55 and (best is None or score > best[0]):
                best = (score, tx)
        if best:
            score, tx = best
            tx_id = tx[0]
            if auto_apply:
                c.execute(
                    "UPDATE cash_inflows SET status='Received', matched_transaction_row_id=?, matched_at=?, confidence=? WHERE user_id = ? AND id=?",
                    (
                        tx_id,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        score / 100.0,
                        user_id,
                        rid,
                    ),
                )
                used_tx_ids.add(tx_id)
                matched.append(
                    f"- Expected income #{rid} **{source}** → tx#{tx_id} ${abs(float(tx[3])):,.2f} (score {score})"
                )

    for row in planned:
        (
            rid,
            direction,
            merchant,
            description,
            expected_amt,
            expected_date,
            account,
            status,
            context_tag,
            context_note,
        ) = row
        expected_sign = -1 if direction == "income" else 1
        best = None
        for tx in txs:
            if tx[0] in used_tx_ids:
                continue
            amt = float(tx[3] or 0)
            tx_direction = "income" if amt < 0 else "expense"
            if direction == "transfer":
                direction_score = 10
            else:
                direction_score = 30 if direction == tx_direction else 0
            amount_score = 0
            if expected_amt is not None:
                diff = abs(abs(amt) - float(expected_amt))
                if diff <= max(1.0, abs(float(expected_amt)) * 0.02):
                    amount_score = 35
                elif diff <= max(5.0, abs(float(expected_amt)) * 0.05):
                    amount_score = 15
            # Merchant evidence must come from the merchant identity itself.
            # Do NOT use the planned description here: descriptions commonly
            # contain generic words such as "order", "payment", "power bank",
            # etc. Those words are not reliable merchant identity evidence.
            planned_merchant = str(merchant or "").strip().lower()
            tx_merchants = [
                str(tx[1] or "").strip().lower(),
                str(tx[2] or "").strip().lower(),
            ]

            merchant_score = 0
            if planned_merchant:
                for tx_merchant in tx_merchants:
                    if not tx_merchant:
                        continue
                    if (
                        planned_merchant == tx_merchant
                        or planned_merchant in tx_merchant
                        or tx_merchant in planned_merchant
                    ):
                        merchant_score = 25
                        break
            date_score = 0
            d = _date_distance_days(expected_date, tx[5])
            if d is not None:
                date_score = 15 if d <= 2 else (8 if d <= 5 else 0)
            account_score = (
                10 if account and tx[4] and account.lower() in tx[4].lower() else 0
            )
            score = (
                direction_score
                + amount_score
                + merchant_score
                + date_score
                + account_score
            )

            # SAFETY: amount + direction alone are NOT sufficient evidence
            # to match a planned transaction. Without this guard, two
            # unrelated transactions with similar amounts can reach the
            # 60-point threshold (30 direction + 35 amount = 65) even when
            # the merchant and date are completely unrelated.
            #
            # Require either:
            #   1. merchant evidence, OR
            #   2. a reasonably close date AND matching account.
            #
            # This prevents stale/unrelated transactions such as an old
            # $84.49 Candy Love purchase from being matched to a new $85
            # Andrew Chacon planned payment.
            has_merchant_evidence = merchant_score >= 10
            has_temporal_account_evidence = (
                date_score >= 8 and account_score >= 10
            )

            if (
                score >= 60
                and (has_merchant_evidence or has_temporal_account_evidence)
                and (best is None or score > best[0])
            ):
                best = (score, tx)
        if best:
            score, tx = best
            tx_id = tx[0]
            if auto_apply:
                c.execute(
                    """UPDATE planned_transactions SET actual_amount=?, posted_date=?, status='Matched',
                    matched_transaction_row_id=?, matched_at=?, completed_at=? WHERE id=?""",
                    (
                        abs(float(tx[3] or 0)),
                        str(tx[5])[:32],
                        tx_id,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        rid,
                    ),
                )
                if context_tag:
                    c.execute(
                        "INSERT INTO transaction_context (user_id, transaction_row_id, tag, note, created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            user_id,
                            tx_id,
                            context_tag,
                            context_note,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                used_tx_ids.add(tx_id)
                matched.append(
                    f"- Planned #{rid} **{merchant or description or 'event'}** → tx#{tx_id} ${abs(float(tx[3])):,.2f} (score {score})"
                )

    if auto_apply:
        safe_commit()

    if not matched:
        return " Plaid reconciliation: no high-confidence expected/planned matches found."
    mode = "applied" if auto_apply else "previewed"
    return (
        f" Plaid reconciliation **{mode} {len(matched)} match(es)**:\n"
        + "\n".join(matched)
    )

def get_upcoming_cash_flow(*, days: int = 30, user_id: str,) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_upcoming_cash_flow: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    try:
        days = max(1, min(int(days), 3650))
    except (TypeError, ValueError):
        days = 30
    c.execute(
        """SELECT direction, COALESCE(SUM(expected_amount), 0) FROM planned_transactions
        WHERE user_id = ? AND status='Expected' AND expected_date IS NOT NULL
        AND expected_date <= date('now', '+' || ? || ' days') GROUP BY direction""",
        (user_id, days),
    )
    planned_map = dict(c.fetchall())
    c.execute(
        """SELECT COALESCE(SUM(net_expected), 0) FROM cash_inflows
        WHERE user_id = ? AND status='Pending' AND expected_date IS NOT NULL
        AND expected_date <= date('now', '+' || ? || ' days')""",
        (user_id, days),
    )
    expected_income = float(c.fetchone()[0] or 0)
    expense = float(planned_map.get("expense", 0) or 0)
    transfer = float(planned_map.get("transfer", 0) or 0)
    income = expected_income + float(planned_map.get("income", 0) or 0)

    c.execute(
        """SELECT context_tag, SUM(expected_amount), COUNT(*) FROM planned_transactions
        WHERE user_id = ? AND status='Expected' AND direction='expense' AND expected_date IS NOT NULL
        AND expected_date <= date('now', '+' || ? || ' days')
        AND context_tag IS NOT NULL AND trim(context_tag) <> ''
        GROUP BY context_tag ORDER BY SUM(expected_amount) DESC""",
        (user_id, days),
    )
    planned_context = c.fetchall()
    context_text = ""
    if planned_context:
        context_text = "\n- Planned-expense context: " + "; ".join(
            f"{tag}: ${_num(amt):,.2f} ({n} event{'s' if n != 1 else ''})"
            for tag, amt, n in planned_context
        )

    return (
        f" **Upcoming cash flow — next {days} days**\n"
        f"- Expected income: ${income:,.2f}\n"
        f"- Planned expenses: ${expense:,.2f}\n"
        f"- Planned transfers: ${transfer:,.2f}\n"
        f"- Expected net change: ${(income - expense):,.2f}"
        f"{context_text}\n"
        "Future events are not treated as settled transactions."
    )


def simulate_cash_flow_scenario(
    days: int = 60,
    scenario_events: Optional[List[Dict[str, Any]]] = None,
    *,
    user_id: str,
    starting_balance_override: Optional[float] = None,
) -> dict:
    """
    Deterministic cash flow runway and 'What-If?' scenario simulation engine.
    Projects daily balances over `days` into the future based on:
      - Starting liquid cash
      - Daily discretionary burn rate (trailing 30 days)
      - Planned income & expense transactions
      - Optional hypothetical scenario events
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("simulate_cash_flow_scenario: forced isolation violation — user_id is required")

    days = max(7, min(int(days), 365))
    events = [dict(e) for e in scenario_events] if scenario_events else []

    # 1. Determine starting liquid depository cash
    if starting_balance_override is not None:
        start_liquid = float(starting_balance_override)
    else:
        c.execute(
            "SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'depository' AND active = 1",
            (user_id,)
        )
        row = c.fetchone()
        depository_sum = float(row[0]) if row and row[0] is not None else None
        if depository_sum is not None and depository_sum > 0:
            start_liquid = depository_sum
        else:
            pos = get_current_financial_position(user_id=user_id)
            start_liquid = float(pos.get("total_liquid") or 0.0)

    # 2. Historical daily discretionary burn rate (trailing 30 days)
    c.execute(
        """SELECT SUM(amount) FROM transactions
        WHERE user_id = ? AND amount > 0 AND date >= datetime('now', '-30 days')
          AND merchant NOT LIKE '%System Balance Sync%'
          AND COALESCE(category, '') NOT LIKE 'Credit Card Bill Payment%'
          AND COALESCE(category, '') NOT LIKE 'P2P Transfer%'""",
        (user_id,)
    )
    spend_row = c.fetchone()
    monthly_discretionary = float(spend_row[0] or 0.0) if spend_row else 0.0
    daily_burn = round(monthly_discretionary / 30.0, 2)

    # 3. Fetch planned transactions
    c.execute(
        """SELECT expected_date, direction, expected_amount, description
        FROM planned_transactions
        WHERE user_id = ? AND status = 'Expected' AND expected_date IS NOT NULL
          AND expected_date <= date('now', '+' || ? || ' days')""",
        (user_id, days)
    )
    planned_rows = c.fetchall()
    planned_by_date = defaultdict(list)
    for p_date, p_dir, p_amt, p_desc in planned_rows:
        planned_by_date[p_date[:10]].append({
            "direction": p_dir,
            "amount": float(p_amt or 0.0),
            "description": p_desc,
        })

    # 4. Fetch expected income
    c.execute(
        """SELECT expected_date, net_expected, source
        FROM cash_inflows
        WHERE user_id = ? AND status = 'Pending' AND expected_date IS NOT NULL
          AND expected_date <= date('now', '+' || ? || ' days')""",
        (user_id, days)
    )
    inflow_rows = c.fetchall()
    inflows_by_date = defaultdict(list)
    for i_date, i_amt, i_src in inflow_rows:
        inflows_by_date[i_date[:10]].append({
            "amount": float(i_amt or 0.0),
            "source": i_src,
        })

    def _run_timeline(apply_events: bool):
        now = datetime.now()
        bal = start_liquid
        min_bal = bal
        min_date = now.strftime("%Y-%m-%d")
        runway = None
        timeline = []

        for d in range(1, days + 1):
            cur_date = (now + timedelta(days=d)).strftime("%Y-%m-%d")
            day_income = 0.0
            day_expenses = daily_burn

            # Planned inflows
            for inf in inflows_by_date.get(cur_date, []):
                day_income += inf["amount"]

            # Planned transactions
            for pl in planned_by_date.get(cur_date, []):
                if pl["direction"] == "income":
                    day_income += pl["amount"]
                elif pl["direction"] == "expense":
                    day_expenses += pl["amount"]

            # Scenario events
            if apply_events:
                for ev in events:
                    ev_amt = float(ev.get("amount", 0.0))
                    target_date = ev.get("date")
                    offset = ev.get("offset_days")
                    recurring = ev.get("recurring_monthly", False)

                    matches = False
                    if target_date and target_date == cur_date:
                        matches = True
                    elif offset is not None and offset == d:
                        matches = True
                    elif recurring and offset is not None and d >= offset and (d - offset) % 30 == 0:
                        matches = True

                    if matches:
                        if ev_amt > 0:
                            day_income += ev_amt
                        else:
                            day_expenses += abs(ev_amt)

            bal = bal + day_income - day_expenses

            if bal < min_bal:
                min_bal = bal
                min_date = cur_date

            if bal < 0 and runway is None:
                runway = d

            # Keep sample intervals (every 7 days or final day)
            if d % 7 == 0 or d == days or (apply_events and any(ev.get("offset_days") == d for ev in events)):
                timeline.append({
                    "day": d,
                    "date": cur_date,
                    "balance": round(bal, 2),
                })

        return {
            "final_balance": round(bal, 2),
            "min_balance": round(min_bal, 2),
            "min_date": min_date,
            "runway_days": runway,
            "timeline": timeline,
        }

    baseline = _run_timeline(apply_events=False)
    scenario_res = _run_timeline(apply_events=True) if events else baseline

    return {
        "status": "success",
        "simulation_days": days,
        "starting_liquid": round(start_liquid, 2),
        "daily_burn_rate": daily_burn,
        "events_applied": events,
        "scenario": {
            "final_balance": scenario_res["final_balance"],
            "min_balance": scenario_res["min_balance"],
            "min_date": scenario_res["min_date"],
            "runway_days": scenario_res["runway_days"],
            "timeline": scenario_res["timeline"],
        },
        "baseline": {
            "final_balance": baseline["final_balance"],
            "min_balance": baseline["min_balance"],
            "min_date": baseline["min_date"],
            "runway_days": baseline["runway_days"],
        },
        "delta": {
            "final_balance_impact": round(scenario_res["final_balance"] - baseline["final_balance"], 2),
            "min_balance_impact": round(scenario_res["min_balance"] - baseline["min_balance"], 2),
        }
    }

# ============================================================
# Spending Queries
# ============================================================
def query_spending(merchant: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_pending: bool = False, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "query_spending: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    def run_query(conditions: list[str], params: list) -> tuple[float, int]:
        where_clause = " AND ".join(conditions)
        c.execute(
            f"SELECT SUM(amount), COUNT(*) FROM transactions WHERE user_id = ? AND {where_clause}",
            [user_id] + params,
        )
        total, count = c.fetchone()
        return (total or 0.0, count or 0)

    base_conditions = ["amount > 0", "merchant NOT LIKE '%System Balance Sync%'"]
    if not include_pending:
        base_conditions.append("status = 'Evaluated'")
    base_params: list = []
    if start_date:
        base_conditions.append("date >= ?")
        base_params.append(start_date)
    if end_date:
        base_conditions.append("date <= ?")
        base_params.append(f"{end_date} 23:59:59")

    conditions = list(base_conditions)
    params = list(base_params)

    if merchant:
        conditions.append(
            "(LOWER(clean_merchant) LIKE LOWER(?) OR LOWER(merchant) LIKE LOWER(?))"
        )
        params.extend([f"%{merchant}%", f"%{merchant}%"])
    if category:
        conditions.append("LOWER(category) LIKE LOWER(?)")
        params.append(f"%{category}%")

    total, count = run_query(conditions, params)
    fallback_used = False
    if count == 0 and merchant and not category:
        fallback_conditions = list(base_conditions)
        fallback_params = list(base_params)
        fallback_conditions.append(
            "(LOWER(category) LIKE LOWER(?) OR LOWER(merchant) LIKE LOWER(?) OR LOWER(clean_merchant) LIKE LOWER(?))"
        )
        fallback_params.extend([f"%{merchant}%", f"%{merchant}%", f"%{merchant}%"])
        total, count = run_query(fallback_conditions, fallback_params)
        fallback_used = count > 0

    label = merchant or category or "all spending"
    range_label = (
        f" from {start_date} to {end_date}"
        if start_date and end_date
        else (
            f" since {start_date}"
            if start_date
            else f" through {end_date}" if end_date else " (all time)"
        )
    )
    if count == 0:
        return f" No transactions found for **{label}**{range_label}."
    result = f" **{label.title()}**{range_label}: **${total:.2f}** across {count} transaction(s)."

    context_where = " AND ".join(conditions)
    c.execute(
        f"""SELECT tc.tag, SUM(t.amount), COUNT(DISTINCT t.id)
        FROM transactions t JOIN transaction_context tc ON tc.transaction_row_id=t.id
        WHERE t.user_id = ? AND tc.user_id = ? AND {context_where}
        GROUP BY tc.tag ORDER BY SUM(t.amount) DESC""",
        [user_id, user_id] + params,
    )
    context_rows = c.fetchall()
    if context_rows:
        result += "\n **Context:** " + "; ".join(
            f"{tag}: ${_num(amt):,.2f} ({n} tx)" for tag, amt, n in context_rows
        )
    else:
        result += (
            "\n **Context:** No behavioral context recorded for these transactions."
        )
    if fallback_used:
        result += " *(matched by category, not merchant name)*"
    return result

def get_spending_breakdown(*, days: int = 30, user_id: str,) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_spending_breakdown: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        """SELECT COALESCE(category, 'Uncategorized'), SUM(amount), COUNT(*)
        FROM transactions
        WHERE user_id = ? AND status = 'Evaluated' AND amount > 0
        AND merchant NOT LIKE '%System Balance Sync%'
        AND date >= datetime('now', ?)
        GROUP BY COALESCE(category, 'Uncategorized')
        ORDER BY SUM(amount) DESC""",
        (user_id, f"-{days} days",),
    )
    rows = c.fetchall()
    if not rows:
        return f" No evaluated spending found in the last {days} days."
    total = sum(r[1] for r in rows)
    lines = [f" **Spending breakdown, last {days} days** (total ${total:.2f}):"]
    for category, amt, count in rows:
        pct = (amt / total * 100) if total and total != 0 else 0
        lines.append(
            f"- {category}: ${amt:.2f} ({pct:.0f}%) across {count} transaction(s)"
        )

    c.execute(
        """SELECT tc.tag, SUM(t.amount), COUNT(DISTINCT t.id)
        FROM transactions t JOIN transaction_context tc ON tc.transaction_row_id=t.id
        WHERE t.user_id = ? AND t.status='Evaluated' AND t.amount > 0
        AND t.merchant NOT LIKE '%System Balance Sync%'
        AND t.date >= datetime('now', ?)
        GROUP BY tc.tag ORDER BY SUM(t.amount) DESC""",
        (user_id, f"-{days} days",),
    )
    context_rows = c.fetchall()
    lines.append("")
    lines.append(" **Behavioral context** (tag totals may overlap):")
    if context_rows:
        lines.extend(
            f"- {tag}: ${_num(amt):,.2f} across {n} transaction(s)"
            for tag, amt, n in context_rows
        )
    else:
        lines.append("- No behavioral context recorded.")
    return "\n".join(lines)

def adjust_savings_bucket(name: str,
    delta: float | None = None,
    set_current: float | None = None,
    target: float | None = None, *, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "adjust_savings_bucket: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not isinstance(name, str) or not name.strip():
        return " A non-empty savings bucket name is required."
    if delta is not None and set_current is not None:
        return " Use either delta or set_current, not both."
    try:
        if delta is not None:
            delta = float(delta)
        if set_current is not None:
            set_current = float(set_current)
        if target is not None:
            target = float(target)
    except (TypeError, ValueError):
        return " Savings amounts must be numeric."
    if delta is None and set_current is None and target is None:
        return " No savings-bucket change was requested."

    c.execute(
        "SELECT id, target_amount, current_amount FROM savings_buckets WHERE user_id = ? AND LOWER(name) = LOWER(?)",
        (user_id, name),
    )
    row = c.fetchone()
    if not row:
        new_target = max(0.0, target if target is not None else 0.0)
        new_current = set_current if set_current is not None else (delta or 0.0)
        c.execute(
            "INSERT INTO savings_buckets (user_id, name, target_amount, current_amount) VALUES (?, ?, ?, ?)",
            (user_id, name, new_target, new_current),
        )
        safe_commit()
        return f" Created savings bucket **{name}**: ${new_current:,.2f} / ${new_target:,.2f}"

    bucket_id, cur_target, cur_amount = row
    new_amount = cur_amount or 0.0
    if set_current is not None:
        new_amount = set_current
    elif delta is not None:
        new_amount = (cur_amount or 0.0) + delta
    new_target = target if target is not None else cur_target
    c.execute(
        "UPDATE savings_buckets SET current_amount = ?, target_amount = ? WHERE user_id = ? AND id = ?",
        (new_amount, new_target, user_id, bucket_id),
    )
    safe_commit()
    return f" Updated **{name}**: ${new_amount:,.2f} / ${new_target:,.2f}"

_SANDBOX_PY_BLOCK_RE = re.compile(
    r"<<<RUN_PYTHON_SANDBOX(?:\s+timeout=(\d+))?>>>\s*\n(.*?)\n<<<END_RUN_PYTHON_SANDBOX>>>",
    re.DOTALL,
)
_SANDBOX_SHELL_BLOCK_RE = re.compile(
    r"<<<RUN_SHELL(?:\s+timeout=(\d+))?>>>\s*\n(.*?)\n<<<END_RUN_SHELL>>>",
    re.DOTALL,
)

def _extract_sandbox_blocks(text: str) -> list[dict]:
    blocks = []
    for m in _SANDBOX_PY_BLOCK_RE.finditer(text):
        timeout_str, code = m.groups()
        blocks.append(
            {
                "kind": "python",
                "code": code.strip("\n"),
                "timeout": int(timeout_str) if timeout_str else None,
                "span": m.span(),
            }
        )
    for m in _SANDBOX_SHELL_BLOCK_RE.finditer(text):
        timeout_str, code = m.groups()
        blocks.append(
            {
                "kind": "shell",
                "code": code.strip("\n"),
                "timeout": int(timeout_str) if timeout_str else None,
                "span": m.span(),
            }
        )
    blocks.sort(key=lambda b: b["span"][0])
    return blocks

def _strip_sandbox_blocks(text: str, blocks: list[dict]) -> str:
    out = text
    for b in sorted(blocks, key=lambda b: b["span"][0], reverse=True):
        start, end = b["span"]
        out = out[:start] + out[end:]
    return out.strip()

def check_budget_status(*, user_id: str) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "check_budget_status: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    weekly_spending = get_weekly_spending(user_id=user_id)
    weekly_limit, impulse_threshold = get_budget_settings(user_id=user_id)
    remaining = weekly_limit - weekly_spending
    pct = (weekly_spending / weekly_limit * 100) if weekly_limit else 0
    if weekly_spending > weekly_limit:
        status = f" **Over weekly budget** by ${weekly_spending - weekly_limit:,.2f}."
    elif pct >= 80:
        status = f" **Approaching weekly limit** — {pct:.0f}% used, ${remaining:,.2f} left."
    else:
        status = f" **On track** — {pct:.0f}% of weekly budget used, ${remaining:,.2f} left."
    return (
        f"{status}\n"
        f"Spent this week: ${weekly_spending:,.2f} / ${weekly_limit:,.2f} "
        f"(single-purchase impulse threshold: ${impulse_threshold:,.2f})"
    )

# ============================================================
# Chat Memory
# ============================================================
def save_chat_turn(user_id: str, role: str, content: str):
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "save_chat_turn: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        "INSERT INTO chat_history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (str(user_id), role, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    safe_commit()

def get_recent_chat_history(
    user_id: str, limit: int = CHAT_HISTORY_TURNS
) -> list[dict]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "get_recent_chat_history: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    c.execute(
        "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (str(user_id), limit, 0),
    )
    rows = c.fetchall()
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]

# ============================================================
# Audit Batch Mutations
# ============================================================
def delete_manual_transaction(*, transaction_row_id: int, user_id: str) -> str:
    """Delete a manually-created transaction with no Plaid transaction_id."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "delete_manual_transaction: forced isolation violation — "
            "user_id is required and must be a non-empty string."
        )

    try:
        transaction_row_id = int(transaction_row_id)
    except (TypeError, ValueError):
        return " transaction_row_id must be an integer."

    c.execute(
        """SELECT id, transaction_id, merchant, amount, date, account_used
           FROM transactions
           WHERE user_id = ? AND id = ?
           LIMIT 1""",
        (user_id, transaction_row_id),
    )
    row = c.fetchone()

    if not row:
        return f" No transaction #{transaction_row_id} exists."

    row_id, plaid_tx_id, merchant, amount, date, account_used = row

    # Plaid-backed transactions are never deletable.
    if plaid_tx_id is not None and str(plaid_tx_id).strip():
        return (
            f" REJECTED: Transaction #{row_id} is Plaid-backed "
            f"(transaction_id={plaid_tx_id}) and cannot be deleted."
        )

    c.execute(
        """DELETE FROM transactions
           WHERE user_id = ?
             AND id = ?
             AND (transaction_id IS NULL OR TRIM(transaction_id) = '')""",
        (user_id, row_id),
    )

    if c.rowcount != 1:
        return f" Failed to delete transaction #{row_id}."

    safe_commit()

    return (
        f" DELETED manual transaction #{row_id}: "
        f"{merchant or '(unknown)'} ${abs(float(amount or 0)):,.2f}."
    )


def batch_correct_transactions(items: list[dict], user_id: str,) -> str:
    """Apply up to 50 independent transaction corrections in one tool call."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "batch_correct_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not isinstance(items, list) or not items:
        return " items must be a non-empty list."

    if len(items) > 50:
        return " Maximum 50 corrections per batch. Split the audit into smaller batches."

    results: list[str] = []
    ok = 0
    failed = 0

    # Deduplicate by transaction_row_id.
    seen_ids: set[int] = set()
    unique_items: list[dict] = []

    for item in items:
        if not isinstance(item, dict):
            failed += 1
            results.append("invalid item: expected object")
            continue

        rid = item.get("transaction_row_id")

        if rid is None:
            failed += 1
            results.append("missing transaction_row_id")
            continue

        try:
            rid = int(rid)
        except (TypeError, ValueError):
            failed += 1
            results.append(f"invalid transaction_row_id: {rid!r}")
            continue

        if rid in seen_ids:
            continue

        seen_ids.add(rid)
        item = dict(item)
        item["transaction_row_id"] = rid
        unique_items.append(item)

    # Every individual correction gets its own failure boundary.
    # A rejected row must NEVER be counted as successful.
    for item in unique_items:
        rid = int(item["transaction_row_id"])

        try:
            result = correct_transaction(
                transaction_row_id=rid,
                corrected_amount=item.get("corrected_amount"),
                reason=str(item.get("reason", "")).strip(),
                source=str(item.get("source", "user")),
                context_tag=item.get("context_tag"),
                context_note=item.get("context_note"),
                category=item.get("category"),
                status=item.get("status"),
                judgment=item.get("judgment"),
                auto_commit=False,
                user_id=user_id,
            )

            result_text = str(result).strip()

            # correct_transaction uses this exact success prefix.
            # Do NOT use startswith("") — that is always True.
            if result_text.startswith(f"#{rid} updated:") and "audit logged." in result_text:
                # Verify SQLite actually contains the requested mutation.
                c.execute(
                    """
                    SELECT category, status, judgment, override_amount
                    FROM transactions
                    WHERE user_id = ? AND id = ?
                    LIMIT 1
                    """,
                    (user_id, rid),
                )
                row = c.fetchone()

                if not row:
                    failed += 1
                    results.append(
                        f"#{rid}: correction reported success but transaction could not "
                        "be verified in SQLite"
                    )
                    continue

                actual_category, actual_status, actual_judgment, actual_override = row

                verified = True

                requested_category = item.get("category")
                requested_status = item.get("status")
                requested_judgment = item.get("judgment")
                requested_amount = item.get("corrected_amount")

                if (
                    requested_category is not None
                    and actual_category != requested_category
                ):
                    verified = False

                if (
                    requested_status is not None
                    and actual_status != requested_status
                ):
                    verified = False

                if (
                    requested_judgment is not None
                    and actual_judgment != requested_judgment.strip()
                ):
                    # correct_transaction sanitizes the judgment before writing.
                    safe_judgment = (
                        requested_judgment.strip()
                        .replace(
                            "Model Unavailable flag cleared",
                            "previously unresolved flag cleared",
                        )
                        .replace("Model Unavailable", "Unresolved")
                    )
                    if actual_judgment != safe_judgment:
                        verified = False

                if requested_amount is not None:
                    try:
                        if (
                            actual_override is None
                            or abs(float(actual_override) - float(requested_amount)) > 0.01
                        ):
                            verified = False
                    except (TypeError, ValueError):
                        verified = False

                # Verify the audit log exists for this successful correction.
                c.execute(
                    """
                    SELECT COUNT(*)
                    FROM transaction_correction_log
                    WHERE user_id = ?
                      AND transaction_row_id = ?
                      AND action = 'correct'
                    """,
                    (user_id, rid),
                )
                audit_count = int(c.fetchone()[0] or 0)

                if audit_count < 1:
                    verified = False

                if verified:
                    ok += 1
                else:
                    failed += 1
                    results.append(
                        f"#{rid}: correction returned success but SQLite verification failed"
                    )
            else:
                failed += 1
                results.append(f"#{rid}: {result_text}")

        except Exception as exc:
            failed += 1
            results.append(
                f"#{rid}: {type(exc).__name__}: {exc}"
            )

    # Commit all successful corrections together.
    try:
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return (
            " BATCH CORRECT COMMIT FAILED: "
            f"{type(exc).__name__}: {exc}"
        )

    summary = f" BATCH CORRECT: {ok} succeeded, {failed} failed."

    if results:
        summary += "\n" + "\n".join(results[:20])
        if len(results) > 20:
            summary += f"\n…and {len(results) - 20} more failures."

    return summary


def batch_lock_transactions(
    *,
    transaction_row_ids: list[int],
    locked: bool = True,
    user_id: str,
) -> str:
    """Lock or unlock up to 100 transactions in one tool call."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "batch_lock_transactions: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not isinstance(transaction_row_ids, list) or not transaction_row_ids:
        return " transaction_row_ids must be a non-empty list."
    if len(transaction_row_ids) > 100:
        return " Maximum 100 transaction IDs per batch. Split the batch."

    ok = 0
    failed = 0
    failures: list[str] = []
    seen_ids: set[int] = set()

    for raw_rid in transaction_row_ids:
        try:
            rid = int(raw_rid)
        except (TypeError, ValueError):
            failed += 1
            failures.append(f"{raw_rid!r}: invalid transaction_row_id")
            continue

        if rid in seen_ids:
            continue
        seen_ids.add(rid)

        try:
            result = lock_transaction(
                transaction_row_id=rid,
                locked=locked,
                auto_commit=False,
                user_id=user_id,
            )
            result_text = str(result).strip()

            # Never use startswith("") here either.
            # Verify the actual SQLite lock state after the mutation.
            c.execute(
                """
                SELECT is_locked
                FROM transactions
                WHERE user_id = ? AND id = ?
                LIMIT 1
                """,
                (user_id, rid),
            )
            row = c.fetchone()

            if not row:
                failed += 1
                failures.append(
                    f"#{rid}: lock operation returned {result_text!r} "
                    "but transaction could not be verified"
                )
                continue

            actual_locked = bool(row[0])

            if actual_locked == bool(locked):
                ok += 1
            else:
                failed += 1
                failures.append(
                    f"#{rid}: lock operation returned {result_text!r} "
                    f"but SQLite is_locked={int(actual_locked)}"
                )

        except Exception as exc:
            failed += 1
            failures.append(
                f"#{rid}: {type(exc).__name__}: {exc}"
            )

    try:
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return (
            f" BATCH {'LOCK' if locked else 'UNLOCK'} COMMIT FAILED: "
            f"{type(exc).__name__}: {exc}"
        )

    action = "LOCK" if locked else "UNLOCK"
    summary = f" BATCH {action}: {ok} succeeded, {failed} failed."

    if failures:
        summary += "\n" + "\n".join(failures[:20])
        if len(failures) > 20:
            summary += f"\n…and {len(failures) - 20} more failures."

    return summary


__all__ = [
    'get_plaid_credential', 'batch_correct_transactions', 'update_expected_income',
    'get_planned_transactions', 'get_recent_transactions', '_date_distance_days',
    'save_known_merchant', 'correct_transaction', 'get_financial_dashboard',
    '_strip_sandbox_blocks', 'get_recent_chat_history', 'delete_memory',
    'search_transactions', '_TX_FIELDS', 'add_planned_transaction',
    '_SANDBOX_SHELL_BLOCK_RE', 'add_expected_income', 'get_net_worth_history',
    '_format_compact_tx_line', 'get_transaction_context', 'check_and_reconcile_income',
    'format_transaction_context', 'check_budget_status', 'cancel_planned_transaction',
    '_get_transactions_by_lock_state', 'get_unlocked_transactions', 'get_spending_breakdown',
    'get_subscriptions', 'get_recent_financial_activity', 'detect_and_update_subscriptions',
    'query_spending', 'log_lifestyle_context', 'get_known_merchant',
    'get_unique_unregistered_merchants', 'update_planned_transaction',
    'reconcile_expected_and_planned_transactions', 'get_weekly_spending', 'save_memory',
    'get_locked_transactions', 'get_savings_buckets', 'get_budget_settings',
    '_SANDBOX_PY_BLOCK_RE', '_extract_sandbox_blocks', 'adjust_savings_bucket',
    'get_recent_income', 'get_accounts_overview', '_num', 'save_chat_turn',
    'batch_lock_transactions', 'cancel_expected_income', 'get_upcoming_cash_flow',
    'delete_savings_bucket', 'tag_transaction_context', 'lock_transaction',
    'get_recent_corrections', 'get_lifestyle_context', 'clear_transaction_correction',
    'get_debt_overview', 'get_expected_income', 'get_transactions_by_context',
    'get_current_financial_position', 'add_transaction', 'get_memories',
    'get_cash_flow_summary', 'delete_transaction', 'delete_manual_transaction', 'bot',
    'calculate_debt_payoff', 'add_or_update_debt', 'list_user_debts', 'delete_user_debt',
    'sync_debts_from_plaid', 'add_transaction_item', 'get_transaction_items',
    'delete_transaction_item', 'reconcile_receipts_for_user', 'tag_transaction_tax',
    'get_tax_deductions_summary', 'export_tax_csv', 'simulate_cash_flow_scenario',
    'add_subscription_trial', 'list_subscription_trials', 'update_trial_status',
    'detect_zombie_subscriptions', 'get_roundup_settings', 'set_roundup_settings',
    'calculate_transaction_roundup', 'apply_transaction_roundups', 'get_sinking_funds_overview',
    'generate_financial_digest'
]

def get_plaid_credential(user_id, key: str) -> str | None:
    """Look up a Plaid credential saved via !plaidsetup, falling back to .env."""
    c.execute(
        "SELECT value FROM user_info WHERE user_id = ? AND key = ?",
        (str(user_id), key.lower()),
    )
    row = c.fetchone()
    if row and row[0]:
        return row[0]
    return os.getenv(key.upper())

