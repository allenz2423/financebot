import sqlite3
from typing import Dict, Any

def verify_claim(claim: str, sql_query: str = None, user_id: str = None) -> str:
    """
    The Verification Layer.
    Executes a deterministic check to verify an LLM's claim before it is presented to the user.
    """
    if not user_id:
        return "ERROR: user_id is required for verification."
    
    if not sql_query:
        return "ERROR: Verification requires a deterministic sql_query to check the claim."
        
    # Security: Ensure only SELECT queries are run in the verification layer
    if not sql_query.strip().upper().startswith("SELECT"):
        return "ERROR: Only SELECT queries are permitted in verify_claim."
        
    try:
        from src.core.state import DB_PATH
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # Rewrite common hallucinated table aliases for world model
            sanitized_query = sql_query
            for hallucinated, actual in [
                ("world_model_claims", "kg_claims"),
                ("world_model_entities", "kg_entities"),
                ("knowledge_graph_claims", "kg_claims"),
                ("knowledge_graph_entities", "kg_entities"),
                ("world_model", "kg_claims"),
            ]:
                if hallucinated in sanitized_query:
                    sanitized_query = sanitized_query.replace(hallucinated, actual)

            # In kg_claims, user is subject_id (e.g. user:<user_id>)
            if "kg_claims" in sanitized_query:
                sanitized_query = sanitized_query.replace(
                    f"user_id = '{user_id}'", f"(subject_id = 'user:{user_id}' OR subject_id = '{user_id}')"
                )
                sanitized_query = sanitized_query.replace(
                    "user_id = ?", f"(subject_id = 'user:{user_id}' OR subject_id = '{user_id}')"
                )

            c.execute(sanitized_query)
            rows = c.fetchall()
            
            # Format results
            result_str = "\n".join([str(row) for row in rows]) if rows else "(0 rows returned)"
            
            return f"CLAIM: {claim}\n\nDETERMINISTIC RESULT:\n{result_str}\n\nAssess this result. If your claim was incorrect, use this true data going forward."
    except sqlite3.OperationalError as e:
        err_msg = str(e)
        if "no such table" in err_msg:
            return (
                f"ERROR executing verification query: {err_msg}. "
                "HINT: For relational knowledge graph facts/claims (institutions, caps, rules, schedule, employers), "
                "do not guess SQL tables. Use `get_world_model_entity(entity_id_or_name)` or `search_world_model(query)`. "
                "For ledger verification, available tables are: transactions, plaid_accounts, subscriptions, savings_buckets, planned_transactions, balance_snapshots."
            )
        return f"ERROR executing verification query: {err_msg}"
    except Exception as e:
        return f"ERROR executing verification query: {str(e)}"
