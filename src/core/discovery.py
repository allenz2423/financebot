import json

# Hierarchical Capability Registry
# Maps Domain -> Capability -> List of Tool Names

CAPABILITY_REGISTRY = {
    "Spending": {
        "History & Totals": ["query_spending", "get_spending_breakdown"],
        "Budgets": ["check_budget_status"],
    },
    "Balances": {
        "Overview": ["get_current_financial_position", "get_accounts_overview"],
        "Live Data": ["pull_live_financial_data", "sync_plaid_accounting"],
    },
    "Transactions": {
        "Review & Edit": ["get_unlocked_transactions", "correct_transaction", "batch_correct_transactions", "lock_transaction", "batch_lock_transactions", "add_transaction", "delete_transaction"],
        "History": ["get_recent_transactions", "search_transactions", "get_locked_transactions"],
        "Dashboard": ["get_financial_dashboard"],
    },
    "Future & Planning": {
        "Cash Flow": ["get_cash_flow_summary", "get_upcoming_cash_flow", "get_temporal_projection", "get_planned_transactions", "add_planned_transaction", "update_planned_transaction", "cancel_planned_transaction"],
        "Income": ["get_recent_income", "get_expected_income", "add_expected_income", "update_expected_income", "cancel_expected_income"],
        "Subscriptions": ["get_subscriptions"],
    },
    "Savings & Debt": {
        "Buckets": ["get_savings_buckets", "adjust_savings_bucket", "delete_savings_bucket", "get_sinking_funds_overview"],
        "Debt": ["get_debt_overview"],
        "Net Worth": ["get_net_worth_history"],
    },
    "Web Research": {
        "Search & Extract": ["search_web", "fetch_webpage", "crawl_deeper"],
        "Merchants": ["get_known_merchant", "save_known_merchant", "get_unique_unregistered_merchants"],
    },
    "Epistemic Memory": {
        "Query": ["semantic_search_memory"],
        "Save": ["save_epistemic_memory"],
        "Delete": ["delete_memory"],
        "Context": ["tag_transaction_context", "get_transactions_by_context", "log_lifestyle_context", "get_lifestyle_context"],
    },
    "Analysis & Sandbox": {
        "Python Execution": ["run_python_sandbox", "install_python_package"],
        "Simulations": ["simulate_cash_flow_scenario", "simulate_debt_payoff", "calculate_rebalancing_drift"],
        "Reports": ["generate_financial_digest"],
    },
    "Settings": {
        "Preferences": ["set_user_timezone", "get_user_timezone"]
    },
    "Monitoring & Automation": {
        "Monitor Rules": ["monitor_list_rules", "monitor_add_rule", "monitor_delete_rule", "monitor_clear_all_rules", "monitor_create_natural_rule"],
        "Alerts": ["monitor_run_pass", "monitor_list_alerts", "monitor_ack_alert", "send_push_alert"],
        "Reminders": ["schedule_reminder", "get_scheduled_reminders", "update_scheduled_reminder", "delete_scheduled_reminder"],
    },
    "External Services": {
        "Gmail": ["search_gmail", "read_gmail_thread", "read_gmail_message", "analyze_price_drop_and_draft_refund"],
    }
}

def explore_domain(domain: str) -> str:
    """Returns capabilities and tool signatures for a specific domain."""
    if domain.lower() == "all":
        result = []
        for key, caps in CAPABILITY_REGISTRY.items():
            result.append(f"Domain: {key}")
            for cap, tools in caps.items():
                result.append(f"- {cap}: {', '.join(tools)}")
            result.append("")
        result.append("To load full JSON schemas for these tools, call `load_tool_schemas` with the tool names.")
        return "\n".join(result)
        
    domain_map = {k.lower(): k for k in CAPABILITY_REGISTRY.keys()}
    key = domain_map.get(domain.lower())
    if not key:
        return f"Domain '{domain}' not found. Available domains: {', '.join(CAPABILITY_REGISTRY.keys())}"
    
    result = [f"Domain: {key}\n"]
    for cap, tools in CAPABILITY_REGISTRY[key].items():
        result.append(f"- {cap}: {', '.join(tools)}")
    
    result.append("\nTo load full JSON schemas for these tools, call `load_tool_schemas` with the tool names.")
    return "\n".join(result)

def list_domains() -> str:
    """Returns the top-level list of domains."""
    return "Available Domains:\n- " + "\n- ".join(CAPABILITY_REGISTRY.keys())

