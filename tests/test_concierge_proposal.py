"""B2 proposal + approval-store tests: validation, gating, persistence."""

import json
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


def make(tmp_path, domain="example.com", tier="write"):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    audit = AuditLog(db)
    tenants.enable(ADMIN, "user:1", tier=tier)
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


def make_spend(tmp_path, domain="example.com"):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    audit = AuditLog(db)
    tenants.enable(ADMIN, "user:1", tier="spend", spend_cap=50.0)
    tenants.allow_domain(ADMIN, "user:1", domain)
    return tenants, audit, db


def test_propose_approval_ttl_default(tmp_path):
    tenants, audit, _ = make(tmp_path)  # read tier -> standard window
    res = propose_concierge_act(
        tenant="user:1", kind="fill_form", args=_steps(),
        tenants=tenants, audit=audit,
    )
    assert res["approval_ttl"] == 600.0


def test_propose_approval_ttl_short_for_spend_tier(tmp_path):
    tenants, audit, _ = make_spend(tmp_path)
    res = propose_concierge_act(
        tenant="user:1", kind="send_email", args=_steps(),
        tenants=tenants, audit=audit,
    )
    assert res["approval_ttl"] == 120.0


def test_approval_store_stores_and_returns_ttl(tmp_path):
    store = ApprovalStore(str(tmp_path / "s.db"))
    pid = store.create(
        tenant="user:1", uid="1", kind="fill_form",
        args={"domain": "example.com", "steps": [{"action": "navigate", "sel": "https://example.com"}]},
        url="https://example.com",
        steps=[{"action": "navigate", "sel": "https://example.com"}],
        approval_ttl=120.0,
    )
    assert store.get(pid)["approval_ttl"] == 120.0

    pid_default = store.create(
        tenant="user:1", uid="1", kind="send_email",
        args={}, url="https://example.com",
    )
    assert store.get(pid_default)["approval_ttl"] == 600.0


