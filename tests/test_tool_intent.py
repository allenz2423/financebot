from src.services.tool_intent import infer_required_tools


def test_explicit_plaid_sync_requires_the_sync_tool():
    assert infer_required_tools("Can you run a plaid sync") == {"sync_plaid_accounting"}
    assert infer_required_tools("please sync my Plaid accounts now") == {"sync_plaid_accounting"}


def test_plaid_mention_or_negated_request_does_not_force_sync():
    assert infer_required_tools("What is Plaid?") == frozenset()
    assert infer_required_tools("Don't run a Plaid sync") == frozenset()
