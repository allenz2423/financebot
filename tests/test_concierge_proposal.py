"""B2 proposal + approval-store tests: validation, gating, persistence."""

import sqlite3

import pytest

from src.security.vault import DEFAULT_DB_PATH
from src.services.concierge.act_actions import ACT_KINDS
from src.services.concierge.approval_store import ApprovalStore
from src.services.concierge.audit import AuditLog
from src.services.concierge.llm_tool import (
    ConciergeToolError,
    propose_concierge_act,
)
from src.services.concierge.tenants import TenantStore

ADMIN = "admin:1"


def make(tmp_path, domain="example.com"):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    audit = AuditLog(db)
    tenants.enable(ADMIN, "user:1")
    tenants.allow_domain(ADMIN, "user:1", domain)
    return tenants, audit, db


def _steps(domain="example.com", sel="#btn", value="hello"):
    return {
        "domain": domain,
        "steps": [
            {"action": "navigate", "sel": "https://" + domain + "/login"},
            {"action": "click", "sel": sel},
            {"action": "type", "sel": "#user", "value": value},
            {"action": "assert_text", "sel": "#welcome", "value": "Welcome"},
            {"action": "screenshot"},
        ],
    }


def test_propose_write_kind_creates_pending_proposal(tmp_path):
    tenants, audit, db = make(tmp_path)
    res = propose_concierge_act(
        tenant="user:1", kind="fill_form", args=_steps(),
        tenants=tenants, audit=audit,
    )
    assert res["kind"] == "fill_form"
    assert res["status"] == "pending"
    assert res["proposal_id"]
    assert res["url"] == "https://example.com"
    assert len(res["steps"]) == 5


def test_propose_read_kind_is_refused(tmp_path):
    tenants, audit, _ = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="write-tier only"):
        propose_concierge_act(
            tenant="user:1", kind="order_status", args={"domain": "example.com"},
            tenants=tenants, audit=audit,
        )


def test_propose_unknown_kind_is_refused(tmp_path):
    tenants, audit, _ = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="write-tier only"):
        propose_concierge_act(
            tenant="user:1", kind="delete_account", args=_steps(),
            tenants=tenants, audit=audit,
        )


def test_propose_non_dict_args_is_refused(tmp_path):
    tenants, audit, _ = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="args must be a dict"):
        propose_concierge_act(
            tenant="user:1", kind="fill_form", args="nope",
            tenants=tenants, audit=audit,
        )


def test_propose_tenant_not_enabled_is_refused(tmp_path):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    tenants.enable(ADMIN, "user:2")  # only user:2 enabled
    audit = AuditLog(db)
    with pytest.raises(ConciergeToolError, match="not enabled"):
        propose_concierge_act(
            tenant="user:1", kind="fill_form", args=_steps(),
            tenants=tenants, audit=audit,
        )


def test_propose_bad_steps_are_refused(tmp_path):
    tenants, audit, _ = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="permitted act verb"):
        propose_concierge_act(
            tenant="user:1", kind="fill_form",
            args={"domain": "example.com", "steps": [{"action": "rm", "sel": "#x"}]},
            tenants=tenants, audit=audit,
        )


def test_propose_invalid_domain_is_audited_and_refused(tmp_path):
    tenants, audit, _ = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="not allowed"):
        propose_concierge_act(
            tenant="user:1", kind="fill_form",
            args={"domain": "evil.example.org", "steps": [{"action": "navigate", "sel": "https://evil.example.org/x"}]},
            tenants=tenants, audit=audit,
        )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"


def test_propose_writes_proposed_audit_row(tmp_path):
    tenants, audit, _ = make(tmp_path)
    propose_concierge_act(
        tenant="user:1", kind="send_email", args=_steps(),
        tenants=tenants, audit=audit,
    )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_proposed"
    assert rows[0]["detail"]["kind"] == "send_email"
    assert rows[0]["detail"]["steps"] == 5


def test_approval_store_lifecycle(tmp_path):
    store = ApprovalStore(str(tmp_path / "store.db"))
    pid = store.create(
        tenant="user:1", uid="1", kind="fill_form",
        args={"domain": "example.com", "steps": [{"action": "navigate", "sel": "https://example.com"}]},
        url="https://example.com",
        steps=[{"action": "navigate", "sel": "https://example.com"}],
    )
    p = store.get(pid)
    assert p["status"] == "pending"
    assert p["kind"] == "fill_form"
    assert p["tenant"] == "user:1"

    assert store.update_status(pid, "approved") is True
    assert store.get(pid)["status"] == "approved"

    assert store.update_status(pid, "executed",
                               result_detail={"status": "ok"}) is True
    assert store.get(pid)["status"] == "executed"
    assert store.get(pid)["result_detail"]["status"] == "ok"

    assert store.update_status(pid + "x", "approved") is False


def test_approval_store_pending_for_tenant(tmp_path):
    store = ApprovalStore(str(tmp_path / "s.db"))
    pid1 = store.create(tenant="user:1", uid="1", kind="send_email", args={}, url="https://example.com")
    pid2 = store.create(tenant="user:2", uid="2", kind="fill_form", args={}, url="https://example.com")
    store.update_status(pid1, "approved")
    pending_1 = store.pending_for_tenant("user:1")
    pending_2 = store.pending_for_tenant("user:2")
    assert len(pending_1) == 0
    assert len(pending_2) == 1
    assert pending_2[0]["proposal_id"] == pid2


def test_act_kinds_set():
    assert ACT_KINDS == frozenset({"send_email", "schedule_event", "fill_form"})
