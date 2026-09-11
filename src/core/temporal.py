import sqlite3
from typing import List, Dict, Any
from datetime import datetime, timedelta
import json

from src.core.state import DB_PATH

def get_temporal_projection(user_id: str, days_ahead: int = 90) -> str:
    """
    Deterministically simulates the ledger forward in time to calculate projected balances.
    Produces a verifiable state timeline.
    """
    try:
        days_ahead = int(days_ahead)
        if days_ahead <= 0 or days_ahead > 365:
            return "ERROR: days_ahead must be between 1 and 365."
            
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            # 1. Get t0 (Current Liquid Balance)
            # Find the most recent snapshot of liquid balance, or sum accounts.
            c.execute("SELECT SUM(current_balance) FROM plaid_accounts WHERE user_id = ? AND type = 'depository' AND active = 1", (user_id,))
            row = c.fetchone()
            t0_balance = row[0] if row and row[0] is not None else 0.0
            
            # 2. Gather future deterministic events
            events = []
            
            # Planned Transactions
            c.execute("SELECT expected_date, expected_amount, direction, description FROM planned_transactions WHERE user_id = ? AND status = 'Expected'", (user_id,))
            for date, amt, direction, desc in c.fetchall():
                if date and amt:
                    signed_amt = float(amt) if direction == 'inbound' else -float(amt)
                    events.append({"date": date, "amount": signed_amt, "desc": f"Planned: {desc}", "type": "planned"})
            
            # Expected Cash Inflows
            c.execute("SELECT expected_date, net_expected, source FROM cash_inflows WHERE user_id = ? AND status = 'Pending'", (user_id,))
            for date, amt, source in c.fetchall():
                if date and amt:
                    events.append({"date": date, "amount": float(amt), "desc": f"Income: {source}", "type": "income"})
                    
            # Subscriptions (Assume monthly from last_date if active)
            c.execute("SELECT merchant, last_amount, last_date FROM subscriptions WHERE user_id = ? AND status = 'Active'", (user_id,))
            for merchant, last_amt, last_date_str in c.fetchall():
                if last_date_str and last_amt:
                    try:
                        last_date = datetime.strptime(last_date_str[:10], "%Y-%m-%d")
                        # Generate monthly recurring events up to days_ahead
                        for i in range(1, int(days_ahead / 30) + 2):
                            next_date = last_date + timedelta(days=30 * i)
                            events.append({"date": next_date.strftime("%Y-%m-%d"), "amount": -float(last_amt), "desc": f"Sub: {merchant}", "type": "subscription"})
                    except Exception:
                        pass
            
            # 3. Sort events chronologically
            events.sort(key=lambda x: x["date"])
            
            # 4. Simulate timeline
            today = datetime.now()
            end_date = today + timedelta(days=days_ahead)
            end_date_str = end_date.strftime("%Y-%m-%d")
            
            current_balance = t0_balance
            
            timeline = [f"--- TEMPORAL PROJECTION (T0 to T+{days_ahead}) ---"]
            timeline.append(f"T0 ({today.strftime('%Y-%m-%d')}): Starting Liquid Balance = ${current_balance:.2f}")
            
            low_water_mark = current_balance
            low_water_date = today.strftime('%Y-%m-%d')
            
            for ev in events:
                if ev["date"] < today.strftime('%Y-%m-%d'):
                    continue # Skip past expected events that didn't post
                if ev["date"] > end_date_str:
                    break
                    
                current_balance += ev["amount"]
                
                if current_balance < low_water_mark:
                    low_water_mark = current_balance
                    low_water_date = ev["date"]
                    
                timeline.append(f"{ev['date']} | {ev['desc'][:30]:<30} | {ev['amount']:>8.2f} | Bal: ${current_balance:.2f}")
                
            timeline.append("--------------------------------------------------")
            timeline.append(f"FINAL PROJECTED BALANCE (T+{days_ahead}): ${current_balance:.2f}")
            timeline.append(f"LOW WATER MARK: ${low_water_mark:.2f} on {low_water_date}")
            
            return "\n".join(timeline)
    except Exception as e:
        return f"ERROR generating temporal projection: {str(e)}"

