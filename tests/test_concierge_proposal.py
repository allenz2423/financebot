"""B2 proposal + approval-store tests: validation, gating, persistence."""

import json
import sqlite3

import pytest

from src.security.vault import DEFAULT_DB_PATH
from src.services.concierge.act_actions import ACT_KINDS, Actuator
from src.services.concierge.approval_store import ApprovalStore
from src.services.concierge.audit import AuditLog
from src.services.concierge.llm_tool import (
    ConciergeToolError,
    concierge_act_status,
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
        tenants=tenants, audit=audit, store=ApprovalStore(db),
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


def test_propose_accepts_selector_alias_for_click_steps(tmp_path):
    tenants, audit, _ = make(tmp_path, domain="amazon.com")
    result = propose_concierge_act(
        tenant="user:1", kind="fill_form",
        args={
            "domain": "amazon.com",
            "steps": [
                {"action": "navigate", "url": "https://amazon.com/gp/cart"},
                {"action": "click", "selector": "#remove-item"},
            ],
        },
        tenants=tenants, audit=audit,
    )
    assert result["steps"][1]["sel"] == "#remove-item"


@pytest.mark.parametrize("field", ["target", "value"])
def test_propose_normalizes_generic_navigation_target_aliases(tmp_path, field):
    tenants, audit, _ = make(tmp_path, domain="example.com")
    result = propose_concierge_act(
        tenant="user:1", kind="fill_form",
        args={
            "domain": "example.com",
            "steps": [{"action": "navigate", field: "https://example.com/cart"}],
        },
        tenants=tenants, audit=audit,
    )
    assert result["steps"][0]["sel"] == "https://example.com/cart"


def test_propose_cross_domain_redirect_plan_refused(tmp_path):
    """A cross-domain redirect is refused at propose time: a
    navigate that leaves the act's own target host (even to an allowlisted
    domain) never reaches an approval prompt, and the refusal is audited."""
    tenants, audit, db = make(tmp_path, domain="amazon.com")
    tenants.allow_domain(ADMIN, "user:1", "example.com")
    args = {
        "domain": "amazon.com",
        "steps": [
            {"action": "navigate", "sel": "https://amazon.com/ap/signin"},
            {"action": "navigate", "sel": "https://example.com/url",
             "value": "https://www.amazon.com"},
            {"action": "click", "sel": "a#nav-link-accountList"},
        ],
    }
    with pytest.raises(ConciergeToolError, match="target host"):
        propose_concierge_act(
            tenant="user:1", kind="fill_form", args=args,
            tenants=tenants, audit=audit, store=ApprovalStore(db),
        )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"
    assert "target host" in str(rows[0]["detail"]["reason"])


def test_propose_bare_vault_template_value_refused(tmp_path):
    """A type step value that is a bare vault template (no step vault_ref, no
    braces — the model's `vault:s_...:field` habit) is refused at propose and
    audited before any proposal persists."""
    tenants, audit, db = make(tmp_path)
    args = {
        "domain": "example.com",
        "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "type", "sel": "input#ap_email",
             "value": "vault:s_a7359d2e5d6e:email"},
        ],
    }
    with pytest.raises(ConciergeToolError, match="vault_ref on the step"):
        propose_concierge_act(
            tenant="user:1", kind="fill_form", args=args,
            tenants=tenants, audit=audit, store=ApprovalStore(db),
        )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"


