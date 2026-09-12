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
            total_liability = 0.0
            c.execute("SELECT expected_amount FROM planned_transactions WHERE user_id = ? AND status = 'Expected' AND direction = 'outbound' AND expected_date < ?", (user_id, target_date))
            for row in c.fetchall():
                total_liability += float(row[0] or 0.0)

            # Subscriptions due before target date
            c.execute("SELECT last_amount, last_date FROM subscriptions WHERE user_id = ? AND status = 'Active'", (user_id,))
            for amt, last_date_str in c.fetchall():
                try:
                    last_date = datetime.strptime(last_date_str[:10], "%Y-%m-%d")
                    next_date = last_date + timedelta(days=30)
                    if datetime.now() <= next_date <= datetime.strptime(target_date, "%Y-%m-%d"):
                        total_liability += float(amt or 0.0)
                except Exception:
                    pass
                
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


def calculate_emergency_fund_health(user_id: str) -> str:
    """
    Emergency Fund Runway & Bare-Bones Survival Calculator.
    Distinguishes essential baseline expenses (housing, groceries, utilities, debt minimums)
    from discretionary lifestyle spending, calculating the exact survival runway in months.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            # Liquid Cash
            c.execute("""
                SELECT SUM(current_balance) 
                FROM plaid_accounts 
                WHERE user_id = ? AND type = 'depository' AND active = 1
            """, (user_id,))
            liquid = c.fetchone()[0] or 0.0
            
            # Dedicated Emergency Sinking Fund
            c.execute("""
                SELECT SUM(current_amount) 
                FROM savings_buckets 
                WHERE user_id = ? AND name LIKE '%Emergency%'
            """, (user_id,))
            ef_bucket = c.fetchone()[0] or 0.0
            
            effective_emergency_cash = max(liquid, ef_bucket)

            # Minimum debt obligations
            c.execute("""
                SELECT SUM(COALESCE(min_payment, balance * 0.025)) 
                FROM user_debts 
                WHERE user_id = ? AND balance > 0
            """, (user_id,))
            debt_mins = c.fetchone()[0] or 0.0

            # 90-day expenses
            ninety_days_ago = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            c.execute("""
                SELECT category, amount 
                FROM transactions 
                WHERE user_id = ? AND date >= ? AND amount > 0 
                  AND merchant NOT LIKE '%System Balance Sync%'
                  AND category NOT IN ('Credit Card Bill Payment 💳', 'Financial Transfer 💰')
            """, (user_id, ninety_days_ago))
            rows = c.fetchall()

            if not rows:
                return "Insufficient transaction history (last 90 days) to calculate emergency runway."

            essential_cats = {
                'housing', 'rent', 'mortgage', 'groceries', 'utilities', 
                'insurance', 'healthcare', 'medical', 'transit', 'gasoline', 'gas'
            }

            total_spend_90d = 0.0
            essential_spend_90d = 0.0

            for cat, amt in rows:
                amount = float(amt)
                total_spend_90d += amount
                cat_lower = (cat or "").lower()
                if any(k in cat_lower for k in essential_cats):
                    essential_spend_90d += amount

            # Normalize to monthly (90 days = ~3 months)
            months = 3.0
            monthly_lifestyle_burn = total_spend_90d / months
            monthly_essential_burn = (essential_spend_90d / months) + debt_mins

            survival_runway = effective_emergency_cash / monthly_essential_burn if monthly_essential_burn > 0 else 999.0
            lifestyle_runway = effective_emergency_cash / monthly_lifestyle_burn if monthly_lifestyle_burn > 0 else 999.0

            target_3mo = monthly_essential_burn * 3.0
            target_6mo = monthly_essential_burn * 6.0
            gap_3mo = max(0.0, target_3mo - effective_emergency_cash)

            report = [
                "🛡️ EMERGENCY FUND HEALTH & RUNWAY AUDIT",
                f"Liquid Reserves Available:     ${effective_emergency_cash:,.2f}",
                "-" * 45,
                f"Monthly Bare-Bones Burn Rate:  ${monthly_essential_burn:,.2f}/mo",
                f"  ↳ Essentials:                ${essential_spend_90d / months:,.2f}/mo",
                f"  ↳ Debt Minimum Obligations:  ${debt_mins:,.2f}/mo",
                f"Monthly Full Lifestyle Burn:   ${monthly_lifestyle_burn:,.2f}/mo",
                "-" * 45,
                f"⏱️ Bare-Bones Survival Runway:  {survival_runway:.1f} Months",
                f"⏱️ Current Lifestyle Runway:   {lifestyle_runway:.1f} Months",
                "-" * 45,
                "🎯 TARGET BENCHMARKS:",
                f" • 3-Month Buffer Target:      ${target_3mo:,.2f} (Shortfall: ${gap_3mo:,.2f})",
                f" • 6-Month Buffer Target:      ${target_6mo:,.2f}"
            ]

            if survival_runway < 1.0:
                report.append("\n🚨 RED ALERT: Less than 1 month of emergency cash reserves. Critical risk if income is interrupted.")
            elif survival_runway < 3.0:
                report.append("\n⚠️ VULNERABLE: Under 3 months of emergency buffer. Prioritize building the emergency fund before investing.")
            elif survival_runway < 6.0:
                report.append("\n✅ ADEQUATE: Over 3 months of survival runway. Stable baseline.")
            else:
                report.append("\n🏆 FORTRESS BALANCE SHEET: 6+ months of reserves. You have high financial shock resilience.")

            return "\n".join(report)

    except Exception as e:
        return f"ERROR calculating emergency fund health: {str(e)}"


def render_progress_bar(percentage: float, width: int = 10) -> str:
    """Render a clean text progress bar, e.g. [██████░░░░] 60%."""
    clamped = max(0.0, min(percentage, 200.0))
    filled = int(round((min(clamped, 100.0) / 100.0) * width))
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {percentage:.0f}%"


def calculate_spending_pace_and_forecast(
    user_id: str,
    now: Optional[datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Computes real-time mid-month spending velocity, category budget pacing,
    and end-of-month cash burn projections.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("calculate_spending_pace_and_forecast: forced isolation violation — user_id is required")

    dt = now or datetime.now()
    year = dt.year
    month = dt.month
    day = dt.day

    start_of_month = datetime(year, month, 1)
    if month == 12:
        next_month_start = datetime(year + 1, 1, 1)
    else:
        next_month_start = datetime(year, month + 1, 1)

    days_in_month = (next_month_start - start_of_month).days
    elapsed_days = max(1, day)
    remaining_days = max(0, days_in_month - day)
    elapsed_pct = (elapsed_days / days_in_month) * 100.0

    def _execute(db_conn):
        c = db_conn.cursor()
        month_start_str = start_of_month.strftime("%Y-%m-%d")
        month_end_str = next_month_start.strftime("%Y-%m-%d")

        # 1. Realized month spending by category
        c.execute("""
            SELECT COALESCE(category, 'Uncategorized'), SUM(amount), COUNT(*)
            FROM transactions
            WHERE user_id = ? AND amount > 0
              AND date >= ? AND date < ?
              AND category != 'Credit Card Bill Payment 💳'
              AND merchant NOT LIKE '%System Balance Sync%'
            GROUP BY COALESCE(category, 'Uncategorized')
        """, (user_id, month_start_str, month_end_str))

        cat_spending = {}
        for row in c.fetchall():
            cat_spending[row[0]] = {
                "spent": float(row[1] or 0.0),
                "count": int(row[2] or 0)
            }

        # 2. Configured category budgets
        c.execute("""
            SELECT category, monthly_limit
            FROM category_budgets
            WHERE user_id = ?
            ORDER BY category ASC
        """, (user_id,))
        budgets_raw = c.fetchall()
        budget_map = {r[0]: float(r[1]) for r in budgets_raw}

        # 3. Monthly realized income
        c.execute("""
            SELECT SUM(amount)
            FROM transactions
            WHERE user_id = ? AND amount < 0
              AND date >= ? AND date < ?
              AND category != 'Credit Card Bill Payment 💳'
              AND merchant NOT LIKE '%System Balance Sync%'
        """, (user_id, month_start_str, month_end_str))
        income_row = c.fetchone()
        realized_income = abs(float(income_row[0] or 0.0)) if income_row and income_row[0] else 0.0

        # 4. Total spending
        total_spent = sum(v["spent"] for v in cat_spending.values())
        daily_burn_rate = total_spent / elapsed_days
        projected_total_spend = daily_burn_rate * days_in_month

        # 5. Build category breakdown
        categories_result = []
        total_budgeted_limit = sum(budget_map.values())

        # Process budgeted categories
        for cat, limit in budget_map.items():
            spent_data = cat_spending.get(cat, {"spent": 0.0, "count": 0})
            spent = spent_data["spent"]
            pct_used = (spent / limit * 100.0) if limit > 0 else 0.0
            expected_spent_to_date = limit * (elapsed_days / days_in_month)
            pace_ratio = (spent / expected_spent_to_date) if expected_spent_to_date > 0 else 1.0
            projected_spend = (spent / elapsed_days) * days_in_month
            remaining_budget = limit - spent
            daily_cap = (max(0.0, remaining_budget) / remaining_days) if remaining_days > 0 else 0.0

            if spent >= limit:
                status = "BLOWN"
                icon = "🔴"
            elif pace_ratio > 1.15:
                status = "OVERPACING"
                icon = "🟡"
            else:
                status = "ON TRACK"
                icon = "🟢"

            categories_result.append({
                "category": cat,
                "monthly_limit": round(limit, 2),
                "spent": round(spent, 2),
                "remaining": round(remaining_budget, 2),
                "pct_used": round(pct_used, 1),
                "progress_bar": render_progress_bar(pct_used),
                "pace_ratio": round(pace_ratio, 2),
                "projected_spend": round(projected_spend, 2),
                "remaining_daily_cap": round(daily_cap, 2),
                "status": status,
                "status_icon": icon,
                "transaction_count": spent_data["count"]
            })

        # Process unbudgeted categories with spend
        unbudgeted = []
        for cat, data in sorted(cat_spending.items(), key=lambda x: x[1]["spent"], reverse=True):
            if cat not in budget_map:
                spent = data["spent"]
                unbudgeted.append({
                    "category": cat,
                    "spent": round(spent, 2),
                    "count": data["count"],
                    "projected_spend": round((spent / elapsed_days) * days_in_month, 2)
                })

        # Overall pacing
        overall_pace_ratio = None
        remaining_total_budget = None
        remaining_daily_allowance = None
        if total_budgeted_limit > 0:
            expected_total_to_date = total_budgeted_limit * (elapsed_days / days_in_month)
            overall_pace_ratio = round(total_spent / expected_total_to_date, 2) if expected_total_to_date > 0 else 1.0
            remaining_total_budget = round(total_budgeted_limit - total_spent, 2)
            remaining_daily_allowance = round(max(0.0, remaining_total_budget) / remaining_days, 2) if remaining_days > 0 else 0.0

        projected_net_savings = round(realized_income - projected_total_spend, 2)

        return {
            "user_id": user_id,
            "period": {
                "year": year,
                "month": month,
                "month_name": dt.strftime("%B %Y"),
                "current_day": day,
                "days_in_month": days_in_month,
                "elapsed_days": elapsed_days,
                "remaining_days": remaining_days,
                "elapsed_pct": round(elapsed_pct, 1),
            },

            "spending_summary": {
                "total_spent_to_date": round(total_spent, 2),
                "current_daily_burn": round(daily_burn_rate, 2),
                "projected_month_spend": round(projected_total_spend, 2),
                "realized_income": round(realized_income, 2),
                "projected_net_savings": projected_net_savings,
            },
            "budget_summary": {
                "total_budgeted_limit": round(total_budgeted_limit, 2),
                "remaining_budget": remaining_total_budget,
                "overall_pace_ratio": overall_pace_ratio,
                "remaining_daily_allowance": remaining_daily_allowance,
            },
            "category_budgets": categories_result,
            "unbudgeted_categories": unbudgeted,
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_spending_pace_report(data: Dict[str, Any]) -> str:
    """Format spending velocity and budget pacing as clean markdown report."""
    period = data["period"]
    spend = data["spending_summary"]
    budget = data["budget_summary"]
    cats = data["category_budgets"]
    unbudgeted = data.get("unbudgeted_categories", [])

    lines = [
        f"📊 MONTHLY BUDGET & PACING: {period['month_name'].upper()}",
        f"Timeline: Day {period['current_day']} of {period['days_in_month']} ({period['elapsed_pct']}% elapsed, {period['remaining_days']} days left)",
        "-" * 55,
        f"Total Spent to Date:       ${spend['total_spent_to_date']:,.2f}",
        f"Current Daily Burn Rate:   ${spend['current_daily_burn']:,.2f}/day",
        f"Projected Month-End Spend: ${spend['projected_month_spend']:,.2f}",
    ]

    if spend["realized_income"] > 0:
        net_str = f"+${spend['projected_net_savings']:,.2f}" if spend['projected_net_savings'] >= 0 else f"-${abs(spend['projected_net_savings']):,.2f}"
        lines.append(f"Realized Income:           ${spend['realized_income']:,.2f}")
        lines.append(f"Projected Net Cash Flow:   {net_str}")

    lines.append("-" * 55)

    if cats:
        lines.append("CATEGORY BUDGETS & BURN VELOCITY:")
        for c in cats:
            lines.append(
                f" • {c['status_icon']} {c['category']}: ${c['spent']:,.2f} / ${c['monthly_limit']:,.2f} {c['progress_bar']}"
            )
            lines.append(
                f"   Pace: {c['pace_ratio']}x ({c['status']}) · Remaining: ${c['remaining']:,.2f} · Safe Cap: ${c['remaining_daily_cap']:,.2f}/day"
            )
    else:
        lines.append("No category budgets set yet. Use `!setbudget <category> <monthly_limit>` to add targets.")

    if unbudgeted:
        lines.append("-" * 55)
        lines.append("TOP UNBUDGETED CATEGORIES:")
        for u in unbudgeted[:4]:
            lines.append(f" • {u['category']}: ${u['spent']:,.2f} (proj ${u['projected_spend']:,.2f})")

    lines.append("-" * 55)
    if budget.get("remaining_daily_allowance") is not None:
        lines.append(f"🎯 Target Daily Burn to finish month on budget: ${budget['remaining_daily_allowance']:,.2f}/day")

    return "\n".join(lines)


