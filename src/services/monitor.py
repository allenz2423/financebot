"""Persistent, event-driven financial monitor.

This module implements a deterministic financial monitoring engine that
does NOT depend on the LLM.  Rules are declarative predicates over the
ledger.  When a predicate fires, an alert row is written to the database
and optionally delivered to Discord / ntfy.

The engine is intentionally simple and dependency-free.  It is designed to
run as a long-lived background task inside the bot's event loop, but every
public function is also callable directly so it can be tested in isolation.

Security model:
  * Rules are owned by user_id and never read or mutate another user's data.
  * The evaluator is a fixed allowlist of rule kinds.  There is no dynamic
    code execution, no eval/exec, and no user-supplied Python.
  * Config values are validated per rule kind before evaluation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# Rule kind allowlist
# ============================================================
# Each kind maps to a deterministic evaluator function.  The allowlist is
# the only way new rule kinds can enter the system; there is no dynamic
# dispatch from user input.

RULE_KINDS = {
    "projected_balance_low",
    "category_spend_exceeded",
    "income_overdue",
    "subscription_price_changed",
    "unusual_transaction",
    "recurring_bill_missing",
    "cash_flow_change",
    "large_deposit",
    "trial_expiring_soon",
    "duplicate_charge_detected",
}


@dataclass
class RuleConfig:
    """Validated configuration for one monitor rule kind."""

    kind: str
    config: Dict[str, Any]


def _parse_json(value: Any, default: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a JSON config string safely, falling back to a default."""
    if value is None:
        return dict(default)
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return dict(default)
    text = value.strip()
    if not text:
        return dict(default)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return dict(default)
    return parsed if isinstance(parsed, dict) else dict(default)


