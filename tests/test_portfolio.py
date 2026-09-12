from src.services.portfolio import (
    calculate_rebalancing_drift,
    set_holding,
    set_target_allocation,
    get_portfolio_summary,
    format_portfolio_report
)
import pytest

def test_portfolio_isolation():
    with pytest.raises(ValueError):
        calculate_rebalancing_drift(user_id=None, current_allocation={}, target_allocation={}, total_portfolio_value=0)
    with pytest.raises(ValueError):
        set_holding(user_id=None, symbol="VOO", shares=10)
    with pytest.raises(ValueError):
        get_portfolio_summary(user_id=None)

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

def test_portfolio_holdings_crud_and_summary():
    user = "test_investor_999"
    # 1. Set holdings
    res1 = set_holding(user_id=user, symbol="VOO", shares=10.0, cost_basis=450.0, current_price=500.0, asset_class="Equities")
    assert res1["status"] == "success"
    assert res1["shares"] == 10.0

    res2 = set_holding(user_id=user, symbol="BND", shares=50.0, cost_basis=75.0, current_price=70.0, asset_class="Fixed Income")
    assert res2["status"] == "success"

    # 2. Set targets
    t_res = set_target_allocation(user_id=user, targets={"VOO": 0.60, "BND": 0.40})
    assert t_res["status"] == "success"

    # 3. Summary
    summary = get_portfolio_summary(user_id=user)
    assert summary["status"] == "success"
    assert summary["holdings_count"] == 2
    assert summary["total_value"] == (10.0 * 500.0) + (50.0 * 70.0) # 5000 + 3500 = 8500
    assert summary["rebalance"] is not None

    # 4. Format report
    report = format_portfolio_report(user_id=user)
    assert "INVESTMENT PORTFOLIO & REBALANCING AUDIT" in report
    assert "VOO" in report
    assert "BND" in report

    # 5. Delete holding
    del_res = set_holding(user_id=user, symbol="BND", shares=0)
    assert del_res["action"] == "deleted"
