"""Self-service credential capture issuer tests (closes the missing-link gap)."""

import pytest

from src.services.concierge.capture_tokens import CaptureTokenError, CaptureTokenStore
from src.services.concierge.credential_capture import CaptureRefused, build_credential_capture
from src.services.concierge.tenants import TenantStore

ADMIN = "admin:1"


def make(tmp_path, domain="amazon.com", tier="write"):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    tenants.enable(ADMIN, "user:1", tier=tier)
    tenants.allow_domain(ADMIN, "user:1", domain)
    return tenants, CaptureTokenStore(db)


def test_issues_capture_url_for_allowlisted_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    res = build_credential_capture("user:1", "amazon.com", tenants, tokens)
    assert res["domain"] == "amazon.com"
    assert res["expires_seconds"] == 600
    assert res["url"].startswith("http://127.0.0.1:8000/concierge/capture/")
    assert "?tenant=user:1" in res["url"]
    # plaintext token appears ONLY in the URL; it is single-use and tenant-bound
    token = res["url"].split("/capture/")[1].split("?")[0]
    intent = tokens.peek(token, "user:1")
    assert intent["kind"] == "secret"
    assert [f["label"] for f in intent["fields"]] == ["Email", "Password"]
    with pytest.raises(CaptureTokenError):
        tokens.peek(token, "user:2")


def test_capture_url_honors_public_env_base(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "https://concierge.incoming.nyc")
    tenants, tokens = make(tmp_path)
    res = build_credential_capture("user:1", "amazon.com", tenants, tokens)
    assert res["url"].startswith(
        "https://concierge.incoming.nyc/concierge/capture/")


def test_normalizes_scheme_www_and_path(tmp_path):
    tenants, tokens = make(tmp_path)
    for raw in ("https://www.amazon.com/login", "www.amazon.com", "AMAZON.COM"):
        res = build_credential_capture("user:1", raw, tenants, tokens)
        assert res["domain"] == "amazon.com"


def test_refuses_non_allowlisted_domain(tmp_path):
    tenants, tokens = make(tmp_path)
    with pytest.raises(CaptureRefused, match="amazon.co.uk"):
        build_credential_capture("user:1", "amazon.co.uk", tenants, tokens)


def test_refuses_when_tenant_disabled(tmp_path):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    tenants.enable(ADMIN, "user:2")  # user:1 not enabled
    with pytest.raises(CaptureRefused, match="not enabled"):
        build_credential_capture("user:1", "amazon.com", tenants,
                                 CaptureTokenStore(db))


def test_refuses_blank_domain(tmp_path):
    tenants, tokens = make(tmp_path)
    with pytest.raises(CaptureRefused, match="requires a domain"):
        build_credential_capture("user:1", "", tenants, tokens)
    with pytest.raises(CaptureRefused, match="requires a domain"):
        build_credential_capture("user:1", "https://", tenants, tokens)


def test_refusal_lists_the_tenant_allowlist(tmp_path):
    tenants, tokens = make(tmp_path, domain="amazon.com")
    tenants.allow_domain(ADMIN, "user:1", "paypal.com")
    with pytest.raises(CaptureRefused, match="amazon.com, paypal.com"):
        build_credential_capture("user:1", "evil.example.org", tenants, tokens)


# --- model-driven field schemas ---


def test_model_supplied_fields_are_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    res = build_credential_capture("user:1", "amazon.com", tenants, tokens, fields=[
        {"type": "text", "label": "Username", "required": True},
        {"type": "password", "label": "Password", "required": True},
        {"type": "otp", "label": "2FA", "required": False},
    ])
    token = res["url"].split("/capture/")[1].split("?")[0]
    intent = tokens.peek(token, "user:1")
    assert [f["label"] for f in intent["fields"]] == ["Username", "Password", "2FA"]