def _validate_rule(kind: str, config: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate a rule kind + config pair.

    Returns (ok, error_message, normalized_config).
    """
    if kind not in RULE_KINDS:
        return False, f"Unknown rule kind '{kind}'. Valid kinds: {sorted(RULE_KINDS)}", {}

    cfg = dict(config or {})

    if kind == "projected_balance_low":
        try:
            cfg["threshold"] = float(cfg.get("threshold", 0))
        except (TypeError, ValueError):
            return False, "projected_balance_low requires numeric 'threshold'", {}
        try:
            cfg["window_days"] = int(cfg.get("window_days", 14))
        except (TypeError, ValueError):
            return False, "projected_balance_low requires integer 'window_days'", {}
        cfg["window_days"] = max(1, min(cfg["window_days"], 365))
        return True, "", cfg

    if kind == "category_spend_exceeded":
        category = str(cfg.get("category", "")).strip()
        if not category:
            return False, "category_spend_exceeded requires a 'category'", {}
        try:
            cfg["limit"] = float(cfg.get("limit", 0))
        except (TypeError, ValueError):
            return False, "category_spend_exceeded requires numeric 'limit'", {}
        try:
            cfg["window_days"] = int(cfg.get("window_days", 30))
        except (TypeError, ValueError):
            return False, "category_spend_exceeded requires integer 'window_days'", {}
        cfg["window_days"] = max(1, min(cfg["window_days"], 365))
        cfg["category"] = category
        return True, "", cfg

    if kind == "income_overdue":
        try:
            cfg["expected_income_id"] = int(cfg.get("expected_income_id", 0))
        except (TypeError, ValueError):
            return False, "income_overdue requires integer 'expected_income_id'", {}
        if cfg["expected_income_id"] <= 0:
            return False, "income_overdue requires a positive 'expected_income_id'", {}
        try:
            cfg["grace_days"] = int(cfg.get("grace_days", 3))
        except (TypeError, ValueError):
            return False, "income_overdue requires integer 'grace_days'", {}
        cfg["grace_days"] = max(0, min(cfg["grace_days"], 30))
        return True, "", cfg

    if kind == "subscription_price_changed":
        merchant = str(cfg.get("merchant", "")).strip()
        if not merchant:
            return False, "subscription_price_changed requires a 'merchant'", {}
        try:
            cfg["pct_increase"] = float(cfg.get("pct_increase", 10.0))
        except (TypeError, ValueError):
            return False, "subscription_price_changed requires numeric 'pct_increase'", {}
        cfg["pct_increase"] = max(0.0, cfg["pct_increase"])
        cfg["merchant"] = merchant
        return True, "", cfg

    if kind == "unusual_transaction":
        try:
            cfg["stddev_multiplier"] = float(cfg.get("stddev_multiplier", 3.0))
        except (TypeError, ValueError):
            return False, "unusual_transaction requires numeric 'stddev_multiplier'", {}
        try:
            cfg["window_days"] = int(cfg.get("window_days", 90))
        except (TypeError, ValueError):
            return False, "unusual_transaction requires integer 'window_days'", {}
        cfg["window_days"] = max(7, min(cfg["window_days"], 365))
        cfg["stddev_multiplier"] = max(1.0, cfg["stddev_multiplier"])
        return True, "", cfg

    if kind == "recurring_bill_missing":
        merchant = str(cfg.get("merchant", "")).strip()
        if not merchant:
            return False, "recurring_bill_missing requires a 'merchant'", {}
        try:
            cfg["window_days"] = int(cfg.get("window_days", 45))
        except (TypeError, ValueError):
            return False, "recurring_bill_missing requires integer 'window_days'", {}
        cfg["window_days"] = max(7, min(cfg["window_days"], 180))
        cfg["merchant"] = merchant
        return True, "", cfg

    if kind == "cash_flow_change":
        try:
            cfg["pct_change"] = float(cfg.get("pct_change", 20.0))
        except (TypeError, ValueError):
            return False, "cash_flow_change requires numeric 'pct_change'", {}
        try:
            cfg["window_days"] = int(cfg.get("window_days", 30))
        except (TypeError, ValueError):
            return False, "cash_flow_change requires integer 'window_days'", {}
        cfg["window_days"] = max(7, min(cfg["window_days"], 365))
        cfg["pct_change"] = max(0.0, cfg["pct_change"])
        return True, "", cfg

    if kind == "large_deposit":
        try:
            amt = cfg.get("min_amount") if "min_amount" in cfg else cfg.get("threshold", 1000.0)
            cfg["min_amount"] = float(amt)
        except (TypeError, ValueError):
            return False, "large_deposit requires numeric 'min_amount'", {}
        cfg["min_amount"] = max(0.0, cfg["min_amount"])
        cfg["threshold"] = cfg["min_amount"]
        return True, "", cfg

    if kind == "trial_expiring_soon":
        try:
            cfg["days_notice"] = int(cfg.get("days_notice", 2))
        except (TypeError, ValueError):
            return False, "trial_expiring_soon requires integer 'days_notice'", {}
        cfg["days_notice"] = max(1, min(cfg["days_notice"], 30))
        return True, "", cfg

    if kind == "duplicate_charge_detected":
        try:
            cfg["window_days"] = int(cfg.get("window_days", 3))
        except (TypeError, ValueError):
            return False, "duplicate_charge_detected requires integer 'window_days'", {}
        cfg["window_days"] = max(1, min(cfg["window_days"], 30))
        try:
            cfg["min_amount"] = float(cfg.get("min_amount", 5.0))
        except (TypeError, ValueError):
            return False, "duplicate_charge_detected requires numeric 'min_amount'", {}
        cfg["min_amount"] = max(0.0, cfg["min_amount"])
        cfg["merchant"] = str(cfg.get("merchant", "")).strip()
        return True, "", cfg

    return False, f"Unhandled rule kind '{kind}'", {}


# ============================================================
# Ledger query helpers
# ============================================================
# All queries are parameterized and user-scoped.  The monitor never writes
# to the ledger; it only reads it.


def _fetch_user_id(conn: sqlite3.Connection, user_id: str) -> str:
    """Normalize and validate a user_id before any query."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    return uid


def _liquid_balance(conn: sqlite3.Connection, user_id: str) -> float:
    """Best-effort liquid balance from the latest balance snapshot."""
    uid = _fetch_user_id(conn, user_id)
    row = conn.execute(
        """
        SELECT liquid_balance
        FROM balance_snapshots
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (uid,),
    ).fetchone()
    if not row or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return 0.0


def _projected_balance(
    conn: sqlite3.Connection,
    user_id: str,
    window_days: int,
) -> float:
    """Project the liquid balance window_days into the future.

    Uses settled transactions only.  Expected income and planned expenses
    are treated as *projections*, never as settled cash.
    """
    uid = _fetch_user_id(conn, user_id)
    start = liquid = _liquid_balance(conn, uid)
    now = datetime.now()
    cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ?
          AND status = 'Evaluated'
          AND date >= ?
        """,
        (uid, cutoff),
    ).fetchone()
    try:
        delta = float(row[0] or 0.0)
    except (TypeError, ValueError):
        delta = 0.0
    return float(start) + delta


def _category_spend(
    conn: sqlite3.Connection,
    user_id: str,
    category: str,
    window_days: int,
) -> float:
    """Total settled spend for one category over a trailing window."""
    uid = _fetch_user_id(conn, user_id)
    cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ?
          AND status = 'Evaluated'
          AND amount > 0
          AND LOWER(COALESCE(category, '')) = LOWER(?)
          AND date >= ?
        """,
        (uid, category, cutoff),
    ).fetchone()
    try:
        return float(row[0] or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _latest_amount_for_merchant(
    conn: sqlite3.Connection,
    user_id: str,
    merchant: str,
    window_days: int,
) -> Optional[Tuple[float, str]]:
    """Return (amount, date) for the most recent settled charge at a merchant."""
    uid = _fetch_user_id(conn, user_id)
    cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT amount, date
        FROM transactions
        WHERE user_id = ?
          AND status = 'Evaluated'
          AND amount > 0
          AND (LOWER(clean_merchant) LIKE LOWER(?) OR LOWER(merchant) LIKE LOWER(?))
          AND date >= ?
        ORDER BY date DESC, id DESC
        LIMIT 1
        """,
        (uid, f"%{merchant}%", f"%{merchant}%", cutoff),
    ).fetchone()
    if not row:
        return None
    try:
        return float(row[0]), str(row[1] or "")
    except (TypeError, ValueError):
        return None


