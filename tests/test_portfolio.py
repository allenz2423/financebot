from src.services.portfolio import calculate_rebalancing_drift
import pytest

def test_portfolio_isolation():
    with pytest.raises(ValueError):
        calculate_rebalancing_drift(user_id=None, current_allocation={}, target_allocation={}, total_portfolio_value=0)

def test_portfolio_rebalance():
    current = {"AAPL": 6000, "BND": 4000}
    target = {"AAPL": 0.5, "BND": 0.5}
    res = calculate_rebalancing_drift(user_id="user1", current_allocation=current, target_allocation=target, total_portfolio_value=10000)
    
    assert res["status"] == "success"
    assert res["total_drift_pct"] == 0.1
    assert len(res["recommended_trades"]) == 2
    
    sell_trade = next(t for t in res["recommended_trades"] if t["asset"] == "AAPL")
    assert sell_trade["action"] == "SELL"
    assert sell_trade["amount"] == 1000.0

    buy_trade = next(t for t in res["recommended_trades"] if t["asset"] == "BND")
    assert buy_trade["action"] == "BUY"
    assert buy_trade["amount"] == 1000.0
