import sqlite3
import re
from datetime import datetime
from src.core.state import DB_PATH
from src.services.search import search_searxng

async def generate_negotiation_script(*, user_id: str, service_name: str, current_price: float = None, zip_code: str = "") -> dict:
    """
    Autonomous Bill Negotiation Agent.
    Searches the web for competitor pricing, calculates customer loyalty metrics, 
    and generates an aggressive multi-step escalation script.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("generate_negotiation_script: forced isolation violation")

    # 1. Calculate Customer Loyalty (LTV) from DB
    loyalty_months = 0
    total_spent = 0.0
    first_date = None
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT date, amount FROM transactions 
            WHERE user_id = ? AND (merchant LIKE ? OR clean_merchant LIKE ?)
            ORDER BY date ASC
        """, (user_id, f"%{service_name}%", f"%{service_name}%"))
        rows = c.fetchall()
        
        if rows:
            loyalty_months = len(rows)
            total_spent = sum(r[1] for r in rows)
            first_date = rows[0][0][:10]

    # 2. Web Search for Promotions / Competitors
    search_queries = [
        f"{service_name} new customer promo pricing {zip_code}",
        f"internet provider deals {zip_code}" if "internet" in service_name.lower() or "wifi" in service_name.lower() else f"{service_name} competitor alternatives"
    ]
    
    competitor_data = []
    for query in search_queries:
        try:
            res = await search_searxng(query, scrape=False)
            results = res.get("results", [])
            for r in results[:3]:
                snip = (r.get("snippet") or "").replace("\n", " ")
                # Look for price patterns
                if "$" in snip:
                    competitor_data.append(snip)
        except Exception:
            pass

    # 3. Synthesize Data
    promo_evidence = ""
    if competitor_data:
        promo_evidence = "\n".join([f" - {d}" for d in set(competitor_data[:3])])
    else:
        promo_evidence = " - (No direct web promo data found for this zip code, default to generic competitive rates)."

    # 4. Generate Escalation Script
    loyalty_text = f"I've been a loyal customer since {first_date} (that's {loyalty_months} payments totaling over ${total_spent:.2f})." if loyalty_months > 3 else "I've been a customer here recently."
    price_text = f"My bill is currently ${current_price:.2f}/mo, which is too high." if current_price else "My bill has crept up recently and is too high."
    
    script = f"""=== NEGOTIATION DOSSIER: {service_name.upper()} ===

**Your Leverage:**
- Loyalty: {loyalty_months} months
- Total Lifetime Value: ${total_spent:.2f}
- Current Bill: ${current_price or 'Unknown'}/mo

**Live Web Intelligence (Zip: {zip_code}):**
{promo_evidence}

=== THE SCRIPT ===

**Level 1: The Polite Ask (Retention Rep)**
"Hi, {loyalty_text} {price_text} I am looking at my budget and I need to lower this expense. Are there any current promotions or loyalty discounts you can apply to my account to keep my rate competitive?"

**Level 2: The Competitor Threat (If Level 1 Fails)**
"I understand you don't have standard promos, but I'm looking at competitive offers right now for my zip code offering rates closer to $30-50/mo. I really don't want the hassle of switching equipment, but I can't justify paying my current rate. Can you check with your supervisor to see if you can match new customer pricing?"

**Level 3: The 'Cancel Me' Bluff (If Level 2 Fails)**
"If that's the absolute best you can do, I'm going to have to cancel my service. Could you please transfer me to the cancellation department so I can schedule my disconnection?"
*(Note: When transferred to the actual Cancellation/Retention department, repeat Level 2. They have the actual authority to override prices).*
"""

    return {
        "status": "success",
        "service": service_name,
        "script": script
    }