def _merchant_amounts(
    conn: sqlite3.Connection,
    user_id: str,
    merchant: str,
    window_days: int,
) -> List[float]:
    """All settled positive amounts for a merchant in the window."""
    uid = _fetch_user_id(conn, user_id)
    cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT amount
        FROM transactions
        WHERE user_id = ?
          AND status = 'Evaluated'
          AND amount > 0
          AND (LOWER(clean_merchant) LIKE LOWER(?) OR LOWER(merchant) LIKE LOWER(?))
          AND date >= ?
        ORDER BY date ASC, id ASC
        """,
        (uid, f"%{merchant}%", f"%{merchant}%", cutoff),
    ).fetchall()
    out: List[float] = []
    for r in rows:
        try:
            out.append(float(r[0]))
        except (TypeError, ValueError):
            continue
    return out


def _expected_income_record(
    conn: sqlite3.Connection,
    user_id: str,
    income_id: int,
) -> Optional[dict]:
    uid = _fetch_user_id(conn, user_id)
    row = conn.execute(
        """
        SELECT id, source, net_expected, expected_date, status, recurrence
        FROM cash_inflows
        WHERE user_id = ? AND id = ?
        LIMIT 1
        """,
        (uid, income_id),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "source": row[1],
        "net_expected": row[2],
        "expected_date": row[3],
        "status": row[4],
        "recurrence": row[5],
    }


def _count_recent_settled_charges(
    conn: sqlite3.Connection,
    user_id: str,
    merchant: str,
    window_days: int,
) -> int:
    uid = _fetch_user_id(conn, user_id)
    cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE user_id = ?
          AND status = 'Evaluated'
          AND amount > 0
          AND (LOWER(clean_merchant) LIKE LOWER(?) OR LOWER(merchant) LIKE LOWER(?))
          AND date >= ?
        """,
        (uid, f"%{merchant}%", f"%{merchant}%", cutoff),
    ).fetchone()
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


