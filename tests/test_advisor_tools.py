import pytest
import sqlite3
import json

from src.services.advisor_tools import ADVISOR_TOOLS_DISPATCH, NEW_50_TOOLS_SCHEMA


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:")
    from src.db.migrations import apply_all
    apply_all(conn)

    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS plaid_accounts (
            plaid_account_id TEXT PRIMARY KEY,
            user_id TEXT,
            name TEXT,
            type TEXT,
            subtype TEXT,
            institution_name TEXT,
            current_balance REAL,
            available_balance REAL,
            credit_limit REAL,
            active INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            account_id TEXT,
            amount REAL,
            date TEXT,
            name TEXT,
            merchant TEXT,
            clean_merchant TEXT,
            category TEXT,
            status TEXT,
            tax_deductible INTEGER DEFAULT 0,
            tax_category TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            debt_name TEXT,
            balance REAL,
            min_payment REAL,
            apr REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscription_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            service_name TEXT,
            amount REAL,
            cadence TEXT,
            due_day INTEGER,
            category TEXT,
            status TEXT,
            trial_end_date TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS merchant_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            raw_pattern TEXT,
            canonical_name TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS transaction_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            transaction_id TEXT,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def test_advisor_tools_total_count():
    assert len(ADVISOR_TOOLS_DISPATCH) == 50
    assert len(NEW_50_TOOLS_SCHEMA) == 50
    # Every tool in schema has a corresponding dispatcher function
    schema_names = {t["function"]["name"] for t in NEW_50_TOOLS_SCHEMA}
    dispatch_names = set(ADVISOR_TOOLS_DISPATCH.keys())
    assert schema_names == dispatch_names


def test_tools_1_to_5_solvency_and_credit(test_db):
    user_id = "test_user_a"
    c = test_db.cursor()
    c.execute("INSERT INTO plaid_accounts (plaid_account_id, user_id, name, type, current_balance, credit_limit, active) VALUES ('c1', ?, 'Visa', 'credit', 1000.0, 5000.0, 1)", (user_id,))
    test_db.commit()

    # 1. get_financial_health_scorecard
    res1 = ADVISOR_TOOLS_DISPATCH["get_financial_health_scorecard"](user_id, {}, test_db)
    assert "total_score" in res1
    assert "grade" in res1

    # 2. get_credit_utilization_breakdown
    res2 = ADVISOR_TOOLS_DISPATCH["get_credit_utilization_breakdown"](user_id, {}, test_db)
    assert res2["aggregate_utilization_pct"] == 20.0
    assert res2["overall_tier"] == "Good"

    # 3. simulate_credit_paydown_impact
    res3 = ADVISOR_TOOLS_DISPATCH["simulate_credit_paydown_impact"](user_id, {"paydown_amount": 600.0}, test_db)
    assert res3["projected_balance"] == 400.0
    assert res3["projected_utilization_pct"] == 8.0
    assert res3["projected_tier"] == "Optimal"

    # 4. get_cash_drag_analysis
    res4 = ADVISOR_TOOLS_DISPATCH["get_cash_drag_analysis"](user_id, {}, test_db)
    assert "total_liquid_cash" in res4

    # 5. calculate_debt_snowball_vs_avalanche
    res5 = ADVISOR_TOOLS_DISPATCH["calculate_debt_snowball_vs_avalanche"](user_id, {"extra_monthly_payment": 50.0}, test_db)
    assert "snowball" in res5
    assert "avalanche" in res5


def test_tools_6_to_10_portfolio_and_allocation(test_db):
    user_id = "test_user_b"

    # 7. set_portfolio_holding
    res7 = ADVISOR_TOOLS_DISPATCH["set_portfolio_holding"](user_id, {"symbol": "VTI", "shares": 10.0, "current_price": 250.0, "cost_basis": 200.0, "asset_class": "Equities"}, test_db)
    assert res7["action"] in ["created", "updated"]

    # 6. get_portfolio_holdings
    res6 = ADVISOR_TOOLS_DISPATCH["get_portfolio_holdings"](user_id, {}, test_db)
    assert res6["total_holdings_count"] == 1
    assert res6["total_portfolio_value"] == 2500.0

    # 8. calculate_portfolio_drift
    res8 = ADVISOR_TOOLS_DISPATCH["calculate_portfolio_drift"](user_id, {"target_allocations": {"Equities": 0.80, "Fixed Income": 0.20}}, test_db)
    assert "rebalance_orders" in res8

    # 9. calculate_compound_growth
    res9 = ADVISOR_TOOLS_DISPATCH["calculate_compound_growth"](user_id, {"principal": 10000.0, "monthly_contribution": 500.0, "annual_return_pct": 7.0, "years": 10}, test_db)
    assert res9["total_future_value"] > 10000.0
    assert len(res9["milestones"]) > 0

    # 10. get_portfolio_dividend_projection
    res10 = ADVISOR_TOOLS_DISPATCH["get_portfolio_dividend_projection"](user_id, {"assumed_yield_pct": 2.5}, test_db)
    assert res10["projected_annual_dividends"] == 62.50


def test_tools_11_to_15_budgeting_and_pacing(test_db):
    user_id = "test_user_c"

    # 11. get_category_budget_pacing
    res11 = ADVISOR_TOOLS_DISPATCH["get_category_budget_pacing"](user_id, {}, test_db)
    assert "period" in res11

    # 12. delete_category_budget
    res12 = ADVISOR_TOOLS_DISPATCH["delete_category_budget"](user_id, {"category": "Dining"}, test_db)
    assert "deleted" in res12

    # 13. auto_generate_50_30_20_budget
    res13 = ADVISOR_TOOLS_DISPATCH["auto_generate_50_30_20_budget"](user_id, {"monthly_after_tax_income": 5000.0}, test_db)
    assert res13["needs"]["amount"] == 2500.0
    assert res13["wants"]["amount"] == 1500.0
    assert res13["savings_and_debt"]["amount"] == 1000.0

    # 14. compare_period_spending
    res14 = ADVISOR_TOOLS_DISPATCH["compare_period_spending"](user_id, {}, test_db)
    assert "recent_period_total" in res14

    # 15. get_daily_spending_average
    res15 = ADVISOR_TOOLS_DISPATCH["get_daily_spending_average"](user_id, {"days": 30}, test_db)
    assert "daily_average" in res15


def test_tools_16_to_20_bills_and_cash_flow(test_db):
    user_id = "test_user_d"

    # 17. add_recurring_bill
    res17 = ADVISOR_TOOLS_DISPATCH["add_recurring_bill"](user_id, {"merchant": "Spotify", "amount": 11.99, "due_day": 15}, test_db)
    assert res17["merchant"] == "Spotify"

    # 16. get_bills_calendar
    res16 = ADVISOR_TOOLS_DISPATCH["get_bills_calendar"](user_id, {"days_ahead": 30}, test_db)
    assert "upcoming_bills" in res16

    # 19. project_cash_balance
    res19 = ADVISOR_TOOLS_DISPATCH["project_cash_balance"](user_id, {"days": 30}, test_db)
    assert "forecast_days" in res19
    assert "end_projected_balance" in res19

    # 20. detect_unusual_bill_increases
    res20 = ADVISOR_TOOLS_DISPATCH["detect_unusual_bill_increases"](user_id, {}, test_db)
    assert "flagged_bill_increases" in res20

    # 18. remove_recurring_bill
    res18 = ADVISOR_TOOLS_DISPATCH["remove_recurring_bill"](user_id, {"merchant": "Spotify"}, test_db)
    assert res18["status"] == "removed"


def test_tools_21_to_25_goals_and_milestones(test_db):
    user_id = "test_user_e"

    # 22. fund_savings_goal
    res22 = ADVISOR_TOOLS_DISPATCH["fund_savings_goal"](user_id, {"name": "New Laptop", "amount": 500.0}, test_db)
    assert res22["goal_name"] == "New Laptop"
    assert res22["current_balance"] == 500.0

    # 21. get_savings_goals
    res21 = ADVISOR_TOOLS_DISPATCH["get_savings_goals"](user_id, {}, test_db)
    assert res21["total_goals_count"] >= 1

    # 24. calculate_goal_timeline
    res24 = ADVISOR_TOOLS_DISPATCH["calculate_goal_timeline"](user_id, {"target_amount": 2000.0, "current_amount": 500.0, "monthly_contribution": 250.0}, test_db)
    assert res24["months_to_completion"] == 6

    # 25. prioritize_savings_goals
    res25 = ADVISOR_TOOLS_DISPATCH["prioritize_savings_goals"](user_id, {}, test_db)
    assert "ranked_goals_by_priority" in res25

    # 23. delete_savings_goal
    res23 = ADVISOR_TOOLS_DISPATCH["delete_savings_goal"](user_id, {"name": "New Laptop"}, test_db)
    assert res23["deleted"] is True


def test_tools_26_to_30_tax_planning(test_db):
    user_id = "test_user_f"

    # 26. scan_tax_deductions
    res26 = ADVISOR_TOOLS_DISPATCH["scan_tax_deductions"](user_id, {"days": 30}, test_db)
    assert "estimated_tax_savings" in res26

    # 27. get_tax_bracket_estimate
    res27 = ADVISOR_TOOLS_DISPATCH["get_tax_bracket_estimate"](user_id, {"annual_gross_income": 85000.0, "filing_status": "single"}, test_db)
    assert res27["taxable_income"] == 70000.0
    assert res27["marginal_tax_bracket_pct"] == 22.0
    assert res27["effective_tax_rate_pct"] > 0

    # 28. get_charitable_donations_summary
    res28 = ADVISOR_TOOLS_DISPATCH["get_charitable_donations_summary"](user_id, {}, test_db)
    assert "total_charitable_donations" in res28

    # 29. calculate_hsa_fsa_tax_savings
    res29 = ADVISOR_TOOLS_DISPATCH["calculate_hsa_fsa_tax_savings"](user_id, {"annual_contribution": 4150.0, "marginal_tax_rate_pct": 22.0}, test_db)
    assert res29["total_tax_savings"] > 1000.0

    # 30. estimate_capital_gains_tax
    res30 = ADVISOR_TOOLS_DISPATCH["estimate_capital_gains_tax"](user_id, {"cost_basis": 5000.0, "sale_price": 8000.0, "holding_period_months": 18}, test_db)
    assert res30["realized_gain"] == 3000.0
    assert res30["classification"] == "Long-Term (>=1 Year)"
    assert res30["estimated_tax_due"] == 450.0


def test_tools_31_to_35_ledger_and_receipts(test_db):
    user_id = "test_user_g"
    c = test_db.cursor()
    c.execute("INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status) VALUES ('tx99', ?, 45.50, '2026-09-01', 'Coffee', 'Starbucks', 'Dining', 'Evaluated')", (user_id,))
    test_db.commit()

    # 31. get_transaction_ledger
    res31 = ADVISOR_TOOLS_DISPATCH["get_transaction_ledger"](user_id, {"limit": 10}, test_db)
    assert res31["count"] == 1
    assert res31["transactions"][0]["merchant"] == "Starbucks"

    # 32. get_transaction_detail
    res32 = ADVISOR_TOOLS_DISPATCH["get_transaction_detail"](user_id, {"transaction_id": "tx99"}, test_db)
    assert res32["amount"] == 45.50
    assert res32["merchant"] == "Starbucks"

    # 33. parse_text_receipt
    res33 = ADVISOR_TOOLS_DISPATCH["parse_text_receipt"](user_id, {"receipt_text": "Target Store\nMilk $3.99\nEggs $4.50\nTotal $8.49"}, test_db)
    assert "line_items" in res33

    # 35. add_merchant_alias_mapping
    res35 = ADVISOR_TOOLS_DISPATCH["add_merchant_alias_mapping"](user_id, {"raw_pattern": "STARBUCKS #1234", "canonical_name": "Starbucks"}, test_db)
    assert res35["status"] == "registered"

    # 34. search_merchants_and_aliases
    res34 = ADVISOR_TOOLS_DISPATCH["search_merchants_and_aliases"](user_id, {"query": "Starbucks"}, test_db)
    assert len(res34["aliases_found"]) >= 1


def test_tools_36_to_40_loans_and_mortgages(test_db):
    user_id = "test_user_h"

    # 36. calculate_loan_amortization
    res36 = ADVISOR_TOOLS_DISPATCH["calculate_loan_amortization"](user_id, {"principal": 300000.0, "annual_interest_rate_pct": 6.5, "term_years": 30}, test_db)
    assert res36["monthly_principal_and_interest"] > 1800.0
    assert res36["total_interest_paid"] > 300000.0

    # 37. calculate_extra_payment_impact
    res37 = ADVISOR_TOOLS_DISPATCH["calculate_extra_payment_impact"](user_id, {"principal": 200000.0, "annual_interest_rate_pct": 6.0, "term_years": 30, "extra_monthly_payment": 200.0}, test_db)
    assert res37["time_shaved_off_years"] > 0
    assert res37["total_interest_saved"] > 0

    # 38. compare_rent_vs_buy
    res38 = ADVISOR_TOOLS_DISPATCH["compare_rent_vs_buy"](user_id, {"home_price": 400000.0, "down_payment_pct": 20.0, "mortgage_rate_pct": 6.5, "monthly_rent": 2200.0, "years": 10}, test_db)
    assert "cumulative_rent_over_horizon" in res38
    assert "cumulative_ownership_over_horizon" in res38

    # 39. calculate_mortgage_refinance_breakeven
    res39 = ADVISOR_TOOLS_DISPATCH["calculate_mortgage_refinance_breakeven"](user_id, {"current_balance": 300000.0, "current_rate_pct": 7.5, "new_rate_pct": 5.5, "remaining_years": 28, "closing_costs": 4000.0}, test_db)
    assert res39["monthly_savings"] > 300.0
    assert res39["breakeven_horizon_months"] < 24
    assert res39["worth_refinancing"] is True

    # 40. calculate_student_loan_payoff
    res40 = ADVISOR_TOOLS_DISPATCH["calculate_student_loan_payoff"](user_id, {"loan_balance": 25000.0, "interest_rate_pct": 5.0, "monthly_payment": 300.0}, test_db)
    assert res40["payoff_duration_months"] > 0


def test_tools_41_to_45_retirement_and_fire(test_db):
    user_id = "test_user_i"

    # 41. calculate_fire_number
    res41 = ADVISOR_TOOLS_DISPATCH["calculate_fire_number"](user_id, {"annual_expenses": 50000.0, "safe_withdrawal_rate_pct": 4.0}, test_db)
    assert res41["fire_target_number"] == 1250000.0

    # 42. calculate_401k_match_maximizer
    res42 = ADVISOR_TOOLS_DISPATCH["calculate_401k_match_maximizer"](user_id, {"annual_salary": 100000.0, "match_pct": 50.0, "match_cap_pct": 6.0, "current_contribution_pct": 3.0}, test_db)
    assert res42["maximum_free_match_dollars"] == 3000.0
    assert res42["actual_employer_match_earned"] == 1500.0
    assert res42["foregone_free_money"] == 1500.0

    # 43. calculate_roth_conversion_tax
    res43 = ADVISOR_TOOLS_DISPATCH["calculate_roth_conversion_tax"](user_id, {"conversion_amount": 20000.0, "current_marginal_bracket_pct": 22.0, "expected_retirement_bracket_pct": 24.0}, test_db)
    assert res43["tax_due_at_conversion"] == 4400.0
    assert res43["conversion_recommended"] is True

    # 44. calculate_required_minimum_distributions
    res44 = ADVISOR_TOOLS_DISPATCH["calculate_required_minimum_distributions"](user_id, {"age": 75, "pre_tax_balance": 600000.0}, test_db)
    assert res44["rmd_required"] is True
    assert res44["annual_rmd_amount"] > 20000.0

    # 45. simulate_retirement_drawdown
    res45 = ADVISOR_TOOLS_DISPATCH["simulate_retirement_drawdown"](user_id, {"starting_portfolio": 1000000.0, "annual_withdrawal": 40000.0, "years": 25}, test_db)
    assert res45["remains_solvent_over_horizon"] is True


def test_tools_46_to_50_fees_and_analytics(test_db):
    user_id = "test_user_j"
    c = test_db.cursor()
    # Insert a bank fee
    c.execute("INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status) VALUES ('fee1', ?, 35.0, date('now', '-2 days'), 'OVERDRAFT CHARGE', 'Bank', 'Fees', 'Evaluated')", (user_id,))
    # Insert duplicate transactions
    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('dup1', ?, 25.0, date('now', '-1 days'), 'Uber', 'Uber', 'Ride', 'Evaluated'),
               ('dup2', ?, 25.0, date('now', '-1 days'), 'Uber', 'Uber', 'Ride', 'Evaluated')
    """, (user_id, user_id))
    test_db.commit()

    # 46. detect_bank_fee_leakage
    res46 = ADVISOR_TOOLS_DISPATCH["detect_bank_fee_leakage"](user_id, {}, test_db)
    assert res46["total_fee_leakage"] == 35.0
    assert len(res46["fee_transactions"]) == 1

    # 47. get_duplicate_transactions
    res47 = ADVISOR_TOOLS_DISPATCH["get_duplicate_transactions"](user_id, {}, test_db)
    assert res47["duplicate_count"] >= 1

    # 48. render_financial_chart
    res48 = ADVISOR_TOOLS_DISPATCH["render_financial_chart"](user_id, {"chart_type": "category"}, test_db)
    assert res48["status"] == "success"
    assert res48["byte_size"] > 0

    # 49. calculate_inflation_erosion
    res49 = ADVISOR_TOOLS_DISPATCH["calculate_inflation_erosion"](user_id, {"cash_amount": 50000.0, "annual_inflation_pct": 3.0, "years": 10}, test_db)
    assert res49["purchasing_power_lost"] > 10000.0

    # 50. generate_weekly_financial_briefing
    res50 = ADVISOR_TOOLS_DISPATCH["generate_weekly_financial_briefing"](user_id, {}, test_db)
    assert res50["briefing_period"] == "Past 7 Days"
    assert "financial_health_score" in res50


def test_zero_interest_and_debt_rollover_logic(test_db):
    user_id = "test_user_zero_rate"

    # Test 0% loan calculations do not throw ZeroDivisionError
    r1 = ADVISOR_TOOLS_DISPATCH["calculate_extra_payment_impact"](user_id, {"principal": 10000.0, "annual_interest_rate_pct": 0.0, "term_years": 5, "extra_monthly_payment": 50.0}, test_db)
    assert r1["standard_monthly_payment"] > 0
    assert r1["total_interest_saved"] == 0.0

    r2 = ADVISOR_TOOLS_DISPATCH["compare_rent_vs_buy"](user_id, {"home_price": 200000.0, "mortgage_rate_pct": 0.0, "monthly_rent": 1200.0}, test_db)
    assert r2["monthly_mortgage_pi"] > 0

    r3 = ADVISOR_TOOLS_DISPATCH["calculate_mortgage_refinance_breakeven"](user_id, {"current_balance": 100000.0, "current_rate_pct": 0.0, "new_rate_pct": 0.0, "remaining_years": 15}, test_db)
    assert r3["monthly_savings"] == 0.0
