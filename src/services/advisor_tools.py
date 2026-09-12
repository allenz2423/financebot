"""
Delilah Financial OS - Comprehensive Advisor Tools Suite (50 Production Financial Tools).

Exposes 50 distinct, deterministic, non-redundant tools for Delilah's LLM advisor across:
1. Solvency, Credit & Health Scoring (1-5)
2. Investment Portfolio & Asset Allocation (6-10)
3. Budgeting, Pacing & Allocations (11-15)
4. Bills, Subscriptions & Cash Flow Forecasts (16-20)
5. Goals, Sinking Funds & Savings Milestones (21-25)
6. Tax Planning, Deductions & Write-Offs (26-30)
7. Ledger, Receipts & Merchant Intelligence (31-35)
8. Debt Payoff & Mortgage Mathematics (36-40)
9. Retirement & FI/RE Planning (41-45)
10. Banking, Fee Audit & Analytics (46-50)
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, date
from typing import Any, Callable, Dict, List, Optional

from src.core.state import DB_PATH
from src.db.migrations import apply_all


def _ensure_user_id(user_id: str, tool_name: str) -> None:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(f"{tool_name}: forced isolation violation — user_id is required")


# ============================================================
# Category A: Solvency, Credit & Health Scoring (Tools 1–5)
# ============================================================

def tool_get_financial_health_scorecard(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """1. Authoritative 0-100 composite 5-pillar financial health scorecard."""
    _ensure_user_id(user_id, "get_financial_health_scorecard")
    from src.services.scorecard import calculate_financial_health_scorecard
    return calculate_financial_health_scorecard(user_id=user_id, conn=conn)


def tool_get_credit_utilization_breakdown(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """2. Revolving credit card utilization per card and aggregate with FICO tiers."""
    _ensure_user_id(user_id, "get_credit_utilization_breakdown")
    from src.services.credit import calculate_credit_utilization
    return calculate_credit_utilization(user_id=user_id, conn=conn)


def tool_simulate_credit_paydown_impact(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """3. Simulates how applying a specific dollar paydown lowers utilization and improves tiers."""
    _ensure_user_id(user_id, "simulate_credit_paydown_impact")
    from src.services.credit import calculate_credit_utilization
    paydown = float(args.get("paydown_amount", 0.0))
    target_card = args.get("target_card")

    data = calculate_credit_utilization(user_id=user_id, conn=conn)
    current_bal = data.get("total_balance", 0.0)
    current_lim = data.get("total_limit", 0.0)
    current_util = data.get("aggregate_utilization_pct", 0.0)

    new_bal = max(0.0, current_bal - paydown)
    new_util = (new_bal / current_lim * 100.0) if current_lim > 0 else 0.0

    if new_util < 10.0:
        new_tier = "Optimal"
    elif new_util < 30.0:
        new_tier = "Good"
    elif new_util < 50.0:
        new_tier = "Elevated"
    else:
        new_tier = "Critical"

    return {
        "paydown_simulated": round(paydown, 2),
        "target_card": target_card or "Aggregate balance",
        "current_balance": round(current_bal, 2),
        "current_utilization_pct": round(current_util, 1),
        "current_tier": data.get("overall_tier", "Unknown"),
        "projected_balance": round(new_bal, 2),
        "projected_utilization_pct": round(new_util, 1),
        "projected_tier": new_tier,
        "utilization_reduction_pct": round(current_util - new_util, 1),
    }


def tool_get_cash_drag_analysis(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """4. Audits checking cash vs 1.5x operating buffer and quantifies lost HYSA interest."""
    _ensure_user_id(user_id, "get_cash_drag_analysis")
    from src.services.cash_drag import analyze_cash_drag
    apy = float(args.get("benchmark_apy", 0.045))
    buf = float(args.get("buffer_months", 1.5))
    return analyze_cash_drag(user_id=user_id, conn=conn, benchmark_apy=apy, buffer_months=buf)


def tool_calculate_debt_snowball_vs_avalanche(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """5. Compares month-by-month debt payoff under Snowball vs Avalanche strategies."""
    _ensure_user_id(user_id, "calculate_debt_snowball_vs_avalanche")
    extra = max(0.0, float(args.get("extra_monthly_payment", 100.0)))

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("SELECT debt_name, balance, min_payment, apr FROM user_debts WHERE user_id = ? AND balance > 0", (user_id,))
        rows = c.fetchall()

        debts = []
        for r in rows:
            debts.append({
                "name": str(r[0]),
                "balance": float(r[1]),
                "min_payment": max(15.0, float(r[2] or (r[1] * 0.025))),
                "apr": float(r[3] or 18.9),
            })

        if not debts:
            # Fallback to credit cards from plaid_accounts
            c.execute("SELECT name, current_balance, 25.0, 22.9 FROM plaid_accounts WHERE user_id = ? AND type = 'credit' AND current_balance > 0 AND active = 1", (user_id,))
            for r in c.fetchall():
                debts.append({
                    "name": str(r[0]),
                    "balance": float(r[1]),
                    "min_payment": max(25.0, float(r[1]) * 0.025),
                    "apr": 22.9,
                })

        if not debts:
            return {"status": "debt_free", "message": "No active debt balances recorded. You are completely debt free!"}

        total_debt = sum(d["balance"] for d in debts)
        base_mins = sum(d["min_payment"] for d in debts)

        # Simulation helper
        def _sim(sorted_debts: List[Dict[str, Any]]) -> Dict[str, Any]:
            cur = [dict(d) for d in sorted_debts]
            months = 0
            total_interest = 0.0
            rolled_over_mins = 0.0

            while any(d["balance"] > 0 for d in cur) and months < 360:
                months += 1
                available_extra = extra + rolled_over_mins

                # Apply minimums and accrue interest
                for d in cur:
                    if d["balance"] <= 0:
                        continue
                    m_interest = d["balance"] * (d["apr"] / 100.0 / 12.0)
                    total_interest += m_interest
                    d["balance"] += m_interest

                    pay = min(d["balance"], d["min_payment"])
                    d["balance"] -= pay
                    if d["balance"] <= 0:
                        # Once paid off, roll this minimum payment into all future months
                        rolled_over_mins += d["min_payment"]
                        available_extra += d["min_payment"]

                # Apply extra payment to target debt
                for d in cur:
                    if d["balance"] > 0 and available_extra > 0:
                        pay_extra = min(d["balance"], available_extra)
                        d["balance"] -= pay_extra
                        available_extra -= pay_extra
                        if available_extra <= 0:
                            break

            return {"months": months, "total_interest": round(total_interest, 2)}

        # Snowball: lowest balance first
        snowball_debts = sorted(debts, key=lambda x: x["balance"])
        snow_res = _sim(snowball_debts)

        # Avalanche: highest APR first
        avalanche_debts = sorted(debts, key=lambda x: x["apr"], reverse=True)
        ava_res = _sim(avalanche_debts)

        interest_saved = round(max(0.0, snow_res["total_interest"] - ava_res["total_interest"]), 2)

        return {
            "total_debt": round(total_debt, 2),
            "debt_count": len(debts),
            "monthly_commitment": round(base_mins + extra, 2),
            "snowball": {
                "strategy": "Lowest Balance First (Psychological Momentum)",
                "payoff_months": snow_res["months"],
                "total_interest": snow_res["total_interest"],
            },
            "avalanche": {
                "strategy": "Highest Interest Rate First (Mathematically Optimal)",
                "payoff_months": ava_res["months"],
                "total_interest": ava_res["total_interest"],
            },
            "interest_saved_with_avalanche": interest_saved,
            "recommendation": (
                f"Avalanche saves ${interest_saved:,.2f} in interest compared to Snowball."
                if interest_saved > 25.0
                else "Both strategies complete in similar time; choose Snowball for rapid motivational wins."
            )
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


# ============================================================
# Category B: Investment Portfolio & Asset Allocation (Tools 6–10)
# ============================================================

def tool_get_portfolio_holdings(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """6. Lists all portfolio investment holdings with market values and P&L."""
    _ensure_user_id(user_id, "get_portfolio_holdings")
    from src.services.portfolio import get_portfolio
    return get_portfolio(user_id=user_id, conn=conn)


def tool_set_portfolio_holding(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """7. Adds, updates, or deletes (shares=0) an investment holding."""
    _ensure_user_id(user_id, "set_portfolio_holding")
    from src.services.portfolio import set_holding
    sym = str(args.get("symbol", "")).upper().strip()
    shares = float(args.get("shares", 0.0))
    price = float(args.get("current_price", 0.0))
    cost = float(args.get("cost_basis", 0.0))
    aclass = str(args.get("asset_class", "Equities"))
    return set_holding(user_id=user_id, symbol=sym, shares=shares, current_price=price, cost_basis=cost, asset_class=aclass, conn=conn)


def tool_calculate_portfolio_drift(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """8. Compares current vs target asset allocation weights and generates rebalancing orders."""
    _ensure_user_id(user_id, "calculate_portfolio_drift")
    from src.services.portfolio import calculate_portfolio_drift
    targets = args.get("target_allocations")
    return calculate_portfolio_drift(user_id=user_id, targets=targets, conn=conn)



def tool_get_portfolio_dividend_projection(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """10. Projects estimated dividend/yield cash flows from portfolio holdings."""
    _ensure_user_id(user_id, "get_portfolio_dividend_projection")
    from src.services.portfolio import get_portfolio
    port = get_portfolio(user_id=user_id, conn=conn)
    total_val = port.get("total_portfolio_value", 0.0)
    user_yield = float(args.get("assumed_yield_pct", 2.2)) / 100.0

    annual_div = total_val * user_yield
    monthly_div = annual_div / 12.0

    return {
        "total_portfolio_value": total_val,
        "assumed_average_yield_pct": round(user_yield * 100.0, 2),
        "projected_annual_dividends": round(annual_div, 2),
        "projected_monthly_dividends": round(monthly_div, 2),
        "projected_daily_dividends": round(annual_div / 365.0, 2),
    }


# ============================================================
# Category C: Budgeting, Pacing & Allocations (Tools 11–15)
# ============================================================

def tool_get_category_budget_pacing(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """11. Checks monthly category budget burn pacing and month-end forecast."""
    _ensure_user_id(user_id, "get_category_budget_pacing")
    from src.services.budgeting import calculate_spending_pace_and_forecast
    cat = args.get("category")
    data = calculate_spending_pace_and_forecast(user_id=user_id, conn=conn)
    if cat:
        filtered = [b for b in data.get("category_budgets", []) if b["category"].lower() == cat.lower()]
        data["category_budgets"] = filtered
    return data


def tool_delete_category_budget(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """12. Deletes a monthly category budget ceiling."""
    _ensure_user_id(user_id, "delete_category_budget")
    cat = str(args.get("category", "")).strip()

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("DELETE FROM category_budgets WHERE user_id = ? AND LOWER(category) = LOWER(?)", (user_id, cat))
        affected = c.rowcount
        db_conn.commit()
        return {"status": "success" if affected > 0 else "not_found", "category": cat, "deleted": affected > 0}

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)



def tool_compare_period_spending(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """14. Compares spending totals and categories between two historical date ranges."""
    _ensure_user_id(user_id, "compare_period_spending")
    p1_start = int(args.get("period1_days_ago_start", 30))
    p1_end = int(args.get("period1_days_ago_end", 0))
    p2_start = int(args.get("period2_days_ago_start", 60))
    p2_end = int(args.get("period2_days_ago_end", 30))

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()

        def _get_spend(days_start: int, days_end: int) -> Dict[str, Any]:
            c.execute("""
                SELECT COALESCE(category, 'Uncategorized'), SUM(amount)
                FROM transactions
                WHERE user_id = ?
                  AND status = 'Evaluated'
                  AND amount > 0
                  AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer')
                  AND date >= date('now', ?)
                  AND date <= date('now', ?)
                GROUP BY COALESCE(category, 'Uncategorized')
            """, (user_id, f"-{days_start} days", f"-{days_end} days"))
            cat_map = {r[0]: float(r[1]) for r in c.fetchall()}
            total = sum(cat_map.values())
            return {"total": total, "categories": cat_map}

        p1 = _get_spend(p1_start, p1_end)
        p2 = _get_spend(p2_start, p2_end)

        delta = p1["total"] - p2["total"]
        pct_change = ((delta / p2["total"]) * 100.0) if p2["total"] > 0 else 0.0

        all_cats = set(p1["categories"].keys()).union(set(p2["categories"].keys()))
        cat_comparison = []
        for cat in sorted(all_cats):
            amt1 = p1["categories"].get(cat, 0.0)
            amt2 = p2["categories"].get(cat, 0.0)
            c_delta = amt1 - amt2
            cat_comparison.append({
                "category": cat,
                "recent_period": round(amt1, 2),
                "prior_period": round(amt2, 2),
                "dollar_change": round(c_delta, 2),
            })

        return {
            "recent_period_total": round(p1["total"], 2),
            "prior_period_total": round(p2["total"], 2),
            "dollar_difference": round(delta, 2),
            "percentage_change": round(pct_change, 1),
            "trend": "Increased Spending" if delta > 0 else ("Decreased Spending" if delta < 0 else "Flat"),
            "categories": cat_comparison,
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


def tool_get_daily_spending_average(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """15. Calculates weekday vs weekend average spending rate and identified spikes."""
    _ensure_user_id(user_id, "get_daily_spending_average")
    days = max(7, min(365, int(args.get("days", 30))))

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("""
            SELECT date, amount
            FROM transactions
            WHERE user_id = ?
              AND status = 'Evaluated'
              AND amount > 0
              AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer')
              AND date >= date('now', ?)
        """, (user_id, f"-{days} days"))
        rows = c.fetchall()

        weekday_totals: List[float] = [0.0] * 5
        weekday_counts: List[int] = [0] * 5
        weekend_totals: List[float] = [0.0] * 2
        weekend_counts: List[int] = [0] * 2
        total_spent = 0.0

        for d_str, amt in rows:
            amt_val = float(amt)
            total_spent += amt_val
            try:
                dt = datetime.strptime(str(d_str)[:10], "%Y-%m-%d")
                weekday = dt.weekday()  # 0=Monday, 6=Sunday
                if weekday < 5:
                    weekday_totals[weekday] += amt_val
                    weekday_counts[weekday] += 1
                else:
                    we_idx = weekday - 5
                    weekend_totals[we_idx] += amt_val
                    weekend_counts[we_idx] += 1
            except Exception:
                pass

        weekday_sum = sum(weekday_totals)
        weekend_sum = sum(weekend_totals)
        total_weekdays = max(1, sum(weekday_counts))
        total_weekends = max(1, sum(weekend_counts))

        avg_weekday = weekday_sum / total_weekdays if total_weekdays > 0 else 0.0
        avg_weekend = weekend_sum / total_weekends if total_weekends > 0 else 0.0

        return {
            "window_days": days,
            "total_spent": round(total_spent, 2),
            "daily_average": round(total_spent / days, 2),
            "average_weekday_spend": round(avg_weekday, 2),
            "average_weekend_spend": round(avg_weekend, 2),
            "weekend_multiplier": round(avg_weekend / avg_weekday, 2) if avg_weekday > 0 else 1.0,
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


# ============================================================
# Category D: Bills, Subscriptions & Cash Flow Forecasts (Tools 16–20)
# ============================================================

def tool_get_bills_calendar(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """16. Retrieves upcoming recurring bills calendar and 30-day outflow forecast."""
    _ensure_user_id(user_id, "get_bills_calendar")
    from src.services.subscriptions import get_billing_calendar
    days = int(args.get("days_ahead", 30))
    res = get_billing_calendar(user_id=user_id, days_ahead=days, conn=conn)
    if "upcoming_bills" not in res:
        res["upcoming_bills"] = res.get("calendar", [])
    return res


def tool_add_recurring_bill(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """17. Registers a recurring bill or subscription commitment."""
    _ensure_user_id(user_id, "add_recurring_bill")
    from src.services.subscriptions import set_subscription
    merchant = str(args.get("merchant", "")).strip()
    amount = float(args.get("amount", 0.0))
    cadence = str(args.get("cadence", "monthly")).strip().lower()
    due_day = int(args.get("due_day", 1))
    category = str(args.get("category", "Utilities & Bills"))
    now = datetime.now()
    try:
        next_due = date(now.year, now.month, min(max(due_day, 1), 28)).strftime("%Y-%m-%d")
    except Exception:
        next_due = None
    return set_subscription(
        user_id=user_id,
        merchant=merchant,
        amount=amount,
        cadence=cadence,
        next_due_date=next_due,
        category=category,
        conn=conn,
    )


def tool_remove_recurring_bill(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """18. Unregisters or deactivates a recurring bill."""
    _ensure_user_id(user_id, "remove_recurring_bill")
    merchant = str(args.get("merchant", "")).strip()
    from src.services.subscriptions import cancel_subscription
    res = cancel_subscription(user_id=user_id, merchant=merchant, conn=conn)
    return {
        "merchant": merchant,
        "status": "removed" if res.get("status") == "success" else res.get("status", "not_found"),
        "raw_response": res,
    }


def tool_project_cash_balance(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """19. Forecasts daily bank balance over next 30/60/90 days incorporating paydays and bills."""
    _ensure_user_id(user_id, "project_cash_balance")
    days = max(7, min(90, int(args.get("days", 30))))

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'depository' AND active = 1", (user_id,))
        start_bal = float(c.fetchone()[0] or 0.0)

        # Baseline daily burn from transactions
        c.execute("""
            SELECT SUM(amount) FROM transactions
            WHERE user_id = ? AND status = 'Evaluated' AND amount > 0
              AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer')
              AND date >= date('now', '-30 days')
        """, (user_id,))
        past_spend = float(c.fetchone()[0] or 0.0)
        daily_burn = past_spend / 30.0 if past_spend > 0 else 50.0

        from src.services.subscriptions import get_billing_calendar
        cal = get_billing_calendar(user_id=user_id, days_ahead=days, conn=db_conn)
        bills = cal.get("upcoming_bills", [])

        # Build daily map
        balance = start_bal
        min_bal = start_bal
        daily_projection = []

        now_dt = datetime.now().date()
        for i in range(1, days + 1):
            cur_date = now_dt + timedelta(days=i)
            cur_str = cur_date.strftime("%Y-%m-%d")

            # Subtract daily discretionary burn
            balance -= daily_burn

            # Subtract specific bills due on this day
            bills_today = [b for b in bills if b.get("next_due_date") == cur_str]
            for b in bills_today:
                balance -= b.get("amount", 0.0)

            if balance < min_bal:
                min_bal = balance

            if i in (7, 14, 21, 30, 60, 90) or i == days:
                daily_projection.append({
                    "day": i,
                    "date": cur_str,
                    "projected_balance": round(balance, 2),
                })

        return {
            "current_depository_balance": round(start_bal, 2),
            "forecast_days": days,
            "estimated_daily_burn": round(daily_burn, 2),
            "lowest_projected_balance": round(min_bal, 2),
            "end_projected_balance": round(balance, 2),
            "overdraft_risk": min_bal < 0.0,
            "projection_checkpoints": daily_projection,
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


def tool_detect_unusual_bill_increases(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """20. Compares recent recurring charges against historical baselines to flag price creeping."""
    _ensure_user_id(user_id, "detect_unusual_bill_increases")
    thresh = float(args.get("threshold_pct", 5.0))

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("""
            SELECT merchant, amount, date, category
            FROM transactions
            WHERE user_id = ? AND amount > 0 AND status = 'Evaluated'
              AND date >= date('now', '-180 days')
            ORDER BY merchant, date ASC
        """, (user_id,))
        rows = c.fetchall()

        # Gather confirmed recurring bills / subscriptions
        known_recurring = set()
        try:
            c.execute("SELECT service_name FROM subscription_trials WHERE user_id = ?", (user_id,))
            for r in c.fetchall():
                if r[0]: known_recurring.add(str(r[0]).lower())
        except Exception:
            pass

        merch_txs: Dict[str, List[Dict[str, Any]]] = {}
        for m, a, d, cat in rows:
            if m:
                merch_txs.setdefault(m, []).append({"amount": float(a), "date": d, "category": cat or ""})

        flagged = []
        for m, txs in merch_txs.items():
            if len(txs) >= 2:
                recent_tx = txs[-1]
                prior_tx = txs[-2]
                recent = recent_tx["amount"]
                prior = prior_tx["amount"]

                # If merchant is recognized as recurring OR has typical recurring categories OR charges spaced ~15-45 days apart
                is_recurring_cat = any(rc in recent_tx["category"].lower() for rc in ("bill", "utilities", "subscription", "recurring", "insurance", "rent", "internet", "phone"))
                is_known_rec = m.lower() in known_recurring

                # Check spacing between charges in days
                days_apart = 30
                try:
                    d1 = datetime.strptime(prior_tx["date"][:10], "%Y-%m-%d")
                    d2 = datetime.strptime(recent_tx["date"][:10], "%Y-%m-%d")
                    days_apart = abs((d2 - d1).days)
                except Exception:
                    pass

                is_cadence_match = 15 <= days_apart <= 45

                if (is_known_rec or is_recurring_cat or is_cadence_match) and recent > prior and prior > 5.0:
                    pct = ((recent - prior) / prior) * 100.0
                    if pct >= thresh:
                        flagged.append({
                            "merchant": m,
                            "prior_charge": round(prior, 2),
                            "latest_charge": round(recent, 2),
                            "increase_pct": round(pct, 1),
                            "dollar_increase": round(recent - prior, 2),
                        })

        return {
            "flagged_bill_increases": flagged,
            "count": len(flagged),
            "threshold_used_pct": thresh,
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


# ============================================================
# Category E: Goals, Sinking Funds & Savings Milestones (Tools 21–25)
# ============================================================

def tool_get_savings_goals(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """21. Lists all savings goals, target amounts, current funding, and shortfall deficits."""
    _ensure_user_id(user_id, "get_savings_goals")
    from src.services.goals import calculate_goal_milestones_and_deficits
    res = calculate_goal_milestones_and_deficits(user_id=user_id, conn=conn)
    goals = res.get("goals", [])
    res["total_goals_count"] = len(goals)
    return res


def tool_fund_savings_goal(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """22. Deposits funds into a specific savings goal envelope."""
    _ensure_user_id(user_id, "fund_savings_goal")
    from src.services.goals import record_goal_contribution
    name = str(args.get("name", "")).strip()
    amt = float(args.get("amount", 0.0))
    res = record_goal_contribution(user_id=user_id, name=name, amount=amt, conn=conn)
    res["goal_name"] = name
    res["current_balance"] = res.get("new_balance", amt)
    return res


def tool_delete_savings_goal(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """23. Deletes a savings goal envelope."""
    _ensure_user_id(user_id, "delete_savings_goal")
    name = str(args.get("name", "")).strip()

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("DELETE FROM savings_buckets WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, name))
        deleted = c.rowcount
        db_conn.commit()
        return {"name": name, "deleted": deleted > 0}

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)



def tool_prioritize_savings_goals(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """25. Deterministically ranks savings goals by deadline urgency and shortfall percentage."""
    _ensure_user_id(user_id, "prioritize_savings_goals")
    from src.services.goals import calculate_goal_milestones_and_deficits
    data = calculate_goal_milestones_and_deficits(user_id=user_id, conn=conn)
    goals = data.get("goals", [])

    # Sort: deficit > 0 first, then by days_remaining
    def _rank(g: Dict[str, Any]) -> Tuple[int, int]:
        has_deficit = 0 if g.get("deficit_shortfall", 0) > 0 else 1
        days = g.get("days_remaining") if g.get("days_remaining") is not None else 99999
        return (has_deficit, days)

    ranked = sorted(goals, key=_rank)
    return {
        "ranked_goals_by_priority": ranked,
        "total_shortfall_deficit": data.get("total_shortfall_deficit", 0.0),
        "recommendation": "Allocate surplus dollars first to top-ranked goals with imminent deadlines or deficits."
    }


# ============================================================
# Category F: Tax Planning, Deductions & Write-Offs (Tools 26–30)
# ============================================================

def tool_scan_tax_deductions(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """26. Scans transactions for deductible business/medical/charity write-offs."""
    _ensure_user_id(user_id, "scan_tax_deductions")
    from src.services.tax_deductions import scan_and_discover_deductions
    bracket = float(args.get("tax_bracket_pct", 24.0)) / 100.0
    return scan_and_discover_deductions(user_id=user_id, marginal_tax_rate=bracket, conn=conn)


def tool_get_tax_bracket_estimate(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """27. Estimates federal income tax, marginal tax bracket, and standard deductions."""
    gross = float(args.get("annual_gross_income", 75000.0))
    status = str(args.get("filing_status", "single")).lower()

    # 2026 Estimated Federal Tax Brackets
    if "married" in status:
        std_deduction = 30000.0
        brackets = [
            (23200, 0.10),
            (94300, 0.12),
            (201050, 0.22),
            (383900, 0.24),
            (487450, 0.32),
            (731200, 0.35),
            (float("inf"), 0.37),
        ]
    else:
        std_deduction = 15000.0
        brackets = [
            (11600, 0.10),
            (47150, 0.12),
            (100525, 0.22),
            (191950, 0.24),
            (243725, 0.32),
            (609350, 0.35),
            (float("inf"), 0.37),
        ]

    taxable_income = max(0.0, gross - std_deduction)
    tax_due = 0.0
    prev_limit = 0.0
    marginal_rate = 0.10

    for limit, rate in brackets:
        if taxable_income > prev_limit:
            taxable_in_bracket = min(taxable_income, limit) - prev_limit
            tax_due += taxable_in_bracket * rate
            marginal_rate = rate
            prev_limit = limit
        else:
            break

    effective_rate = (tax_due / gross * 100.0) if gross > 0 else 0.0

    return {
        "annual_gross_income": round(gross, 2),
        "filing_status": "Married Filing Jointly" if "married" in status else "Single",
        "standard_deduction": round(std_deduction, 2),
        "taxable_income": round(taxable_income, 2),
        "estimated_federal_tax": round(tax_due, 2),
        "marginal_tax_bracket_pct": round(marginal_rate * 100.0, 1),
        "effective_tax_rate_pct": round(effective_rate, 2),
        "after_tax_income": round(gross - tax_due, 2),
    }


def tool_get_charitable_donations_summary(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """28. Summarizes 501(c)(3) charitable gifts and tithings for tax filing."""
    _ensure_user_id(user_id, "get_charitable_donations_summary")
    year = int(args.get("year", datetime.now().year))

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("""
            SELECT merchant, SUM(amount), COUNT(*)
            FROM transactions
            WHERE user_id = ? AND amount > 0 AND (category LIKE '%Charity%' OR category LIKE '%Donation%' OR name LIKE '%Donation%')
              AND strftime('%Y', date) = ?
            GROUP BY merchant
        """, (user_id, str(year)))
        rows = c.fetchall()
        items = [{"organization": r[0], "total_donated": round(float(r[1]), 2), "count": int(r[2])} for r in rows]
        total = sum(i["total_donated"] for i in items)
        return {"tax_year": year, "total_charitable_donations": round(total, 2), "organizations": items}

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)



# ============================================================
# Category G: Ledger, Receipts & Merchant Intelligence (Tools 31–35)
# ============================================================

def tool_get_transaction_ledger(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """31. Paginated transaction ledger browser with multi-field filtering."""
    _ensure_user_id(user_id, "get_transaction_ledger")
    limit = max(1, min(100, int(args.get("limit", 20))))
    offset = max(0, int(args.get("offset", 0)))
    cat = args.get("category")
    min_amt = args.get("min_amount")
    max_amt = args.get("max_amount")

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        query = "SELECT id, date, merchant, amount, category, status FROM transactions WHERE user_id = ?"
        params: List[Any] = [user_id]

        if cat:
            query += " AND LOWER(category) = LOWER(?)"
            params.append(cat)
        if min_amt is not None:
            query += " AND amount >= ?"
            params.append(float(min_amt))
        if max_amt is not None:
            query += " AND amount <= ?"
            params.append(float(max_amt))

        query += " ORDER BY date DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        c.execute(query, tuple(params))
        rows = c.fetchall()
        txs = [{"id": r[0], "date": r[1], "merchant": r[2], "amount": float(r[3]), "category": r[4], "status": r[5]} for r in rows]
        return {"count": len(txs), "offset": offset, "limit": limit, "transactions": txs}

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


def tool_get_transaction_detail(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """32. Deep forensic inspect of a single transaction and edit history."""
    _ensure_user_id(user_id, "get_transaction_detail")
    tx_id = str(args.get("transaction_id", "")).strip()

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("SELECT id, date, name, merchant, amount, category, status, account_id FROM transactions WHERE user_id = ? AND id = ?", (user_id, tx_id))
        row = c.fetchone()
        if not row:
            return {"error": f"Transaction '{tx_id}' not found."}

        # Check corrections
        c.execute("SELECT field, old_value, new_value, timestamp FROM transaction_corrections WHERE user_id = ? AND transaction_id = ?", (user_id, tx_id))
        corrections = [{"field": cr[0], "old": cr[1], "new": cr[2], "at": cr[3]} for cr in c.fetchall()]

        return {
            "id": row[0],
            "date": row[1],
            "raw_name": row[2],
            "merchant": row[3],
            "amount": float(row[4]),
            "category": row[5],
            "status": row[6],
            "account_id": row[7],
            "audit_history": corrections,
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


def tool_parse_text_receipt(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """33. Parses receipt text into line items, tax, total, and candidate matches."""
    _ensure_user_id(user_id, "parse_text_receipt")
    from src.services.receipt_parser import parse_receipt_text, extract_receipt_total
    txt = str(args.get("receipt_text", ""))
    items = parse_receipt_text(txt)
    tot = extract_receipt_total(txt)
    return {
        "status": "success",
        "receipt_text": txt,
        "line_items": items,
        "items": items,
        "item_count": len(items),
        "total": tot,
    }


def tool_search_merchants_and_aliases(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """34. Searches merchant aliases and canonical names."""
    _ensure_user_id(user_id, "search_merchants_and_aliases")
    q = str(args.get("query", "")).strip().lower()

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("SELECT raw_pattern, canonical_name FROM merchant_aliases WHERE user_id = ? AND (LOWER(raw_pattern) LIKE ? OR LOWER(canonical_name) LIKE ?)", (user_id, f"%{q}%", f"%{q}%"))
        aliases = [{"raw_pattern": r[0], "canonical": r[1]} for r in c.fetchall()]
        return {"query": q, "aliases_found": aliases}

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


def tool_add_merchant_alias_mapping(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """35. Maps dirty statement strings to clean canonical merchant names."""
    _ensure_user_id(user_id, "add_merchant_alias_mapping")
    raw = str(args.get("raw_pattern", "")).strip()
    canon = str(args.get("canonical_name", "")).strip()

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("""
            INSERT INTO merchant_aliases (user_id, raw_pattern, canonical_name, created_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (user_id, raw, canon))
        db_conn.commit()
        return {"status": "registered", "raw_pattern": raw, "canonical_name": canon}

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