def test_approval_store_migrates_legacy_db(tmp_path):
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE concierge_act_proposals (
        proposal_id TEXT PRIMARY KEY, tenant TEXT, uid TEXT, kind TEXT,
        args TEXT, url TEXT, steps TEXT, status TEXT, created_at TEXT,
        approved_at TEXT, executed_at TEXT, result_detail TEXT)""")
    conn.execute(
        "INSERT INTO concierge_act_proposals "
        "(proposal_id, tenant, uid, kind, args, status, created_at) "
        "VALUES ('legacy1', 'user:1', '1', 'send_email', '{}', 'pending', 't')"
    )
    conn.commit()
    conn.close()

    store = ApprovalStore(db)
    p = store.get("legacy1")
    assert p is not None
    assert p["approval_ttl"] == 600.0
    assert p["status"] == "pending"


def test_approval_store_supports_rolled_back_status(tmp_path):
    db = str(tmp_path / "s.db")
    store = ApprovalStore(db)
    pid = store.create(tenant="user:1", uid="1", kind="send_email", args={})
    assert store.update_status(
        pid, "rolled_back", result_detail={"error": "step failed"}
    ) is True
    p = store.get(pid)
    assert p["status"] == "rolled_back"
    assert p["result_detail"]["error"] == "step failed"


# --- B2 hardening (security audit findings) ---


def test_propose_read_tier_is_refused_and_audited(tmp_path):
    tenants, audit, _ = make(tmp_path, tier="read")
    with pytest.raises(ConciergeToolError, match="write or spend"):
        propose_concierge_act(
            tenant="user:1", kind="send_email", args=_steps(),
            tenants=tenants, audit=audit,
        )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"
    assert "write or spend" in rows[0]["detail"]["reason"]


def test_propose_persists_allowed_domains_on_proposal(tmp_path):
    tenants, audit, db = make(tmp_path)
    proposal_store = ApprovalStore(db)
    res = propose_concierge_act(
        tenant="user:1", kind="fill_form", args=_steps(),
        tenants=tenants, audit=audit, store=proposal_store,
    )
    p = proposal_store.get(res["proposal_id"])
    assert p["allowed_domains"] == ["example.com"]


def test_propose_vaults_secret_type_values(tmp_path):
    from src.security.vault import Vault, VaultRevokedError
    tenants, audit, db = make(tmp_path)
    proposal_store = ApprovalStore(db)
    vault = Vault(db, master_key="test-master-key")
    steps = [
        {"action": "navigate", "sel": "https://example.com/login"},
        {"action": "type", "sel": "#password", "value": "SuperSecret-Passw0rd"},
        {"action": "type", "sel": "#user", "value": "alice"},
        {"action": "click", "sel": "#btn"},
    ]
    res = propose_concierge_act(
        tenant="user:1", kind="fill_form",
        args={"domain": "example.com", "steps": steps},
        tenants=tenants, audit=audit, store=proposal_store, vault=vault,
    )
    # the secret is masked + tunneled behind a vault_ref; plaintext never stored
    by_sel = {s["sel"]: s for s in res["steps"]}
    assert by_sel["#password"]["value"] == "\u2022" * 4
    assert "vault_ref" in by_sel["#password"]
    assert by_sel["#user"]["value"] == "alice"  # non-secret stays as-is
    stored = proposal_store.get(res["proposal_id"])
    assert "SuperSecret-Passw0rd" not in str(stored["args"])
    assert "SuperSecret-Passw0rd" not in str(stored["steps"])
    ref = by_sel["#password"]["vault_ref"]
    # encrypted at rest, resolvable once by the executor
    payload = json.loads(vault.resolve("user:1", ref))
    assert payload["password|value"] == "SuperSecret-Passw0rd"
    # terminal transition revokes the per-proposal secret
    proposal_store.update_status(res["proposal_id"], "executed")
    with pytest.raises(VaultRevokedError):
        vault.resolve("user:1", ref)


def test_propose_vaults_pan_shaped_values(tmp_path):
    from src.security.vault import Vault
    tenants, audit, db = make(tmp_path)
    proposal_store = ApprovalStore(db)
    vault = Vault(db, master_key="test-master-key")
    res = propose_concierge_act(
        tenant="user:1", kind="fill_form",
        args={"domain": "example.com", "steps": [
            {"action": "type", "sel": "#cc", "value": "4111 1111 1111 1111"},
        ]},
        tenants=tenants, audit=audit, store=proposal_store, vault=vault,
    )
    step = res["steps"][0]
    assert "4111" not in str(step)
    stored = proposal_store.get(res["proposal_id"])
    assert "4111" not in str(stored["steps"])
    assert "vault_ref" in step


def test_propose_refuses_ephemeral_cvv_value(tmp_path):
    tenants, audit, _ = make(tmp_path)
    with pytest.raises(ConciergeToolError, match="can never be stored"):
        propose_concierge_act(
            tenant="user:1", kind="fill_form",
            args={"domain": "example.com", "steps": [
                {"action": "type", "sel": "#cvv", "value": "123"},
            ]},
            tenants=tenants, audit=audit,
        )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"


def test_approval_store_claim_is_atomic(tmp_path):
    db = str(tmp_path / "s.db")
    store = ApprovalStore(db)
    pid = store.create(tenant="user:1", uid="1", kind="send_email", args={})
    assert store.claim(pid) is True
    assert store.claim(pid) is False  # second caller cannot double-execute
    assert store.get(pid)["status"] == "approved"


def test_approval_store_claim_refuses_expired_ttl(tmp_path):
    db = str(tmp_path / "s.db")
    store = ApprovalStore(db)
    pid = store.create(tenant="user:1", uid="1", kind="send_email", args={},
                       approval_ttl=0.0)  # window already lapsed
    assert store.claim(pid) is False
    assert store.get(pid)["status"] == "expired"


def test_approval_store_expire_stale_sweeps_pending(tmp_path):
    db = str(tmp_path / "s.db")
    store = ApprovalStore(db)
    pid = store.create(tenant="user:1", uid="1", kind="send_email", args={},
                       approval_ttl=0.0)
    alive = store.create(tenant="user:1", uid="1", kind="send_email", args={},
                         approval_ttl=600.0)
    # pending_for_tenant triggers the sweep: the lapsed window is expired,
    # the fresh window stays pending
    pending = [p["proposal_id"] for p in store.pending_for_tenant("user:1")]
    assert pending == [alive]
    assert store.expire_stale() == 0  # nothing left to sweep
    assert store.get(pid)["status"] == "expired"
    assert store.get(alive)["status"] == "pending"


def test_approval_store_terminal_transition_revokes_vault_refs(tmp_path):
    from src.security.vault import Vault, VaultRevokedError
    db = str(tmp_path / "s.db")
    vault = Vault(db, master_key="test-master-key")
    rec = vault.store_fields(
        tenant="user:1", kind="secret", label="act secret",
        fields={"password|v": "hunter2"}, consumer_scope=["example.com"],
    )
    store = ApprovalStore(db)
    pid = store.create(
        tenant="user:1", uid="1", kind="fill_form",
        args={}, steps=[{"action": "type", "sel": "#password",
                         "value": "\u2022" * 4, "vault_ref": rec["vault_ref"]}],
    )
    assert store.update_status(pid, "executed") is True
    with pytest.raises(VaultRevokedError):
        vault.resolve("user:1", rec["vault_ref"])
