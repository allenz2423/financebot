import sqlite3
import random
import math
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple
from src.core.state import DB_PATH

def calculate_stochastic_projection(user_id: str, days_ahead: int = 90, paths: int = 5000) -> str:
    """
    Monte Carlo Cash Flow Simulator.
    Models future probabilities of account balances using empirical historical discretionary
    spending distributions overlaid with deterministic fixed cash flows.
    """
    if days_ahead <= 0 or days_ahead > 365:
        return "ERROR: days_ahead must be between 1 and 365."
        
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            # 1. Get t0 (Current Liquid Balance)
            c.execute("SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'depository' AND active = 1", (user_id,))
            row = c.fetchone()
            t0_balance = row[0] if row and row[0] is not None else 0.0
            
            # 2. Extract Empirical Daily Discretionary Spend
            c.execute("""
                SELECT date, SUM(amount)
                FROM transactions
                WHERE user_id = ? 
                AND amount > 0 
                AND category NOT IN (
                    'Credit Card Bill Payment 💳', 'Financial Transfer 💰', 
                    'Banking & Finance 🏦', 'Financial Transfers 💰', 'P2P Transfers',
                    'Rent', 'Mortgage', 'Loans'
                )
                GROUP BY date
                ORDER BY date ASC
            """, (user_id,))
            
            rows = c.fetchall()
            spend_map = {}
            for r in rows:
                if r[0]:
                    spend_map[r[0][:10]] = float(r[1])
            
            if spend_map:
                start_date = datetime.strptime(min(spend_map.keys()), "%Y-%m-%d")
                end_date = datetime.strptime(max(spend_map.keys()), "%Y-%m-%d")
                
                daily_spend_pool = []
                curr = start_date
                while curr <= end_date:
                    daily_spend_pool.append(spend_map.get(curr.strftime("%Y-%m-%d"), 0.0))
                    curr += timedelta(days=1)
                
                daily_spend_pool.sort()
                p95_idx = int(len(daily_spend_pool) * 0.95)
                filtered_spend_pool = daily_spend_pool[:p95_idx]
            else:
                filtered_spend_pool = [0.0]
                
            if not filtered_spend_pool:
                filtered_spend_pool = [0.0]
                
            avg_daily = sum(filtered_spend_pool) / len(filtered_spend_pool)
            
            # 3. Gather deterministic events
            events_by_date = {} 
            
            c.execute("SELECT expected_date, expected_amount, direction FROM planned_transactions WHERE user_id = ? AND status = 'Expected'", (user_id,))
            for date, amt, direction in c.fetchall():
                if date and amt:
                    signed_amt = float(amt) if direction == 'inbound' else -float(amt)
                    d = date[:10]
                    events_by_date[d] = events_by_date.get(d, 0.0) + signed_amt
                    
            c.execute("SELECT expected_date, net_expected FROM cash_inflows WHERE user_id = ? AND status = 'Pending'", (user_id,))
            for date, amt in c.fetchall():
                if date and amt:
                    d = date[:10]
                    events_by_date[d] = events_by_date.get(d, 0.0) + float(amt)
                    
            c.execute("SELECT last_amount, last_date FROM subscriptions WHERE user_id = ? AND status = 'Active'", (user_id,))
            for last_amt, last_date_str in c.fetchall():
                if last_date_str and last_amt:
                    try:
                        last_date = datetime.strptime(last_date_str[:10], "%Y-%m-%d")
                        for i in range(1, int(days_ahead / 30) + 2):
                            next_date = last_date + timedelta(days=30 * i)
                            d = next_date.strftime("%Y-%m-%d")
                            events_by_date[d] = events_by_date.get(d, 0.0) - float(last_amt)
                    except Exception:
                        pass

            # 4. Monte Carlo Simulation
            today = datetime.now()
            
            final_balances = []
            min_balances = []
            ruined_count = 0
            
            fixed_events = []
            for i in range(1, days_ahead + 1):
                d_str = (today + timedelta(days=i)).strftime("%Y-%m-%d")
                fixed_events.append(events_by_date.get(d_str, 0.0))
                
            daily_trajectories = [[] for _ in range(days_ahead)]
            
            for _ in range(paths):
                bal = t0_balance
                min_b = bal
                ruined = False
                
                for day_idx, fixed_amt in enumerate(fixed_events):
                    discretionary = random.choice(filtered_spend_pool)
                    bal += fixed_amt - discretionary
                    daily_trajectories[day_idx].append(bal)
                    
                    if bal < min_b:
                        min_b = bal
                    if bal < 0:
                        ruined = True
                        
                if ruined:
                    ruined_count += 1
                final_balances.append(bal)
                min_balances.append(min_b)
                
            # 5. Calculate Statistics
            final_balances.sort()
            min_balances.sort()
            
            def percentile(data, p):
                if not data: return 0.0
                idx = int(len(data) * p)
                if idx >= len(data): idx = len(data) - 1
                return data[idx]
                
            p5_final = percentile(final_balances, 0.05)
            p50_final = percentile(final_balances, 0.50)
            p95_final = percentile(final_balances, 0.95)
            
            p5_min = percentile(min_balances, 0.05)
            p50_min = percentile(min_balances, 0.50)
            
            prob_ruin = (ruined_count / paths) * 100.0
            
            # Extract points for Mermaid Chart
            num_points = min(7, days_ahead)
            step = max(1, days_ahead // (num_points - 1)) if num_points > 1 else 1
            x_labels = []
            y_data_p50 = []
            
            for i in range(0, days_ahead, step):
                if i >= days_ahead: break
                day_data = sorted(daily_trajectories[i])
                p50_val = percentile(day_data, 0.50)
                x_labels.append(f"Day {i+1}")
                y_data_p50.append(round(p50_val, 2))
                
            if (days_ahead - 1) % step != 0:
                day_data = sorted(daily_trajectories[-1])
                p50_val = percentile(day_data, 0.50)
                if f"Day {days_ahead}" not in x_labels:
                    x_labels.append(f"Day {days_ahead}")
                    y_data_p50.append(round(p50_val, 2))

            x_axis_str = "[" + ", ".join([f'"{x}"' for x in x_labels]) + "]"
            y_line_str = "[" + ", ".join([str(y) for y in y_data_p50]) + "]"
            
            mermaid_chart = f"```mermaid\nxychart-beta\n    title \"Expected Balance Trajectory (P50)\"\n    x-axis {x_axis_str}\n    y-axis \"Balance ($)\"\n    line {y_line_str}\n```"
            
            report = [
                f"🎲 MONTE CARLO CASH FLOW SIMULATION ({paths} paths)",
                f"Horizon: {days_ahead} days",
                f"Current Liquid Balance: ${t0_balance:.2f}",
                f"Historical Discretionary Daily Spend (Avg): ${avg_daily:.2f} (top 5% outliers removed)",
                "-" * 40,
                f"📊 PROJECTED FINAL BALANCE (Day {days_ahead}):",
                f"  Worst Case (5th Percentile) : ${p5_final:.2f}",
                f"  Expected   (Median)         : ${p50_final:.2f}",
                f"  Best Case  (95th Percentile): ${p95_final:.2f}",
                "",
                "📉 RISK METRICS:",
                f"  Probability of Ruin (< $0)  : {prob_ruin:.1f}%",
                f"  Expected Min Balance (P50)  : ${p50_min:.2f}",
                f"  Worst Min Balance (P5)      : ${p5_min:.2f}",
                "",
                "📈 TRAJECTORY CHART:",
                mermaid_chart
            ]
            
            if prob_ruin > 10.0:
                report.append("\n⚠️ WARNING: High risk of overdraft detected. Recommend reducing daily discretionary spend or liquidating assets.")
            elif prob_ruin == 0.0:
                report.append("\n✅ SAFE: Zero simulated paths resulted in overdraft. Cash flow is highly stable.")
                
            return "\n".join(report)
            
    except Exception as e:
        return f"ERROR running Monte Carlo simulation: {str(e)}"
