"""B1 LLM tool entry tests: gates, mask-only results, tenant allowlist."""

import asyncio
import json

import pytest

from src.services.concierge.audit import AuditLog
from src.services.concierge.llm_tool import (
    ConciergeToolError,
    request_concierge_action,
    request_concierge_action_async,
    tool_result_json,
)
from src.services.concierge.tenants import TenantStore

ADMIN = "admin:1"


def make(tmp_path, domain="example.com"):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    audit = AuditLog(db)
    tenants.enable(ADMIN, "user:1")
    tenants.allow_domain(ADMIN, "user:1", domain)
    return tenants, audit


def stub_fetch(page_text="Your order has shipped."):
    async def fetch(url):
        return {"text": page_text, "screenshot": b"PNG-BYTES"}
    return fetch


def run(coro):
    return asyncio.run(coro)


def test_non_read_kind_is_refused(tmp_path):
    tenants, audit = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="read-tier only"):
        run(request_concierge_action_async(
            "user:1", "capture_stored", {}, tenants, audit, stub_fetch(),
        ))


def test_tenant_not_enabled_is_refused(tmp_path):
    tenants, audit = make(tmp_path)
    tenants.disable(ADMIN, "user:1")
    with pytest.raises(ConciergeToolError, match="not enabled"):
        run(request_concierge_action_async(
            "user:1", "order_status", {"order_id": "x"}, tenants, audit, stub_fetch(),
        ))


def test_unknown_tenant_is_refused(tmp_path):
    tenants, audit = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="not enabled"):
        run(request_concierge_action_async(
            "user:99", "order_status", {"order_id": "x"}, tenants, audit, stub_fetch(),
        ))


def test_args_must_be_dict(tmp_path):
    tenants, audit = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="args must be a dict"):
        run(request_concierge_action_async(
            "user:1", "order_status", "nope", tenants, audit, stub_fetch(),
        ))


def test_successful_read_returns_mask_only(tmp_path):
    tenants, audit = make(tmp_path)
    res = run(request_concierge_action_async(
        "user:1", "order_status",
        {"domain": "example.com", "order_id": "ORD-42"},
        tenants, audit, stub_fetch("Your PAN is 4111111111111111 and it shipped."),
    ))
    assert res["status"] == "ok"
    assert res["kind"] == "order_status"
    assert "4111111111111111" not in res["summary"]
    assert "••••" in res["summary"]
    assert res["screenshot_png"] == b"PNG-BYTES"
    assert res["audit_seq"] is not None
    rows = audit.tail(limit=10, tenant="user:1")
    assert rows[0]["action"] == "read_order_status"


def test_allowlist_from_tenant_status_enforced(tmp_path):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    audit = AuditLog(db)
    tenants.enable(ADMIN, "user:2")  # enabled but NO allow_domains
    with pytest.raises(ConciergeToolError, match="not allowed"):
        run(request_concierge_action_async(
            "user:2", "order_status",
            {"domain": "example.com", "order_id": "ORD-42"},
            tenants, audit, stub_fetch("text"),
        ))
    rows = audit.tail(limit=10, tenant="user:2")
    assert rows[0]["action"] == "read_refused"


def test_tool_result_json_strips_screenshot(tmp_path):
    tenants, audit = make(tmp_path)
    res = run(request_concierge_action_async(
        "user:1", "price_watch",
        {"domain": "example.com", "product": "widget"},
        tenants, audit, stub_fetch("$19.99"),
    ))
    payload = json.loads(tool_result_json(res))
    assert "screenshot_png" not in payload
    assert payload["status"] == "ok"
    assert payload["summary"] == "$19.99"


def test_sync_wrapper_bridges_coroutine_fetcher(tmp_path):
    tenants, audit = make(tmp_path)
    res = request_concierge_action(
        "user:1", "order_status",
        {"domain": "example.com", "order_id": "ORD-42"},
        tenants, audit, stub_fetch("Order dispatched."),
        include_screenshot=False,
    )
    assert res["status"] == "ok"
    assert res["screenshot_png"] is None