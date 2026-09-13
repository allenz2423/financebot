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
                # Replace table names with word boundaries to avoid partial matches
                import re
                pattern = r'\b' + re.escape(hallucinated) + r'\b'
                sanitized_query = re.sub(pattern, actual, sanitized_query)

            # In kg_claims, user is subject_id (e.g. user:<user_id>)
            if "kg_claims" in sanitized_query:
                # Replace user_id = 'actual_id' or user_id = ? patterns
                sanitized_query = re.sub(r"user_id\s*=\s*'" + re.escape(user_id) + r"'",
                                       f"(subject_id = 'user:{user_id}' OR subject_id = '{user_id}')",
                                       sanitized_query)
                sanitized_query = re.sub(r"user_id\s*=\s*\?",
                                       f"(subject_id = 'user:{user_id}' OR subject_id = '{user_id}')",
                                       sanitized_query)

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
        elif "no such column" in err_msg:
            # Provide enhanced guidance for column errors
            return (
                f"ERROR executing verification query: {err_msg}. \n"
                "HINT: You used an incorrect column name in your SQL query. \n"
                "To verify table schemas, you can:\n"
                "1. Use existing financial tools like `get_accounts_overview` instead of raw SQL when possible\n"
                "2. Use `run_python_sandbox` to execute `PRAGMA table_info(table_name)` to discover correct column names\n"
                "3. For the plaid_accounts table, correct column names include: \n"
                "   - name (account name)\n"
                "   - current_balance (account balance)\n"
                "   - available_balance\n"
                "   - credit_limit\n"
                "   - type, subtype, institution_name\n"
                "   - plaid_account_id, mask, currency, last_synced_at, active\n"
                "Example correct query: `SELECT current_balance FROM plaid_accounts WHERE user_id = ? AND name = 'Account Name'`\n"
                "Consider using the VERIFY step in your reasoning process to first explore the 'Balances' domain "
                "to discover appropriate tools like `get_accounts_overview` before attempting raw SQL queries."
            )
        return f"ERROR executing verification query: {err_msg}"
    except Exception as e:
        return f"ERROR executing verification query: {str(e)}"
