"""B0 capture pipeline tests: the 10 invariants, each one a test."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.security.vault import Vault
from src.services.concierge.capture_schema import (
    CaptureIntentError,
    validate_capture_intent,
    build_capture_page,
)
from src.services.concierge.capture_submit import process_capture_submission
from src.services.concierge.capture_tokens import (
    CaptureTokenError,
    CaptureTokenStore,
    TOKEN_TTL,
)

KEY = "b0-capture-test-master-key"

AMAZON = {
    "label": "Amazon login",
    "explanation": "Sign-in credentials for amazon.com",
    "kind": "secret",
    "consumer_scope": ["amazon.com"],
    "fields": [
        {"type": "text", "label": "Email", "required": True},
        {"type": "password", "label": "Password", "required": True},
        {"type": "captcha", "label": "Captcha", "required": False, "retention": "ephemeral"},
    ],
}

PAY = {
    "label": "Amex card",
    "explanation": "Card details for amex.com",
    "kind": "payment",
    "consumer_scope": ["amex.com"],
    "fields": [
        {"type": "card_pan", "label": "Card", "required": True},
        {"type": "card_exp", "label": "Exp", "required": True},
        {"type": "card_cvv", "label": "CVV", "required": True, "retention": "ephemeral"},
    ],
}


def make_stack(tmp_path):
    db = str(tmp_path / "c.db")
    return CaptureTokenStore(db), Vault(db, master_key=KEY)


def issue(store, tenant="user:1", intent=None):
    return store.issue(tenant, validate_capture_intent(intent or AMAZON, tenant))


def test_1_unknown_field_type_rejected_never_coerced():
    bad = dict(
        AMAZON,
        fields=[{"type": "email", "label": "Email"}, {"type": "password", "label": "Password"}],
    )
    with pytest.raises(CaptureIntentError, match="never coerced"):
        validate_capture_intent(bad, "user:1")


def test_2_token_single_use(tmp_path):
    store, _ = make_stack(tmp_path)
    token = issue(store)
    store.redeem(token, "user:1")
    with pytest.raises(CaptureTokenError, match="already used"):
        store.redeem(token, "user:1")


def test_3_token_tenant_bound(tmp_path):
    store, _ = make_stack(tmp_path)
    token = issue(store)
    with pytest.raises(CaptureTokenError, match="does not belong"):
        store.redeem(token, "user:99")
    with pytest.raises(CaptureTokenError, match="does not belong"):
        store.peek(token, "user:99")


def test_4_intent_bound_stored_intent_is_authoritative(tmp_path):
    store, vault = make_stack(tmp_path)
    token = issue(store)
    with pytest.raises(CaptureIntentError, match="undeclared fields"):
        process_capture_submission(
            store, vault, token, "user:1",
            {
                "text|Email": "a@b.co",
                "password|Password": "pw",
                "consumer_scope": ["evil.com"],
            },
        )


def test_4b_model_cannot_supply_tenant_token_delivery_mode():
    for key in ("tenant", "token", "delivery_mode"):
        with pytest.raises(CaptureIntentError, match="never model-supplied"):
            validate_capture_intent(dict(AMAZON, **{key: "x"}), "user:1")


def test_5_expires_about_ten_minutes(tmp_path):
    store, _ = make_stack(tmp_path)
    assert TOKEN_TTL == timedelta(minutes=10)
    token = issue(store)
    conn = sqlite3.connect(str(tmp_path / "c.db"))
    conn.execute(
        "UPDATE capture_tokens SET expires_at = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(CaptureTokenError, match="expired"):
        store.redeem(token, "user:1")


def test_6_values_never_in_errors(tmp_path):
    store, vault = make_stack(tmp_path)
    secret_pw = "s3cret-value-911"
    token = issue(store)
    with pytest.raises(CaptureIntentError) as ei:
        process_capture_submission(
            store, vault, token, "user:1",
            {"text|Email": "", "password|Password": secret_pw},
        )
    assert secret_pw not in str(ei.value)
    assert "Email" in str(ei.value)
    token2 = issue(store)
    with pytest.raises(CaptureIntentError) as e2:
        process_capture_submission(
            store, vault, token2, "user:1",
            {"text|Email": "a@b.co", "evil_field": secret_pw},
        )
    assert secret_pw not in str(e2.value)
    assert "evil_field" in str(e2.value)


def test_7_ephemeral_never_promotes(tmp_path):
    store, vault = make_stack(tmp_path)
    token = issue(store, intent=PAY)
    result = process_capture_submission(
        store, vault, token, "user:1",
        {
            "card_pan|Card": "378282246310005",
            "card_exp|Exp": "12/29",
            "card_cvv|CVV": "983",
        },
    )
    assert result["ephemeral_fields"] == ["CVV"]
    assert "983" not in json.dumps(result)
    rows = vault.list_records("user:1")
    assert len(rows) == 1
    manifest = json.dumps(vault.build_manifest("user:1"))
    assert "983" not in manifest
    out = vault.resolve("user:1", rows[0]["vault_ref"], consumer_url="https://amex.com/login")
    assert b"378282246310005" in out
    assert b"983" not in out


def test_8_vault_values_encrypted_before_persistence(tmp_path):
    store, vault = make_stack(tmp_path)
    token = issue(store, intent=PAY)
    result = process_capture_submission(
        store, vault, token, "user:1",
        {
            "card_pan|Card": "378282246310005",
            "card_exp|Exp": "12/29",
            "card_cvv|CVV": "983",
        },
    )
    ref = result["stored"][0]["vault_ref"]
    conn = sqlite3.connect(str(tmp_path / "c.db"))
    ct = conn.execute(
        "SELECT ciphertext FROM vault_records WHERE record_id = ?", (ref,)
    ).fetchone()[0]
    conn.close()
    assert ct.startswith("v1:")
    assert "378282246310005" not in ct


def test_9_consumer_scope_immutable_after_capture(tmp_path):
    store, vault = make_stack(tmp_path)
    token = issue(store)
    result = process_capture_submission(
        store, vault, token, "user:1",
        {"text|Email": "me@example.com", "password|Password": "pw-secret"},
    )
    assert result["consumer_scope"] == ["amazon.com"]
    rec = vault.list_records("user:1")[0]
    assert rec["consumer_scope"] == ["amazon.com"]


def test_10_model_receives_masks_refs_labels_only(tmp_path):
    store, vault = make_stack(tmp_path)
    token = issue(store, intent=PAY)
    result = process_capture_submission(
        store, vault, token, "user:1",
        {
            "card_pan|Card": "378282246310005",
            "card_exp|Exp": "12/29",
            "card_cvv|CVV": "983",
        },
    )
    s = json.dumps(result)
    assert "378282246310005" not in s
    assert "983" not in s
    assert "12/29" not in s
    display = result["stored"][0]["display"]
    assert "\u2022" in display
    assert "0005" in display


def test_session_profile_not_a_form_intake():
    bad = dict(AMAZON, kind="session_profile")
    with pytest.raises(CaptureIntentError, match="noVNC"):
        validate_capture_intent(bad, "user:1")


def test_generic_page_one_engine_all_kinds():
    # cc, amazon login, captcha: all render from the same machinery; the page
    # is derived data, never per-kind code branches.
    for intent in (AMAZON, PAY):
        validated = validate_capture_intent(intent, "user:1")
        page = build_capture_page(validated)
        assert page["label"] == intent["label"]
        assert page["kind"] == intent["kind"]
        assert page["delivery_mode"]
        assert "value" not in json.dumps(page)