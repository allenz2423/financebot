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
