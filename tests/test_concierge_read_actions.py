"""B1 read-tier executor tests: gating, masking, auditing, screenshots."""

import pytest

from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import ReadGateError
from src.services.concierge.read_actions import (
    MAX_SUMMARY_CHARS,
    ReadActionError,
    _mask_dict,
    _mask_value,
    _summary_from_text,
    perform_read_action,
    perform_read_action_async,
)

ALLOW = ["example.com", "amazon.com"]


def fake_fetcher(url, text="Your order ORD-123 has shipped", screenshot=None):
    def fetch(target):
        assert target == url
        return {"text": text, "screenshot": screenshot}
    return fetch


def test_read_order_status_returns_mask_only(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    fetcher = fake_fetcher(
        "https://example.com/orders/ORD-123",
        text="Order ORD-123 status: out for delivery. Total was 19.99.",
        screenshot=b"\x89PNG-fake",
    )
    res = perform_read_action(
        "order_status", {"domain": "example.com", "order_id": "ORD-123"},
        ALLOW, "user:1", fetcher, audit=audit,
    )
    assert res["status"] == "ok"
    assert res["kind"] == "order_status"
    assert "ORD-123" in res["summary"]
    assert "19.99" in res["summary"]
    assert res["screenshot_png"] == b"\x89PNG-fake"
    assert res["audit_seq"] is not None
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "read_order_status"
    assert rows[0]["tenant"] == "user:1"
    assert "text_hash16" in rows[0]["detail"]


def test_read_pan_in_page_is_masked(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    fetcher = fake_fetcher(
        "https://example.com/tracking/1Z9AA",
        text="Card on file 4111111111111111 and order total 42.00",
    )
    res = perform_read_action(
        "tracking", {"domain": "example.com", "tracking_id": "1Z9AA"},
        ALLOW, "user:1", fetcher, audit=audit,
    )
    assert "4111111111111111" not in res["summary"]
    assert "42.00" in res["summary"]


def test_read_refused_is_audited_and_raised(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    with pytest.raises(ReadActionError):
        perform_read_action(
            "order_status", {"domain": "evil.example.org", "order_id": "A"},
            ALLOW, "user:1", fake_fetcher("never"), audit=audit,
        )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "read_refused"
    assert rows[0]["subject"] == "order_status"
    assert "not allowed" in rows[0]["detail"]["reason"]


def test_read_bad_slug_refused_no_fetch(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    called = []

    def fetcher(url):
        called.append(url)
        return {"text": "x"}

    with pytest.raises(ReadActionError, match="read args require an id"):
        perform_read_action(
            "order_status", {"domain": "example.com", "order_id": "../etc/passwd"},
            ALLOW, "user:1", fetcher, audit=audit,
        )
    assert called == []


def test_screenshot_disabled_returns_none(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    fetcher = fake_fetcher(
        "https://example.com/products/pizza-box",
        text="Pizza box $12.50", screenshot=b"\x89PNG-x",
    )
    res = perform_read_action(
        "price_watch", {"domain": "example.com", "product": "pizza-box"},
        ALLOW, "user:1", fetcher, audit=audit, include_screenshot=False,
    )
    assert res["screenshot_png"] is None
    assert "12.50" in res["summary"]


def test_summary_is_bounded_and_masked():
    long_text = "word " * 5000
    s = _summary_from_text(long_text)
    assert len(s) <= MAX_SUMMARY_CHARS
    s2 = _summary_from_text("pan 4111111111111111 here")
    assert "4111111111111111" not in s2


def test_mask_dict_recurses_on_secret_keys():
    out = _mask_dict({
        "order": "x",
        "card_pan": "4111111111111111",
        "nested": {"password": "hunter2", "total": "5.00"},
    })
    assert out["card_pan"] == "\u2022\u2022\u2022\u2022"
    assert out["nested"]["password"] == "\u2022\u2022\u2022\u2022"
    assert out["nested"]["total"] == "5.00"
    assert out["order"] == "x"


def test_unknown_read_kind_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    with pytest.raises(ReadActionError, match="unknown read kind"):
        perform_read_action(
            "buy_stuff", {"domain": "example.com"}, ALLOW, "user:1",
            fake_fetcher("never"), audit=audit,
        )


def test_disallowed_arg_keys_raise(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    with pytest.raises(ReadActionError, match="disallowed keys"):
        perform_read_action(
            "order_status",
            {"domain": "example.com", "order_id": "A", "shell": "id"},
            ALLOW, "user:1", fake_fetcher("never"), audit=audit,
        )


def test_async_executor_awaits_coroutine_fetcher(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))

    async def async_fetch(url):
        return {"text": "Order ORD-9 delivered", "screenshot": b"\x89PNG-async"}

    import asyncio
    res = asyncio.run(perform_read_action_async(
        "tracking", {"domain": "example.com", "tracking_id": "1Z9AA"},
        ALLOW, "user:1", async_fetch, audit=audit,
    ))
    assert res["status"] == "ok"
    assert "delivered" in res["summary"]
    assert res["screenshot_png"] == b"\x89PNG-async"
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "read_tracking"


def test_async_executor_refusal_audits_and_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))

    async def async_fetch(url):
        return {"text": "x"}

    import asyncio
    with pytest.raises(ReadActionError, match="not allowed"):
        asyncio.run(perform_read_action_async(
            "order_status", {"domain": "evil.example.org", "order_id": "A"},
            ALLOW, "user:1", async_fetch, audit=audit,
        ))
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "read_refused"


def test_price_watch_gate_verdict_sale(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    fetcher = fake_fetcher(
        "https://example.com/products/monitor",
        text="Dell 27 monitor now only $79.99!",
    )
    res = perform_read_action(
        "price_watch",
        {"domain": "example.com", "product": "monitor", "baseline": 100.0},
        ALLOW, "user:1", fetcher, audit=audit,
    )
    assert res["price"]["verdict"] == "sale"
    assert res["price"]["found"] == 79.99
    assert res["price"]["baseline"] == 100.0
    assert res["price"]["diff_pct"] == -20.01
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "read_price_watch"
    assert rows[0]["detail"]["price_verdict"] == "sale"


def test_price_watch_gate_rise_and_steady():
    rise = fake_fetcher("https://example.com/products/a", text="the price is now $110.00")
    res = perform_read_action(
        "price_watch", {"domain": "example.com", "product": "a", "baseline": 100.0},
        ALLOW, "user:1", rise,
    )
    assert res["price"]["verdict"] == "rise"

    steady = fake_fetcher("https://example.com/products/b", text="price: $102.00")
    res = perform_read_action(
        "price_watch", {"domain": "example.com", "product": "b", "baseline": 100.0},
        ALLOW, "user:1", steady,
    )
    assert res["price"]["verdict"] == "steady"


def test_price_watch_gate_no_price_and_invalid_args():
    no_price = fake_fetcher("https://example.com/products/x", text="item unavailable")
    res = perform_read_action(
        "price_watch", {"domain": "example.com", "product": "x", "baseline": 100.0},
        ALLOW, "user:1", no_price,
    )
    assert res["price"]["verdict"] == "no_price_found"

    bad_base = fake_fetcher("https://example.com/products/y", text="Price: $50.00")
    res = perform_read_action(
        "price_watch", {"domain": "example.com", "product": "y", "baseline": "NaN"},
        ALLOW, "user:1", bad_base,
    )
    assert res["price"]["verdict"] == "invalid_baseline"

    bad_tol = fake_fetcher("https://example.com/products/z", text="Price: $50.00")
    res = perform_read_action(
        "price_watch", {"domain": "example.com", "product": "z",
                        "baseline": 100.0, "tolerance": -0.1},
        ALLOW, "user:1", bad_tol,
    )
    assert res["price"]["verdict"] == "invalid_tolerance"


def test_price_watch_without_baseline_has_no_gate(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    fetcher = fake_fetcher("https://example.com/products/m", text="Price $50.00")
    res = perform_read_action(
        "price_watch", {"domain": "example.com", "product": "m"},
        ALLOW, "user:1", fetcher, audit=audit,
    )
    assert "price" not in res
    rows = audit.tail(limit=5)
    assert "price_verdict" not in rows[0]["detail"]


def test_price_watch_gate_async():
    async def async_fetch(url):
        return {"text": "Deal: $45.00"}

    import asyncio
    res = asyncio.run(perform_read_action_async(
        "price_watch", {"domain": "example.com", "product": "c", "baseline": 100.0},
        ALLOW, "user:1", async_fetch,
    ))
    assert res["price"]["verdict"] == "sale"


# --- masking hardening (security audit findings) ---


def test_mask_value_masks_grouped_pan():
    for grouped in ("4111 1111 1111 1111", "4111-1111-1111-1111",
                    "4111 1111 1111"):
        assert _mask_value("paid with " + grouped + " ok") == "paid with " + "\u2022" * 4 + " ok"


def test_mask_value_masks_token_like_shapes():
    for token in ("sk-1234567890abcdef", "ghp_abcdefghijklmnopqrst",
                  "glpat-abcdef123456", "ya29.abcdefghijklmnopqrstuvwx",
                  "AKIAIOSFODNN7EXAMPLE"):
        assert _mask_value("token " + token + " end") == "token " + "\u2022" * 4 + " end"


def test_mask_value_masks_contiguous_pan():
    assert _mask_value("card 4111111111111111 total") == "card " + "\u2022" * 4 + " total"


def test_mask_value_leaves_plain_text_untouched():
    assert _mask_value("Your order ORD-123 shipped for $19.99") == \
        "Your order ORD-123 shipped for $19.99"


def test_mask_value_masked_text_in_read_summary(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    fetcher = fake_fetcher(
        "https://example.com/tracking/1Z9",
        text="ref sk-1234567890abcdef and card 4111 1111 1111 1111",
    )
    res = perform_read_action(
        "tracking", {"domain": "example.com", "tracking_id": "1Z9"},
        ALLOW, "user:1", fetcher, audit=audit,
    )
    assert "sk-1234567890abcdef" not in res["summary"]
    assert "4111 1111 1111 1111" not in res["summary"]