def _net_cash_flow(conn: sqlite3.Connection, user_id: str, window_days: int) -> float:
    """Net cash flow (income - expense) over a trailing window."""
    uid = _fetch_user_id(conn, user_id)
    cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ?
          AND status = 'Evaluated'
          AND merchant NOT LIKE '%System Balance Sync%'
          AND date >= ?
        """,
        (uid, cutoff),
    ).fetchone()
    try:
        return float(row[0] or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mean_std(values: List[float]) -> Tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, var ** 0.5


# ============================================================
# Rule evaluators
# ============================================================
# Each evaluator returns Optional[str]: None means "no alert", a non-empty
# string is the human-readable alert detail.


def _eval_projected_balance_low(conn, cfg, user_id):
    threshold = float(cfg.get("threshold", 0))
    window = int(cfg.get("window_days", 14))
    projected = _projected_balance(conn, user_id, window)
    if projected < threshold:
        return (
            f"Projected balance ${projected:,.2f} is below the ${threshold:,.2f} "
            f"threshold within {window} days."
        )
    return None


def _eval_category_spend_exceeded(conn, cfg, user_id):
    category = str(cfg.get("category", "")).strip()
    limit = float(cfg.get("limit", 0))
    window = int(cfg.get("window_days", 30))
    spent = _category_spend(conn, user_id, category, window)
    if spent > limit:
        return (
            f"{category} spending is ${spent:,.2f}, exceeding the ${limit:,.2f} "
            f"limit over the last {window} days."
        )
    return None


def _eval_income_overdue(conn, cfg, user_id):
    income_id = int(cfg.get("expected_income_id", 0))
    grace = int(cfg.get("grace_days", 3))
    rec = _expected_income_record(conn, user_id, income_id)
    if not rec:
        return None
    if rec["status"] != "Pending":
        return None
    expected = rec.get("expected_date")
    if not expected:
        return None
    try:
        exp_date = datetime.strptime(str(expected)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now().date()
    if (today - exp_date).days >= grace:
        return (
            f"Expected income #{rec['id']} ({rec['source']}, "
            f"${float(rec['net_expected'] or 0):,.2f}) was due {exp_date} "
            f"and has not been received."
        )
    return None


def _eval_subscription_price_changed(conn, cfg, user_id):
    merchant = str(cfg.get("merchant", "")).strip()
    pct = float(cfg.get("pct_increase", 10.0))
    window = 365
    latest = _latest_amount_for_merchant(conn, user_id, merchant, window)
    if not latest:
        return None
    amount, _date = latest
    amounts = _merchant_amounts(conn, user_id, merchant, window)
    if len(amounts) < 2:
        return None
    previous = amounts[-2]
    if previous <= 0:
        return None
    increase_pct = (amount - previous) / previous * 100.0
    if increase_pct >= pct:
        return (
            f"{merchant} charge increased from ${previous:,.2f} to "
            f"${amount:,.2f} ({increase_pct:.1f}%), above the {pct:.1f}% threshold."
        )
    return None


def _eval_unusual_transaction(conn, cfg, user_id):
    multiplier = float(cfg.get("stddev_multiplier", 3.0))
    window = int(cfg.get("window_days", 90))
    amounts = _merchant_amounts(conn, user_id, "", window)
    if not amounts:
        return None
    mean, std = _mean_std(amounts)
    if std <= 0:
        return None
    latest = _latest_amount_for_merchant(conn, user_id, "", window)
    if not latest:
        return None
    amount, _date = latest
    if amount > mean + multiplier * std:
        return (
            f"Unusual charge of ${amount:,.2f} vs merchant mean "
            f"${mean:,.2f} (std ${std:,.2f}) over {window} days."
        )
    return None


def _eval_recurring_bill_missing(conn, cfg, user_id):
    merchant = str(cfg.get("merchant", "")).strip()
    window = int(cfg.get("window_days", 45))
    count = _count_recent_settled_charges(conn, user_id, merchant, window)
    if count == 0:
        return (
            f"Recurring bill from {merchant} has not posted in the last "
            f"{window} days."
        )
    return None


def _eval_cash_flow_change(conn, cfg, user_id):
    pct = float(cfg.get("pct_change", 20.0))
    window = int(cfg.get("window_days", 30))
    current = _net_cash_flow(conn, user_id, window)
    prior = _net_cash_flow(conn, user_id, window * 2)
    prior_window = prior
    if prior_window == 0:
        return None
    change_pct = abs(current - prior_window) / abs(prior_window) * 100.0
    if change_pct >= pct:
        direction = "increased" if current > prior_window else "decreased"
        return (
            f"Cash flow {direction} by {change_pct:.1f}% "
            f"(${current:,.2f} vs ${prior_window:,.2f} over {window} days)."
        )
    return None


def _eval_large_deposit(conn, cfg, user_id):
    minimum = float(cfg.get("min_amount", 1000.0))
    row = conn.execute(
        """
        SELECT merchant, amount, date
        FROM transactions
        WHERE user_id = ? AND status = 'Evaluated'
          AND amount < 0 AND abs(amount) >= ?
        ORDER BY date DESC, id DESC LIMIT 1
        """,
        (user_id, minimum),
    ).fetchone()
    if not row:
        return None
    merchant, amount, date = row
    return (
        f"Large deposit of ${abs(float(amount)):,.2f} from "
        f"{merchant or 'unknown'} on {date}."
    )


def _eval_trial_expiring_soon(conn, config: dict, user_id: str) -> Optional[str]:
    uid = _fetch_user_id(conn, user_id)
    days_notice = int(config.get("days_notice", 2))
    cur = conn.execute(
        """SELECT id, service_name, trial_end_date, projected_cost, cancellation_url
        FROM subscription_trials
        WHERE user_id = ? AND status = 'active'
          AND date(trial_end_date) <= date('now', '+' || ? || ' days')
          AND date(trial_end_date) >= date('now')
        ORDER BY date(trial_end_date) ASC LIMIT 1""",
        (uid, days_notice),
    )
    row = cur.fetchone()
    if not row:
        return None
    tid, sname, edate, cost, url = row
    cost_val = float(cost or 0.0)
    cost_txt = f" (${cost_val:,.2f})" if cost_val > 0 else ""
    url_txt = f" Cancel at: {url}" if url else ""
    return (
        f"Free trial for '{sname}' expires on {edate}{cost_txt}. "
        f"Cancel now to avoid renewal charge.{url_txt}"
    )


def _eval_duplicate_charge_detected(conn: sqlite3.Connection, cfg: dict, user_id: str) -> Optional[str]:
    uid = _fetch_user_id(conn, user_id)
    window = int(cfg.get("window_days", 3))
    min_amount = float(cfg.get("min_amount", 5.0))
    merchant = str(cfg.get("merchant", "")).strip()

    cutoff = (datetime.now() - timedelta(days=window)).strftime("%Y-%m-%d")

    query = """
        SELECT COALESCE(clean_merchant, merchant) AS m_name, amount, COUNT(*) AS cnt, MAX(date) AS latest_date
        FROM transactions
        WHERE user_id = ?
          AND status = 'Evaluated'
          AND amount >= ?
          AND merchant NOT LIKE '%System Balance Sync%'
          AND date >= ?
    """
    params: List[Any] = [uid, min_amount, cutoff]
    if merchant:
        query += " AND (clean_merchant LIKE ? OR merchant LIKE ?)"
        params.extend([f"%{merchant}%", f"%{merchant}%"])
    query += " GROUP BY m_name, amount HAVING cnt >= 2 ORDER BY latest_date DESC LIMIT 1"

    row = conn.execute(query, params).fetchone()
    if not row:
        return None
    m_name, amt, cnt, latest_date = row
    return (
        f"Potential duplicate charge: {cnt} charges of ${float(amt):,.2f} from "
        f"'{m_name or 'unknown'}' within {window} days (latest on {latest_date})."
    )


EVALUATORS = {
    "projected_balance_low": _eval_projected_balance_low,
    "category_spend_exceeded": _eval_category_spend_exceeded,
    "income_overdue": _eval_income_overdue,
    "subscription_price_changed": _eval_subscription_price_changed,
    "unusual_transaction": _eval_unusual_transaction,
    "recurring_bill_missing": _eval_recurring_bill_missing,
    "cash_flow_change": _eval_cash_flow_change,
    "large_deposit": _eval_large_deposit,
    "trial_expiring_soon": _eval_trial_expiring_soon,
    "duplicate_charge_detected": _eval_duplicate_charge_detected,
}


# ============================================================
# Rule CRUD
# ============================================================


def list_monitor_rules(conn, user_id: str, include_disabled: bool = False) -> List[dict]:
    uid = _fetch_user_id(conn, user_id)
    if include_disabled:
        rows = conn.execute(
            "SELECT id, user_id, name, kind, config, enabled, severity, "
            "cooldown_hours, last_fired_at, created_at, updated_at "
            "FROM monitor_rules WHERE user_id = ? ORDER BY id",
            (uid,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, user_id, name, kind, config, enabled, severity, "
            "cooldown_hours, last_fired_at, created_at, updated_at "
            "FROM monitor_rules WHERE user_id = ? AND enabled = 1 ORDER BY id",
            (uid,),
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "user_id": r[1],
                "name": r[2],
                "kind": r[3],
                "config": _parse_json(r[4], {}),
                "enabled": bool(r[5]),
                "severity": r[6],
                "cooldown_hours": r[7],
                "last_fired_at": r[8],
                "created_at": r[9],
                "updated_at": r[10],
            }
        )
    return out


def add_monitor_rule(
    conn,
    user_id: str,
    name: str,
    kind: str,
    config: Dict[str, Any],
    severity: str = "info",
    cooldown_hours: int = 24,
) -> Tuple[bool, str, Optional[int]]:
    uid = _fetch_user_id(conn, user_id)
    name = str(name or "").strip()
    if not name:
        return False, "Rule name is required.", None
    if severity not in {"info", "warning", "critical"}:
        return False, "severity must be info, warning, or critical.", None
    try:
        cooldown_hours = int(cooldown_hours)
    except (TypeError, ValueError):
        return False, "cooldown_hours must be an integer.", None
    cooldown_hours = max(0, min(cooldown_hours, 720))
    ok, err, normalized = _validate_rule(kind, config)
    if not ok:
        return False, err, None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO monitor_rules
        (user_id, name, kind, config, enabled, severity, cooldown_hours, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (uid, name, kind, json.dumps(normalized), severity, cooldown_hours, now, now),
    )
    conn.commit()
    return True, f"Created monitor rule #{cur.lastrowid}: {name}.", cur.lastrowid


def update_monitor_rule(
    conn,
    rule_id: int,
    user_id: str,
    name: Optional[str] = None,
    kind: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    enabled: Optional[bool] = None,
    severity: Optional[str] = None,
    cooldown_hours: Optional[int] = None,
) -> Tuple[bool, str]:
    uid = _fetch_user_id(conn, user_id)
    try:
        rule_id = int(rule_id)
    except (TypeError, ValueError):
        return False, "rule_id must be an integer."
    existing = conn.execute(
        "SELECT id, kind, config FROM monitor_rules WHERE user_id = ? AND id = ?",
        (uid, rule_id),
    ).fetchone()
    if not existing:
        return False, f"Monitor rule #{rule_id} not found."
    cur_kind = existing[1]
    cur_config = _parse_json(existing[2], {})
    new_kind = kind if kind is not None else cur_kind
    new_config = dict(cur_config)
    if config is not None:
        new_config.update(config)
    if new_kind != cur_kind:
        ok, err, normalized = _validate_rule(new_kind, new_config)
        if not ok:
            return False, err
        new_config = normalized
    else:
        ok, err, normalized = _validate_rule(new_kind, new_config)
        if not ok:
            return False, err
        new_config = normalized
    fields = []
    params = []
    if name is not None:
        fields.append("name = ?")
        params.append(str(name).strip())
    fields.append("kind = ?")
    params.append(new_kind)
    fields.append("config = ?")
    params.append(json.dumps(new_config))
    if enabled is not None:
        fields.append("enabled = ?")
        params.append(1 if enabled else 0)
    if severity is not None:
        if severity not in {"info", "warning", "critical"}:
            return False, "severity must be info, warning, or critical."
        fields.append("severity = ?")
        params.append(severity)
    if cooldown_hours is not None:
        try:
            cooldown_hours = int(cooldown_hours)
        except (TypeError, ValueError):
            return False, "cooldown_hours must be an integer."
        fields.append("cooldown_hours = ?")
        params.append(max(0, min(cooldown_hours, 720)))
    fields.append("updated_at = ?")
    params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    params.extend([uid, rule_id])
    conn.execute(
        f"UPDATE monitor_rules SET {', '.join(fields)} WHERE user_id = ? AND id = ?",
        params,
    )
    conn.commit()
    return True, f"Updated monitor rule #{rule_id}."


def delete_monitor_rule(conn, rule_id: int, user_id: str) -> str:
    uid = _fetch_user_id(conn, user_id)
    try:
        rule_id = int(rule_id)
    except (TypeError, ValueError):
        return "rule_id must be an integer."
    row = conn.execute(
        "SELECT id FROM monitor_rules WHERE user_id = ? AND id = ?",
        (uid, rule_id),
    ).fetchone()
    if not row:
        return f" Monitor rule #{rule_id} not found."
    # Cascade-delete the rule's alerts explicitly.  SQLite foreign-key
    # cascades are off by default (PRAGMA foreign_keys defaults to OFF),
    # so without this the orphaned alert rows would linger and
    # list_alerts would still show them under a dead rule_id.
    conn.execute(
        "DELETE FROM monitor_alerts WHERE user_id = ? AND rule_id = ?",
        (uid, rule_id),
    )
    conn.execute(
        "DELETE FROM monitor_rules WHERE user_id = ? AND id = ?",
        (uid, rule_id),
    )
    conn.commit()
    return f" Deleted monitor rule #{rule_id}."


def toggle_monitor_rule(conn, rule_id: int, user_id: str, enabled: bool) -> str:
    uid = _fetch_user_id(conn, user_id)
    try:
        rule_id = int(rule_id)
    except (TypeError, ValueError):
        return "rule_id must be an integer."
    row = conn.execute(
        "SELECT id FROM monitor_rules WHERE user_id = ? AND id = ?",
        (uid, rule_id),
    ).fetchone()
    if not row:
        return f" Monitor rule #{rule_id} not found."
    conn.execute(
        "UPDATE monitor_rules SET enabled = ?, updated_at = ? "
        "WHERE user_id = ? AND id = ?",
        (1 if enabled else 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), uid, rule_id),
    )
    conn.commit()
    state = "enabled" if enabled else "disabled"
    return f" Monitor rule #{rule_id} {state}."

# ============================================================
# Evaluation + alert recording
# ============================================================


def evaluate_rule(conn, row: dict, user_id: str) -> Optional[str]:
    """Evaluate one rule row. Returns alert detail string or None."""
    kind = row.get("kind")
    if kind not in EVALUATORS:
        return None
    cfg = row.get("config") or {}
    if isinstance(cfg, str):
        cfg = _parse_json(cfg, {})
    evaluator = EVALUATORS[kind]
    try:
        return evaluator(conn, cfg, user_id)
    except Exception as exc:
        logger.warning("monitor rule %s evaluation failed: %s", kind, exc)
        return None


def record_alert(
    conn,
    user_id: str,
    rule_id: int,
    rule_kind: str,
    severity: str,
    title: str,
    detail: str,
) -> int:
    """Insert one alert row and update the rule's last_fired_at."""
    uid = _fetch_user_id(conn, user_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO monitor_alerts
        (user_id, rule_id, rule_kind, severity, title, detail, fired_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, rule_id, rule_kind, severity, title, detail, now),
    )
    conn.execute(
        "UPDATE monitor_rules SET last_fired_at = ? WHERE id = ?",
        (now, rule_id),
    )
    conn.commit()
    return cur.lastrowid