def test_model_fields_default_to_email_password(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    res = build_credential_capture("user:1", "amazon.com", tenants, tokens)
    token = res["url"].split("/capture/")[1].split("?")[0]
    intent = tokens.peek(token, "user:1")
    assert [f["label"] for f in intent["fields"]] == ["Email", "Password"]


def test_capture_link_is_idempotent_within_same_store(tmp_path):
    tenants, tokens = make(tmp_path)
    first = build_credential_capture("user:1", "amazon.com", tenants, tokens)
    second = build_credential_capture("user:1", "amazon.com", tenants, tokens)
    assert first["url"] == second["url"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert len(list(tokens._conn().execute("select token_hash from capture_tokens"))) == 1


def test_payment_and_identity_field_labels_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    for label in ("Card Number", "CVV", "SSN", "Debit card", "Routing number"):
        with pytest.raises(CaptureRefused, match="payment/identity"):
            build_credential_capture("user:1", "amazon.com", tenants, tokens,
                                     fields=[{"type": "text", "label": label}])


def test_unknown_field_type_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    with pytest.raises(CaptureRefused, match="type must be one of"):
        build_credential_capture("user:1", "amazon.com", tenants, tokens,
                                 fields=[{"type": "file", "label": "Photo"}])


def test_unsafe_field_label_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    with pytest.raises(CaptureRefused, match="safe characters"):
        build_credential_capture("user:1", "amazon.com", tenants, tokens,
                                 fields=[{"type": "text", "label": '"><script>x</script>'}])


def test_duplicate_field_labels_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    with pytest.raises(CaptureRefused, match="distinct labels"):
        build_credential_capture("user:1", "amazon.com", tenants, tokens, fields=[
            {"type": "text", "label": "Password"},
            {"type": "password", "label": "password"},
        ])


def test_card_typed_field_refused_by_schema(tmp_path, monkeypatch):
    """Secret-kind captures can never ask card_pan: the issuer's type
    allowlist excludes payment types (the vault schema would also reject
    card_pan outside kind=payment)."""
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    tenants, tokens = make(tmp_path)
    with pytest.raises(CaptureRefused, match="type must be one of"):
        build_credential_capture("user:1", "amazon.com", tenants, tokens,
                                 fields=[{"type": "card_pan", "label": "Card"}])


# --- llm_tool wrapper ---


def _tool_env(tmp_path, domain="amazon.com"):
    db = str(tmp_path / "t.db")
    tenants = TenantStore(db, admins=[ADMIN])
    tenants.enable(ADMIN, "user:1", tier="write")
    tenants.allow_domain(ADMIN, "user:1", domain)
    from src.services.concierge.audit import AuditLog
    return tenants, AuditLog(db), CaptureTokenStore(db)


def test_tool_issues_and_audits(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")
    from src.services.concierge.llm_tool import (
        ConciergeToolError, request_credential_capture,
    )
    tenants, audit, tokens = _tool_env(tmp_path)
    res = request_credential_capture(
        "user:1", {"domain": "amazon.com"}, tenants, audit, tokens)
    assert res["domain"] == "amazon.com"
    assert res["url"].startswith("http://127.0.0.1:8000/concierge/capture/")
    rows = audit.tail(limit=5)
    assert rows[-1]["action"] == "capture_issued"


def test_tool_refusal_is_audited_and_reported(tmp_path):
    from src.services.concierge.llm_tool import (
        ConciergeToolError, request_credential_capture,
    )
    tenants, audit, tokens = _tool_env(tmp_path, domain="amazon.com")
    with pytest.raises(ConciergeToolError, match="not on your concierge allowlist"):
        request_credential_capture(
            "user:1", {"domain": "amazon.co.uk"}, tenants, audit, tokens)
    rows = audit.tail(limit=5)
    assert rows[-1]["action"] == "capture_refused"


def test_tool_disabled_tenant_refused(tmp_path):
    from src.services.concierge.llm_tool import (
        ConciergeToolError, request_credential_capture,
    )
    db = str(tmp_path / "t.db")
    from src.services.concierge.audit import AuditLog
    tenants = TenantStore(db, admins=[ADMIN])  # no tenant enabled
    with pytest.raises(ConciergeToolError, match="not enabled"):
        request_credential_capture(
            "user:1", {"domain": "amazon.com"}, tenants, AuditLog(db),
            CaptureTokenStore(db))
