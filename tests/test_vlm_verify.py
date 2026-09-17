"""B2 VLM self-verify tests: assertion extraction + text fast-path."""

from src.services.concierge.vlm_verify import _expected_strings, verify_act


def test_expected_strings_extracts_assert_text():
    steps = [
        {"action": "navigate", "sel": "https://example.com"},
        {"action": "click", "sel": "#login"},
        {"action": "assert_text", "sel": "#welcome", "value": "Welcome"},
        {"action": "assert_text", "sel": "#banner"},
        {"action": "screenshot"},
    ]
    result = _expected_strings(steps)
    assert result == ["Welcome", "#banner"]


def test_expected_strings_empty_for_no_assertions():
    steps = [
        {"action": "navigate", "sel": "https://example.com"},
        {"action": "click", "sel": "#btn"},
    ]
    assert _expected_strings(steps) == []


def test_expected_strings_handles_empty_steps():
    assert _expected_strings([]) == []
    assert _expected_strings(None) == []


def test_verify_act_text_fast_path():
    """When page text contains expected strings, no VLM call needed."""
    steps = [
        {"action": "assert_text", "sel": "#welcome", "value": "Welcome"},
    ]
    res = verify_act.__wrapped__ if hasattr(verify_act, "__wrapped__") else None
    import asyncio
    result = asyncio.run(verify_act(
        url="https://example.com",
        steps=steps,
        page_text="Hello and Welcome to our site!",
    ))
    assert result["verdict"] == "verified"
    assert result["confidence"] == 0.9


def test_verify_act_no_assertions_short_circuits():
    import asyncio
    result = asyncio.run(verify_act(
        url="https://example.com",
        steps=[{"action": "navigate", "sel": "https://example.com"}],
        page_text="",
    ))
    assert result["verdict"] == "no_assertions"


def test_verify_act_renavigation_gated_by_allowed_domains():
    """The post-execution re-navigation must be held to the same allowlist:
    an out-of-scope URL raises before any screenshot/VLM work happens."""
    from src.services.concierge.browser_gate import ReadGateError
    import asyncio
    try:
        asyncio.run(verify_act(
            url="http://169.254.169.254/latest/meta-data/",
            steps=[{"action": "assert_text", "sel": "#x", "value": "hi"}],
            page_text="",
            allowed_domains=["example.com"],
        ))
        raise AssertionError("verify_act should have refused the re-navigation")
    except ReadGateError as exc:
        assert "not allowed" in str(exc)


def test_verify_act_allows_renavigation_inside_scope():
    """Same-scope re-navigation passes the gate and short-circuits on text."""
    import asyncio
    res = asyncio.run(verify_act(
        url="https://example.com/orders",
        steps=[{"action": "assert_text", "sel": "#x", "value": "Shipped"}],
        page_text="Your order Shipped today",
        allowed_domains=["example.com"],
    ))
    assert res["verdict"] == "verified"