def list_alerts(
    conn,
    user_id: str,
    limit: int = 50,
    include_acked: bool = False,
) -> List[dict]:
    uid = _fetch_user_id(conn, user_id)
    limit = max(1, min(int(limit), 500))
    if include_acked:
        rows = conn.execute(
            """
            SELECT id, rule_id, rule_kind, severity, title, detail,
                   fired_at, delivered_discord, delivered_ntfy, acked
            FROM monitor_alerts
            WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (uid, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, rule_id, rule_kind, severity, title, detail,
                   fired_at, delivered_discord, delivered_ntfy, acked
            FROM monitor_alerts
            WHERE user_id = ? AND acked = 0
            ORDER BY id DESC LIMIT ?
            """,
            (uid, limit),
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "rule_id": r[1],
                "rule_kind": r[2],
                "severity": r[3],
                "title": r[4],
                "detail": r[5],
                "fired_at": r[6],
                "delivered_discord": bool(r[7]),
                "delivered_ntfy": bool(r[8]),
                "acked": bool(r[9]),
            }
        )
    return out


def ack_alert(conn, alert_id: int, user_id: str) -> str:
    uid = _fetch_user_id(conn, user_id)
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError):
        return "alert_id must be an integer."
    row = conn.execute(
        "SELECT id FROM monitor_alerts WHERE user_id = ? AND id = ?",
        (uid, alert_id),
    ).fetchone()
    if not row:
        return f" Monitor alert #{alert_id} not found."
    conn.execute(
        "UPDATE monitor_alerts SET acked = 1, acked_at = ? "
        "WHERE user_id = ? AND id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), uid, alert_id),
    )
    conn.commit()
    return f" Acked monitor alert #{alert_id}."


