"""
Delilah Financial OS - Unified Balance Sheet & Net Worth Engine.

Aggregates all depository cash, portfolio holdings, sinking fund reserves,
and liabilities (revolving credit + term loans) to compute an authoritative
balance sheet, leverage ratios, and historical net worth trajectory.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import discord

from src.core.state import DB_PATH
from src.db.migrations import apply_all


def _ensure_tables(conn: sqlite3.Connection) -> None:
    apply_all(conn)


def calculate_unified_net_worth(
    user_id: str,
    conn: Optional[sqlite3.Connection] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Compute comprehensive balance sheet, solvency ratios, and net worth trajectory.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("calculate_unified_net_worth: forced isolation violation — user_id is required")

    dt = now or datetime.now()

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        _ensure_tables(db_conn)
        c = db_conn.cursor()

        # 1. Depository Cash (Plaid accounts)
        c.execute("""
            SELECT name, current_balance, available_balance, subtype
            FROM plaid_accounts
            WHERE user_id = ? AND type = 'depository' AND active = 1
            ORDER BY current_balance DESC
        """, (user_id,))
        depository_accounts = []
        total_depository_cash = 0.0
        for r in c.fetchall():
            bal = float(r[1] or 0.0)
            avail = float(r[2]) if r[2] is not None else bal
            total_depository_cash += bal
            depository_accounts.append({
                "name": r[0] or "Checking/Savings",
                "balance": bal,
                "available": avail,
                "subtype": r[3] or "checking",
            })

        # 2. Portfolio Holdings (Equities, ETFs, Crypto, Fixed Income)
        c.execute("""
            SELECT symbol, shares, cost_basis, current_price, asset_class
            FROM investment_holdings
            WHERE user_id = ?
            ORDER BY (shares * current_price) DESC
        """, (user_id,))
        holdings = []
        total_investments_value = 0.0
        total_investments_cost = 0.0
        asset_class_breakdown: Dict[str, float] = {}

        for r in c.fetchall():
            sym, shares, cost, price, aclass = r[0], float(r[1]), float(r[2] or 0.0), float(r[3] or 0.0), r[4] or "Equities"
            mkt_val = shares * price
            cost_val = shares * cost
            total_investments_value += mkt_val
            total_investments_cost += cost_val
            asset_class_breakdown[aclass] = asset_class_breakdown.get(aclass, 0.0) + mkt_val
            holdings.append({
                "symbol": sym,
                "shares": shares,
                "current_price": price,
                "cost_basis": cost,
                "market_value": round(mkt_val, 2),
                "unrealized_pnl": round(mkt_val - cost_val, 2),
                "asset_class": aclass,
            })

        # 3. Sinking Funds & Savings Buckets
        c.execute("""
            SELECT name, current_amount, target_amount, category
            FROM savings_buckets
            WHERE user_id = ?
            ORDER BY current_amount DESC
        """, (user_id,))
        sinking_funds = []
        total_sinking_funds = 0.0
        for r in c.fetchall():
            cur_amt = float(r[1] or 0.0)
            tgt_amt = float(r[2] or 0.0)
            total_sinking_funds += cur_amt
            sinking_funds.append({
                "name": r[0],
                "current_amount": cur_amt,
                "target_amount": tgt_amt,
                "category": r[3] or "General",
            })

        # 4. Credit Card Debt (Plaid Credit Accounts)
        c.execute("""
            SELECT plaid_account_id, name, current_balance, credit_limit
            FROM plaid_accounts
            WHERE user_id = ? AND type = 'credit' AND active = 1
        """, (user_id,))
        credit_accounts = []
        total_credit_debt = 0.0
        plaid_debt_ids = set()

        for r in c.fetchall():
            paid_id, name, bal, limit = r[0], r[1], float(r[2] or 0.0), float(r[3] or 0.0)
            if paid_id:
                plaid_debt_ids.add(paid_id)
            total_credit_debt += bal
            credit_accounts.append({
                "name": name or "Credit Card",
                "balance": bal,
                "limit": limit,
            })

        # 5. Term Loans & User Debts (Student, Auto, Mortgage, Personal)
        c.execute("""
            SELECT id, name, balance, apr, min_payment, plaid_account_id
            FROM user_debts
            WHERE user_id = ?
        """, (user_id,))
        debts = []
        total_term_loans = 0.0

        for r in c.fetchall():
            did, name, bal, apr, min_p, paid_id = r[0], r[1], float(r[2] or 0.0), float(r[3] or 0.0), float(r[4] or 0.0), r[5]
            # Avoid double-counting if already included from Plaid accounts
            if paid_id and paid_id in plaid_debt_ids:
                continue
            total_term_loans += bal
            debts.append({
                "id": did,
                "name": name,
                "balance": bal,
                "apr": apr,
                "min_payment": min_p,
            })

        # Totals
        total_assets = total_depository_cash + total_investments_value + total_sinking_funds
        total_liabilities = total_credit_debt + total_term_loans
        net_worth = total_assets - total_liabilities

        # Leverage & Solvency Ratios
        debt_to_asset_ratio = round(total_liabilities / total_assets, 4) if total_assets > 0 else (1.0 if total_liabilities > 0 else 0.0)

        if net_worth < 0:
            solvency_status = "Negative Equity (Insolvent)"
            solvency_icon = "🚨"
        elif debt_to_asset_ratio >= 0.75:
            solvency_status = "High Leverage (Vulnerable)"
            solvency_icon = "⚠️"
        elif debt_to_asset_ratio >= 0.35:
            solvency_status = "Moderate Leverage (Balanced)"
            solvency_icon = "⚖️"
        else:
            solvency_status = "Fortress Solvency (Low Leverage)"
            solvency_icon = "🛡️"

        # 6. Historical Net Worth Trajectory (30d, 90d, 365d)
        def _get_historical_snapshot(days_ago: int) -> Optional[float]:
            target_ts = (dt - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("""
                SELECT net_worth
                FROM financial_snapshots
                WHERE user_id = ? AND captured_at <= ?
                ORDER BY captured_at DESC LIMIT 1
            """, (user_id, target_ts))
            row = c.fetchone()
            return float(row[0]) if row and row[0] is not None else None

        nw_30d = _get_historical_snapshot(30)
        nw_90d = _get_historical_snapshot(90)
        nw_365d = _get_historical_snapshot(365)

        def _calc_delta(past_val: Optional[float]) -> Optional[Dict[str, Any]]:
            if past_val is None:
                return None
            diff = net_worth - past_val
            pct = (diff / abs(past_val) * 100.0) if past_val != 0 else 0.0
            return {
                "past_net_worth": round(past_val, 2),
                "diff": round(diff, 2),
                "pct_change": round(pct, 2),
            }

        return {
            "user_id": user_id,
            "calculated_at": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "assets": {
                "depository_cash": round(total_depository_cash, 2),
                "investments_value": round(total_investments_value, 2),
                "investments_cost_basis": round(total_investments_cost, 2),
                "investments_unrealized_pnl": round(total_investments_value - total_investments_cost, 2),
                "asset_classes": {k: round(v, 2) for k, v in asset_class_breakdown.items()},
                "sinking_funds": round(total_sinking_funds, 2),
                "total_assets": round(total_assets, 2),
                "depository_accounts": depository_accounts,
                "holdings_count": len(holdings),
            },
            "liabilities": {
                "credit_card_debt": round(total_credit_debt, 2),
                "term_loans_debt": round(total_term_loans, 2),
                "total_liabilities": round(total_liabilities, 2),
                "credit_accounts": credit_accounts,
                "term_loans": debts,
            },
            "net_worth": round(net_worth, 2),
            "solvency": {
                "debt_to_asset_ratio": debt_to_asset_ratio,
                "debt_to_asset_pct": round(debt_to_asset_ratio * 100.0, 1),
                "status": solvency_status,
                "icon": solvency_icon,
            },
            "history": {
                "delta_30d": _calc_delta(nw_30d),
                "delta_90d": _calc_delta(nw_90d),
                "delta_365d": _calc_delta(nw_365d),
            }
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def _format_currency(val: float) -> str:
    if val < 0:
        return f"-${abs(val):,.2f}"
    return f"${val:,.2f}"


def format_net_worth_embed(data: Dict[str, Any]) -> discord.Embed:
    """Format unified net worth calculation into a polished Discord embed."""
    assets = data["assets"]
    liab = data["liabilities"]
    nw = data["net_worth"]
    solv = data["solvency"]
    hist = data["history"]

    color = discord.Color.green() if nw >= 0 else discord.Color.red()
    embed = discord.Embed(
        title="🏛️ Balance Sheet & Net Worth Statement",
        color=color,
        description=(
            f"**Net Worth:** **{_format_currency(nw)}**\n"
            f"**Solvency Rating:** {solv['icon']} **{solv['status']}** (Leverage: {solv['debt_to_asset_pct']}%)"
        )
    )

    # Assets breakdown
    asset_lines = [
        f"• Liquid Cash: **${assets['depository_cash']:,.2f}**",
        f"• Investments: **${assets['investments_value']:,.2f}** ({assets['holdings_count']} holdings)",
        f"• Sinking Funds: **${assets['sinking_funds']:,.2f}**",
        f"**Total Assets: ${assets['total_assets']:,.2f}**"
    ]
    embed.add_field(name="💼 Assets", value="\n".join(asset_lines), inline=True)

    # Liabilities breakdown
    liab_lines = [
        f"• Credit Cards: **${liab['credit_card_debt']:,.2f}**",
        f"• Term Loans / Debt: **${liab['term_loans_debt']:,.2f}**",
        f"**Total Liabilities: ${liab['total_liabilities']:,.2f}**"
    ]
    embed.add_field(name="💳 Liabilities", value="\n".join(liab_lines), inline=True)

    # Historical momentum
    momentum = []
    if hist.get("delta_30d"):
        d30 = hist["delta_30d"]
        sign = "+" if d30["diff"] >= 0 else ""
        momentum.append(f"• **30-Day:** {sign}${d30['diff']:,.2f} ({sign}{d30['pct_change']}%)")
    if hist.get("delta_90d"):
        d90 = hist["delta_90d"]
        sign = "+" if d90["diff"] >= 0 else ""
        momentum.append(f"• **90-Day:** {sign}${d90['diff']:,.2f} ({sign}{d90['pct_change']}%)")

    if momentum:
        embed.add_field(name="📈 Net Worth Momentum", value="\n".join(momentum), inline=False)

    if assets.get("asset_classes"):
        ac_lines = [
            f"• {ac}: ${val:,.2f}"
            for ac, val in assets["asset_classes"].items()
        ]
        embed.add_field(name="📊 Portfolio Allocation", value="\n".join(ac_lines)[:1024], inline=False)

    embed.set_footer(text=f"As of {data['calculated_at']} · Use !portfolio for holdings, !debts for loan detail")
    return embed


def format_net_worth_report(data: Dict[str, Any]) -> str:
    """Format unified net worth calculation into a text report."""
    assets = data["assets"]
    liab = data["liabilities"]
    nw = data["net_worth"]
    solv = data["solvency"]
    hist = data["history"]

    lines = [
        "🏛️ BALANCE SHEET & AUTHORITATIVE NET WORTH STATEMENT",
        f"Generated At: {data['calculated_at']}",
        "=" * 55,
        f"TOTAL ASSETS:               ${assets['total_assets']:,.2f}",
        f"  ↳ Liquid Cash:            ${assets['depository_cash']:,.2f}",
        f"  ↳ Investment Portfolio:   ${assets['investments_value']:,.2f}",
        f"  ↳ Sinking Reserves:       ${assets['sinking_funds']:,.2f}",
        "-" * 55,
        f"TOTAL LIABILITIES:          ${liab['total_liabilities']:,.2f}",
        f"  ↳ Revolving Credit Debt:  ${liab['credit_card_debt']:,.2f}",
        f"  ↳ Term Loans & Notes:     ${liab['term_loans_debt']:,.2f}",
        "=" * 55,
        f"NET WORTH:                  {_format_currency(nw)}",
        f"Solvency Rating:            {solv['icon']} {solv['status']}",
        f"Debt-to-Asset Leverage:     {solv['debt_to_asset_pct']}%",
    ]

    if hist.get("delta_30d"):
        d30 = hist["delta_30d"]
        sign = "+" if d30["diff"] >= 0 else ""
        lines.append(f"30-Day Change:              {sign}${d30['diff']:,.2f} ({sign}{d30['pct_change']}%)")

    return "\n".join(lines)
