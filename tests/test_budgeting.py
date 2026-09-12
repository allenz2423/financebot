import pytest
from src.services.budgeting import (
    predict_next_paydays,
    calculate_locked_liabilities,
    calculate_credit_float_velocity,
    get_safe_to_spend_metrics,
    calculate_emergency_fund_health
)

def test_predict_next_paydays():
    res = predict_next_paydays('342385739952160769')
    assert "Predicted Cadence" in res or "Insufficient" in res

def test_calculate_locked_liabilities():
    res = calculate_locked_liabilities('342385739952160769', '2026-10-01')
    assert "Total Locked Liabilities" in res

def test_calculate_credit_float_velocity():
    res = calculate_credit_float_velocity('342385739952160769')
    assert "Liquid Cash" in res
    assert "Credit Card Float" in res

def test_get_safe_to_spend_metrics():
    res = get_safe_to_spend_metrics('342385739952160769')
    assert "ZERO-BASED BUDGET: SAFE-TO-SPEND" in res

def test_calculate_emergency_fund_health():
    res = calculate_emergency_fund_health('342385739952160769')
    assert "EMERGENCY FUND HEALTH & RUNWAY AUDIT" in res
    assert "Bare-Bones Survival Runway" in res
