"""
Delilah Financial OS - Comprehensive 5-Pillar Financial Health Scorecard & Diagnostic Index.

Evaluates an authoritative, 0-100 holistic index synthesized across 5 core pillars:
1. Emergency Runway & Liquidity (25 pts)
2. Revolving Credit Card Utilization (20 pts)
3. Debt Burden & Solvency Leverage (20 pts)
4. Cash Flow & Savings Rate (20 pts)
5. Wealth Building & Goal Progress (15 pts)

Includes deterministic letter grades (A+ to F), weakest pillar identification,
and a prioritized #1 highest-leverage action plan for rapid score recovery.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import discord

from src.core.state import DB_PATH
from src.db.migrations import apply_all
from src.services.credit import calculate_credit_utilization


def calculate_financial_health_scorecard(
    *,
    user_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Compute comprehensive 5-pillar financial health score (0-100) for a user."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "calculate_financial_health_scorecard: forced isolation violation — user_id is required and must be a non-empty string."
        )

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        try:
            apply_all(db_conn)
        except Exception:
            pass

        c = db_conn.cursor()

        # ============================================================
        # Pillar 1: Emergency Runway & Liquidity (25 pts max)
        # ============================================================
        c.execute("""
            SELECT SUM(current_balance)
            FROM plaid_accounts
            WHERE user_id = ? AND type = 'depository' AND active = 1
        """, (user_id,))
        row = c.fetchone()
        depository_cash = max(0.0, float(row[0] or 0.0))

        c.execute("""
            SELECT SUM(current_amount)
            FROM savings_buckets
            WHERE user_id = ? AND LOWER(name) LIKE '%emergency%'
        """, (user_id,))
        row = c.fetchone()
        emergency_bucket = max(0.0, float(row[0] or 0.0))

        effective_cash = max(depository_cash, emergency_bucket)

        # 30-day operating spend
        c.execute("""
            SELECT SUM(amount)
            FROM transactions
            WHERE user_id = ?
              AND status = 'Evaluated'
              AND amount > 0
              AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer', 'Loan Repayment')
              AND merchant NOT LIKE '%System Balance Sync%'
              AND date >= date('now', '-30 days')
        """, (user_id,))
        row = c.fetchone()
        recent_burn = float(row[0] or 0.0)

        # Fallback to 90d spend / 3 if 30d is 0
        if recent_burn <= 0:
            c.execute("""
                SELECT SUM(amount)
                FROM transactions
                WHERE user_id = ?
                  AND status = 'Evaluated'
                  AND amount > 0
                  AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer', 'Loan Repayment')
                  AND merchant NOT LIKE '%System Balance Sync%'
                  AND date >= date('now', '-90 days')
            """, (user_id,))
            row = c.fetchone()
            spend_90 = float(row[0] or 0.0)
            monthly_burn = spend_90 / 3.0 if spend_90 > 0 else 1500.0
        else:
            monthly_burn = recent_burn

        runway_months = (effective_cash / monthly_burn) if monthly_burn > 0 else 0.0

        if runway_months >= 6.0:
            p1_score = 25
            p1_status = "Excellent"
            p1_summary = f"{runway_months:.1f} months runway (${effective_cash:,.2f} liquid)"
        elif runway_months >= 4.0:
            p1_score = 21
            p1_status = "Good"
            p1_summary = f"{runway_months:.1f} months runway (${effective_cash:,.2f} liquid)"
        elif runway_months >= 3.0:
            p1_score = 18
            p1_status = "Adequate"
            p1_summary = f"{runway_months:.1f} months runway (Target: 6.0 mo)"
        elif runway_months >= 2.0:
            p1_score = 14
            p1_status = "Fair"
            p1_summary = f"{runway_months:.1f} months runway (Build toward 3–6 mo)"
        elif runway_months >= 1.0:
            p1_score = 9
            p1_status = "Vulnerable"
            p1_summary = f"{runway_months:.1f} months runway (High risk of shortfall)"
        else:
            p1_score = 3
            p1_status = "Critical"
            p1_summary = f"<1 month runway (${effective_cash:,.2f} available)"

        # ============================================================
        # Pillar 2: Revolving Credit Card Utilization (20 pts max)
        # ============================================================
        credit_data = calculate_credit_utilization(user_id=user_id, conn=db_conn)
        card_count = credit_data.get("card_count", 0)
        agg_util = credit_data.get("aggregate_utilization_pct", 0.0)

        if card_count == 0 or credit_data.get("total_balance", 0.0) == 0.0:
            p2_score = 20
            p2_status = "Optimal"
            p2_summary = "Zero revolving credit balance / 0% utilization"
        elif agg_util < 10.0:
            p2_score = 20
            p2_status = "Optimal"
            p2_summary = f"{agg_util:.1f}% utilization (Top credit score tier)"
        elif agg_util < 30.0:
            p2_score = 16
            p2_status = "Good"
            p2_summary = f"{agg_util:.1f}% utilization (Healthy under 30% ceiling)"
        elif agg_util < 50.0:
            p2_score = 8
            p2_status = "Elevated"
            p2_summary = f"{agg_util:.1f}% utilization (Exceeds 30% FICO threshold)"
        else:
            p2_score = 2
            p2_status = "Critical"
            p2_summary = f"{agg_util:.1f}% utilization (Severe credit score penalty)"

        # ============================================================
        # Pillar 3: Debt Burden & Solvency Leverage (20 pts max)
        # ============================================================
        # Term loans / mortgages from Plaid
        c.execute("""
            SELECT SUM(current_balance)
            FROM plaid_accounts
            WHERE user_id = ? AND type = 'loan' AND active = 1
        """, (user_id,))
        row = c.fetchone()
        loan_debt = max(0.0, float(row[0] or 0.0))

        # User debts table
        c.execute("""
            SELECT SUM(balance)
            FROM user_debts
            WHERE user_id = ? AND balance > 0
        """, (user_id,))
        row = c.fetchone()
        user_debt_bal = max(0.0, float(row[0] or 0.0))

        credit_debt = credit_data.get("total_balance", 0.0)
        total_debt = credit_debt + loan_debt + user_debt_bal

        # Investment value
        c.execute("""
            SELECT SUM(shares * current_price)
            FROM investment_holdings
            WHERE user_id = ?
        """, (user_id,))
        row = c.fetchone()
        investments_val = max(0.0, float(row[0] or 0.0))

        # Sinking funds total
        c.execute("""
            SELECT SUM(current_amount)
            FROM savings_buckets
            WHERE user_id = ?
        """, (user_id,))
        row = c.fetchone()
        savings_val = max(0.0, float(row[0] or 0.0))

        total_assets = effective_cash + investments_val + savings_val

        if total_debt <= 0.0:
            p3_score = 20
            p3_status = "Debt-Free"
            p3_summary = "100% debt-free posture ($0 total liabilities)"
        elif total_assets > 0:
            leverage = total_debt / total_assets
            if leverage <= 0.15:
                p3_score = 18
                p3_status = "Strong"
                p3_summary = f"{leverage * 100:.1f}% leverage (${total_debt:,.2f} debt / ${total_assets:,.2f} assets)"
            elif leverage <= 0.35:
                p3_score = 14
                p3_status = "Manageable"
                p3_summary = f"{leverage * 100:.1f}% leverage (${total_debt:,.2f} debt / ${total_assets:,.2f} assets)"
            elif leverage <= 0.65:
                p3_score = 9
                p3_status = "High"
                p3_summary = f"{leverage * 100:.1f}% leverage (Elevated debt load)"
            else:
                p3_score = 4
                p3_status = "Critical"
                p3_summary = f"{leverage * 100:.1f}% leverage (Debt approaches or exceeds assets)"
        else:
            p3_score = 2
            p3_status = "Insolvent"
            p3_summary = f"${total_debt:,.2f} debt with negligible recorded assets"

        # ============================================================
        # Pillar 4: Cash Flow & Savings Rate (20 pts max)
        # ============================================================
        c.execute("""
            SELECT
                SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as inflow,
                SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as outflow
            FROM transactions
            WHERE user_id = ?
              AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer')
              AND merchant NOT LIKE '%System Balance Sync%'
              AND date >= date('now', '-30 days')
        """, (user_id,))
        cf_row = c.fetchone()
        inflow = float(cf_row[0] or 0.0) if cf_row else 0.0
        outflow = float(cf_row[1] or 0.0) if cf_row else 0.0

        if inflow > 0:
            net_cf = inflow - outflow
            savings_rate = (net_cf / inflow) * 100.0
            if savings_rate >= 20.0:
                p4_score = 20
                p4_status = "Excellent"
                p4_summary = f"+{savings_rate:.1f}% savings rate (+${net_cf:,.2f}/mo net)"
            elif savings_rate >= 10.0:
                p4_score = 16
                p4_status = "Good"
                p4_summary = f"+{savings_rate:.1f}% savings rate (+${net_cf:,.2f}/mo net)"
            elif savings_rate >= 0.0:
                p4_score = 11
                p4_status = "Breakeven"
                p4_summary = f"+{savings_rate:.1f}% savings rate (+${net_cf:,.2f}/mo net)"
            else:
                p4_score = 3
                p4_status = "Deficit"
                p4_summary = f"Negative cash flow (-${abs(net_cf):,.2f}/mo burn deficit)"
        else:
            p4_score = 12
            p4_status = "Neutral"
            p4_summary = f"${outflow:,.2f} outflow (no recurring paycheck tracked in ledger)"

        # ============================================================
        # Pillar 5: Wealth Building & Goal Progress (15 pts max)
        # ============================================================
        c.execute("""
            SELECT COUNT(*), SUM(shares * current_price)
            FROM investment_holdings
            WHERE user_id = ?
        """, (user_id,))
        inv_row = c.fetchone()
        inv_count = int(inv_row[0] or 0)
        inv_val = float(inv_row[1] or 0.0)

        if inv_val >= 10000.0 or inv_count >= 3:
            p5_inv_pts = 8
        elif inv_val > 0.0:
            p5_inv_pts = 5
        else:
            p5_inv_pts = 1

        c.execute("""
            SELECT COUNT(*), SUM(current_amount), SUM(target_amount)
            FROM savings_buckets
            WHERE user_id = ?
        """, (user_id,))
        goal_row = c.fetchone()
        goal_cnt = int(goal_row[0] or 0)
        goal_cur = float(goal_row[1] or 0.0)

        if goal_cnt >= 1 and goal_cur > 0:
            p5_goal_pts = 7
        elif goal_cnt >= 1:
            p5_goal_pts = 4
        else:
            p5_goal_pts = 1

        p5_score = p5_inv_pts + p5_goal_pts
        if p5_score >= 13:
            p5_status = "Advanced"
            p5_summary = f"${inv_val:,.2f} invested across {inv_count} holdings · ${goal_cur:,.2f} in goals"
        elif p5_score >= 9:
            p5_status = "Growing"
            p5_summary = f"${inv_val:,.2f} invested · ${goal_cur:,.2f} in {goal_cnt} goals"
        else:
            p5_status = "Early Stage"
            p5_summary = "Limited investment holdings or funded sinking goals"

        # ============================================================
        # Composite Aggregation & Grading
        # ============================================================
        total_score = max(0, min(100, p1_score + p2_score + p3_score + p4_score + p5_score))

        if total_score >= 90:
            grade = "A+"
            badge = "🏆"
            verdict = "Exceptional Financial Fortress"
        elif total_score >= 80:
            grade = "A"
            badge = "🟢"
            verdict = "Strong & Resilient"
        elif total_score >= 70:
            grade = "B"
            badge = "🟡"
            verdict = "Good with Targeted Growth Areas"
        elif total_score >= 60:
            grade = "C"
            badge = "🟠"
            verdict = "Moderate Vulnerability"
        elif total_score >= 50:
            grade = "D"
            badge = "⚠️"
            verdict = "Fragile Financial Position"
        else:
            grade = "F"
            badge = "🚨"
            verdict = "High Financial Distress / Action Required"

        pillars = [
            {
                "id": "runway",
                "name": "Emergency Runway & Liquidity",
                "score": p1_score,
                "max": 25,
                "pct": round(p1_score / 25 * 100, 1),
                "status": p1_status,
                "summary": p1_summary,
            },
            {
                "id": "credit",
                "name": "Credit Card Utilization",
                "score": p2_score,
                "max": 20,
                "pct": round(p2_score / 20 * 100, 1),
                "status": p2_status,
                "summary": p2_summary,
            },
            {
                "id": "solvency",
                "name": "Debt Burden & Solvency",
                "score": p3_score,
                "max": 20,
                "pct": round(p3_score / 20 * 100, 1),
                "status": p3_status,
                "summary": p3_summary,
            },
            {
                "id": "cash_flow",
                "name": "Cash Flow & Savings Rate",
                "score": p4_score,
                "max": 20,
                "pct": round(p4_score / 20 * 100, 1),
                "status": p4_status,
                "summary": p4_summary,
            },
            {
                "id": "wealth",
                "name": "Wealth Building & Goals",
                "score": p5_score,
                "max": 15,
                "pct": round(p5_score / 15 * 100, 1),
                "status": p5_status,
                "summary": p5_summary,
            },
        ]

        # Weakest pillar
        weakest = min(pillars, key=lambda p: p["pct"])

        # Prioritized recommendations and #1 leverage action
        recommendations: List[str] = []
        highest_leverage_action = ""

        if p2_score < 16 and credit_data.get("cards_over_30_pct"):
            top_card = credit_data["cards_over_30_pct"][0]
            action = f"Pay down **${top_card['paydown_for_30_pct']:,.2f}** on **{top_card['name']}** to drop under 30% utilization (+{20 - p2_score} pts)."
            recommendations.append(action)
            if not highest_leverage_action:
                highest_leverage_action = action

        if p1_score < 18:
            shortfall = max(0.0, (monthly_burn * 3.0) - effective_cash)
            action = f"Build emergency fund by **${shortfall:,.2f}** to achieve a safe 3-month baseline buffer (+{18 - p1_score} pts)."
            recommendations.append(action)
            if not highest_leverage_action:
                highest_leverage_action = action

        if p4_score < 16:
            action = "Reduce discretionary burn or trim recurring leakage to restore a >=15% monthly savings rate (+8 pts)."
            recommendations.append(action)
            if not highest_leverage_action:
                highest_leverage_action = action

        if p5_score < 9:
            action = "Establish at least one dedicated sinking goal (`!setgoal`) or index fund investment (`!setholding`) (+6 pts)."
            recommendations.append(action)
            if not highest_leverage_action:
                highest_leverage_action = action

        if not highest_leverage_action:
            highest_leverage_action = "Maintain disciplined automated transfers and keep credit utilization below 10%."

        return {
            "total_score": total_score,
            "grade": grade,
            "badge": badge,
            "verdict": verdict,
            "pillars": pillars,
            "weakest_pillar": weakest,
            "highest_leverage_action": highest_leverage_action,
            "recommendations": recommendations,
            "metrics": {
                "liquid_cash": round(effective_cash, 2),
                "runway_months": round(runway_months, 1),
                "monthly_burn": round(monthly_burn, 2),
                "credit_utilization_pct": round(agg_util, 1),
                "total_debt": round(total_debt, 2),
                "total_assets": round(total_assets, 2),
                "investments_val": round(investments_val, 2),
            },
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_scorecard_embed(data: Dict[str, Any]) -> discord.Embed:
    """Format the 5-pillar scorecard into a Discord embed."""
    score = data.get("total_score", 0)
    grade = data.get("grade", "N/A")
    badge = data.get("badge", "📊")
    verdict = data.get("verdict", "Financial Health Audit")

    color_map = {
        "A+": 0x2ECC71,  # Emerald Green
        "A": 0x2ECC71,
        "B": 0x3498DB,   # Blue
        "C": 0xF1C40F,   # Yellow
        "D": 0xE67E22,   # Orange
        "F": 0xE74C3C,   # Red
    }
    color = color_map.get(grade, 0x3498DB)

    embed = discord.Embed(
        title=f"{badge} Financial Health Scorecard: {score}/100 (Grade: {grade})",
        description=f"**{verdict}**\nDeterministic 5-pillar solvency, liquidity, and wealth index.",
        color=color,
    )

    for p in data.get("pillars", []):
        progress_bar = "█" * int(p["score"] / p["max"] * 10) + "░" * (10 - int(p["score"] / p["max"] * 10))
        embed.add_field(
            name=f"{p['name']}: {p['score']}/{p['max']} pts ({p['status']})",
            value=f"`{progress_bar}` {p['pct']}%\n↳ {p['summary']}",
            inline=False,
        )

    embed.add_field(
        name="🎯 #1 Highest Leverage Action",
        value=data.get("highest_leverage_action", "Maintain current financial discipline."),
        inline=False,
    )

    embed.set_footer(text="Delilah Financial OS · Autonomous Financial Diagnostics")
    return embed
