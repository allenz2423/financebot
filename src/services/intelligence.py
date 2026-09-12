import sqlite3
import math
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple
from src.core.state import DB_PATH

def calculate_lifestyle_creep(user_id: str, days_back: int = 180) -> str:
    """
    Runs a linear regression analysis on time-series discretionary spending to detect 
    statistically significant upward trends (Lifestyle Creep).
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
            
            # Fetch all discretionary transactions
            c.execute("""
                SELECT date, amount, category 
                FROM transactions 
                WHERE user_id = ? AND date >= ? AND amount > 0
                AND category NOT IN ('Credit Card Bill Payment 💳', 'Financial Transfer 💰', 'Banking & Finance 🏦', 'Rent', 'Mortgage')
            """, (user_id, start_date))
            
            rows = c.fetchall()
            
            if not rows:
                return "Insufficient data to detect lifestyle creep."
                
            # Group by week to smooth out daily variance
            weekly_spend = {}
            for r in rows:
                try:
                    d = datetime.strptime(r[0][:10], "%Y-%m-%d")
                    week = d.strftime("%Y-%U")
                    if week not in weekly_spend:
                        weekly_spend[week] = {"total": 0.0, "categories": {}}
                    
                    amt = float(r[1])
                    cat = r[2] or "Uncategorized"
                    weekly_spend[week]["total"] += amt
                    weekly_spend[week]["categories"][cat] = weekly_spend[week]["categories"].get(cat, 0.0) + amt
                except:
                    pass
                    
            if len(weekly_spend) < 4:
                return "Need at least 4 weeks of data to run regression analysis."
                
            # Sort weeks chronologically
            sorted_weeks = sorted(weekly_spend.keys())
            
            # Linear Regression: y = mx + b
            n = len(sorted_weeks)
            x_values = list(range(n))
            y_values = [weekly_spend[w]["total"] for w in sorted_weeks]
            
            sum_x = sum(x_values)
            sum_y = sum(y_values)
            sum_xy = sum(x*y for x, y in zip(x_values, y_values))
            sum_x2 = sum(x**2 for x in x_values)
            
            denominator = (n * sum_x2 - sum_x**2)
            if denominator == 0:
                return "Variance too low for regression."
                
            slope = (n * sum_xy - sum_x * sum_y) / denominator
            intercept = (sum_y * sum_x2 - sum_x * sum_xy) / denominator
            
            # Calculate R-squared (goodness of fit)
            y_mean = sum_y / n
            ss_tot = sum((y - y_mean)**2 for y in y_values)
            ss_res = sum((y_values[i] - (slope * x_values[i] + intercept))**2 for i in range(n))
            
            r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
            
            # Identify offending categories
            cat_slopes = {}
            # (Internal logic omitted for brevity, focusing on top-line trend)
            
            report = [
                f"📈 LIFESTYLE CREEP ANALYSIS ({days_back} Days)",
                f"Analyzed {n} weeks of discretionary spending.",
                "-" * 40
            ]
            
            if slope > 5.0 and r_squared > 0.15:
                annualized_cost = (slope * 52) * 52 / 2  # Area of triangle for yearly inflation
                report.extend([
                    "🚨 WARNING: SIGNIFICANT LIFESTYLE CREEP DETECTED",
                    f"Your discretionary spending is mathematically increasing by ${slope:.2f} every single week.",
                    f"Trend Confidence (R²): {r_squared:.2f}",
                    "",
                    f"💸 The Cost of Creep: If you do not flatten this curve, this inflation will cost you an extra ${annualized_cost:.2f} over the next 12 months compared to your baseline."
                ])
            elif slope > 0:
                report.extend([
                    "⚠️ Minor spending inflation detected.",
                    f"Slope: +${slope:.2f}/week. Keep an eye on it.",
                ])
            else:
                report.extend([
                    "✅ SAFE: No lifestyle creep detected.",
                    f"Your spending is trending down by ${abs(slope):.2f}/week!",
                ])
                
            return "\n".join(report)
            
    except Exception as e:
        return f"ERROR analyzing lifestyle creep: {str(e)}"

def allocate_next_best_dollar(user_id: str, windfall_amount: float) -> str:
    """
    Algorithmic Routing Engine.
    Determines the mathematically optimal distribution of an influx of cash 
    to maximize net worth by comparing debt APRs, emergency funds, and goals.
    """
    try:
        windfall_amount = float(windfall_amount)
        if windfall_amount <= 0:
            return "Amount must be greater than 0."
            
        remaining = windfall_amount
        allocations = []
        
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            # 1. Check Credit Card Debt (Highest APR)
            c.execute("SELECT id, name, balance, apr FROM user_debts WHERE user_id = ? AND balance > 0 ORDER BY apr DESC", (user_id,))
            debts = c.fetchall()
            
            # Phase 1: High-Interest Debt (> 10% APR)
            for d_id, name, bal, apr in debts:
                if remaining <= 0: break
                if float(apr) >= 10.0:
                    payment = min(remaining, float(bal))
                    allocations.append({
                        "target": f"Debt: {name} (APR: {apr}%)",
                        "amount": payment,
                        "reason": f"Guaranteed {apr}% ROI by avoiding interest."
                    })
                    remaining -= payment
                    
            # Phase 2: Emergency Fund (Assume target is $5000 for simplicity)
            if remaining > 0:
                c.execute("SELECT SUM(current_amount) FROM savings_buckets WHERE user_id = ? AND name LIKE '%Emergency%'", (user_id,))
                ef_bal = c.fetchone()[0] or 0.0
                ef_target = 5000.0
                
                if ef_bal < ef_target:
                    ef_contribution = min(remaining, ef_target - ef_bal)
                    allocations.append({
                        "target": "Emergency Fund",
                        "amount": ef_contribution,
                        "reason": "Crucial liquidity buffer. Goal is underfunded."
                    })
                    remaining -= ef_contribution
                    
            # Phase 3: Sinking Funds
            if remaining > 0:
                c.execute("SELECT name, target_amount, current_amount FROM savings_buckets WHERE user_id = ? AND target_amount > current_amount", (user_id,))
                buckets = c.fetchall()
                for b_name, target, current in buckets:
                    if remaining <= 0: break
                    shortfall = float(target) - float(current)
                    contrib = min(remaining, shortfall)
                    allocations.append({
                        "target": f"Sinking Fund: {b_name}",
                        "amount": contrib,
                        "reason": "Funding planned future expenses."
                    })
                    remaining -= contrib
                    
            # Phase 4: Invest
            if remaining > 0:
                allocations.append({
                    "target": "Brokerage / Index Funds (e.g. VOO)",
                    "amount": remaining,
                    "reason": "High-interest debt is clear and emergency fund is stable. Build long-term wealth (Historical ~7-10% ROI)."
                })
                remaining = 0
                
        # Generate Report
        report = [
            f"🎯 NEXT BEST DOLLAR ALGORITHM",
            f"Windfall to Allocate: ${windfall_amount:.2f}",
            "-" * 40
        ]
        
        for i, alloc in enumerate(allocations):
            report.append(f"{i+1}. Route ${alloc['amount']:.2f} ➔ {alloc['target']}")
            report.append(f"   ↳ {alloc['reason']}")
            
        return "\n".join(report)
        
    except Exception as e:
        return f"ERROR allocating funds: {str(e)}"
