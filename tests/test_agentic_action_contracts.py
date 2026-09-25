"""Regression checks for the concrete workflows in the agentic roadmap."""

import inspect


def test_step_zero_workflow_tools_are_exposed_with_correct_mutation_class():
    from src.services import llm

    schemas = {
        item["function"]["name"]: item["function"]
        for item in llm.BOT_TOOLS_SCHEMA
    }

    assert {
        "search_gmail",
        "read_gmail_message",
        "monitor_create_natural_rule",
        "monitor_list_rules",
    } <= schemas.keys()
    assert "monitor_create_natural_rule" in llm.MUTATION_TOOLS
    assert "monitor_list_rules" not in llm.MUTATION_TOOLS
    assert "user_id" in inspect.signature(llm.search_gmail).parameters
