"""B0 demo action_fn + full executor path tests (order_pizza stub)."""

import json

import pytest

from src.security.vault import Vault
from src.services.concierge.demo_actions import order_pizza
from src.services.concierge.state import DraftStore, execute_draft

KEY = "b0-demo-test-master-key"
PAN = "378282246310005"
CVV = "882"


def make(tmp_path):
    db = str(tmp_path / "s.db")
    return DraftStore(db), Vault(db, master_key=KEY)


def store_card(vault, tenant="user:1"):
    return vault.store_fields(
        tenant=tenant, kind="payment", label="Amex",
        fields={"card_pan|Card": PAN, "card_exp|Exp": "12/29"},
        consumer_scope=["pizza.example.com"],
    )


def resolved_bytes(ref):
    return {ref: json.dumps({"card_pan|Card": PAN, "card_exp|Exp": "12/29"}).encode()}


def good_payload():
    return {
        "merchant": "Pizza Corner",
        "items": ["pepperoni"],
        "delivery_address": "123 Demo St",
    }


def test_order_pizza_masks_pan_only_last4():
    res = order_pizza(resolved_bytes("p_abc123"), good_payload())
    assert res["card_last4"] == "0005"
    assert res["order_id"] == "FAKE-ABC123"
    assert res["status"] == "confirmed (demo)"
    assert res["amount_usd"] == 19.99
    encoded = json.dumps(res)
    assert PAN not in encoded
    assert CVV not in encoded
    assert "12/29" not in encoded


def test_order_pizza_requires_resolved():
    with pytest.raises(ValueError, match="at least one vault_ref"):
        order_pizza({}, good_payload())


def test_order_pizza_requires_dict_payload():
    with pytest.raises(ValueError, match="payload must be a dict"):
        order_pizza(resolved_bytes("p_abc123"), "nope")


def test_order_pizza_requires_payload_fields():
    with pytest.raises(ValueError, match="missing payload fields"):
        order_pizza(resolved_bytes("p_abc123"), {"merchant": "Pizza Corner"})
    with pytest.raises(ValueError, match="missing payload fields"):
        order_pizza(
            resolved_bytes("p_abc123"),
            {"merchant": "Pizza Corner", "items": [], "delivery_address": ""},
        )


def test_full_execute_draft_path_confirms_with_receipt(tmp_path):
    store, vault = make(tmp_path)
    rec = store_card(vault)
    d = store.create("user:1", "payment", "order_pizza", [rec["vault_ref"]], good_payload())
    store.approve(d, "user:1", "user:1")
    out = execute_draft(store, vault, d, "user:1", order_pizza)
    assert out["status"] == "confirmed"
    assert out["result"]["card_last4"] == PAN[-4:]
    assert out["result"]["amount_usd"] == 19.99
    assert PAN not in json.dumps(out)
    assert CVV not in json.dumps(out)
    got = store.get(d)
    assert got["status"] == "confirmed"
    assert PAN not in json.dumps(got["result"])


def test_missing_field_escalates_with_reason(tmp_path):
    store, vault = make(tmp_path)
    rec = store_card(vault)
    bad = {"merchant": "Pizza Corner", "items": ["pepperoni"]}  # no delivery_address
    d = store.create("user:1", "payment", "order_pizza", [rec["vault_ref"]], bad)
    store.approve(d, "user:1", "user:1")
    with pytest.raises(ValueError, match="missing payload fields"):
        execute_draft(store, vault, d, "user:1", order_pizza)
    got = store.get(d)
    assert got["status"] == "escalated"
    assert "delivery_address" in got["result"]["reason"]