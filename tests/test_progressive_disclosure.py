import pytest
from unittest.mock import patch, MagicMock
from src.services.llm import build_advisor_context

def test_build_advisor_context_progressive_index():
    with patch("src.services.llm.get_weekly_spending", return_value=45.50), \
         patch("src.services.llm.get_budget_settings", return_value=(150.0, 50.0)), \
         patch("src.services.llm.get_current_financial_position", return_value={
             "available": True,
             "total_liquid": 1250.00,
             "net_cash": 1250.00,
             "all_balances": "Checking: $1250.00"
         }), \
         patch("src.services.llm.get_recent_financial_activity", return_value=[
             {"date": "2026-09-10", "merchant": "Target", "category": "Groceries", "amount": 35.00}
         ]), \
         patch("src.services.llm.get_recent_income", return_value=[]), \
         patch("src.services.llm.get_savings_buckets", return_value=[
             {"name": "Emergency Fund", "current": 500.0, "target": 1000.0}
         ]), \
         patch("src.services.llm.get_subscriptions", return_value=[
             {"merchant": "Netflix", "amount": 15.99, "last_date": "2026-09-01", "status": "Active"}
         ]):
        
        # Layer 1 Index test
        index_context = build_advisor_context(user_id="test_user", detailed_dump=False)
        assert "FINANCIAL GROUND TRUTH INDEX (Progressive Disclosure - Layer 1)" in index_context
        assert "get_current_financial_position" in index_context
        assert "Liquid: $1,250.00" in index_context
        assert "query_spending" in index_context
        assert "~50 tok" in index_context

        # Detailed dump test
        detailed_context = build_advisor_context(user_id="test_user", detailed_dump=True)
        assert "CURRENT FINANCIAL POSITION:" in detailed_context
        assert "Liquid checking/cash: $1,250.00" in detailed_context
        assert "Target" in detailed_context
        assert "Netflix" in detailed_context
