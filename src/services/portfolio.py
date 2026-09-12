import sqlite3
from datetime import datetime
from typing import Dict, Any, List, Optional
from src.core.state import DB_PATH
from src.db.migrations import apply_all

def _ensure_tables(conn: sqlite3.Connection):
    apply_all(conn)

def calculate_rebalancing_drift(*, user_id: str, current_allocation: dict, target_allocation: dict, total_portfolio_value: float) -> dict:
    """
    Calculates the drift of the current portfolio allocation against a target allocation.
    Returns the required trades to rebalance back to the target.
    
    current_allocation: dict mapping asset symbol -> current value
    target_allocation: dict mapping asset symbol -> target percentage (0.0 to 1.0)
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("calculate_rebalancing_drift: forced isolation violation")

    drift_report = {}
    total_drift = 0.0
    trades = []
    
    for asset, target_pct in target_allocation.items():
        current_val = current_allocation.get(asset, 0.0)
        current_pct = current_val / total_portfolio_value if total_portfolio_value > 0 else 0.0
        
        target_val = total_portfolio_value * target_pct
        drift_val = current_val - target_val
        drift_pct = current_pct - target_pct
        
        drift_report[asset] = {
            "current_pct": round(current_pct, 4),
            "target_pct": round(target_pct, 4),
            "drift_pct": round(drift_pct, 4),
            "drift_val": round(drift_val, 2)
        }
        
        total_drift += abs(drift_pct)
        
        if abs(drift_val) > 0.01:
            trade_type = "SELL" if drift_val > 0 else "BUY"
            trades.append({
                "asset": asset,
                "action": trade_type,
                "amount": round(abs(drift_val), 2)
            })

    # Sort trades by amount descending
    trades.sort(key=lambda x: x["amount"], reverse=True)
    
    return {
        "status": "success",
        "total_drift_pct": round(total_drift / 2, 4), # Total drift is half the sum of absolute drifts
        "asset_drift": drift_report,
        "recommended_trades": trades
    }


def set_holding(*, user_id: str, symbol: str, shares: float, cost_basis: float = 0.0, current_price: float = 0.0, asset_class: str = "Equities") -> dict:
    """Save or update an investment holding."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("set_holding: forced isolation violation")

    sym = symbol.strip().upper()
    if not sym:
        raise ValueError("Symbol cannot be empty.")
    if shares < 0:
        raise ValueError("Shares cannot be negative.")

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_tables(conn)
        c = conn.cursor()
        if shares == 0:
            c.execute("DELETE FROM investment_holdings WHERE user_id = ? AND symbol = ?", (user_id, sym))
            conn.commit()
            return {"status": "success", "action": "deleted", "symbol": sym}

        c.execute("""
            INSERT INTO investment_holdings (user_id, symbol, shares, cost_basis, current_price, asset_class, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                shares = excluded.shares,
                cost_basis = CASE WHEN excluded.cost_basis > 0 THEN excluded.cost_basis ELSE investment_holdings.cost_basis END,
                current_price = CASE WHEN excluded.current_price > 0 THEN excluded.current_price ELSE investment_holdings.current_price END,
                asset_class = excluded.asset_class,
                updated_at = datetime('now')
        """, (user_id, sym, shares, cost_basis, current_price, asset_class))
        conn.commit()

    return {
        "status": "success",
        "action": "saved",
        "symbol": sym,
        "shares": shares,
        "current_price": current_price,
        "cost_basis": cost_basis,
        "asset_class": asset_class
    }


