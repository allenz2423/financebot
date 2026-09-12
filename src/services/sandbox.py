import sqlite3
import json
from unittest.mock import patch
from datetime import datetime, timedelta
from typing import Dict, Any, List

from src.core.state import DB_PATH
from src.core.stochastic import calculate_stochastic_projection
from src.services.budgeting import get_safe_to_spend_metrics

def create_sandbox(user_id: str) -> sqlite3.Connection:
    sandbox_conn = sqlite3.connect(":memory:")
    with sqlite3.connect(DB_PATH) as prod_conn:
        for table in ['plaid_accounts', 'transactions', 'planned_transactions', 'cash_inflows', 'subscriptions', 'savings_buckets']:
            cursor = prod_conn.cursor()
            cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
            row = cursor.fetchone()
            if row:
                schema = row[0]
                sandbox_conn.execute(schema)
                cursor.execute(f"SELECT * FROM {table} WHERE user_id = ?", (user_id,))
                rows = cursor.fetchall()
                if rows:
                    placeholders = ",".join(["?"] * len(rows[0]))
                    sandbox_conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    sandbox_conn.commit()
    return sandbox_conn

def apply_mutations(sandbox_conn: sqlite3.Connection, user_id: str, mutations: List[Dict[str, Any]]):
    c = sandbox_conn.cursor()
    for mut in mutations:
        mtype = mut.get("type")
        amount = float(mut.get("amount", 0.0))
        name = mut.get("name", "Simulated Event")
        
        if mtype == "add_cash":
            # Add to the first depository account
            c.execute("SELECT plaid_account_id FROM plaid_accounts WHERE user_id = ? AND type = 'depository' LIMIT 1", (user_id,))
            row = c.fetchone()
            if row:
                c.execute("UPDATE plaid_accounts SET current_balance = current_balance + ? WHERE plaid_account_id = ?", (amount, row[0]))
                
        elif mtype == "add_subscription":
            c.execute(
                "INSERT INTO subscriptions (user_id, merchant, last_amount, last_date, status) VALUES (?, ?, ?, ?, 'Active')",
                (user_id, name, amount, datetime.now().strftime("%Y-%m-%d"))
            )
            
        elif mtype == "add_expense":
            exp_date = mut.get("date", (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"))
            c.execute(
                "INSERT INTO planned_transactions (user_id, description, expected_amount, expected_date, direction, status) VALUES (?, ?, ?, ?, 'outbound', 'Expected')",
                (user_id, name, amount, exp_date)
            )
            
        elif mtype == "change_income":
            # Just add a new pending cash inflow every 14 days
            c.execute(
                "INSERT INTO cash_inflows (user_id, source, net_expected, expected_date, status) VALUES (?, ?, ?, ?, 'Pending')",
                (user_id, name, amount, (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"))
            )
            
    sandbox_conn.commit()

def run_what_if_scenario(user_id: str, scenario_name: str, mutations: List[Dict[str, Any]], days_ahead: int = 30) -> str:
    """
    Parallel Universe Simulation Engine.
    Runs Monte Carlo and Budgeting engines on baseline reality, then clones the database 
    into memory, mutates it based on parameters, and runs them again to generate a delta report.
    """
    try:
        # 1. Baseline Run
        baseline_mc = calculate_stochastic_projection(user_id, days_ahead=days_ahead, paths=1000)
        baseline_budget = get_safe_to_spend_metrics(user_id)
        
        # 2. Setup Sandbox
        sandbox_conn = create_sandbox(user_id)
        apply_mutations(sandbox_conn, user_id, mutations)
        
        # 3. Sandbox Run
        def mock_connect(*args, **kwargs):
            return sandbox_conn

        with patch('sqlite3.connect', side_effect=mock_connect):
            sandbox_mc = calculate_stochastic_projection(user_id, days_ahead=days_ahead, paths=1000)
            sandbox_budget = get_safe_to_spend_metrics(user_id)
            
        sandbox_conn.close()
        
        # 4. Generate Report
        report = [
            f"🌌 WHAT-IF SCENARIO: {scenario_name}",
            "Mutations Applied:",
        ]
        for m in mutations:
            report.append(f" - {m.get('type')}: {m.get('name')} (${m.get('amount')})")
            
        report.extend([
            "",
            "========================================",
            "🔵 BASELINE REALITY (Current State)",
            "========================================",
            baseline_budget,
            "",
            baseline_mc,
            "",
            "========================================",
            "🟠 ALTERNATE REALITY (Simulated State)",
            "========================================",
            sandbox_budget,
            "",
            sandbox_mc,
            "",
            "💡 ANALYSIS:",
            "Compare the 'Probability of Ruin' and 'True Free Cash' between the two realities to understand the risk impact of this scenario."
        ])
        
        return "\n".join(report)
        
    except Exception as e:
        return f"ERROR running What-If scenario: {str(e)}"
