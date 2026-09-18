"""B1 read-tier URL gate tests: templates, SSRF guard, domain allowlist."""

import pytest

from src.services.concierge.browser_gate import (
    ReadGateError,
    READ_KINDS,
    domain_matches,
    resolve_action_target,
    validate_target_url,
)

ALLOW = ["example.com", "amazon.com"]


def test_resolve_order_status_template():
    url = resolve_action_target("order_status", {"domain": "example.com", "order_id": "ORD-123"}, ALLOW)
    assert url == "https://example.com/orders/ORD-123"


def test_resolve_tracking_and_price_watch():
    assert resolve_action_target("tracking", {"domain": "example.com", "tracking_id": "1Z9AA"}, ALLOW) == \
        "https://example.com/tracking/1Z9AA"
    assert resolve_action_target("price_watch", {"domain": "example.com", "product": "pizza-box"}, ALLOW) == \
        "https://example.com/products/pizza-box"


def test_unknown_kind_refused():
    with pytest.raises(ReadGateError, match="unknown read kind"):
        resolve_action_target("buy_stuff", {"domain": "example.com"}, ALLOW)


def test_disallowed_arg_keys_refused():
    with pytest.raises(ReadGateError, match="disallowed keys"):
        resolve_action_target("order_status", {"domain": "example.com", "order_id": "A", "shell": "id"}, ALLOW)


def test_bad_slugs_refused():
    with pytest.raises(ReadGateError, match="read args require an id"):
        resolve_action_target("order_status", {"domain": "example.com", "order_id": "../etc/passwd"}, ALLOW)
    with pytest.raises(ReadGateError, match="read args require an id"):
        resolve_action_target("order_status", {"domain": "example.com", "order_id": "x" * 65}, ALLOW)
    with pytest.raises(ReadGateError, match="read args require an id"):
        resolve_action_target("order_status", {"domain": "example.com"}, ALLOW)


def test_scheme_and_raw_ip_refused():
    with pytest.raises(ReadGateError, match="read target must be a full http"):
        validate_target_url("ftp://example.com/x", ALLOW)
    with pytest.raises(ReadGateError, match="https://amazon.com"):
        validate_target_url("example.com/x", ALLOW)
    with pytest.raises(ReadGateError, match="raw IP"):
        validate_target_url("http://127.0.0.1/admin", ALLOW)


def test_private_host_refused_via_dns():
    with pytest.raises(ReadGateError, match="not allowed|public address"):
        validate_target_url("http://localhost/secrets", ALLOW)


def test_non_allowlisted_domain_refused():
    with pytest.raises(ReadGateError, match="not allowed"):
        validate_target_url("http://evil.example.org/x", ALLOW)


def test_refusal_lists_tenant_allowlist_for_self_correction():
    # the model should see what IS allowed so it can retry with the right host
    with pytest.raises(ReadGateError, match=r"allowed: amazon\.com, example\.com"):
        validate_target_url("http://amazon.se/x", ALLOW)


def test_wildcard_and_subdomain_matching():
    assert domain_matches("amazon.com", "www.amazon.com")
    assert domain_matches("*.example.com", "shop.example.com")
    assert not domain_matches("amazon.com", "amazon.co.uk")
    assert not domain_matches("example.com", "notexample.com")


def test_public_host_accepted_when_allowlisted():
    url = validate_target_url("https://example.com/orders/1", ["example.com"])
    assert url.startswith("https://example.com/")


def test_http_scheme_accepted():
    url = validate_target_url("http://example.com/orders/1", ["example.com"])
    assert url.startswith("http://example.com/orders/1")


def test_read_kinds_expose_exactly_three():
    assert READ_KINDS == {"order_status", "tracking", "price_watch"}