def set_target_allocation(*, user_id: str, targets: Dict[str, float]) -> dict:
    """Set target allocation percentages (e.g. {'VOO': 0.60, 'VXUS': 0.20, 'BND': 0.20})."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("set_target_allocation: forced isolation violation")

    total_pct = sum(targets.values())
    if abs(total_pct - 1.0) > 0.02 and abs(total_pct - 100.0) > 2.0:
        raise ValueError(f"Targets must sum to 1.0 (or 100%). Current sum: {total_pct}")

    normalized = {}
    for sym, val in targets.items():
        pct = val if total_pct <= 1.05 else val / 100.0
        normalized[sym.strip().upper()] = round(pct, 4)

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_tables(conn)
        c = conn.cursor()
        c.execute("DELETE FROM portfolio_targets WHERE user_id = ?", (user_id,))
        for sym, pct in normalized.items():
            c.execute("""
                INSERT INTO portfolio_targets (user_id, symbol, target_pct, updated_at)
                VALUES (?, ?, ?, datetime('now'))
            """, (user_id, sym, pct))
        conn.commit()

    return {"status": "success", "targets": normalized}


def get_portfolio_summary(*, user_id: str) -> dict:
    """Retrieve full portfolio status, asset allocation, and rebalancing recommendations."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("get_portfolio_summary: forced isolation violation")

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_tables(conn)
        c = conn.cursor()
        c.execute("""
            SELECT symbol, shares, cost_basis, current_price, asset_class
            FROM investment_holdings
            WHERE user_id = ?
            ORDER BY (shares * current_price) DESC
        """, (user_id,))
        holdings_rows = c.fetchall()

        c.execute("SELECT symbol, target_pct FROM portfolio_targets WHERE user_id = ?", (user_id,))
        targets_rows = c.fetchall()

    targets = {sym: float(pct) for sym, pct in targets_rows}

    holdings = []
    total_val = 0.0
    total_cost = 0.0
    current_alloc = {}
    asset_classes = {}

    for sym, shares, cost, price, a_class in holdings_rows:
        shares = float(shares)
        cost = float(cost or 0.0)
        price = float(price or cost or 0.0)
        val = shares * price
        invested = shares * cost
        pnl = val - invested
        pnl_pct = (pnl / invested * 100.0) if invested > 0 else 0.0

        total_val += val
        total_cost += invested
        current_alloc[sym] = val
        asset_classes[a_class] = asset_classes.get(a_class, 0.0) + val

        holdings.append({
            "symbol": sym,
            "shares": shares,
            "current_price": price,
            "cost_basis": cost,
            "value": round(val, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "asset_class": a_class
        })

    total_pnl = total_val - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0

    rebalance_data = None
    if targets and total_val > 0:
        rebalance_data = calculate_rebalancing_drift(
            user_id=user_id,
            current_allocation=current_alloc,
            target_allocation=targets,
            total_portfolio_value=total_val
        )

    return {
        "status": "success",
        "total_value": round(total_val, 2),
        "total_cost": round(total_cost, 2),
        "total_unrealized_pnl": round(total_pnl, 2),
        "total_unrealized_pnl_pct": round(total_pnl_pct, 2),
        "holdings_count": len(holdings),
        "holdings": holdings,
        "asset_classes": {k: round(v, 2) for k, v in asset_classes.items()},
        "targets": targets,
        "rebalance": rebalance_data
    }


def format_portfolio_report(*, user_id: str) -> str:
    """Render a human-readable portfolio and rebalancing report."""
    data = get_portfolio_summary(user_id=user_id)
    if data["holdings_count"] == 0:
        return "No investment holdings recorded. Add holdings using `!setholding <symbol> <shares> [price] [cost_basis]`."

    report = [
        "📈 INVESTMENT PORTFOLIO & REBALANCING AUDIT",
        f"Total Portfolio Value: ${data['total_value']:,.2f}",
        f"Cost Basis:            ${data['total_cost']:,.2f}",
        f"Unrealized P&L:        {'+' if data['total_unrealized_pnl'] >= 0 else ''}${data['total_unrealized_pnl']:,.2f} ({data['total_unrealized_pnl_pct']:+.1f}%)",
        "-" * 45,
        "💼 HOLDINGS:"
    ]

    for h in data["holdings"]:
        pct = (h["value"] / data["total_value"] * 100.0) if data["total_value"] > 0 else 0.0
        pnl_str = f"{'+' if h['unrealized_pnl'] >= 0 else ''}${h['unrealized_pnl']:,.2f} ({h['unrealized_pnl_pct']:+.1f}%)"
        report.append(
            f" • **{h['symbol']}**: {h['shares']} shares @ ${h['current_price']:,.2f} = ${h['value']:,.2f} ({pct:.1f}%) | P&L: {pnl_str}"
        )

    report.append("-" * 45)
    report.append("🏷️ ASSET CLASS DIVERSIFICATION:")
    for a_class, val in data["asset_classes"].items():
        pct = (val / data["total_value"] * 100.0) if data["total_value"] > 0 else 0.0
        report.append(f" • {a_class}: ${val:,.2f} ({pct:.1f}%)")

    reb = data.get("rebalance")
    if reb and reb.get("recommended_trades"):
        report.append("-" * 45)
        report.append(f"⚖️ REBALANCING TRADES (Total Drift: {reb['total_drift_pct']*100:.1f}%):")
        for t in reb["recommended_trades"]:
            report.append(f" • **{t['action']}** ${t['amount']:,.2f} of **{t['asset']}**")
    elif data.get("targets"):
        report.append("-" * 45)
        report.append("✅ PORTFOLIO BALANCED: Holdings closely match target allocation.")
    else:
        report.append("-" * 45)
        report.append("💡 Tip: Set target allocations (e.g. `!settarget VOO 60, VXUS 20, BND 20`) to enable drift rebalancing.")

    return "\n".join(report)
