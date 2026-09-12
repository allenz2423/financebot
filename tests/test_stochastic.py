import pytest
from src.core.stochastic import calculate_stochastic_projection

def test_stochastic_simulator():
    result = calculate_stochastic_projection('342385739952160769', days_ahead=30, paths=100)
    
    assert "MONTE CARLO CASH FLOW SIMULATION" in result
    assert "PROJECTED FINAL BALANCE" in result
    assert "Probability of Ruin" in result
    assert "```mermaid" in result
    assert "xychart-beta" in result
    
def test_invalid_inputs():
    result = calculate_stochastic_projection('342385739952160769', days_ahead=-5, paths=100)
    assert "ERROR" in result
