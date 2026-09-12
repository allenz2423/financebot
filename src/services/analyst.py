import sqlite3
import json
import httpx
from datetime import datetime, timedelta

from src.core.state import DB_PATH, OLLAMA_URL, ADVISOR_MODEL
from src.db.memory import save_epistemic_memory

def _init_analyst_state():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS analyst_state (
                user_id TEXT PRIMARY KEY,
                last_analyzed_tx_id INTEGER DEFAULT 0,
                last_run_at TEXT
            )
        """)
        conn.commit()

async def run_autonomous_analyst(user_id: str) -> str:
    """
    Epistemic Autonomous Analyst.
    Reads recent unanalyzed transactions, generates hypotheses, 
    and saves them to Epistemic Memory as candidate insights.
    """
    _init_analyst_state()
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        
        # Get state
        c.execute("SELECT last_analyzed_tx_id FROM analyst_state WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        last_tx_id = row[0] if row else 0
        
        # Get unanalyzed transactions
        c.execute("""
            SELECT id, date, merchant, amount, category 
            FROM transactions 
            WHERE user_id = ? AND id > ? 
            ORDER BY date DESC LIMIT 100
        """, (user_id, last_tx_id))
        
        txs = c.fetchall()
        
        if not txs:
            return "Analyst run complete: No new transactions to analyze."
            
        max_id = max(t[0] for t in txs)
        
        # Format for LLM
        tx_lines = []
        for tid, tdate, merch, amt, cat in txs:
            tx_lines.append(f"[{tid}] {tdate} - {merch} - ${amt:.2f} ({cat})")
        
        tx_data = "\n".join(tx_lines)

    system_prompt = """You are the Epistemic Autonomous Analyst, an advanced quantitative background intelligence process for Delilah Financial OS.
Analyze the following recent transactions with rigorous forensic scrutiny. Formulate actionable hypotheses regarding:
1. Recurring Price Creep: Any subscription, SaaS, or utility increasing in cost compared to prior months.
2. High-Frequency Micro-Friction: Daily/multiple weekly micro-purchases (coffee, convenience, delivery) eroding cash flow.
3. Lifestyle Inflation: Accelerated burn rate or discretionary luxury spending expanding without corresponding income growth.
4. Phantom Subscriptions: Recurring charges for digital tools or services with low utility or dormant usage.
5. Tax Deductible Signals: Uncategorized business expenses, medical payments, or charitable donations.

Return your output ONLY as a JSON list of objects with this exact schema:
[
  {
    "insight": "Concise, data-backed financial hypothesis with quantified impact.",
    "evidence_refs": ["Transaction IDs that support this insight"],
    "confidence": 0.85
  }
]
IMPORTANT: You are formulating empirical hypotheses. Ground every insight strictly in the provided transaction lines. If no significant anomalies or patterns exist, return [].
"""

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(
                OLLAMA_URL,
                json={
                    "model": ADVISOR_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": tx_data}
                    ],
                    "format": "json",
                    "stream": False
                }
            )
            if res.status_code != 200:
                return f"Analyst LLM failed: {res.status_code}"
                
            response_data = res.json()["message"]["content"]
            insights = json.loads(response_data)
            
            saved_count = 0
            for insight in insights:
                text = insight.get("insight")
                if text:
                    await save_epistemic_memory(
                        user_id=user_id,
                        content=text,
                        memory_type="hypothesis",
                        provenance_type="llm_inferred",
                        confidence=float(insight.get("confidence", 0.5)),
                        evidence_refs=[str(e) for e in insight.get("evidence_refs", [])]
                    )
                    saved_count += 1
            
            # Update state
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO analyst_state (user_id, last_analyzed_tx_id, last_run_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET 
                    last_analyzed_tx_id = excluded.last_analyzed_tx_id,
                    last_run_at = excluded.last_run_at
                """, (user_id, max_id))
                conn.commit()
                
            return f"Analyst run complete: Generated {saved_count} hypotheses from {len(txs)} transactions."
            
    except Exception as e:
        return f"Analyst run failed: {str(e)}"
