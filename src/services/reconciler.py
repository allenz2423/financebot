import sqlite3
import math
from datetime import datetime, timedelta
from typing import Dict, Any, List
from src.core.state import DB_PATH

def levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

def string_similarity(s1: str, s2: str) -> float:
    s1, s2 = s1.lower(), s2.lower()
    max_len = max(len(s1), len(s2))
    if max_len == 0: return 1.0
    dist = levenshtein_distance(s1, s2)
    return 1 - (dist / max_len)

def clean_merchant_string(merchant: str) -> str:
    """Removes common payment processor prefixes/suffixes to get the root merchant."""
    if not merchant: return ""
    m = merchant.lower()
    prefixes = ['tst* ', 'tst*', 'sq *', 'sq * ', 'amzn mktp ', 'amazon.com*', 'paypal *', 'venmo*']
    for p in prefixes:
        if m.startswith(p):
            m = m[len(p):]
    m = m.replace(' pending', '').strip()
    return m

def auto_reconcile_ledger(user_id: str) -> str:
    """
    Fuzzy-Match Ledger Self-Cleaning Engine.
    Detects orphaned pending transactions that duplicate posted transactions and merges them.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            
            # Fetch recent transactions (last 30 days) to look for duplicates
            thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            c.execute("""
                SELECT id, date, amount, merchant, status, transaction_id
                FROM transactions
                WHERE user_id = ? AND date >= ?
                ORDER BY date DESC
            """, (user_id, thirty_days_ago))
            
            rows = c.fetchall()
            if not rows:
                return "No recent transactions to reconcile."
                
            pending = [r for r in rows if str(r[4]).lower() == 'pending']
            posted = [r for r in rows if str(r[4]).lower() == 'posted']
            
            if not pending:
                return "Ledger is clean. No pending orphans found."
                
            merged_count = 0
            merged_amount = 0.0
            log = []
            
            for p in pending:
                p_id, p_date, p_amount, p_merchant, p_status, p_plaid = p
                p_amount = float(p_amount)
                p_date_obj = datetime.strptime(p_date[:10], "%Y-%m-%d")
                p_clean = clean_merchant_string(p_merchant)
                
                # Look for a posted transaction within 7 days, exact same amount, and similar merchant string
                best_match = None
                best_score = 0.0
                
                for post in posted:
                    post_id, post_date, post_amount, post_merchant, post_status, post_plaid = post
                    post_amount = float(post_amount)
                    
                    if p_amount != post_amount:
                        continue
                        
                    post_date_obj = datetime.strptime(post_date[:10], "%Y-%m-%d")
                    days_diff = abs((post_date_obj - p_date_obj).days)
                    
                    if days_diff > 7:
                        continue
                        
                    post_clean = clean_merchant_string(post_merchant)
                    score = string_similarity(p_clean, post_clean)
                    
                    if score > 0.6 and score > best_score:
                        best_match = post
                        best_score = score
                        
                if best_match:
                    post_id, post_date, post_amount, post_merchant, _, _ = best_match
                    
                    # Merge action: Delete the pending orphan
                    c.execute("DELETE FROM transactions WHERE id = ?", (p_id,))
                    merged_count += 1
                    merged_amount += p_amount
                    log.append(f"🔄 Merged: Pending '{p_merchant}' ({p_date[:10]}) ➔ Posted '{post_merchant}' ({post_date[:10]}) | Amount: ${p_amount:.2f} | Confidence: {best_score*100:.1f}%")
                    
                    # Remove from posted list so it's not matched again
                    posted.remove(best_match)
                    
            conn.commit()
            
            if merged_count == 0:
                return "Ledger is clean. Checked all pending transactions and found no orphans."
                
            report = [
                f"🧹 AUTO-RECONCILIATION COMPLETE",
                f"Identified and purged {merged_count} orphaned pending transactions.",
                f"Removed ${merged_amount:.2f} of artificially inflated ledger data.",
                "-" * 40
            ]
            report.extend(log)
            
            return "\n".join(report)
            
    except Exception as e:
        return f"ERROR reconciling ledger: {str(e)}"
