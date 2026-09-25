from src.services.result_contracts import contract_for, extract_facts


def test_contract_extracts_only_approved_facts():
    facts = extract_facts(
        "get_current_financial_position",
        '{"total_liquid": 10, "net_worth": 20, "secret_token": "no"}',
    )
    assert facts == {"total_liquid": 10, "net_worth": 20}


def test_unknown_tool_has_conservative_empty_fact_contract():
    assert contract_for("unknown").fact_keys == ()
    assert extract_facts("unknown", '{"value": 1}') == {}
