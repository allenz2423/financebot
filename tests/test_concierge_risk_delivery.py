"""B1 concierge risk-delivery tests: watchdog pass, DM delivery, due alerts."""

import asyncio
import sqlite3

import pytest

from src.security.vault import DEFAULT_DB_PATH as _VAULT_DB  # noqa: F401 (sanity import)
from src.services.concierge.audit import AuditLog
from src.services.concierge.monitor import (
    _deliver_concierge_alerts,
    _tenant_user_id,
    concierge_alerts_due,
    run_once,
)
from src.services.concierge.risk_monitor import RiskRuleStore, run_risk_pass
from src.services.concierge.tenants import TenantStore

ADMIN = "admin:1"


class FakeUser:
    def __init__(self, uid):
        self.uid = uid
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class FakeBot:
    def __init__(self, fail_fetch=False):
        self.fail_fetch = fail_fetch
        self.users = {}
        self.fetch_calls = []

    async def fetch_user(self, uid):
        self.fetch_calls.append(uid)
        if self.fail_fetch:
            raise RuntimeError("fetch_user unavailable")
        u = FakeUser(uid)
        self.users[uid] = u
        return u


def make(tmp_path):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    tenants.enable(ADMIN, "user:1")
    store = RiskRuleStore(db)
    audit = AuditLog(db)
    return tenants, store, audit, db


def seed_storm(audit, tenant, n=3, label="Amex"):
    for i in range(n):
        audit.append(actor=tenant, action="capture_stored", tenant=tenant,
                     subject=label, detail={"label": label, "i": i})


def test_parse_uid_from_tenant():
    assert _tenant_user_id("user:342385739952160769") == "342385739952160769"
    assert _tenant_user_id("user:1") == "1"
    assert _tenant_user_id("not_a_tenant") is None
    assert _tenant_user_id("") is None


def test_deliver_dm_payload_shape_and_count():
    bot = FakeBot()
    rows = [
        {"id": 1, "rule_kind": "capture_storm", "severity": "warn",
         "title": "risk capture_storm: storm", "detail": "burst for user:1"},
        {"id": 2, "rule_kind": "out_of_allowlist_attempt", "severity": "warn",
         "title": "risk out_of_allowlist_attempt: x", "detail": "3 attempts"},
    ]
    delivered = asyncio.run(_deliver_concierge_alerts(rows, bot, "1"))
    assert delivered == 2
    u = bot.users[1]
    assert len(u.sent) == 2
    assert u.sent[0].startswith("[Concierge Risk WARN]")
    assert "burst for user:1" in u.sent[0]


def test_deliver_does_not_raise_when_fetch_user_fails():
    bot = FakeBot(fail_fetch=True)
    rows = [{"id": 1, "rule_kind": "capture_storm", "severity": "warn",
             "title": "t", "detail": "d"}]
    delivered = asyncio.run(_deliver_concierge_alerts(rows, bot, "1"))
    assert delivered == 0


def test_alerts_due_returns_only_unacked(tmp_path):
    tenants, store, audit, db = make(tmp_path)
    store.upsert("user:1", "storm", "capture_storm", {"threshold": 2})
    seed_storm(audit, "user:1", n=2)
    res = run_risk_pass(store, audit, "user:1")
    assert res["rules_fired"] == 1
    conn = sqlite3.connect(db)
    due = concierge_alerts_due(conn, "1")
    assert len(due) == 1
    assert due[0]["rule_kind"] == "capture_storm"
    assert due[0]["acked"] is False
    assert store.ack_alert("user:1", due[0]["id"]) is True
    assert concierge_alerts_due(conn, "1") == []


def test_run_once_end_to_end(tmp_path):
    tenants, store, audit, db = make(tmp_path)
    store.upsert("user:1", "storm", "capture_storm", {"threshold": 3})
    seed_storm(audit, "user:1", n=3)
    bot = FakeBot()
    summary = asyncio.run(run_once(store, audit, tenants, bot))
    assert summary["rules_fired"] == 1
    assert summary["alert_ids"]  # non-empty
    assert summary["delivered"] == 1
    u = bot.users[1]
    assert any("concierge" in s.lower() or "risk" in s.lower() for s in u.sent)


def test_cooldown_suppresses_second_pass(tmp_path):
    tenants, store, audit, db = make(tmp_path)
    store.upsert("user:1", "storm", "capture_storm", {"threshold": 2})
    seed_storm(audit, "user:1", n=2)
    bot = FakeBot()
    first = asyncio.run(run_once(store, audit, tenants, bot))
    assert first["rules_fired"] == 1
    seed_storm(audit, "user:1", n=2)  # more events, but cooldown (6h default)
    second = asyncio.run(run_once(store, audit, tenants, bot))
    assert second["rules_fired"] == 0
    assert len(bot.users[1].sent) == 1  # no second DM


def test_rules_are_per_tenant(tmp_path):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    tenants.enable(ADMIN, "user:1")
    tenants.enable(ADMIN, "user:2")
    store = RiskRuleStore(db)
    audit = AuditLog(db)
    store.upsert("user:1", "storm", "capture_storm", {"threshold": 2})
    seed_storm(audit, "user:1", n=2)  # user:1 rule fires
    seed_storm(audit, "user:2", n=2)  # user:2 has no rule
    bot = FakeBot()
    summary = asyncio.run(run_once(store, audit, tenants, bot))
    assert summary["rules_fired"] == 1
    assert summary["alert_ids"]  # non-empty
    assert 1 in bot.users  # user:1 got a DM
    assert "user:2" not in bot.users  # user:2 never gets a DM


def test_bad_tenant_and_empty_rules_yield_empty(tmp_path):
    db = str(tmp_path / "c.db")
    tenants = TenantStore(db, admins=[ADMIN])
    # enabled but no rules, no audit
    tenants.enable(ADMIN, "user:5")
    store = RiskRuleStore(db)
    audit = AuditLog(db)
    bot = FakeBot()
    summary = asyncio.run(run_once(store, audit, tenants, bot))
    assert summary["rules_evaluated"] == 0
    assert summary["rules_fired"] == 0
    assert summary["alert_ids"] == []
    assert summary["delivered"] == 0


def test_alerts_due_filters_by_tenant(tmp_path):
    tenants, store, audit, db = make(tmp_path)
    db2 = str(tmp_path / "c2.db")
    tenants2 = TenantStore(db2, admins=[ADMIN])
    tenants2.enable(ADMIN, "user:2")
    store2 = RiskRuleStore(db2)
    audit2 = AuditLog(db2)
    store.upsert("user:1", "storm", "capture_storm", {"threshold": 2})
    store2.upsert("user:2", "storm", "capture_storm", {"threshold": 2})
    seed_storm(audit, "user:1", n=2)
    seed_storm(audit2, "user:2", n=2)
    run_risk_pass(store, audit, "user:1")
    run_risk_pass(store2, audit2, "user:2")
    conn1 = sqlite3.connect(db)
    conn2 = sqlite3.connect(db2)
    assert len(concierge_alerts_due(conn1, "1")) == 1
    assert len(concierge_alerts_due(conn1, "2")) == 0  # cross-tenant isolation
    assert len(concierge_alerts_due(conn2, "2")) == 1