def _in_cooldown(conn, row: dict) -> bool:
    last = row.get("last_fired_at")
    cooldown = int(row.get("cooldown_hours") or 0)
    if not last or cooldown <= 0:
        return False
    try:
        last_dt = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return datetime.now() - last_dt < timedelta(hours=cooldown)


def run_monitor_pass(conn, user_id: str, *, deliver: bool = True) -> dict:
    """Evaluate all enabled rules for one user.

    Returns a summary dict.  This is deterministic and LLM-free.  When
    ``deliver`` is True the caller is expected to push alerts to Discord
    / ntfy; the engine itself never touches the network.
    """
    uid = _fetch_user_id(conn, user_id)
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rules = list_monitor_rules(conn, uid, include_disabled=False)
    evaluated = 0
    fired = 0
    alert_ids: List[int] = []
    errors: List[str] = []
    for row in rules:
        evaluated += 1
        try:
            if _in_cooldown(conn, row):
                continue
            detail = evaluate_rule(conn, row, uid)
            if not detail:
                continue
            title = f"{row['kind']}: {row['name']}"
            alert_id = record_alert(
                conn, uid, int(row["id"]), row["kind"], row["severity"],
                title, detail,
            )
            fired += 1
            alert_ids.append(alert_id)
        except Exception as exc:
            errors.append(f"rule #{row.get('id')}: {type(exc).__name__}: {exc}")
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO monitor_run_log
        (user_id, started_at, finished_at, rules_evaluated, rules_fired, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            uid, started, finished, evaluated, fired,
            "; ".join(errors) if errors else None,
        ),
    )
    conn.commit()
    return {
        "user_id": uid,
        "rules_evaluated": evaluated,
        "rules_fired": fired,
        "alert_ids": alert_ids,
        "errors": errors,
    }


def run_monitor_pass_all(conn, *, deliver: bool = True) -> List[dict]:
    """Run one evaluation pass for every user that has monitor rules."""
    user_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT user_id FROM monitor_rules WHERE enabled = 1"
        ).fetchall()
    ]
    results = []
    for uid in user_ids:
        try:
            results.append(run_monitor_pass(conn, uid, deliver=deliver))
        except Exception as exc:
            logger.warning("monitor pass failed for user %s: %s", uid, exc)
    return results


__all__ = [
    "RULE_KINDS",
    "EVALUATORS",
    "list_monitor_rules",
    "add_monitor_rule",
    "update_monitor_rule",
    "delete_monitor_rule",
    "toggle_monitor_rule",
    "evaluate_rule",
    "record_alert",
    "list_alerts",
    "ack_alert",
    "run_monitor_pass",
    "run_monitor_pass_all",
]


# ============================================================
# Background watchdog
# ============================================================

MONITOR_POLL_INTERVAL_SECONDS = int(os.getenv("MONITOR_POLL_INTERVAL_SECONDS", "1800"))
MONITOR_DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")


