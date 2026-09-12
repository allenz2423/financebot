import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from src.core.state import DB_PATH

def predict_next_paydays(user_id: str) -> str:
    """
    Analyzes historical income transactions to mathematically infer the cadence,
    date, and expected amount of the user's next paychecks.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # Find all income transactions
            c.execute("""
                SELECT date, amount, merchant
                FROM transactions
                WHERE user_id = ? AND amount < 0 AND (category LIKE '%Income%' OR category LIKE '%Payroll%' OR merchant LIKE '%PAYROLL%')
                ORDER BY date ASC
            """, (user_id,))
            
            income_txs = c.fetchall()
            if len(income_txs) < 2:
                return "Insufficient historical income data to predict cadence."
                
            # Naive cadence detection
            dates = []
            amounts = []
            for date_str, amt, merch in income_txs:
                try:
                    dates.append(datetime.strptime(date_str[:10], "%Y-%m-%d"))
                    amounts.append(abs(float(amt)))
                except:
                    pass
                    
            if len(dates) < 2:
                return "Insufficient valid date formats."
                
            deltas = [(dates[i] - dates[i-1]).days for i in range(1, len(dates))]
            avg_delta = sum(deltas) / len(deltas)
            avg_amount = sum(amounts) / len(amounts)
            
            cadence = "Bi-weekly"
            if 6 <= avg_delta <= 8:
                cadence = "Weekly"
            elif 13 <= avg_delta <= 16:
                cadence = "Bi-weekly"
            elif 28 <= avg_delta <= 32:
                cadence = "Monthly"
            else:
                cadence = f"Irregular (avg {avg_delta:.1f} days)"
                
            last_payday = dates[-1]
            next_payday = last_payday + timedelta(days=avg_delta)
            
            if next_payday < datetime.now():
                # Fast forward to future
                while next_payday < datetime.now():
                    next_payday += timedelta(days=avg_delta)
            
            report = [
                f"Predicted Cadence: {cadence}",
                f"Average Paycheck Amount: ${avg_amount:.2f}",
                f"Next Predicted Payday: {next_payday.strftime('%Y-%m-%d')}",
                f"Following Payday: {(next_payday + timedelta(days=avg_delta)).strftime('%Y-%m-%d')}"
            ]
            return "\n".join(report)
    except Exception as e:
        return f"ERROR predicting paydays: {str(e)}"

def calculate_locked_liabilities(user_id: str, until_date: str) -> str:
    """
    Calculates exact cash required for fixed bills, subscriptions, and planned transactions
    that will hit BEFORE the specified until_date.
    """
    try:
        target = datetime.strptime(until_date, "%Y-%m-%d")
        total_liability = 0.0
        details = []
        
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            # Subscriptions
            c.execute("SELECT merchant, last_amount, last_date FROM subscriptions WHERE user_id = ? AND status = 'Active'", (user_id,))
            for merchant, amt, last_date_str in c.fetchall():
                try:
                    last_date = datetime.strptime(last_date_str[:10], "%Y-%m-%d")
                    next_date = last_date + timedelta(days=30)
                    while next_date < target:
                        if next_date > datetime.now():
                            total_liability += float(amt)
                            details.append(f"Sub: {merchant} (${amt:.2f}) on {next_date.strftime('%Y-%m-%d')}")
                        next_date += timedelta(days=30)
                except:
                    pass
                    
            # Planned Transactions
            c.execute("SELECT description, expected_amount, expected_date FROM planned_transactions WHERE user_id = ? AND status = 'Expected' AND direction = 'outbound'", (user_id,))
            for desc, amt, exp_date in c.fetchall():
                try:
                    d = datetime.strptime(exp_date[:10], "%Y-%m-%d")
                    if datetime.now() < d < target:
                        total_liability += float(amt)
                        details.append(f"Planned: {desc} (${amt:.2f}) on {exp_date[:10]}")
                except:
                    pass
                    
        report = [f"Total Locked Liabilities until {until_date}: ${total_liability:.2f}"]
        report.extend(details)
        return "\n".join(report)
    except Exception as e:
        return f"ERROR calculating liabilities: {str(e)}"

def calculate_credit_float_velocity(user_id: str) -> str:
    """
    Calculates the user's reliance on the credit card float.
    Compares total liquid cash against total credit card debt, and analyzes
    the 30-day derivative of debt to determine if float is expanding or shrinking.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            # Liquid Cash
            c.execute("SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'depository' AND active = 1", (user_id,))
            liquid = c.fetchone()[0] or 0.0
            
            # CC Float
            c.execute("SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'credit' AND subtype = 'credit card' AND active = 1", (user_id,))
            cc_debt = c.fetchone()[0] or 0.0
            
            net_liquidity = liquid - cc_debt
            
            # 30-day spend vs 30-day income
            thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            c.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND date >= ? AND amount > 0 AND category != 'Credit Card Bill Payment 💳'", (user_id, thirty_days_ago))
            spend = c.fetchone()[0] or 0.0
            
            c.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND date >= ? AND amount < 0 AND category != 'Credit Card Bill Payment 💳'", (user_id, thirty_days_ago))
            income = abs(c.fetchone()[0] or 0.0)
            
            deficit = spend - income
            
            report = [
                f"Liquid Cash: ${liquid:.2f}",
                f"Credit Card Float (Debt): ${cc_debt:.2f}",
                f"Net Liquidity: ${net_liquidity:.2f}",
                f"30-Day Discretionary Spend: ${spend:.2f}",
                f"30-Day Realized Income: ${income:.2f}",
            ]
            
            if net_liquidity < 0:
                report.append("STATUS: RIDING THE FLOAT (High Risk)")
                if deficit > 0:
                    report.append(f"VELOCITY: Debt is EXPANDING by ~${deficit:.2f}/month. You are mathematically falling behind.")
                else:
                    months_to_escape = abs(net_liquidity) / abs(deficit) if deficit != 0 else 999
                    report.append(f"VELOCITY: Debt is SHRINKING. At this rate, it will take {months_to_escape:.1f} months to escape the float.")
            else:
                report.append("STATUS: FULLY LIQUID (Safe)")
                
            return "\n".join(report)
    except Exception as e:
        return f"ERROR calculating float velocity: {str(e)}"

