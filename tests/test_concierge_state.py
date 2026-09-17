"""B0 draft/approve state machine + executor skeleton tests."""

import json
from datetime import timedelta

import pytest

from src.security.vault import Vault, VaultRecordNotFound, VaultRevokedError
from src.services.concierge.state import (
    DraftStore,
    DraftNotFound,
    DraftNotActionable,
    ExecutorRefused,
    execute_draft,
    reject_untrusted_payload,
    APPROVAL_TTL,
)

KEY = "b0-state-test-master-key"


def make(tmp_path):
    db = str(tmp_path / "s.db")
    return DraftStore(db), Vault(db, master_key=KEY)


def store_card(vault, tenant="user:1"):
    return vault.store_fields(
        tenant=tenant, kind="payment", label="Amex",
        fields={"card_pan|Card": "378282246310005", "card_exp|Exp": "12/29"},
        consumer_scope=["amex.com"],
    )


def test_approve_is_owner_only(tmp_path):
    store, _ = make(tmp_path)
    d = store.create("user:1", "pay", "pay_bill", [], {"merchant": "acme"})
    with pytest.raises(DraftNotFound):
        store.approve(d, "user:2", "user:9")
    store.approve(d, "user:1", "user:1")
    assert store.get(d)["status"] == "approved"
    with pytest.raises(DraftNotActionable):
        store.approve(d, "user:1", "user:1")


def test_approval_ttl_and_expiry(tmp_path):
    store, _ = make(tmp_path)
    assert APPROVAL_TTL == timedelta(minutes=10)
    d = store.create("user:1", "pay", "pay", [], {})
    store.approve(d, "user:1", "user:1")
    assert store.expire_stale(timedelta(seconds=0)) >= 1
    assert store.get(d)["status"] == "expired"


def test_deny_works_from_draft_and_approved(tmp_path):
    store, _ = make(tmp_path)
    d = store.create("user:1", "pay", "pay", [], {})
    store.deny(d, "user:1")
    assert store.get(d)["status"] == "denied"
    d2 = store.create("user:1", "pay", "pay", [], {})
    store.approve(d2, "user:1", "user:1")
    store.deny(d2, "user:1")
    assert store.get(d2)["status"] == "denied"


def test_full_lifecycle_resolved_secret_stays_in_call_frame(tmp_path):
    store, vault = make(tmp_path)
    rec = store_card(vault)
    d = store.create("user:1", "pay", "pay_bill", [rec["vault_ref"]], {"merchant": "acme"})
    store.approve(d, "user:1", "user:1")
    seen = {}

    def action_fn(resolved, payload):
        seen["payload"] = json.loads(resolved[rec["vault_ref"]].decode())
        return {"status": "ok", "merchant": payload["merchant"], "amount_cents": 1234}

    out = execute_draft(store, vault, d, "user:1", action_fn)
    assert out["status"] == "confirmed"
    assert out["result"]["status"] == "ok"
    assert "378282246310005" not in json.dumps(out)
    assert seen["payload"]["card_pan|Card"] == "378282246310005"
    got = store.get(d)
    assert got["status"] == "confirmed"
    assert got["result"]["amount_cents"] == 1234


def test_execute_requires_approved(tmp_path):
    store, vault = make(tmp_path)
    d = store.create("user:1", "pay", "pay", [], {})
    with pytest.raises(DraftNotActionable):
        execute_draft(store, vault, d, "user:1", lambda r, p: {"ok": True})


def test_executor_refuses_secret_looking_payload(tmp_path):
    store, vault = make(tmp_path)
    d = store.create("user:1", "pay", "pay", [], {"password=auth": "hunter2"})
    store.approve(d, "user:1", "user:1")
    with pytest.raises(ExecutorRefused):
        execute_draft(store, vault, d, "user:1", lambda r, p: {"ok": True})
    assert store.get(d)["status"] == "escalated"


def test_executor_refuses_foreign_ref(tmp_path):
    store, vault = make(tmp_path)
    rec = store_card(vault, tenant="user:2")
    d = store.create("user:1", "pay", "pay", [rec["vault_ref"]], {})
    store.approve(d, "user:1", "user:1")
    with pytest.raises(VaultRecordNotFound):
        execute_draft(store, vault, d, "user:1", lambda r, p: {"ok": True})
    assert store.get(d)["status"] == "escalated"


def test_executor_refuses_revoked_ref_even_if_lingering(tmp_path):
    store, vault = make(tmp_path)
    rec = store_card(vault)
    vault.revoke("user:1", rec["vault_ref"])
    d = store.create("user:1", "pay", "pay", [rec["vault_ref"]], {})
    store.approve(d, "user:1", "user:1")
    with pytest.raises(VaultRevokedError):
        execute_draft(store, vault, d, "user:1", lambda r, p: {"ok": True})
    assert store.get(d)["status"] == "escalated"


def test_action_failure_escalates(tmp_path):
    store, vault = make(tmp_path)
    rec = store_card(vault)
    d = store.create("user:1", "pay", "pay", [rec["vault_ref"]], {})
    store.approve(d, "user:1", "user:1")

    def boom(resolved, payload):
        raise RuntimeError("processor down")

    with pytest.raises(RuntimeError):
        execute_draft(store, vault, d, "user:1", boom)
    got = store.get(d)
    assert got["status"] == "escalated"
    assert "processor down" in got["result"]["reason"]


def test_reject_untrusted_payload_accepts_clean_refs(tmp_path):
    _, vault = make(tmp_path)
    rec = store_card(vault)
    reject_untrusted_payload(vault, "user:1", [rec["vault_ref"]], {"merchant": "acme"})
    with pytest.raises(ExecutorRefused):
        reject_untrusted_payload(vault, "user:1", [], {"x": "token=abc123"})