async def _deliver_alerts(alert_ids: List[int], bot, channel_id) -> None:
    """Best-effort Discord delivery for newly fired alerts."""
    if not alert_ids or not bot or not channel_id:
        return
    try:
        channel = bot.get_channel(int(channel_id))
    except (TypeError, ValueError):
        return
    if channel is None:
        return
    from src.services.monitor import list_alerts  # local import safety
    # The caller already holds conn; we re-read alerts via the shared conn.
    import src.db.queries as queries
    alerts = list_alerts(queries.conn, "1", limit=len(alert_ids), include_acked=False)
    for alert in alerts:
        if alert["id"] not in alert_ids:
            continue
        try:
            await channel.send(
                f" **Monitor Alert** [{alert['severity'].upper()}] {alert['title']}\n"
                f"{alert['detail']}"
            )
            import sqlite3 as _sqlite
            queries.conn.execute(
                "UPDATE monitor_alerts SET delivered_discord = 1 WHERE id = ?",
                (alert["id"],),
            )
            queries.conn.commit()
        except Exception as exc:
            print(f" [MONITOR] discord delivery failed: {type(exc).__name__}: {exc}")


async def monitor_watchdog_loop():
    """Long-lived background task that evaluates monitor rules periodically.

    This is an asyncio task, not a thread.  It is safe to run alongside
    the Discord bot because it only reads the ledger and occasionally
    posts to Discord via the bot's channel.
    """
    import src.db.queries as queries
    from src.services.llm import send_push_alert as _send_push_alert

    print(" [MONITOR] watchdog loop starting.")
    while True:
        try:
            from src.services.monitor import run_monitor_pass_all, list_alerts
            results = run_monitor_pass_all(queries.conn, deliver=False)
            total_fired = sum(r.get("rules_fired", 0) for r in results)
            if total_fired:
                print(f" [MONITOR] pass fired {total_fired} alert(s).")
                # Deliver via Discord if a channel is configured.
                channel_id = os.getenv("DISCORD_CHANNEL_ID")
                bot = getattr(queries, "bot", None)
                for r in results:
                    await _deliver_alerts(r.get("alert_ids", []), bot, channel_id)
                # Push via ntfy for critical alerts.
                try:
                    for r in results:
                        for aid in r.get("alert_ids", []):
                            alerts = list_alerts(queries.conn, r["user_id"], limit=1, include_acked=False)
                            for a in alerts:
                                if a["id"] != aid:
                                    continue
                                if a["severity"] == "critical":
                                    try:
                                        await _send_push_alert(
                                            f"{a['title']}: {a['detail']}",
                                            title="Delilah Monitor",
                                            priority="high",
                                            user_id=r["user_id"],
                                        )
                                    except Exception:
                                        pass
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f" [MONITOR] watchdog iteration failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(MONITOR_POLL_INTERVAL_SECONDS)


