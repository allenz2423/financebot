"""B0 vault tests: masks-only API (confinement), encrypted at rest,
schema-assigned delivery modes, tenant/scope/revocation enforcement.
"""

import json
import sqlite3

import pytest

from src.security.vault import (
    Vault,
    KIND_DELIVERY_MODE,
    VaultRecordNotFound,
    VaultRevokedError,
    VaultScopeError,
)

KEY = "b0-vault-test-master-key"


def make_vault(tmp_path, name="v.db"):
    return Vault(str(tmp_path / name), master_key=KEY)


def store_card(vault, tenant="user:1"):
    return vault.store_fields(
        tenant=tenant,
        kind="payment",
        label="Amex",
        fields={
            "card_pan|Card": "378282246310005",
            "card_exp|Exp": "12/29",
        },
        consumer_scope=["amex.com"],
    )


def test_kind_delivery_mode_is_schema_assigned(tmp_path):
    v = make_vault(tmp_path)
    assert KIND_DELIVERY_MODE == {
        "secret": "resolve_plaintext",
        "payment": "resolve_plaintext",
        "session_profile": "mount_only",
    }
    rec = store_card(v)
    assert rec["delivery_mode"] == "resolve_plaintext"
    rec2 = v.store_fields(
        tenant="user:1", kind="secret", label="Amazon",
        fields={"password|Password": "hunter2"},
        consumer_scope=["amazon.com"],
    )
    assert rec2["delivery_mode"] == "resolve_plaintext"
    prof = v.store_session_profile(
        tenant="user:1", label="brightspace profile",
        profile_bytes=b"\x00\x01profile-secret", consumer_scope=["brightspace.com"],
    )
    assert prof["delivery_mode"] == "mount_only"


def test_get_record_is_mask_only_no_value_fields(tmp_path):
    v = make_vault(tmp_path)
    rec = store_card(v)
    view = v.get_record("user:1", rec["vault_ref"])
    assert "ciphertext" not in view
    assert "378282246310005" not in json.dumps(view)
    assert "\u2022\u2022\u2022\u2022 0005" in view["display"]


def test_encrypted_at_rest(tmp_path):
    v = make_vault(tmp_path)
    rec = store_card(v)
    conn = sqlite3.connect(str(tmp_path / "v.db"))
    ct = conn.execute(
        "SELECT ciphertext FROM vault_records WHERE record_id = ?", (rec["vault_ref"],)
    ).fetchone()[0]
    conn.close()
    assert ct.startswith("v1:")
    assert "378282246310005" not in ct


def test_resolve_is_single_plaintext_path_scope_checked(tmp_path):
    v = make_vault(tmp_path)
    rec = store_card(v)
    with pytest.raises(VaultScopeError):
        v.resolve("user:1", rec["vault_ref"], consumer_url="https://evil.example.com/login")
    out = v.resolve("user:1", rec["vault_ref"], consumer_url="https://www.amex.com/login")
    assert b"378282246310005" in out


def test_cross_tenant_blocked(tmp_path):
    v = make_vault(tmp_path)
    rec = store_card(v)
    with pytest.raises(VaultRecordNotFound):
        v.get_record("user:2", rec["vault_ref"])
    with pytest.raises(VaultRecordNotFound):
        v.resolve("user:2", rec["vault_ref"])


def test_revocation_drops_manifest_and_refuses_resolve(tmp_path):
    v = make_vault(tmp_path)
    rec = store_card(v)
    assert v.build_manifest("user:1")["payments"]
    v.revoke("user:1", rec["vault_ref"])
    assert "payments" not in v.build_manifest("user:1")
    with pytest.raises(VaultRevokedError):
        v.resolve("user:1", rec["vault_ref"])


def test_session_profile_is_mount_only_no_plaintext_path(tmp_path):
    v = make_vault(tmp_path)
    prof = v.store_session_profile(
        tenant="user:1", label="noVNC brightspace",
        profile_bytes=b"profile-bytes-secret", consumer_scope=["brightspace.com"],
    )
    assert prof["delivery_mode"] == "mount_only"
    with pytest.raises(Exception):
        v.resolve("user:1", prof["vault_ref"])
    conn = sqlite3.connect(str(tmp_path / "v.db"))
    ct = conn.execute(
        "SELECT ciphertext FROM vault_records WHERE record_id = ?", (prof["vault_ref"],)
    ).fetchone()[0]
    conn.close()
    assert "profile-bytes-secret" not in ct


def test_manifest_never_contains_plaintext(tmp_path):
    v = make_vault(tmp_path)
    store_card(v)
    v.store_fields(
        tenant="user:1", kind="secret", label="Amazon",
        fields={"password|Password": "amzn-supersecret"}, consumer_scope=["amazon.com"],
    )
    s = json.dumps(v.build_manifest("user:1")) + json.dumps(v.list_records("user:1"))
    assert "amzn-supersecret" not in s
    assert "378282246310005" not in s


def test_unknown_field_type_rejected_never_coerced(tmp_path):
    v = make_vault(tmp_path)
    with pytest.raises(Exception, match="unknown field type"):
        v.store_fields(
            tenant="user:1", kind="secret", label="x",
            fields={"word|Word": "abc"}, consumer_scope=["a.com"],
        )


def test_forced_ephemeral_never_stored_defense_in_depth(tmp_path):
    v = make_vault(tmp_path)
    with pytest.raises(Exception, match="can never be vaulted"):
        v.store_fields(
            tenant="user:1", kind="payment", label="x",
            fields={"card_cvv|CVV": "123"}, consumer_scope=["amex.com"],
        )
    with pytest.raises(Exception, match="can never be vaulted"):
        v.store_fields(
            tenant="user:1", kind="secret", label="x",
            fields={"captcha|Captcha": "abcd"}, consumer_scope=["a.com"],
        )


def test_card_fields_require_kind_payment(tmp_path):
    v = make_vault(tmp_path)
    with pytest.raises(Exception, match="requires kind=payment"):
        v.store_fields(
            tenant="user:1", kind="secret", label="x",
            fields={"card_pan|Card": "4111111111111111"}, consumer_scope=["a.com"],
        )