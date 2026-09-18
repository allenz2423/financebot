"""Milestone 2: Database & State Persistence Tests.

Verifies:
1. Vault.store_browser_state and Vault.resolve_browser_state:
   - AES-256-GCM encryption/decryption of browser storage state at rest in SQLite.
   - Faithful round-trip serialization of both cookies AND origins (localStorage).
   - Tamper detection (corrupted ciphertext, corrupted auth tag, malformed envelope).
   - In-place update of existing state references with policy/kind validation.
   - Tenant isolation and revocation enforcement.
2. _make_resolver credential matching:
   - Apex domain credential matching subdomains (e.g. example.com -> signin.example.com, paypal.com -> auth.paypal.com).
   - Disambiguation when multiple active credentials exist for a domain (selecting latest record without ValueError).
   - Multi-field credential payload resolution matching field hints (username, email, password) and default password fallback.
   - Missing credential handling when visited domain has no matching secret.
3. Mission state persistence and rehydration:
   - ApprovalStore.set_mission_state_ref and retrieval via active_mission / active_missions_for_tenant.
   - execute_approved_act auto-snapshotting browser storage state and recording state_vault_ref in SQLite.
   - BrowserlessActuator.restore_storage_state injecting cookies and localStorage prior to page navigation.
   - agentic_browser_step rehydrating an attached session from SQLite vault after a process restart.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import pytest

from src.security.vault import (
    Vault,
    VaultError,
    VaultRecordNotFound,
    VaultRevokedError,
)
from src.security.crypto import EncryptionError
from src.bot.approval_views import (
    _make_resolver,
    BrowserlessActuator,
    execute_approved_act,
    agentic_browser_step,
    AGENTIC_BROWSER_SESSIONS,
)
from src.services.concierge.approval_store import ApprovalStore
from src.services.concierge.act_actions import Actuator
from src.services.concierge import browser_driver


MASTER_KEY = "test-browser-persistence-master-key-32b"


@pytest.fixture
def test_vault(tmp_path):
    db_path = str(tmp_path / "persistence_test.db")
    return Vault(db_path=db_path, master_key=MASTER_KEY)


@pytest.fixture
def test_store(tmp_path):
    db_path = str(tmp_path / "persistence_test.db")
    return ApprovalStore(db_path=db_path)


@pytest.fixture(autouse=True)
def configure_browser_driver():
    orig_url = browser_driver.BROWSERLESS_URL
    browser_driver.BROWSERLESS_URL = "http://localhost:3002"
    yield
    browser_driver.BROWSERLESS_URL = orig_url


# ============================================================================
# Part 1: Vault.store_browser_state and Vault.resolve_browser_state
# ============================================================================


def test_vault_store_browser_state_aes_gcm_encrypted_at_rest(test_vault):
    """Browser storage state must be AES-256-GCM encrypted in SQLite vault_records."""
    tenant = "user:12345"
    sample_state = {
        "cookies": [
            {
                "name": "session_id",
                "value": "secret_session_token_xyz_987",
                "domain": "example.com",
                "path": "/",
                "expires": 1800000000,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [
            {
                "origin": "https://example.com",
                "localStorage": [
                    {"name": "authToken", "value": "jwt_secret_payload_456"},
                    {"name": "userPref", "value": "darkMode"},
                ],
            }
        ],
        "__current_url": "https://example.com/dashboard",
    }
    state_bytes = json.dumps(sample_state).encode("utf-8")

    rec = test_vault.store_browser_state(
        tenant=tenant,
        label="Example Session",
        state_bytes=state_bytes,
        consumer_scope=["example.com"],
    )

    vault_ref = rec["vault_ref"]
    assert vault_ref.startswith("profile_")
    assert rec["kind"] == "session_profile"
    assert rec["delivery_mode"] == "mount_only"
    assert rec["consumer_scope"] == ["example.com"]

    # Verify policy in stored mask record
    mask = test_vault.get_record(tenant, vault_ref)
    assert mask["policy"] == "browser_state"

    # Verify directly from SQLite that ciphertext is encrypted and plaintext is absent
    conn = sqlite3.connect(test_vault.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM vault_records WHERE record_id = ? AND tenant = ?",
        (vault_ref, tenant),
    ).fetchone()
    conn.close()

    assert row is not None
    ciphertext = row["ciphertext"]
    assert isinstance(ciphertext, str)
    assert ciphertext.startswith("v1:")
    # Plaintext credentials/cookies must NOT appear anywhere in the database row
    assert "secret_session_token_xyz_987" not in ciphertext
    assert "jwt_secret_payload_456" not in ciphertext
    assert "Example Session" not in ciphertext  # ciphertext only contains encrypted state bytes

    # Verify envelope structure: v1:<nonce_b64>:<ct_b64>:<tag_b64>
    parts = ciphertext.split(":")
    assert len(parts) == 4
    nonce = base64.urlsafe_b64decode(parts[1])
    ct = base64.urlsafe_b64decode(parts[2])
    tag = base64.urlsafe_b64decode(parts[3])
    assert len(nonce) == 12  # 12-byte GCM nonce
    assert len(tag) == 16  # 16-byte GCM authentication tag
    assert len(ct) == len(state_bytes)  # AES-GCM preserves plaintext length

    # Decrypt and verify exact match
    resolved_bytes = test_vault.resolve_browser_state(tenant, vault_ref)
    assert resolved_bytes == state_bytes
    resolved_state = json.loads(resolved_bytes.decode("utf-8"))
    assert resolved_state == sample_state


def test_vault_store_browser_state_cookies_and_origins_roundtrip(test_vault):
    """Both cookies AND origins (localStorage) must serialize and round-trip faithfully."""
    tenant = "user:financial_advisor"
    complex_state = {
        "cookies": [
            {
                "name": "cf_clearance",
                "value": "abcdef1234567890",
                "domain": ".example.com",
                "path": "/",
                "expires": 1789000000.5,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            },
            {
                "name": "auth_cookie",
                "value": "token_val_==--",
                "domain": "checkout.example.com",
                "path": "/cart",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Strict",
            },
        ],
        "origins": [
            {
                "origin": "https://checkout.example.com",
                "localStorage": [
                    {"name": "x-session-id", "value": "uuid-999-aaa-bbb"},
                    {"name": "cart_items", "value": '{"id": 101, "qty": 2}'},
                    {"name": "empty_val", "value": ""},
                ],
            },
            {
                "origin": "https://auth.example.com",
                "localStorage": [
                    {"name": "pkce_verifier", "value": "secret_pkce_string_xyz"},
                ],
            },
        ],
        "__current_url": "https://checkout.example.com/cart/step2",
    }
    state_bytes = json.dumps(complex_state).encode("utf-8")

    rec = test_vault.store_browser_state(
        tenant=tenant,
        label="Merchant Session State",
        state_bytes=state_bytes,
        consumer_scope=["example.com"],
    )

    resolved_bytes = test_vault.resolve_browser_state(tenant, rec["vault_ref"])
    rehydrated = json.loads(resolved_bytes.decode("utf-8"))

    # Assert cookies roundtrip
    assert len(rehydrated["cookies"]) == 2
    assert rehydrated["cookies"][0]["name"] == "cf_clearance"
    assert rehydrated["cookies"][0]["value"] == "abcdef1234567890"
    assert rehydrated["cookies"][1]["name"] == "auth_cookie"
    assert rehydrated["cookies"][1]["path"] == "/cart"

    # Assert origins / localStorage roundtrip
    assert len(rehydrated["origins"]) == 2
    merchant_origin = next(o for o in rehydrated["origins"] if o["origin"] == "https://checkout.example.com")
    assert len(merchant_origin["localStorage"]) == 3
    assert {"name": "x-session-id", "value": "uuid-999-aaa-bbb"} in merchant_origin["localStorage"]
    assert {"name": "cart_items", "value": '{"id": 101, "qty": 2}'} in merchant_origin["localStorage"]
    assert {"name": "empty_val", "value": ""} in merchant_origin["localStorage"]

    auth_origin = next(o for o in rehydrated["origins"] if o["origin"] == "https://auth.example.com")
    assert auth_origin["localStorage"] == [{"name": "pkce_verifier", "value": "secret_pkce_string_xyz"}]

    assert rehydrated["__current_url"] == "https://checkout.example.com/cart/step2"


def test_vault_store_browser_state_update_existing_ref(test_vault):
    """Updating state via existing_ref must overwrite ciphertext in place."""
    tenant = "user:updater"
    initial_state = json.dumps({"cookies": [{"name": "s", "value": "v1"}]}).encode("utf-8")
    updated_state = json.dumps({"cookies": [{"name": "s", "value": "v2_updated"}]}).encode("utf-8")

    rec = test_vault.store_browser_state(
        tenant=tenant,
        label="Initial State",
        state_bytes=initial_state,
        consumer_scope=["example.com"],
    )
    initial_ref = rec["vault_ref"]

    # Update using existing_ref
    updated_rec = test_vault.store_browser_state(
        tenant=tenant,
        label="Ignored on update",
        state_bytes=updated_state,
        consumer_scope=["example.com"],
        existing_ref=initial_ref,
    )
    assert updated_rec["vault_ref"] == initial_ref

    # Resolve and verify updated content
    decrypted = test_vault.resolve_browser_state(tenant, initial_ref)
    assert decrypted == updated_state

    # Trying to update with non-existent ref raises VaultRecordNotFound
    with pytest.raises(VaultRecordNotFound):
        test_vault.store_browser_state(
            tenant=tenant,
            label="bad",
            state_bytes=updated_state,
            consumer_scope=["example.com"],
            existing_ref="profile_nonexistent123",
        )


def test_vault_resolve_browser_state_tamper_detection(test_vault):
    """Corrupted ciphertext, tag, or nonce must be rejected with EncryptionError."""
    tenant = "user:tamper_test"
    state_bytes = json.dumps({"cookies": [{"name": "a", "value": "b"}]}).encode("utf-8")
    rec = test_vault.store_browser_state(tenant, "Tamper Test", state_bytes, ["example.com"])
    vault_ref = rec["vault_ref"]

    conn = sqlite3.connect(test_vault.db_path)
    cur = conn.cursor()
    cur.execute("SELECT ciphertext FROM vault_records WHERE record_id = ?", (vault_ref,))
    orig_envelope = cur.fetchone()[0]

    parts = orig_envelope.split(":")
    assert len(parts) == 4
    prefix, nonce_b64, ct_b64, tag_b64 = parts

    # 1. Tamper ciphertext: flip bits
    ct_raw = bytearray(base64.urlsafe_b64decode(ct_b64))
    ct_raw[0] ^= 0x01
    tampered_ct = base64.urlsafe_b64encode(bytes(ct_raw)).decode("ascii")
    tampered_envelope = f"{prefix}:{nonce_b64}:{tampered_ct}:{tag_b64}"
    cur.execute("UPDATE vault_records SET ciphertext = ? WHERE record_id = ?", (tampered_envelope, vault_ref))
    conn.commit()

    with pytest.raises(EncryptionError, match="tamper or wrong key"):
        test_vault.resolve_browser_state(tenant, vault_ref)

    # 2. Tamper tag: flip bits
    tag_raw = bytearray(base64.urlsafe_b64decode(tag_b64))
    tag_raw[0] ^= 0x01
    tampered_tag = base64.urlsafe_b64encode(bytes(tag_raw)).decode("ascii")
    tampered_envelope = f"{prefix}:{nonce_b64}:{ct_b64}:{tampered_tag}"
    cur.execute("UPDATE vault_records SET ciphertext = ? WHERE record_id = ?", (tampered_envelope, vault_ref))
    conn.commit()

    with pytest.raises(EncryptionError, match="tamper or wrong key"):
        test_vault.resolve_browser_state(tenant, vault_ref)

    # 3. Corrupt envelope structure: invalid base64
    cur.execute("UPDATE vault_records SET ciphertext = ? WHERE record_id = ?", ("v1:@@@:bad:tag", vault_ref))
    conn.commit()

    with pytest.raises(EncryptionError):
        test_vault.resolve_browser_state(tenant, vault_ref)

    conn.close()


def test_vault_resolve_browser_state_access_controls(test_vault):
    """Tenant isolation, revoked records, and policy validation must fail closed."""
    tenant1 = "user:owner"
    tenant2 = "user:intruder"
    state_bytes = json.dumps({"cookies": []}).encode("utf-8")
    rec = test_vault.store_browser_state(tenant1, "Isolated State", state_bytes, ["example.com"])
    ref = rec["vault_ref"]

    # Intruder tenant cannot resolve owner's record
    with pytest.raises(VaultRecordNotFound):
        test_vault.resolve_browser_state(tenant2, ref)

    # Revoking record causes resolve to raise VaultRevokedError
    test_vault.revoke(tenant1, ref)
    with pytest.raises(VaultRevokedError, match="unavailable"):
        test_vault.resolve_browser_state(tenant1, ref)

    # Non-browser_state records cannot be resolved via resolve_browser_state
    rec_secret = test_vault.store_fields(
        tenant1, "secret", "My Secret", {"password|Password": "abc"}, ["example.com"]
    )
    with pytest.raises(VaultError, match="not a browser state snapshot"):
        test_vault.resolve_browser_state(tenant1, rec_secret["vault_ref"])


# ============================================================================
# Part 2: _make_resolver credential matching
# ============================================================================


def test_make_resolver_apex_domain_matches_subdomains(test_vault):
    """Credentials scoped to apex domain must match authentication subdomains."""
    tenant = "user:shopper"

    # Store credential scoped to apex amazon.com
    test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="Amazon Login",
        fields={
            "text|Email": "shopper@example.com",
            "password|Password": "amz_super_secret_pass_2026",
        },
        consumer_scope=["amazon.com"],
    )

    # Store credential scoped to apex paypal.com
    test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="PayPal Login",
        fields={
            "text|User": "paypal_shopper",
            "password|Password": "paypal_pass_secure_99",
        },
        consumer_scope=["paypal.com"],
    )

    # 1. Target URL on subdomain signin.amazon.com
    amz_sub_resolver = _make_resolver(tenant, test_vault, "https://signin.amazon.com/ap/signin", field_hint="password")
    assert amz_sub_resolver() == "amz_super_secret_pass_2026"

    # 2. Target URL on nested subdomain auth.sub.amazon.com
    amz_nested_resolver = _make_resolver(tenant, test_vault, "https://auth.sub.amazon.com/login", field_hint="password")
    assert amz_nested_resolver() == "amz_super_secret_pass_2026"

    # 3. Target URL on apex domain amazon.com
    amz_apex_resolver = _make_resolver(tenant, test_vault, "https://amazon.com/gp/sign-in.html", field_hint="password")
    assert amz_apex_resolver() == "amz_super_secret_pass_2026"

    # 4. Target URL on auth.paypal.com/signin
    paypal_sub_resolver = _make_resolver(tenant, test_vault, "https://auth.paypal.com/signin", field_hint="password")
    assert paypal_sub_resolver() == "paypal_pass_secure_99"

    # 5. Target URL on www.paypal.com
    paypal_www_resolver = _make_resolver(tenant, test_vault, "https://www.paypal.com/signin", field_hint="password")
    assert paypal_www_resolver() == "paypal_pass_secure_99"


def test_make_resolver_wildcard_scope_and_exact_subdomain(test_vault):
    """Wildcard scope and exact subdomain scopes must resolve accurately."""
    tenant = "user:wildcard"

    # Wildcard scope: *.service.com
    test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="Service Wildcard",
        fields={"password|Password": "wildcard_secret"},
        consumer_scope=["*.service.com"],
    )

    # Specific subdomain scope: login.special.com
    test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="Specific Subdomain",
        fields={"password|Password": "special_secret"},
        consumer_scope=["login.special.com"],
    )

    # Wildcard matches app.service.com
    res_wildcard = _make_resolver(tenant, test_vault, "https://app.service.com/login")
    assert res_wildcard() == "wildcard_secret"

    # Specific subdomain matches login.special.com
    res_special = _make_resolver(tenant, test_vault, "https://login.special.com/oauth")
    assert res_special() == "special_secret"

    # Specific subdomain does NOT match other.special.com
    res_other = _make_resolver(tenant, test_vault, "https://other.special.com/oauth")
    with pytest.raises(ValueError, match="no credential is available"):
        res_other()


def test_make_resolver_multiple_active_credentials_disambiguation(test_vault, caplog):
    """When multiple active credentials exist for a domain, latest record is chosen without raising ValueError."""
    import logging
    tenant = "user:multi_cred"

    # Store older credential
    rec_old = test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="Old PayPal Credential",
        fields={"password|Password": "old_paypal_password_2024"},
        consumer_scope=["paypal.com"],
    )

    # Store newer credential
    rec_new = test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="New PayPal Credential",
        fields={"password|Password": "new_paypal_password_2026"},
        consumer_scope=["paypal.com"],
    )

    # Ensure rec_new is deterministically newer in created_at / updated_at
    conn = sqlite3.connect(test_vault.db_path)
    conn.execute(
        "UPDATE vault_records SET created_at = '2026-01-01 10:00:00', updated_at = '2026-01-01 10:00:00' WHERE record_id = ?",
        (rec_old["vault_ref"],),
    )
    conn.execute(
        "UPDATE vault_records SET created_at = '2026-01-02 10:00:00', updated_at = '2026-01-02 10:00:00' WHERE record_id = ?",
        (rec_new["vault_ref"],),
    )
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING):
        resolver = _make_resolver(tenant, test_vault, "https://auth.paypal.com/signin", field_hint="password")
        resolved_val = resolver()

    # Must resolve the newest password
    assert resolved_val == "new_paypal_password_2026"

    # Must have logged a warning informing about disambiguation instead of raising ValueError
    assert any("Multiple stored credentials exist for" in record.message for record in caplog.records)


def test_make_resolver_field_hint_resolution(test_vault):
    """_make_resolver extracts requested field (username/email vs password) based on field_hint."""
    tenant = "user:hints"

    # 1. Credential with username and password
    test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="Username Account",
        fields={
            "text|Username": "alice_account",
            "password|Password": "p@ssword_primary_987",
        },
        consumer_scope=["portal.company.org"],
    )

    url_portal = "https://portal.company.org/login"

    # Username hint returns username
    res_user = _make_resolver(tenant, test_vault, url_portal, field_hint="user")
    assert res_user() == "alice_account"

    # Password hint returns password
    res_pass = _make_resolver(tenant, test_vault, url_portal, field_hint="#password")
    assert res_pass() == "p@ssword_primary_987"

    # Default fallback when hint is empty prefers password
    res_default = _make_resolver(tenant, test_vault, url_portal, field_hint="")
    assert res_default() == "p@ssword_primary_987"

    # 2. Credential with email and password
    test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="Email Account",
        fields={
            "text|Email": "bob@mailcorp.org",
            "password|Password": "bob_secret_pass_456",
        },
        consumer_scope=["mail.mailcorp.org"],
    )

    url_mail = "https://mail.mailcorp.org/login"

    # Email hint returns email
    res_email = _make_resolver(tenant, test_vault, url_mail, field_hint="input[type='email']")
    assert res_email() == "bob@mailcorp.org"

    # Password hint returns password
    res_mail_pass = _make_resolver(tenant, test_vault, url_mail, field_hint="password")
    assert res_mail_pass() == "bob_secret_pass_456"


def test_make_resolver_missing_and_unrelated_domains_raise_value_error(test_vault):
    """Unregistered or out-of-scope domains fail gracefully with descriptive ValueError."""
    tenant = "user:missing"

    test_vault.store_fields(
        tenant=tenant,
        kind="secret",
        label="Example Scope",
        fields={"password|Password": "pass123"},
        consumer_scope=["example.com"],
    )

    # 1. Completely unrelated domain
    resolver_unrelated = _make_resolver(tenant, test_vault, "https://unrelated-domain.com/login")
    with pytest.raises(ValueError, match="no credential is available for the visited domain unrelated-domain.com"):
        resolver_unrelated()

    # 2. Lookalike domain (attacker: evil-example.com vs example.com)
    resolver_evil = _make_resolver(tenant, test_vault, "https://evil-example.com/signin")
    with pytest.raises(ValueError, match="no credential is available for the visited domain evil-example.com"):
        resolver_evil()

    # 3. Explicit invalid vault_ref
    resolver_bad_ref = _make_resolver(tenant, test_vault, "https://example.com/login")
    with pytest.raises(ValueError):
        resolver_bad_ref(vault_ref="s_nonexistent_ref")


# ============================================================================
# Part 3: Mission State & Rehydration across restarts
# ============================================================================


def test_approval_store_set_and_get_mission_state_ref(test_store):
    """ApprovalStore must persist state_vault_ref and retrieve it on active missions."""
    tenant = "user:mission_tracker"
    domain = "example.com"
    url = "https://example.com/login"

    # Create proposal and grant mission
    p_id = test_store.create(
        tenant=tenant,
        uid="u1",
        kind="fill_form",
        args={"domain": domain, "steps": [{"action": "screenshot"}]},
        url=url,
        steps=[{"action": "screenshot"}],
        allowed_domains=[domain],
    )
    test_store.claim(p_id)
    mission = test_store.grant_mission(p_id)
    assert mission is not None
    mission_id = mission["mission_id"]
    assert mission.get("state_vault_ref") is None

    # Set mission state ref
    state_ref = "profile_snapshot_state_999"
    test_store.set_mission_state_ref(mission_id, tenant, state_ref)

    # Verify retrieval via active_mission
    active = test_store.active_mission(tenant, "fill_form", domain)
    assert active is not None
    assert active["mission_id"] == mission_id
    assert active["state_vault_ref"] == state_ref

    # Verify retrieval via active_missions_for_tenant
    tenant_missions = test_store.active_missions_for_tenant(tenant)
    assert len(tenant_missions) == 1
    assert tenant_missions[0]["state_vault_ref"] == state_ref


def test_execute_approved_act_persists_initial_mission_state(test_vault, test_store):
    """execute_approved_act must snapshot storage_state and store state_vault_ref on mission approval."""
    tenant = "user:exec_test"
    domain = "example.com"
    url = "https://example.com/checkout"
    state_payload = json.dumps({
        "cookies": [{"name": "shop_sess", "value": "xyz_session_123", "domain": domain, "path": "/"}],
        "origins": [{"origin": f"https://{domain}", "localStorage": [{"name": "cart_id", "value": "c_555"}]}],
        "__current_url": url,
    }).encode("utf-8")

    class MockActuatorWithState(Actuator):
        def __init__(self, url, allowed):
            self.url = url
            self.allowed = allowed
            self._page = None

        def navigate(self, url): pass
        def screenshot(self, fast=False): return b"fake-png-data"
        def get_text(self): return "Checkout Confirmation"
        def storage_state(self): return state_payload
        def close(self): pass

    async def mock_verify(*a, **k):
        return {"verdict": "verified", "confidence": 1.0}

    p_id = test_store.create(
        tenant=tenant,
        uid="u1",
        kind="fill_form",
        args={"domain": domain, "steps": [{"action": "screenshot"}]},
        url=url,
        steps=[{"action": "screenshot"}],
        allowed_domains=[domain],
    )

    res = execute_approved_act(
        proposal_id=p_id,
        tenant=tenant,
        store=test_store,
        vault=test_vault,
        actuator_factory=lambda u, a: MockActuatorWithState(u, a),
        verify_factory=mock_verify,
    )

    assert res["ok"] is True
    mission = res["mission"]
    assert mission is not None
    saved_ref = mission.get("state_vault_ref")
    assert saved_ref is not None
    assert saved_ref.startswith("profile_")

    # Confirm in ApprovalStore SQLite DB
    db_mission = test_store.active_mission(tenant, "fill_form", domain)
    assert db_mission["state_vault_ref"] == saved_ref

    # Confirm in Vault SQLite DB that decrypted bytes match state_payload
    decrypted = test_vault.resolve_browser_state(tenant, saved_ref)
    assert decrypted == state_payload
    parsed = json.loads(decrypted.decode("utf-8"))
    assert parsed["cookies"][0]["value"] == "xyz_session_123"
    assert parsed["origins"][0]["localStorage"][0]["value"] == "c_555"


def test_browserless_actuator_restore_storage_state():
    """restore_storage_state restores both cookies AND localStorage prior to page navigation."""
    act = BrowserlessActuator(
        "https://example.com",
        cdp_url="ws://localhost:3002/",
        allowed_domains=["example.com"],
    )

    raw_state = {
        "cookies": [
            {
                "name": "auth_cookie_test",
                "value": "secret_cookie_val_111",
                "domain": "example.com",
                "path": "/",
                "httpOnly": False,
                "secure": False,
            }
        ],
        "origins": [
            {
                "origin": "https://example.com",
                "localStorage": [
                    {"name": "user_token_key", "value": "jwt_token_val_222"},
                ],
            }
        ],
        "__current_url": "https://example.com",
    }
    state_bytes = json.dumps(raw_state).encode("utf-8")

    try:
        act.restore_storage_state(state_bytes)

        # Verify localStorage is accessible in the page
        ls_val = act._loop.run_until_complete(
            act._page.evaluate("() => window.localStorage.getItem('user_token_key')")
        )
        assert ls_val == "jwt_token_val_222"

        # Verify cookie is set in context
        cookies = act._loop.run_until_complete(act._context.cookies())
        matching = [c for c in cookies if c["name"] == "auth_cookie_test"]
        assert len(matching) >= 1
        assert matching[0]["value"] == "secret_cookie_val_111"

    finally:
        act.close()


def test_agentic_browser_step_rehydration_after_restart(test_vault, test_store, monkeypatch):
    """agentic_browser_step must rehydrate session from SQLite vault when not in memory."""
    tenant = "user:rehydrater"
    domain = "example.com"
    url = "https://example.com"

    # 1. Setup stored browser state in Vault
    state_payload = json.dumps({
        "cookies": [{"name": "rehydrate_cookie", "value": "restored_val_777", "domain": domain, "path": "/"}],
        "origins": [{"origin": url, "localStorage": [{"name": "rehydrate_token", "value": "tok_888"}]}],
        "__current_url": url,
    }).encode("utf-8")

    rec = test_vault.store_browser_state(tenant, "Rehydration Session", state_payload, [domain])
    vault_ref = rec["vault_ref"]

    # 2. Setup active mission in ApprovalStore
    p_id = test_store.create(
        tenant=tenant,
        uid="u1",
        kind="fill_form",
        args={"domain": domain, "steps": [{"action": "observe"}]},
        url=url,
        steps=[{"action": "observe"}],
        allowed_domains=[domain],
    )
    test_store.claim(p_id)
    mission = test_store.grant_mission(p_id)
    mission_id = mission["mission_id"]
    test_store.set_mission_state_ref(mission_id, tenant, vault_ref)

    # 3. Simulate bot restart: clear in-memory sessions
    AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)

    # Monkeypatch Vault and ApprovalStore default constructors in approval_views to point to test DB
    monkeypatch.setattr("src.bot.approval_views.Vault", lambda db=None: test_vault)
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: test_store)

    try:
        # Step action rehydrates from vault
        step_res = agentic_browser_step(
            tenant=tenant,
            mission_id=mission_id,
            action="observe",
        )

        assert step_res["mission_id"] == mission_id
        assert step_res["action"] == "observe"
        assert mission_id in AGENTIC_BROWSER_SESSIONS

        rehydrated_actuator = AGENTIC_BROWSER_SESSIONS[mission_id]
        # Verify that localStorage item from vault state was restored into page
        ls_val = rehydrated_actuator._loop.run_until_complete(
            rehydrated_actuator._page.evaluate("() => window.localStorage.getItem('rehydrate_token')")
        )
        assert ls_val == "tok_888"

    finally:
        act = AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)
        if act:
            act.close()


def test_agentic_browser_step_missing_saved_state_raises_error(test_store, monkeypatch):
    """When mission is detached and has no state_vault_ref, step must raise descriptive error."""
    tenant = "user:nostate"
    domain = "example.com"
    url = "https://example.com"

    p_id = test_store.create(
        tenant=tenant,
        uid="u1",
        kind="fill_form",
        args={"domain": domain},
        url=url,
        steps=[],
        allowed_domains=[domain],
    )
    test_store.claim(p_id)
    mission = test_store.grant_mission(p_id)
    mission_id = mission["mission_id"]

    # Ensure detached and state_vault_ref is None
    AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)

    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: test_store)

    with pytest.raises(ValueError, match="browser mission is no longer attached and has no saved session state"):
        agentic_browser_step(tenant=tenant, mission_id=mission_id, action="observe")
