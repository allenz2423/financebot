import pytest
from src.services.intelligence import calculate_lifestyle_creep, allocate_next_best_dollar, analyze_recurring_leakage

def test_lifestyle_creep():
    res = calculate_lifestyle_creep('342385739952160769')
    assert "LIFESTYLE CREEP ANALYSIS" in res

def test_next_best_dollar():
    res = allocate_next_best_dollar('342385739952160769', 10000)
    assert "NEXT BEST DOLLAR" in res
    assert "Emergency Fund" in res
    assert "Blue Cash" in res

def test_recurring_leakage():
    res = analyze_recurring_leakage('342385739952160769', lookback_days=180)
    assert "RECURRING PAYMENT & SUBSCRIPTION LEAKAGE AUDIT" in res or "No definite recurring cadences" in res