def get_safe_to_spend_metrics(user_id: str) -> str:
    """
    Master Zero-Based Budgeting calculation.
    Determines exactly how much cash is 'Safe to Spend' today by locking away
    money needed for CC float, sinking funds, and upcoming bills before payday.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            c.execute("SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'depository' AND active = 1", (user_id,))
            liquid = c.fetchone()[0] or 0.0
            
            c.execute("SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'credit' AND active = 1", (user_id,))
            cc_float = c.fetchone()[0] or 0.0
            
            c.execute("SELECT SUM(current_amount) FROM savings_buckets WHERE user_id = ?", (user_id,))
            sinking_funds = c.fetchone()[0] or 0.0
            
            # Use predict_next_paydays logic to get date
            # For simplicity, we just look at the next 14 days
            target_date = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
            
            # calculate liabilities
            # (simplified internal logic for speed)
            total_liability = 0.0
            c.execute("SELECT expected_amount FROM planned_transactions WHERE user_id = ? AND status = 'Expected' AND direction = 'outbound' AND expected_date < ?", (user_id, target_date))
            for row in c.fetchall():
                total_liability += float(row[0] or 0.0)
                
            true_free_cash = liquid - cc_float - sinking_funds - total_liability
            daily_safe = true_free_cash / 14.0 if true_free_cash > 0 else 0.0
            
            report = [
                "🛡️ ZERO-BASED BUDGET: SAFE-TO-SPEND (14-Day Horizon)",
                f"Gross Liquid Cash: ${liquid:.2f}",
                f"[-] Credit Card Float: ${cc_float:.2f} (Already spent)",
                f"[-] Sinking Funds: ${sinking_funds:.2f} (Goal locked)",
                f"[-] Upcoming 14-Day Bills: ${total_liability:.2f}",
                "-" * 30,
                f"True Free Cash: ${true_free_cash:.2f}",
                "",
                f"💵 DAILY SAFE-TO-SPEND: ${daily_safe:.2f}/day"
            ]
            if true_free_cash < 0:
                report.append("\n🚨 WARNING: True Free Cash is negative. You are reliant on future income to cover past spending.")
                
            return "\n".join(report)
    except Exception as e:
        return f"ERROR calculating safe-to-spend: {str(e)}"