def test_propose_writes_proposed_audit_row(tmp_path):
    tenants, audit, db = make(tmp_path)
    propose_concierge_act(
        tenant="user:1", kind="send_email", args=_steps(),
        tenants=tenants, audit=audit, store=ApprovalStore(db),
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
    tenants, audit, db = make(tmp_path)  # read tier -> standard window
    res = propose_concierge_act(
        tenant="user:1", kind="fill_form", args=_steps(),
        tenants=tenants, audit=audit, store=ApprovalStore(db),
    )
    assert res["approval_ttl"] == 600.0


def test_propose_approval_ttl_short_for_spend_tier(tmp_path):
    tenants, audit, db = make_spend(tmp_path)
    res = propose_concierge_act(
        tenant="user:1", kind="send_email", args=_steps(),
        tenants=tenants, audit=audit, store=ApprovalStore(db),
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


def test_approval_store_terminal_keeps_persistent_secret(tmp_path):
    """User-stored credentials (policy='persistent') survive the proposal
    lifecycle: captured once for reuse, not vaulted for a single act."""
    from src.security.vault import Vault
    db = str(tmp_path / "s.db")
    vault = Vault(db, master_key="test-master-key")
    rec = vault.store_fields(
        tenant="user:1", kind="secret", label="Amazon login",
        fields={"password|value": "hunter2"}, consumer_scope=["amazon.com"],
        policy="persistent",
    )
    store = ApprovalStore(db)
    pid = store.create(
        tenant="user:1", uid="1", kind="fill_form",
        args={}, steps=[{"action": "type", "sel": "#password",
                         "value": "\u2022" * 4, "vault_ref": rec["vault_ref"]}],
    )
    assert store.update_status(pid, "executed") is True
    # still resolvable: the credential can log in again
    payload = json.loads(vault.resolve("user:1", rec["vault_ref"]))
    assert payload["password|value"] == "hunter2"


def test_propose_domain_arg_tolerates_scheme_and_path(tmp_path):
    """The model habitually passes 'https://amazon.com/...' as the domain arg;
    the gate must normalize it to a bare host before allowlisting."""
    tenants, audit, db = make(tmp_path)
    res = propose_concierge_act(
        tenant="user:1", kind="fill_form",
        args={"domain": "https://example.com/login?next=/x", "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "type", "sel": "#user", "value": "alice"},
        ]},
        tenants=tenants, audit=audit, store=ApprovalStore(db),
    )
    assert res["url"] == "https://example.com"


def test_concierge_act_status_reports_post_approval_outcomes(tmp_path):
    """The status tool surfaces terminal proposal state + outcome text so the
    model can answer 'is it done / what happened' from stored state."""
    store = ApprovalStore(str(tmp_path / "s.db"))
    rolled = store.create(tenant="user:1", uid="1", kind="fill_form",
                          args={}, url="https://example.com/login", steps=[])
    assert store.update_status(rolled, "rolled_back", result_detail={
        "error": "TimeoutError: Page.fill: waiting for #ap_email",
    })
    done = store.create(tenant="user:1", uid="1", kind="fill_form",
                        args={}, url="https://example.com/login", steps=[])
    assert store.update_status(done, "executed", result_detail={
        "act_result": {"status": "ok", "summary": "Sign in  Amazon   Hello, alice"},
    })
    # Execute granted an auto-pilot mission for the (now executed) proposal.
    # The status row must hand the model that mission_id so it never passes
    # the proposal_id to concierge_browser_step.
    mission = store.grant_mission(done)
    pending = store.create(tenant="user:1", uid="1", kind="fill_form",
                           args={}, url="https://example.com/login", steps=[])

    statuses = {s["proposal_id"]: s for s in concierge_act_status(tenant="user:1", store=store)}
    assert statuses[pending]["status"] == "pending"
    assert "outcome" not in statuses[pending]
    assert "mission_id" not in statuses[pending]

    assert statuses[done]["status"] == "executed"
    assert statuses[done]["mission_id"] == mission["mission_id"]
    assert statuses[done]["outcome"].startswith("act_status=ok |")
    assert "Hello, alice" in statuses[done]["outcome"]

    assert statuses[rolled]["status"] == "rolled_back"
    assert "waiting for #ap_email" in statuses[rolled]["outcome"]
    # newest first
    assert list(statuses)[0] == pending


def test_status_schema_and_payload_declare_it_is_not_login_proof():
    """`concierge_act_status` reports a page snapshot from when the act ran,
    which for a signed-out landing page looks like success. The model was
    answering "you're logged in" from it without touching the browser, so the
    tool description and the injected result both say it is not proof."""
    from src.services.llm import BOT_TOOLS_SCHEMA

    by_name = {
        t["function"]["name"]: t["function"].get("description", "")
        for t in BOT_TOOLS_SCHEMA
    }
    status_desc = by_name["concierge_act_status"].lower()
    assert "not proof" in status_desc
    assert "concierge_browser_step" in status_desc

    step_desc = by_name["concierge_browser_step"].lower()
    assert "live page" in step_desc


def test_mission_grant_and_scope_lookup(tmp_path):
    """One approval grants an auto-pilot mission scoped to (tenant, kind,
    www-normalized domain); other scopes never see it."""
    store = ApprovalStore(str(tmp_path / "m.db"))
    pid = store.create(tenant="user:1", uid="1", kind="fill_form",
                       args={}, url="https://www.amazon.com/gp/sign-in.html",
                       steps=[])
    mission = store.grant_mission(pid)
    assert mission is not None
    assert mission["domain"] == "amazon.com"  # www stripped for scope keying
    assert mission["status"] == "active"

    # A freshly proposed follow-up on the same scope auto-pilot's
    followup = {"tenant": "user:1", "kind": "fill_form",
                "url": "https://amazon.com/login"}
    assert store.active_mission_for(followup) is not None
    assert store.active_missions_for_tenant("user:1")[0]["domain"] == "amazon.com"

    # Different domain / kind / tenant: no mission access
    assert store.active_mission("user:1", "fill_form", "example.com") is None
    assert store.active_mission("user:1", "send_email", "amazon.com") is None
    assert store.active_mission("user:2", "fill_form", "amazon.com") is None


def test_mission_expires_after_ttl(tmp_path):
    store = ApprovalStore(str(tmp_path / "m.db"))
    pid = store.create(tenant="user:1", uid="1", kind="fill_form",
                       args={}, url="https://amazon.com", steps=[])
    store.grant_mission(pid)
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    try:
        conn.execute(
            "UPDATE concierge_act_missions SET expires_at = '2020-01-01T00:00:00.000000Z'"
        )
        conn.commit()
    finally:
        conn.close()
    assert store.active_mission("user:1", "fill_form", "amazon.com") is None


class _OkActuator(Actuator):
    """Minimal actuator contract for execute_approved_act tests."""

    def __init__(self):
        self.calls = []

    def navigate(self, url):
        self.calls.append(("nav", url))

    def click(self, s):
        self.calls.append(("click", s))

    def type_text(self, s, v):
        self.calls.append(("type", s, v))

    def screenshot(self):
        return b"\x89PNG-stub"

    def get_text(self):
        return "Welcome alice"

    def element_visible(self, s):
        return True


def _approved_flow_proposal(store, url="https://example.com/login"):
    steps = [
        {"action": "navigate", "sel": url},
        {"action": "type", "sel": "#user", "value": "alice"},
    ]
    return store.create(
        tenant="user:1", uid="1", kind="fill_form",
        args={"domain": "example.com", "steps": steps},
        url=url, steps=steps, allowed_domains=["example.com"],
    )


def test_execute_approved_act_grants_mission_and_executes(tmp_path):
    from src.bot.approval_views import execute_approved_act
    from src.security.vault import Vault

    db = str(tmp_path / "e.db")
    store = ApprovalStore(db)
    vault = Vault(db)
    pid = _approved_flow_proposal(store)

    async def _verify(*a, **k):
        return {"verdict": "confirmed", "confidence": 0.9}

    result = execute_approved_act(
        pid, "user:1", store=store, vault=vault,
        actuator_factory=lambda url, allowed: _OkActuator(),
        verify_factory=_verify,
    )
    assert result["ok"] is True
    assert result["outcome"] == "executed"
    # A concise fill_form proposal starts an agentic mission with an initial
    # observation; opening that page is not proof that the requested task
    # completed.
    assert result["execution_phase"] == "started"
    assert result["mission"]["domain"] == "example.com"
    assert store.get(pid)["status"] == "executed"
    assert store.active_mission("user:1", "fill_form", "example.com") is not None


def test_execute_approved_act_hard_failure_keeps_mission(tmp_path):
    from src.bot.approval_views import execute_approved_act
    from src.security.vault import Vault

    db = str(tmp_path / "e.db")
    store = ApprovalStore(db)
    vault = Vault(db)
    pid = _approved_flow_proposal(store)

    class _FailActuator(_OkActuator):
        def navigate(self, url):
            raise RuntimeError("Amazon bot-check wall")

    async def _verify(*a, **k):
        return {"verdict": "confirmed", "confidence": 0.5}

    result = execute_approved_act(
        pid, "user:1", store=store, vault=vault,
        actuator_factory=lambda url, allowed: _FailActuator(),
        verify_factory=_verify,
    )
    # Step-level errors are captured inside the act result (perform_act never
    # lets the browser die on a caught step failure), so the act completes as
    # a rolled_back outcome rather than an exception.
    assert result["ok"] is True
    assert result["outcome"] == "rolled_back"
    assert "bot-check" in result["res"]["error"]
    assert store.get(pid)["status"] == "rolled_back"
    # approval was real: mission stays live so a corrected plan can continue
    assert store.active_mission("user:1", "fill_form", "example.com") is not None


def test_execute_approved_act_unclaimed_when_already_terminal(tmp_path):
    from src.bot.approval_views import execute_approved_act
    from src.security.vault import Vault

    db = str(tmp_path / "e.db")
    store = ApprovalStore(db)
    vault = Vault(db)
    pid = _approved_flow_proposal(store)
    store.update_status(pid, "rejected")
    result = execute_approved_act(pid, "user:1", store=store, vault=vault)
    assert result.get("unclaimed") is True
    assert result["ok"] is False
