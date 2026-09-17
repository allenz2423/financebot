"""B0 admin plane tests: CONCIERGE_ADMINS-only gating, least-privilege enable,
cross-tenant isolation."""

import pytest

from src.services.concierge.tenants import (
    TenantStore,
    TenantError,
    NotAdminError,
    is_admin,
    TIERS,
)

ADMINS = ["user:1"]


def make(tmp_path):
    return TenantStore(str(tmp_path / "t.db"), admins=ADMINS)


def test_admins_come_from_config_only():
    assert TIERS == frozenset({"read", "write", "spend"})
    assert is_admin("user:1", ADMINS)
    assert not is_admin("user:2", ADMINS)


def test_non_admin_refused_on_every_operation(tmp_path):
    ts = make(tmp_path)
    operations = (
        lambda: ts.enable("user:2", "user:3"),
        lambda: ts.disable("user:2", "user:3"),
        lambda: ts.set_tier("user:2", "user:3", "write"),
        lambda: ts.set_spend_cap("user:2", "user:3", 5.0),
        lambda: ts.allow_domain("user:2", "user:3", "amazon.com"),
    )
    for op in operations:
        with pytest.raises(NotAdminError):
            op()


def test_least_privilege_enable(tmp_path):
    ts = make(tmp_path)
    st = ts.enable("user:1", "user:3")
    assert st["enabled"] is True
    assert st["tier"] == "read"
    assert st["spend_cap"] == 0.0
    assert ts.is_enabled("user:3") is True


def test_cross_tenant_isolation(tmp_path):
    ts = make(tmp_path)
    ts.enable("user:1", "user:3")
    ts.enable("user:1", "user:4", tier="spend", spend_cap=25.0)
    ts.allow_domain("user:1", "user:3", "amazon.com")
    st3 = ts.status("user:3")
    st4 = ts.status("user:4")
    assert "amazon.com" in st3["allow_domains"]
    assert "amazon.com" not in st4["allow_domains"]
    assert st4["spend_cap"] == 25.0
    ts.disable("user:1", "user:3")
    assert ts.is_enabled("user:3") is False
    assert ts.is_enabled("user:4") is True


def test_validation_errors(tmp_path):
    ts = make(tmp_path)
    with pytest.raises(TenantError):
        ts.enable("user:1", "user:3", tier="owner")
    with pytest.raises(TenantError):
        ts.set_spend_cap("user:1", "user:3", -1.0)
    with pytest.raises(TenantError):
        ts.allow_domain("user:1", "user:3", "a..b")