def compile_natural_language_rule(text: str) -> Dict[str, Any]:
    """
    Deterministically compile plain English intent into a validated monitor rule.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Rule instruction cannot be empty.")

    cleaned = re.sub(r'^(?:please\s+)?(?:alert\s+me|notify\s+me|warn\s+me|alert|notify|warn)\s+(?:if|when|on)?\s*', '', raw, flags=re.IGNORECASE).strip()
    lowered = cleaned.lower()

    # 1. Low balance / projected balance
    # e.g. "balance drops below $1,000 in 14 days" or "checking falls under 500"
    bal_match = re.search(r'(?:balance|checking|liquid|cash)\s+(?:drops|falls|is|goes)?\s*(?:below|under)\s*[\$]?([0-9,]+(?:\.[0-9]{2})?)', lowered)
    if bal_match:
        threshold = float(bal_match.group(1).replace(",", ""))
        window_match = re.search(r'(?:in|within|over)\s+(\d+)\s*(?:days|d)', lowered)
        window_days = int(window_match.group(1)) if window_match else 14
        config = {"threshold": threshold, "window_days": window_days}
        ok, err, norm = _validate_rule("projected_balance_low", config)
        if not ok:
            raise ValueError(err)
        return {
            "kind": "projected_balance_low",
            "name": f"Low Balance (${threshold:,.0f} in {window_days}d)",
            "config": norm,
            "severity": "critical" if threshold < 500 else "warning",
            "cooldown_hours": 24,
        }

    # 2. Large deposit
    # e.g. "deposit over $2,000" or "deposits exceeding 1500"
    dep_match = re.search(r'deposit[s]?\s+(?:over|above|exceeding|more than|greater than)\s*[\$]?([0-9,]+(?:\.[0-9]{2})?)', lowered)
    if dep_match:
        threshold = float(dep_match.group(1).replace(",", ""))
        config = {"min_amount": threshold}
        ok, err, norm = _validate_rule("large_deposit", config)
        if not ok:
            raise ValueError(err)
        return {
            "kind": "large_deposit",
            "name": f"Large Deposit (>${threshold:,.0f})",
            "config": norm,
            "severity": "info",
            "cooldown_hours": 6,
        }

    # 2b. Duplicate charge detection
    # e.g. "Alert me on duplicate charges within 3 days" or "warn if charged twice at Starbucks"
    if "duplicate" in lowered or "double charge" in lowered or "charged twice" in lowered:
        w_m = re.search(r'(?:within|in|over)\s+(\d+)\s*(?:days|d)', lowered)
        window_days = int(w_m.group(1)) if w_m else 3
        amt_m = re.search(r'(?:over|above|exceeding|more than)?\s*[\$]?([0-9]+(?:\.[0-9]{1,2})?)', lowered)
        min_amount = float(amt_m.group(1)) if amt_m else 5.0

        merch_m = re.search(r'(?:from|at|for)\s+([A-Za-z0-9\s]+?)(?:\s+within|\s+over|\s+in|\s+more than|\s+above|\s*$)', cleaned, re.IGNORECASE)
        merchant = merch_m.group(1).strip() if merch_m else ""
        if merchant.lower() in {"duplicate", "double charge", "charges", "any"}:
            merchant = ""

        config = {"window_days": window_days, "min_amount": min_amount, "merchant": merchant}
        ok, err, norm = _validate_rule("duplicate_charge_detected", config)
        if not ok:
            raise ValueError(err)
        return {
            "kind": "duplicate_charge_detected",
            "name": f"Duplicate Charge Alert ({merchant or 'All Merchants'})",
            "config": norm,
            "severity": "warning",
            "cooldown_hours": 24,
        }

    # 3. Subscription price change
    # e.g. "Netflix price increases by 10%" or "Spotify increases"
    sub_match = re.search(r'([a-zA-Z0-9\s]+?)\s+(?:price|subscription)?\s*(?:increases|jumps|hikes|rises)(?:\s+by\s+([0-9]+(?:\.[0-9]+)?))?%?', lowered)
    if sub_match and "spend" not in sub_match.group(1) and "balance" not in sub_match.group(1):
        merch = sub_match.group(1).replace("my", "").strip().title()
        pct = float(sub_match.group(2)) if sub_match.group(2) else 10.0
        config = {"merchant": merch, "pct_increase": pct}
        ok, err, norm = _validate_rule("subscription_price_changed", config)
        if not ok:
            raise ValueError(err)
        return {
            "kind": "subscription_price_changed",
            "name": f"{merch} Price Increase",
            "config": norm,
            "severity": "warning",
            "cooldown_hours": 72,
        }

    # 4. Unusual transaction
    if "unusual" in lowered or "anomaly" in lowered or "irregular" in lowered:
        std_match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(?:stddev|sigmas|standard deviation)', lowered)
        multiplier = float(std_match.group(1)) if std_match else 3.0
        config = {"stddev_multiplier": multiplier, "window_days": 90}
        ok, err, norm = _validate_rule("unusual_transaction", config)
        if not ok:
            raise ValueError(err)
        return {
            "kind": "unusual_transaction",
            "name": f"Unusual Spend ({multiplier:.1f}σ)",
            "config": norm,
            "severity": "warning",
            "cooldown_hours": 12,
        }

    # 5. Category spending exceeded
    # e.g. "spend more than $200 on dining out in 7 days" or "groceries exceeds $400 this month"
    cat_match1 = re.search(r'(?:spend|spending)\s+(?:more than|over|above|exceeding)\s*[\$]?([0-9,]+(?:\.[0-9]{2})?)\s+(?:on|for|in)\s+([a-zA-Z\s]+)', lowered)
    cat_match2 = re.search(r'([a-zA-Z\s]+?)\s+(?:exceeds|over|more than|above)\s*[\$]?([0-9,]+(?:\.[0-9]{2})?)', lowered)

    if cat_match1:
        limit = float(cat_match1.group(1).replace(",", ""))
        category_raw = cat_match1.group(2)
    elif cat_match2:
        category_raw = cat_match2.group(1)
        limit = float(cat_match2.group(2).replace(",", ""))
    else:
        category_raw = None
        limit = 0.0

    if category_raw:
        # Extract window if present in text
        window_days = 30
        if "this week" in lowered or "weekly" in lowered:
            window_days = 7
        elif "this month" in lowered or "monthly" in lowered:
            window_days = 30
        else:
            w_m = re.search(r'(?:in|over|within)\s+(\d+)\s*(?:days|d)', lowered)
            if w_m:
                window_days = int(w_m.group(1))

        # Clean category name
        category_clean = re.sub(r'\s+(?:in|over|within|this week|this month).*$', '', category_raw).strip().title()
        config = {"category": category_clean, "limit": limit, "window_days": window_days}
        ok, err, norm = _validate_rule("category_spend_exceeded", config)
        if not ok:
            raise ValueError(err)
        return {
            "kind": "category_spend_exceeded",
            "name": f"{category_clean} Cap (${limit:,.0f}/{window_days}d)",
            "config": norm,
            "severity": "warning",
            "cooldown_hours": 24,
        }

    raise ValueError(
        f"Could not parse natural language monitor rule from: '{text}'.\n"
        "Examples of supported instructions:\n"
        "  • 'Alert me if dining exceeds $200 this week'\n"
        "  • 'Notify if balance falls below $1,000 in 14 days'\n"
        "  • 'Alert on deposits over $2,500'\n"
        "  • 'Warn if Netflix price increases by 10%'\n"
        "  • 'Detect unusual transactions at 2.5 stddev'"
    )


def add_monitor_rule_from_nl(conn: sqlite3.Connection, user_id: str, instruction: str) -> Dict[str, Any]:
    """Compile a natural language instruction and add the resulting rule to monitor_rules."""
    spec = compile_natural_language_rule(instruction)
    ok, msg, new_id = add_monitor_rule(
        conn,
        user_id=user_id,
        name=spec["name"],
        kind=spec["kind"],
        config=spec["config"],
        severity=spec.get("severity", "warning"),
        cooldown_hours=spec.get("cooldown_hours", 24),
    )
    if not ok:
        raise ValueError(msg)
    spec["id"] = new_id
    return spec
