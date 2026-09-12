"""
Delilah Financial OS - High-Yield Cash Drag & Uninvested Idle Cash Optimizer.

Identifies excess, uninvested idle cash sitting in low-yield traditional checking
accounts earning near 0% APY. Computes monthly operating expense burn rate, establishes
a safe checking buffer (default: 1.5x monthly expenses), quantifies annual and monthly
lost yield compared to a high-yield savings / Treasury money market benchmark (default: 4.5% APY),
and provides deterministic, actionable cash reallocation transfer instructions.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from src.core.state import DB_PATH
from src.db.migrations import apply_all


def _is_savings_account(name: str, subtype: Optional[str]) -> bool:
    """Classify whether a depository account is savings / money market or checking."""
    sub = (subtype or "").strip().lower()
    nm = (name or "").strip().lower()

    if sub in ("savings", "money market", "cd", "certificate of deposit", "hysa"):
        return True
    if any(k in nm for k in ("savings", "hysa", "high yield", "money market", "mmf", "treasury")):
        return True
    return False


def analyze_cash_drag(
    *,
    user_id: str,
    conn: Optional[sqlite3.Connection] = None,
    benchmark_apy: float = 0.045,  # 4.5% APY benchmark (HYSA / Treasury MMF)
    buffer_months: float = 1.5,    # 1.5x monthly operating expense buffer
) -> Dict[str, Any]:
    """Analyze depository balances and quantify cash drag from excess checking balances."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "analyze_cash_drag: forced isolation violation — user_id is required and must be a non-empty string."
        )

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        try:
            apply_all(db_conn)
        except Exception:
            pass

        c = db_conn.cursor()

        # 1. Fetch active depository accounts
        c.execute(
            """
            SELECT plaid_account_id, name, current_balance, available_balance, subtype, institution_name
            FROM plaid_accounts
            WHERE user_id = ? AND type = 'depository' AND active = 1
            ORDER BY current_balance DESC
            """,
            (user_id,),
        )
        rows = c.fetchall()

        checking_accounts: List[Dict[str, Any]] = []
        savings_accounts: List[Dict[str, Any]] = []
        total_checking_cash = 0.0
        total_savings_cash = 0.0

        for r in rows:
            acc_id, name, curr_bal, avail_bal, subtype, inst = r
            bal = max(0.0, float(curr_bal or 0.0))
            avail = float(avail_bal) if avail_bal is not None else bal
            acc_info = {
                "account_id": acc_id,
                "name": name or "Bank Account",
                "institution": inst or "Bank",
                "balance": round(bal, 2),
                "available": round(avail, 2),
                "subtype": subtype or "checking",
            }

            if _is_savings_account(name or "", subtype):
                savings_accounts.append(acc_info)
                total_savings_cash += bal
            else:
                checking_accounts.append(acc_info)
                total_checking_cash += bal

        # 2. Compute 30-day operating expenses to determine monthly burn rate
        c.execute(
            """
            SELECT SUM(amount)
            FROM transactions
            WHERE user_id = ?
              AND status = 'Evaluated'
              AND amount > 0
              AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer', 'Loan Repayment')
              AND merchant NOT LIKE '%System Balance Sync%'
              AND date >= date('now', '-30 days')
            """,
            (user_id,),
        )
        exp_row = c.fetchone()
        past_30d_spent = float(exp_row[0]) if exp_row and exp_row[0] is not None else 0.0

        # If recent 30d spend is 0, check 90-day spend normalized to monthly
        if past_30d_spent <= 0:
            c.execute(
                """
                SELECT SUM(amount)
                FROM transactions
                WHERE user_id = ?
                  AND status = 'Evaluated'
                  AND amount > 0
                  AND category NOT IN ('Credit Card Bill Payment 💳', 'Transfer', 'Loan Repayment')
                  AND merchant NOT LIKE '%System Balance Sync%'
                  AND date >= date('now', '-90 days')
                """,
                (user_id,),
            )
            exp_90 = c.fetchone()
            past_90d_spent = float(exp_90[0]) if exp_90 and exp_90[0] is not None else 0.0
            monthly_burn = past_90d_spent / 3.0 if past_90d_spent > 0 else 0.0
        else:
            monthly_burn = past_30d_spent

        # Recommended checking buffer: buffer_months * monthly_burn (floor at $1,000 for safety)
        if monthly_burn > 0:
            recommended_buffer = max(1000.0, monthly_burn * buffer_months)
        else:
            # If no history, assume a prudent minimum operating buffer of $2,000
            recommended_buffer = 2000.0

        excess_idle_cash = max(0.0, total_checking_cash - recommended_buffer)
        annual_lost_interest = excess_idle_cash * benchmark_apy
        monthly_lost_interest = annual_lost_interest / 12.0

        # Cash allocation ratio
        total_liquid = total_checking_cash + total_savings_cash
        checking_pct = (total_checking_cash / total_liquid * 100.0) if total_liquid > 0 else 0.0
        savings_pct = (total_savings_cash / total_liquid * 100.0) if total_liquid > 0 else 0.0

        action_plan: List[str] = []
        if not checking_accounts and not savings_accounts:
            action_plan.append("No active depository bank accounts found. Link your accounts via Plaid to audit cash drag.")
        elif excess_idle_cash < 250.0:
            action_plan.append(
                f"✅ Optimal checking allocation! Your checking cash (${total_checking_cash:,.2f}) is right in line with your {buffer_months:.1f}x operating buffer (${recommended_buffer:,.2f})."
            )
        else:
            target_dest = savings_accounts[0]["name"] if savings_accounts else "a high-yield savings account (HYSA) or Treasury MMF (4.5%+ APY)"
            source_acc = checking_accounts[0]["name"] if checking_accounts else "Checking"
            action_plan.append(
                f"Move **${excess_idle_cash:,.2f}** from **{source_acc}** to **{target_dest}**."
            )
            action_plan.append(
                f"Earning **${annual_lost_interest:,.2f}/yr** (**${monthly_lost_interest:,.2f}/mo**) extra risk-free yield while leaving a healthy **${recommended_buffer:,.2f}** operating buffer in checking."
            )

        return {
            "total_liquid_cash": round(total_liquid, 2),
            "total_checking_cash": round(total_checking_cash, 2),
            "total_savings_cash": round(total_savings_cash, 2),
            "checking_pct": round(checking_pct, 1),
            "savings_pct": round(savings_pct, 1),
            "monthly_burn_rate": round(monthly_burn, 2),
            "buffer_months": buffer_months,
            "recommended_buffer": round(recommended_buffer, 2),
            "excess_idle_cash": round(excess_idle_cash, 2),
            "benchmark_apy_pct": round(benchmark_apy * 100.0, 2),
            "annual_lost_interest": round(annual_lost_interest, 2),
            "monthly_lost_interest": round(monthly_lost_interest, 2),
            "checking_accounts": checking_accounts,
            "savings_accounts": savings_accounts,
            "action_plan": action_plan,
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_cash_drag_report(data: Dict[str, Any]) -> str:
    """Format cash drag analysis into a Discord markdown report."""
    total_liquid = data.get("total_liquid_cash", 0.0)
    if total_liquid <= 0 and not data.get("checking_accounts"):
        return (
            "🏦 **High-Yield Cash Drag & Idle Cash Audit**\n"
            "No active bank accounts found in your wallet.\n"
            "Link your accounts via Plaid to detect uninvested cash and optimize yield."
        )

    idle = data.get("excess_idle_cash", 0.0)
    chk_bal = data.get("total_checking_cash", 0.0)
    sav_bal = data.get("total_savings_cash", 0.0)
    rec_buf = data.get("recommended_buffer", 0.0)
    burn = data.get("monthly_burn_rate", 0.0)
    apy = data.get("benchmark_apy_pct", 4.5)
    ann_loss = data.get("annual_lost_interest", 0.0)
    mo_loss = data.get("monthly_lost_interest", 0.0)

    status_badge = "🚨" if idle >= 2500 else ("⚠️" if idle > 250 else "🟢")

    lines = [
        f"🏦 **High-Yield Cash Drag & Idle Cash Optimizer**",
        f"Status: {status_badge} **{'High Cash Drag' if idle >= 2500 else ('Moderate Idle Cash' if idle > 250 else 'Optimized Liquid Cash')}**",
        f"Total Liquid Cash: **${total_liquid:,.2f}** (Checking: **${chk_bal:,.2f}** · Savings/MMF: **${sav_bal:,.2f}**)\n",
        "**Operating Buffer Analysis:**",
        f"• Monthly Operating Burn: **${burn:,.2f}/mo**",
        f"• Prudent Checking Buffer (1.5x): **${rec_buf:,.2f}**",
        f"• Dormant Idle Cash in Checking: **${idle:,.2f}**\n",
        f"**Opportunity Cost ({apy:.1f}% APY Benchmark):**",
        f"• Foregone Annual Interest: **-${ann_loss:,.2f}/yr**",
        f"• Foregone Monthly Interest: **-${mo_loss:,.2f}/mo**\n",
        "**Action Plan:**",
    ]

    for step in data.get("action_plan", []):
        lines.append(f"• {step}")

    return "\n".join(lines)