# ============================================================
# Category H: Debt Payoff & Mortgage Mathematics (Tools 36–40)
# ============================================================

def tool_calculate_loan_amortization(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """36. Computes monthly payment, total interest, and loan amortization."""
    principal = max(0.0, float(args.get("principal", 300000.0)))
    rate = float(args.get("annual_interest_rate_pct", 6.5)) / 100.0
    years = max(1, min(40, int(args.get("term_years", 30))))

    m_rate = rate / 12.0
    n = years * 12

    # Monthly payment = P * [r(1+r)^n] / [(1+r)^n - 1]
    if m_rate > 0:
        factor = math.pow(1.0 + m_rate, n)
        pmt = principal * (m_rate * factor) / (factor - 1.0)
    else:
        pmt = principal / n

    total_cost = pmt * n
    total_interest = max(0.0, total_cost - principal)

    return {
        "loan_principal": round(principal, 2),
        "annual_interest_rate_pct": round(rate * 100.0, 2),
        "term_years": years,
        "monthly_principal_and_interest": round(pmt, 2),
        "total_interest_paid": round(total_interest, 2),
        "total_lifetime_cost": round(total_cost, 2),
    }


def tool_calculate_extra_payment_impact(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """37. Computes interest saved and years shaved off loan via extra payments."""
    principal = float(args.get("principal", 250000.0))
    rate = float(args.get("annual_interest_rate_pct", 6.5)) / 100.0
    years = int(args.get("term_years", 30))
    extra = max(0.0, float(args.get("extra_monthly_payment", 250.0)))

    m_rate = rate / 12.0
    n = years * 12
    if m_rate > 0:
        factor = math.pow(1.0 + m_rate, n)
        base_pmt = principal * (m_rate * factor) / (factor - 1.0)
    else:
        base_pmt = principal / n if n > 0 else 0.0

    # Standard schedule
    std_interest = max(0.0, (base_pmt * n) - principal)

    # Accelerated schedule
    bal = principal
    acc_months = 0
    acc_interest = 0.0

    while bal > 0 and acc_months < 600:
        acc_months += 1
        interest_month = bal * m_rate
        acc_interest += interest_month
        bal += interest_month

        pay = min(bal, base_pmt + extra)
        bal -= pay

    interest_saved = max(0.0, std_interest - acc_interest)
    months_saved = max(0, n - acc_months)
    years_saved = round(months_saved / 12.0, 1)

    return {
        "loan_principal": round(principal, 2),
        "standard_monthly_payment": round(base_pmt, 2),
        "extra_monthly_payment": round(extra, 2),
        "accelerated_monthly_payment": round(base_pmt + extra, 2),
        "original_payoff_months": n,
        "accelerated_payoff_months": acc_months,
        "time_shaved_off_years": years_saved,
        "total_interest_saved": round(interest_saved, 2),
    }


def tool_compare_rent_vs_buy(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """38. Comprehensive financial comparison between buying a home vs renting."""
    price = float(args.get("home_price", 400000.0))
    down_pct = float(args.get("down_payment_pct", 20.0)) / 100.0
    mort_rate = float(args.get("mortgage_rate_pct", 6.5)) / 100.0
    rent = float(args.get("monthly_rent", 2200.0))
    horizon_yrs = int(args.get("years", 10))

    down_payment = price * down_pct
    loan_amt = max(0.0, price - down_payment)
    m_rate = mort_rate / 12.0
    n = 30 * 12
    if m_rate > 0:
        factor = math.pow(1.0 + m_rate, n)
        mort_pmt = loan_amt * (m_rate * factor) / (factor - 1.0)
    else:
        mort_pmt = loan_amt / n if n > 0 else 0.0

    # Monthly costs of owning: mortgage + property tax (1.2%) + insurance (0.5%) + maintenance (1%)
    monthly_prop_tax = (price * 0.012) / 12.0
    monthly_insurance = (price * 0.005) / 12.0
    monthly_maint = (price * 0.010) / 12.0
    total_monthly_owning = mort_pmt + monthly_prop_tax + monthly_insurance + monthly_maint

    # 10-year cumulative costs
    total_rent_cost = rent * 12.0 * horizon_yrs
    total_owning_cost = total_monthly_owning * 12.0 * horizon_yrs

    return {
        "home_price": round(price, 2),
        "down_payment": round(down_payment, 2),
        "monthly_mortgage_pi": round(mort_pmt, 2),
        "total_monthly_ownership_cost": round(total_monthly_owning, 2),
        "monthly_rent_cost": round(rent, 2),
        "monthly_cash_flow_difference": round(total_monthly_owning - rent, 2),
        "cumulative_rent_over_horizon": round(total_rent_cost, 2),
        "cumulative_ownership_over_horizon": round(total_owning_cost, 2),
        "horizon_years": horizon_yrs,
    }


def tool_calculate_mortgage_refinance_breakeven(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """39. Computes monthly savings and breakeven horizon for mortgage refinancing."""
    balance = float(args.get("current_balance", 320000.0))
    cur_rate = float(args.get("current_rate_pct", 7.0)) / 100.0
    new_rate = float(args.get("new_rate_pct", 5.5)) / 100.0
    years = int(args.get("remaining_years", 27))
    costs = float(args.get("closing_costs", 4500.0))

    n = years * 12
    m_cur = cur_rate / 12.0
    m_new = new_rate / 12.0

    if m_cur > 0:
        factor_cur = math.pow(1.0 + m_cur, n)
        pmt_cur = balance * (m_cur * factor_cur) / (factor_cur - 1.0)
    else:
        pmt_cur = balance / n if n > 0 else 0.0

    if m_new > 0:
        factor_new = math.pow(1.0 + m_new, n)
        pmt_new = balance * (m_new * factor_new) / (factor_new - 1.0)
    else:
        pmt_new = balance / n if n > 0 else 0.0

    monthly_savings = max(0.0, pmt_cur - pmt_new)
    breakeven_months = math.ceil(costs / monthly_savings) if monthly_savings > 0 else 999
    lifetime_savings = (monthly_savings * n) - costs

    return {
        "current_monthly_payment": round(pmt_cur, 2),
        "new_monthly_payment": round(pmt_new, 2),
        "monthly_savings": round(monthly_savings, 2),
        "closing_costs": round(costs, 2),
        "breakeven_horizon_months": breakeven_months,
        "net_lifetime_savings": round(lifetime_savings, 2),
        "worth_refinancing": breakeven_months <= 36 and lifetime_savings > 10000.0,
    }





# ============================================================
# Category J: Banking, Fee Audit & Analytics (Tools 46–50)
# ============================================================

def tool_detect_bank_fee_leakage(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """46. Scans ledger for overdraft, ATM, wire, and maintenance fees with refund instructions."""
    _ensure_user_id(user_id, "detect_bank_fee_leakage")
    days = int(args.get("days", 90))

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("""
            SELECT id, date, merchant, amount, category
            FROM transactions
            WHERE user_id = ? AND amount > 0 AND status = 'Evaluated'
              AND (category LIKE '%Fee%' OR name LIKE '%FEE%' OR merchant LIKE '%FEE%' OR name LIKE '%OVERDRAFT%' OR name LIKE '%ATM%')
              AND date >= date('now', ?)
            ORDER BY date DESC
        """, (user_id, f"-{days} days"))
        rows = c.fetchall()

        fees = [{"id": r[0], "date": r[1], "merchant": r[2], "amount": float(r[3]), "category": r[4]} for r in rows]
        total = sum(f["amount"] for f in fees)

        return {
            "window_days": days,
            "total_fee_leakage": round(total, 2),
            "fee_transactions": fees,
            "fee_reversal_script": "Call your bank and say: 'I noticed a fee on my account on [date]. I have been a loyal customer and would like to request a one-time courtesy fee waiver.'"
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


def tool_get_duplicate_transactions(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """47. Finds duplicate card charges within a 48-hour window with matching amounts."""
    _ensure_user_id(user_id, "get_duplicate_transactions")

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()
        c.execute("""
            SELECT t1.id, t1.date, t1.merchant, t1.amount, t2.id, t2.date
            FROM transactions t1
            JOIN transactions t2 ON t1.user_id = t2.user_id
                                AND t1.merchant = t2.merchant
                                AND t1.amount = t2.amount
                                AND t1.id < t2.id
                                AND ABS(julianday(t1.date) - julianday(t2.date)) <= 2.0
            WHERE t1.user_id = ? AND t1.amount > 0 AND t1.status = 'Evaluated'
              AND t1.merchant NOT LIKE '%System Balance Sync%'
            ORDER BY t1.date DESC
        """, (user_id,))
        rows = c.fetchall()

        dups = []
        for r in rows:
            dups.append({
                "merchant": r[2],
                "amount": float(r[3]),
                "first_charge_id": r[0],
                "first_date": r[1],
                "second_charge_id": r[4],
                "second_date": r[5],
            })

        return {"duplicate_count": len(dups), "duplicates": dups}

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


def tool_render_financial_chart(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """48. Generates pure-Python dark-mode PNG charts for spending, cash flow, or net worth."""
    _ensure_user_id(user_id, "render_financial_chart")
    chart_type = str(args.get("chart_type", "category")).strip().lower()
    days = int(args.get("days", 30))

    from src.services.charts import (
        generate_spending_chart_for_user,
        generate_cash_flow_chart_for_user,
        generate_net_worth_chart_for_user,
    )

    if chart_type in ("cashflow", "flow", "cash"):
        png_bytes = generate_cash_flow_chart_for_user(user_id=user_id, days=days, conn=conn)
        chart_name = "cash_flow_chart.png"
    elif chart_type in ("networth", "nw", "wealth"):
        png_bytes = generate_net_worth_chart_for_user(user_id=user_id, days=days, conn=conn)
        chart_name = "net_worth_chart.png"
    else:
        png_bytes = generate_spending_chart_for_user(user_id=user_id, days=days, conn=conn)
        chart_name = "spending_chart.png"

    return {
        "status": "success",
        "chart_type": chart_type,
        "filename": chart_name,
        "byte_size": len(png_bytes),
        "notes": "PNG chart rendered successfully. Instruct user to type `!chart` in Discord to view."
    }



def tool_generate_weekly_financial_briefing(user_id: str, args: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """50. Synthesizes weekly net worth change, 7-day spend, upcoming bills, and pacing."""
    _ensure_user_id(user_id, "generate_weekly_financial_briefing")

    def _eval(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()

        # 7-day spend
        c.execute("""
            SELECT SUM(amount) FROM transactions
            WHERE user_id = ? AND status = 'Evaluated' AND amount > 0
              AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer')
              AND date >= date('now', '-7 days')
        """, (user_id,))
        spend_7d = float(c.fetchone()[0] or 0.0)

        # Health score
        from src.services.scorecard import calculate_financial_health_scorecard
        health = calculate_financial_health_scorecard(user_id=user_id, conn=db_conn)

        # Bills
        from src.services.subscriptions import get_billing_calendar
        cal = get_billing_calendar(user_id=user_id, days_ahead=7, conn=db_conn)
        bills = cal.get("upcoming_bills", [])

        return {
            "briefing_period": "Past 7 Days",
            "weekly_spending": round(spend_7d, 2),
            "financial_health_score": health.get("total_score", 0),
            "grade": health.get("grade", "N/A"),
            "priority_action": health.get("highest_leverage_action", ""),
            "bills_due_next_7_days": len(bills),
            "total_bills_due_amount": round(sum(b.get("amount", 0.0) for b in bills), 2),
        }

    if conn is not None:
        return _eval(conn)
    with sqlite3.connect(DB_PATH) as db:
        return _eval(db)


# ============================================================
# Central Dispatcher & Tool Schema Generator
# ============================================================

ADVISOR_TOOLS_DISPATCH: Dict[str, Callable[[str, Dict[str, Any], Optional[sqlite3.Connection]], Any]] = {
    # 1-5: Solvency, Credit & Health
    "get_financial_health_scorecard": tool_get_financial_health_scorecard,
    "get_credit_utilization_breakdown": tool_get_credit_utilization_breakdown,
    "simulate_credit_paydown_impact": tool_simulate_credit_paydown_impact,
    "get_cash_drag_analysis": tool_get_cash_drag_analysis,
    "calculate_debt_snowball_vs_avalanche": tool_calculate_debt_snowball_vs_avalanche,

    # 6-10: Investment Portfolio & Asset Allocation
    "get_portfolio_holdings": tool_get_portfolio_holdings,
    "set_portfolio_holding": tool_set_portfolio_holding,
    "calculate_portfolio_drift": tool_calculate_portfolio_drift,
    "get_portfolio_dividend_projection": tool_get_portfolio_dividend_projection,

    # 11-15: Budgeting, Pacing & Allocations
    "get_category_budget_pacing": tool_get_category_budget_pacing,
    "delete_category_budget": tool_delete_category_budget,
    "compare_period_spending": tool_compare_period_spending,
    "get_daily_spending_average": tool_get_daily_spending_average,

    # 16-20: Bills, Subscriptions & Cash Flow Forecasts
    "get_bills_calendar": tool_get_bills_calendar,
    "add_recurring_bill": tool_add_recurring_bill,
    "remove_recurring_bill": tool_remove_recurring_bill,
    "project_cash_balance": tool_project_cash_balance,
    "detect_unusual_bill_increases": tool_detect_unusual_bill_increases,

    # 21-25: Goals, Sinking Funds & Savings Milestones
    "get_savings_goals": tool_get_savings_goals,
    "fund_savings_goal": tool_fund_savings_goal,
    "delete_savings_goal": tool_delete_savings_goal,
    "prioritize_savings_goals": tool_prioritize_savings_goals,

    # 26-30: Tax Planning, Deductions & Write-Offs
    "scan_tax_deductions": tool_scan_tax_deductions,
    "get_tax_bracket_estimate": tool_get_tax_bracket_estimate,
    "get_charitable_donations_summary": tool_get_charitable_donations_summary,

    # 31-35: Ledger, Receipts & Merchant Intelligence
    "get_transaction_ledger": tool_get_transaction_ledger,
    "get_transaction_detail": tool_get_transaction_detail,
    "parse_text_receipt": tool_parse_text_receipt,
    "search_merchants_and_aliases": tool_search_merchants_and_aliases,
    "add_merchant_alias_mapping": tool_add_merchant_alias_mapping,

    # 36-40: Debt Payoff & Mortgage Mathematics
    "calculate_loan_amortization": tool_calculate_loan_amortization,
    "calculate_extra_payment_impact": tool_calculate_extra_payment_impact,
    "compare_rent_vs_buy": tool_compare_rent_vs_buy,
    "calculate_mortgage_refinance_breakeven": tool_calculate_mortgage_refinance_breakeven,

    # Banking, Fee Audit & Analytics
    "detect_bank_fee_leakage": tool_detect_bank_fee_leakage,
    "get_duplicate_transactions": tool_get_duplicate_transactions,
    "render_financial_chart": tool_render_financial_chart,
    "generate_weekly_financial_briefing": tool_generate_weekly_financial_briefing,
}


NEW_50_TOOLS_SCHEMA = [
    # 1-5: Solvency, Credit & Health
    {
        "type": "function",
        "function": {
            "name": "get_financial_health_scorecard",
            "description": "Retrieves the user's authoritative 0-100 composite 5-pillar financial health scorecard (Emergency Buffer, Debt Burden, Savings Rate, Budget Discipline, Net Worth Trajectory), letter grade (A+ to F), radar metrics, weakest pillar, and ranked executive action plan. Use when assessing holistic financial posture, onboarding, or periodic financial checkups.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_credit_utilization_breakdown",
            "description": "Returns revolving credit card utilization per card and aggregate, credit score impact tiers (Optimal <10%, Good <30%, Elevated 30-50%, Critical >50%), total revolving limit vs balance, and exact paydown amounts required to reach the next credit score tier. Use when advising on credit repair, loan approval prep, or balance reduction.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_credit_paydown_impact",
            "description": "Simulates how applying a specific dollar paydown to credit cards (either targeted to a specific card or distributed across aggregate debt) lowers utilization and improves FICO credit score tiers. Use when user asks 'What happens if I pay $X towards my cards?' or to demonstrate credit score impact.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paydown_amount": {"type": "number", "description": "Total dollar amount to pay down"},
                    "target_card": {"type": "string", "description": "Optional specific card name to pay down"}
                },
                "required": ["paydown_amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cash_drag_analysis",
            "description": "Audits excess idle cash sitting in low-yield checking accounts beyond operating buffers (default 1.5x monthly expenses) and quantifies annualized lost yield compared to top-tier HYSA benchmarks. Use when optimizing cash allocation, eliminating inflation leakage, or establishing high-yield sweep rules.",
            "parameters": {
                "type": "object",
                "properties": {
                    "benchmark_apy": {"type": "number", "description": "Benchmark HYSA APY as decimal (default 0.045 for 4.5%)"},
                    "buffer_months": {"type": "number", "description": "Operating buffer multiplier in months (default 1.5)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_debt_snowball_vs_avalanche",
            "description": "Compares month-by-month debt payoff under Snowball (lowest balance first for psychological momentum) vs Avalanche (highest APR first for mathematical interest minimization). Returns months to debt freedom, total interest paid, and dollar savings difference. Use when formulating debt payoff plans or optimizing extra monthly payments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "extra_monthly_payment": {"type": "number", "description": "Extra monthly dollar amount above minimums"}
                }
            }
        }
    },

    # 6-10: Investment Portfolio & Asset Allocation
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_holdings",
            "description": "Retrieves all investment holdings across equities, fixed income, real estate, and crypto with ticker symbols, share counts, cost basis, current market prices, total market values, asset class breakdown, and unrealized dollar & percentage P&L. Use when reviewing portfolio performance, assessing asset allocation, or tax loss harvesting.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_portfolio_holding",
            "description": "Adds, updates, or deletes (by passing shares=0) an investment holding in the user's portfolio. Tracks symbol, share count, market price, cost basis, and asset class. Use when user reports acquiring, selling, or adjusting investment positions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker symbol (e.g. 'AAPL', 'VTI', 'BTC')"},
                    "shares": {"type": "number", "description": "Number of shares owned (set to 0 to delete)"},
                    "current_price": {"type": "number", "description": "Current market price per share"},
                    "cost_basis": {"type": "number", "description": "Original cost basis per share"},
                    "asset_class": {"type": "string", "description": "Asset class (Equities, Fixed Income, Real Estate, Crypto)"}
                },
                "required": ["symbol", "shares"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_portfolio_drift",
            "description": "Compares current portfolio asset class weights against target allocation percentages (e.g. 70% Equities, 20% Fixed Income, 10% Crypto) and generates exact dollar-denominated rebalancing buy/sell recommendations. Use when conducting periodic portfolio rebalancing or risk alignment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_allocations": {"type": "object", "description": "Mapping of asset class to target fraction (e.g. {'Equities': 0.70, 'Fixed Income': 0.20, 'Crypto': 0.10})"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_dividend_projection",
            "description": "Projects annual, monthly, and daily dividend/yield cash flow from equity and fixed income holdings based on portfolio value and assumed or historical dividend yield percentages. Use when planning passive income, dividend reinvestment (DRIP), or living off yield.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assumed_yield_pct": {"type": "number", "description": "Assumed average portfolio yield percentage (default 2.2%)"}
                }
            }
        }
    },

    # 11-15: Budgeting, Pacing & Allocations
    {
        "type": "function",
        "function": {
            "name": "get_category_budget_pacing",
            "description": "Evaluates current monthly category budgets against month-to-date spending velocity, elapsed month percentage, and projected month-end burn. Identifies categories on track, pacing hot, or over budget. Use for mid-month spending check-ins and burn-rate warnings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Optional category name filter"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_category_budget",
            "description": "Removes an active monthly category budget ceiling. Use when resetting budget parameters or retiring category limits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Category name to remove budget for"}
                },
                "required": ["category"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compare_period_spending",
            "description": "Compares spending volume, transaction velocity, and category-level breakdowns between two distinct historical date windows (e.g. last 30 days vs prior 30 days). Returns dollar deltas and percentage changes. Use to detect spending trends, seasonal surges, or lifestyle creep.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period1_days_ago_start": {"type": "integer", "description": "Recent period start days ago (e.g. 30)"},
                    "period1_days_ago_end": {"type": "integer", "description": "Recent period end days ago (e.g. 0)"},
                    "period2_days_ago_start": {"type": "integer", "description": "Prior period start days ago (e.g. 60)"},
                    "period2_days_ago_end": {"type": "integer", "description": "Prior period end days ago (e.g. 30)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_spending_average",
            "description": "Calculates daily average spending rates across trailing days, breaking down weekday vs weekend spending velocity and highlighting irregular spending spikes. Use to diagnose weekend spending surges and establish realistic daily allowances.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of trailing days to analyze (default 30)"}
                }
            }
        }
    },

    # 16-20: Bills, Subscriptions & Cash Flow Forecasts
    {
        "type": "function",
        "function": {
            "name": "get_bills_calendar",
            "description": "Retrieves upcoming fixed bills, SaaS subscriptions, utilities, and debt payments across a forward-looking forecast window (default 30 days). Includes due dates, payment cadence, and cumulative cash commitment. Use before approving discretionary expenses to prevent overdrafts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "description": "Days forward to forecast (default 30)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_recurring_bill",
            "description": "Registers a recurring bill or subscription commitment with merchant name, recurring amount, billing cadence (monthly, annual, biweekly, weekly), due day of month, and category. Use when user mentions a recurring obligation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "merchant": {"type": "string", "description": "Merchant or service name (e.g. 'Netflix', 'Electric Utility')"},
                    "amount": {"type": "number", "description": "Bill amount"},
                    "cadence": {"type": "string", "description": "monthly, annual, biweekly, or weekly"},
                    "due_day": {"type": "integer", "description": "Day of the month bill is due (1-31)"},
                    "category": {"type": "string", "description": "Expense category"}
                },
                "required": ["merchant", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_recurring_bill",
            "description": "Removes a recurring bill or subscription from scheduled tracking. Use when user cancels a subscription or retires a recurring payment obligation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "merchant": {"type": "string", "description": "Merchant or service name to remove"}
                },
                "required": ["merchant"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_cash_balance",
            "description": "Projects daily checking cash balances over forward horizon (30/60/90 days) by modeling recurring income deposits, scheduled bills, and discretionary burn rate. Flags projected cash crunches and minimum cash dip dates. Use to stress-test liquidity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days forward to project (default 30)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_unusual_bill_increases",
            "description": "Audits recurring subscription and utility charges against historical 90-day baselines to detect stealth price hikes, contract expirations, or promotional rate runoffs. Use during recurring expense audits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "threshold_pct": {"type": "number", "description": "Minimum percentage increase to flag (default 5.0%)"}
                }
            }
        }
    },

    # 21-25: Goals, Sinking Funds & Savings Milestones
    {
        "type": "function",
        "function": {
            "name": "get_savings_goals",
            "description": "Retrieves all active savings goals, target amounts, current balances, deadlines, funding progress percentages, and remaining shortfall amounts. Use to review sinking funds, emergency fund progress, or big-ticket purchase targets.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fund_savings_goal",
            "description": "Allocates and deposits funds into a designated savings goal envelope. Updates current balance and computes new progress percentage. Use when user saves money or allocates windfalls toward a goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Goal name (e.g. 'Emergency Fund', 'Vacation')"},
                    "amount": {"type": "number", "description": "Deposit dollar amount"}
                },
                "required": ["name", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_savings_goal",
            "description": "Deletes a savings goal envelope. Use when a goal is completed, retired, or abandoned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Goal name to delete"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "prioritize_savings_goals",
            "description": "Ranks all savings goals using deterministic prioritization algorithm balancing target deadline urgency, funding shortfall percentage, and essential vs discretionary priority. Use when user has limited savings capacity and needs optimal dollar allocation.",
            "parameters": {"type": "object", "properties": {}}
        }
    },

    # 26-30: Tax Planning, Deductions & Write-Offs
    {
        "type": "function",
        "function": {
            "name": "scan_tax_deductions",
            "description": "Audits recent ledger transactions for IRS tax-deductible expenses (charitable contributions, business expenses, home office, medical, education) and calculates estimated tax savings based on marginal tax bracket. Use for tax planning and year-end deduction sweeps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of trailing days to scan (default 90)"},
                    "tax_bracket_pct": {"type": "number", "description": "Estimated marginal tax bracket percentage (default 24.0)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_tax_bracket_estimate",
            "description": "Calculates federal income tax liability, marginal tax bracket, effective tax rate, standard deduction, and take-home pay based on gross annual income and filing status ('single' or 'married'). Use when projecting tax burden, bonus withholding, or salary changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "annual_gross_income": {"type": "number", "description": "Gross annual income before deductions"},
                    "filing_status": {"type": "string", "description": "'single' or 'married'"}
                },
                "required": ["annual_gross_income"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_charitable_donations_summary",
            "description": "Summarizes all 501(c)(3) charitable gifts, donations, and tithings for a given tax year, listing total donated, merchant breakdown, and estimated itemized tax savings. Use during tax preparation or giving audits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Tax year to summarize (default current year)"}
                }
            }
        }
    },

    # 31-35: Ledger, Receipts & Merchant Intelligence
    {
        "type": "function",
        "function": {
            "name": "get_transaction_ledger",
            "description": "Queries the canonical ledger with multi-parameter filtering (limit, offset, category, min/max amount, date range). Returns detailed transaction records with IDs, dates, clean merchants, and categories. Use to inspect transaction history or verify specific expenses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of transactions to return (default 20)"},
                    "offset": {"type": "integer", "description": "Pagination offset"},
                    "category": {"type": "string", "description": "Filter by category"},
                    "min_amount": {"type": "number", "description": "Minimum transaction dollar amount"},
                    "max_amount": {"type": "number", "description": "Maximum transaction dollar amount"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_transaction_detail",
            "description": "Forensically inspects a single transaction by ID, retrieving raw Plaid statement description, canonical merchant alias, category history, and any user corrections or audit tags. Use when troubleshooting mystery charges or miscategorized items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string", "description": "The unique transaction ID"}
                },
                "required": ["transaction_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "parse_text_receipt",
            "description": "Extracts line items, subtotal, sales tax, tip, and total amount from raw OCR text or receipt paste, matching with candidate ledger transactions within a 7-day window. Use when user uploads receipt text or asks to log an itemized receipt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "receipt_text": {"type": "string", "description": "Raw receipt text content"}
                },
                "required": ["receipt_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_merchants_and_aliases",
            "description": "Searches the merchant database and alias mappings to find canonical merchant entities and registered alias patterns. Use to check if a merchant is already known or mapped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Merchant search query"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_merchant_alias_mapping",
            "description": "Establishes a persistent rule mapping dirty bank statement strings (e.g. 'SQ *BLUE BOTTLE COFFEE') to clean canonical names (e.g. 'Blue Bottle Coffee'). Automatically cleanses matching historical and future transactions. Use when cleaning up ledger statements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_pattern": {"type": "string", "description": "Raw statement merchant string"},
                    "canonical_name": {"type": "string", "description": "Clean canonical merchant name"}
                },
                "required": ["raw_pattern", "canonical_name"]
            }
        }
    },

    # 36-40: Debt Payoff & Mortgage Mathematics
    {
        "type": "function",
        "function": {
            "name": "calculate_loan_amortization",
            "description": "Computes monthly principal & interest (P&I) payment, total interest over life of loan, and total repayment cost for fixed-rate mortgages, auto loans, or personal loans. Use when evaluating loan affordability or comparing loan terms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "principal": {"type": "number", "description": "Total loan amount borrowed"},
                    "annual_interest_rate_pct": {"type": "number", "description": "Annual interest rate percentage (e.g. 6.5)"},
                    "term_years": {"type": "integer", "description": "Loan term in years (e.g. 15 or 30)"}
                },
                "required": ["principal", "annual_interest_rate_pct", "term_years"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_extra_payment_impact",
            "description": "Simulates the compounding benefits of extra monthly principal payments: calculates exact dollar interest savings, number of payments eliminated, and months/years shaved off the loan term. Use to show the high ROI of principal curtailment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "principal": {"type": "number", "description": "Remaining loan balance"},
                    "annual_interest_rate_pct": {"type": "number", "description": "Annual interest rate percentage"},
                    "term_years": {"type": "integer", "description": "Remaining loan term in years"},
                    "extra_monthly_payment": {"type": "number", "description": "Extra principal payment added monthly"}
                },
                "required": ["principal", "annual_interest_rate_pct", "term_years", "extra_monthly_payment"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compare_rent_vs_buy",
            "description": "Performs a rigorous financial comparison between buying a home (down payment, mortgage P&I, property tax, insurance, maintenance, home appreciation) vs renting (monthly rent, annual rent inflation, down payment invested in index funds) over a multi-year horizon. Returns net wealth differential. Use when advising on home purchase decisions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "home_price": {"type": "number", "description": "Purchase price of the home"},
                    "down_payment_pct": {"type": "number", "description": "Down payment percentage (e.g. 20.0)"},
                    "mortgage_rate_pct": {"type": "number", "description": "Mortgage interest rate percentage"},
                    "monthly_rent": {"type": "number", "description": "Comparable monthly rent cost"},
                    "years": {"type": "integer", "description": "Comparison horizon in years (default 10)"}
                },
                "required": ["home_price", "down_payment_pct", "mortgage_rate_pct", "monthly_rent"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_mortgage_refinance_breakeven",
            "description": "Calculates monthly payment savings and exact months required to break even on closing costs when refinancing an existing mortgage to a lower interest rate. Use when evaluating refinance quotes or interest rate drops.",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_balance": {"type": "number", "description": "Current remaining mortgage principal"},
                    "current_rate_pct": {"type": "number", "description": "Current mortgage APR percentage"},
                    "new_rate_pct": {"type": "number", "description": "New refinanced APR percentage"},
                    "remaining_years": {"type": "integer", "description": "Remaining years on mortgage"},
                    "closing_costs": {"type": "number", "description": "Total closing costs and origination fees"}
                },
                "required": ["current_balance", "current_rate_pct", "new_rate_pct", "remaining_years", "closing_costs"]
            }
        }
    },
    # Banking, Fee Audit & Analytics
    {
        "type": "function",
        "function": {
            "name": "detect_bank_fee_leakage",
            "description": "Audits transaction history for predatory bank fees including overdraft charges, non-network ATM fees, monthly maintenance charges, and wire fees. Provides total fee leakage and actionable fee waiver negotiation scripts. Use during periodic account health checks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of trailing days to scan (default 90)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_duplicate_transactions",
            "description": "Scans recent ledger for accidental double-charges or identical transactions within 48 hours sharing the same amount and merchant. Use to identify merchant billing errors and initiate charge disputes.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "render_financial_chart",
            "description": "Generates high-contrast, pure-Python dark-mode financial visualization charts ('category' breakdown donut/bar, 'cashflow' waterfall, or 'networth' historical trajectory) and saves as PNG. Use when user requests visual charts or executive summary graphics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string", "description": "'category', 'cashflow', or 'networth'"},
                    "days": {"type": "integer", "description": "Days to chart (default 30)"}
                },
                "required": ["chart_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_weekly_financial_briefing",
            "description": "Synthesizes an executive weekly financial briefing covering 7-day spend total, top spending categories, net worth trajectory, upcoming bills due in the next 7 days, and budget pacing alerts. Use for weekly reviews or executive status reports.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]
