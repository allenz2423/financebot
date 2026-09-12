import pytest
from src.services.sandbox import run_what_if_scenario

def test_sandbox_what_if():
    mutations = [
        {"type": "add_expense", "name": "Test Expense", "amount": 1000},
        {"type": "add_cash", "amount": 500}
    ]
    res = run_what_if_scenario('342385739952160769', "Test Scenario", mutations, days_ahead=15)
    
    assert "WHAT-IF SCENARIO: Test Scenario" in res
    assert "BASELINE REALITY" in res
    assert "ALTERNATE REALITY" in res
    assert "Test Expense ($1000)" in res
    assert "add_cash" in res
