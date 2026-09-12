"""
Delilah Financial OS - Revolving Credit Card Utilization & Debt-Ratio Engine.

Calculates real-time revolving credit utilization per card and across the entire wallet.
Determines credit score impact tiers (<10% Optimal, 10-29.9% Good, 30-49.9% Elevated, >=50% Critical)
and calculates exact paydown amounts required to cross below critical credit reporting thresholds.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from src.core.state import DB_PATH
from src.db.migrations import apply_all


def calculate_credit_utilization(
    *,
    user_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Calculate per-card and aggregate credit utilization for a user."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "calculate_credit_utilization: forced isolation violation — user_id is required and must be a non-empty string."
        )

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        try:
            apply_all(db_conn)
        except Exception:
            pass

        c = db_conn.cursor()
        c.execute(
            """
            SELECT plaid_account_id, name, current_balance, credit_limit, available_balance, subtype
            FROM plaid_accounts
            WHERE user_id = ? AND type = 'credit' AND active = 1
            ORDER BY current_balance DESC
            """,
            (user_id,),
        )
        rows = c.fetchall()

        cards: List[Dict[str, Any]] = []
        total_balance = 0.0
        total_limit = 0.0
        total_available = 0.0

        for row in rows:
            acc_id, name, curr_bal, cred_lim, avail_bal, subtype = row
            raw_bal = float(curr_bal or 0.0)
            balance = max(0.0, raw_bal)  # Negative balance is an overpayment/credit
            limit = float(cred_lim) if cred_lim is not None and float(cred_lim) > 0 else None
            available = float(avail_bal) if avail_bal is not None else ((limit - balance) if limit is not None else 0.0)

            if limit is not None and limit > 0:
                util_pct = (balance / limit) * 100.0
                total_limit += limit
            else:
                util_pct = 100.0 if balance > 0 else 0.0

            total_balance += balance
            if available > 0:
                total_available += available

            if util_pct < 10.0:
                tier = "Optimal"
                badge = "🟢"
            elif util_pct < 30.0:
                tier = "Good"
                badge = "🟡"
            elif util_pct < 50.0:
                tier = "Elevated"
                badge = "🟠"
            else:
                tier = "Critical"
                badge = "🔴"

            # Compute paydown amounts required to hit target utilization
            paydown_30 = max(0.0, balance - (limit * 0.299)) if limit and util_pct >= 30.0 else 0.0
            paydown_10 = max(0.0, balance - (limit * 0.099)) if limit and util_pct >= 10.0 else 0.0

            cards.append({
                "account_id": acc_id,
                "name": name or "Credit Card",
                "subtype": subtype or "credit card",
                "balance": round(balance, 2),
                "limit": round(limit, 2) if limit is not None else None,
                "available": round(available, 2),
                "utilization_pct": round(util_pct, 1),
                "tier": tier,
                "badge": badge,
                "paydown_for_30_pct": round(paydown_30, 2),
                "paydown_for_10_pct": round(paydown_10, 2),
            })

        aggregate_util_pct = (total_balance / total_limit * 100.0) if total_limit > 0 else 0.0

        if aggregate_util_pct < 10.0:
            agg_tier = "Optimal"
            agg_badge = "🟢"
        elif aggregate_util_pct < 30.0:
            agg_tier = "Good"
            agg_badge = "🟡"
        elif aggregate_util_pct < 50.0:
            agg_tier = "Elevated"
            agg_badge = "🟠"
        else:
            agg_tier = "Critical"
            agg_badge = "🔴"

        # Cards needing attention
        over_30 = [c for c in cards if c["utilization_pct"] >= 30.0]
        over_50 = [c for c in cards if c["utilization_pct"] >= 50.0]

        total_paydown_30 = sum(c["paydown_for_30_pct"] for c in cards)
        total_paydown_10 = sum(c["paydown_for_10_pct"] for c in cards)

        recommendations = []
        if not cards:
            recommendations.append("No active credit card accounts found. Link a credit card via Plaid or check your account sync.")
        else:
            if over_50:
                top = max(over_50, key=lambda x: x["utilization_pct"])
                recommendations.append(
                    f"⚠️ High credit utilization on **{top['name']}** ({top['utilization_pct']}%). Pay **${top['paydown_for_30_pct']:,.2f}** to bring it below 30% before statement close."
                )
            elif over_30:
                top = max(over_30, key=lambda x: x["utilization_pct"])
                recommendations.append(
                    f"Notice: **{top['name']}** is at **{top['utilization_pct']}%** utilization. Pay **${top['paydown_for_30_pct']:,.2f}** to drop under 30%."
                )

            if aggregate_util_pct >= 30.0:
                recommendations.append(
                    f"Overall credit utilization is **{aggregate_util_pct:.1f}%**, exceeding the recommended 30% FICO ceiling. Total paydown needed: **${total_paydown_30:,.2f}**."
                )
            elif aggregate_util_pct < 10.0:
                recommendations.append(
                    f"Excellent! Your aggregate utilization is **{aggregate_util_pct:.1f}%**, keeping you in the optimal credit tier."
                )
            else:
                recommendations.append(
                    f"Good standing! Overall utilization is **{aggregate_util_pct:.1f}%**. Pay down **${total_paydown_10:,.2f}** to achieve top-tier (<10%) scoring status."
                )

        return {
            "card_count": len(cards),
            "cards": cards,
            "total_balance": round(total_balance, 2),
            "total_limit": round(total_limit, 2),
            "total_available": round(total_available, 2),
            "aggregate_utilization_pct": round(aggregate_util_pct, 1),
            "overall_tier": agg_tier,
            "overall_badge": agg_badge,
            "cards_over_30_pct": over_30,
            "cards_over_50_pct": over_50,
            "total_paydown_for_30_pct": round(total_paydown_30, 2),
            "total_paydown_for_10_pct": round(total_paydown_10, 2),
            "recommendations": recommendations,
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_credit_utilization_report(data: Dict[str, Any]) -> str:
    """Format credit utilization analysis into a Discord markdown report."""
    cards = data.get("cards", [])
    if not cards:
        return (
            "💳 **Revolving Credit Card Utilization**\n"
            "No active credit card accounts found in your wallet.\n"
            "Link your cards via Plaid to monitor utilization, statement impact, and credit score tiers."
        )

    agg_util = data.get("aggregate_utilization_pct", 0.0)
    badge = data.get("overall_badge", "🟡")
    tier = data.get("overall_tier", "Good")
    tot_bal = data.get("total_balance", 0.0)
    tot_lim = data.get("total_limit", 0.0)
    tot_avail = data.get("total_available", 0.0)

    lines = [
        f"💳 **Revolving Credit Card Utilization & Debt Audit**",
        f"Aggregate Utilization: {badge} **{agg_util:.1f}%** ({tier})",
        f"Total Debt: **${tot_bal:,.2f}** / Limit: **${tot_lim:,.2f}** (Available: **${tot_avail:,.2f}**)\n",
        "**Per-Card Breakdown:**",
    ]

    for c in cards:
        lim_str = f"${c['limit']:,.2f}" if c["limit"] is not None else "No limit set"
        line = f"{c['badge']} **{c['name']}**: **{c['utilization_pct']}%** (${c['balance']:,.2f} / {lim_str})"
        if c["paydown_for_30_pct"] > 0:
            line += f" ➔ Pay **${c['paydown_for_30_pct']:,.2f}** for <30%"
        elif c["paydown_for_10_pct"] > 0:
            line += f" ➔ Pay **${c['paydown_for_10_pct']:,.2f}** for <10%"
        lines.append(line)

    lines.append("\n**Actionable Credit Guidance:**")
    for rec in data.get("recommendations", []):
        lines.append(f"• {rec}")

    return "\n".join(lines)
