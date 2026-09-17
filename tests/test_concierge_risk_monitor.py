"""B1 risk-monitor skeleton tests: rule allowlist, audit-driven alerts."""

import pytest

from src.services.concierge.audit import AuditLog
from src.services.concierge.risk_monitor import (
    RULE_KINDS,
    RiskRuleStore,
    run_risk_pass,
    validate_risk_rule,
)


def make(tmp_path):
    db = str(tmp_path / "r.db")
    return RiskRuleStore(db), AuditLog(db)


def seed_captures(audit, tenant, label, n, action="capture_stored"):
    for i in range(n):
        audit.append(actor=tenant, action=action, tenant=tenant,
                     subject=label, detail={"label": label, "i": i})


def test_rule_kinds_expose_three():
    assert RULE_KINDS == {
        "capture_storm", "repeated_stored_secret", "out_of_allowlist_attempt",
    }


def test_validate_rule_kinds_and_defaults():
    ok, err, cfg = validate_risk_rule("capture_storm", {})
    assert ok and not err
    assert cfg["window_minutes"] == 10 and cfg["threshold"] == 5
    ok, _, cfg = validate_risk_rule("repeated_stored_secret", {})
    assert ok and cfg["threshold"] == 3
    ok, _, cfg = validate_risk_rule("out_of_allowlist_attempt", {})
    assert ok and cfg["threshold"] == 3
    ok, err, _ = validate_risk_rule("bogus_rule", {})
    assert not ok and "Unknown risk rule kind" in err
    ok, err, _ = validate_risk_rule("capture_storm", {"threshold": "x"})
    assert not ok and "integer" in err


def test_capture_storm_fires(tmp_path):
    store, audit = make(tmp_path)
    store.upsert("user:1", "storm", "capture_storm", {"window_minutes": 30, "threshold": 4})
    seed_captures(audit, "user:1", "Amex", 5)
    res = run_risk_pass(store, audit, "user:1")
    assert res["rules_evaluated"] == 1
    assert res["rules_fired"] == 1
    alerts = store.list_alerts("user:1")
    assert len(alerts) == 1
    assert alerts[0]["rule_kind"] == "capture_storm"
    assert "Capture storm" in alerts[0]["detail"]


def test_capture_storm_below_threshold_silent(tmp_path):
    store, audit = make(tmp_path)
    store.upsert("user:1", "storm", "capture_storm", {"window_minutes": 30, "threshold": 4})
    seed_captures(audit, "user:1", "Amex", 3)
    res = run_risk_pass(store, audit, "user:1")
    assert res["rules_fired"] == 0


def test_repeated_stored_secret_fires(tmp_path):
    store, audit = make(tmp_path)
    store.upsert("user:1", "repeat", "repeated_stored_secret",
                 {"window_minutes": 1440, "threshold": 3})
    seed_captures(audit, "user:1", "Brightspace login", 3)
    res = run_risk_pass(store, audit, "user:1")
    assert res["rules_fired"] == 1
    alerts = store.list_alerts("user:1")
    assert "Brightspace login x3" in alerts[0]["detail"]


def test_out_of_allowlist_fires_on_refusals(tmp_path):
    store, audit = make(tmp_path)
    store.upsert("user:1", "egress", "out_of_allowlist_attempt",
                 {"window_minutes": 60, "threshold": 3})
    for i in range(3):
        audit.append(actor="user:1", action="read_refused", tenant="user:1",
                     subject="order_status",
                     detail={"reason": "domain 'evil.org' is not allowed"})
    res = run_risk_pass(store, audit, "user:1")
    assert res["rules_fired"] == 1
    alerts = store.list_alerts("user:1")
    assert "out-of-allowlist" in alerts[0]["detail"]


def test_cooldown_suppresses_repeat_fire(tmp_path):
    store, audit = make(tmp_path)
    rule_id = store.upsert("user:1", "storm", "capture_storm",
                           {"window_minutes": 30, "threshold": 2})
    seed_captures(audit, "user:1", "Amex", 2)
    res1 = run_risk_pass(store, audit, "user:1")
    assert res1["rules_fired"] == 1
    seed_captures(audit, "user:1", "Amex", 2)
    res2 = run_risk_pass(store, audit, "user:1")
    assert res2["rules_fired"] == 0  # last_fired_at set; cooldown 6h default
    store.set_enabled("user:1", rule_id, False)
    seed_captures(audit, "user:1", "Amex", 2)
    res3 = run_risk_pass(store, audit, "user:1")
    assert res3["rules_fired"] == 0


def test_rules_are_per_tenant(tmp_path):
    store, audit = make(tmp_path)
    store.upsert("user:1", "storm", "capture_storm", {"threshold": 2})
    seed_captures(audit, "user:1", "Amex", 2)
    seed_captures(audit, "user:2", "Visa", 2)
    res1 = run_risk_pass(store, audit, "user:1")
    res2 = run_risk_pass(store, audit, "user:2")
    assert res1["rules_fired"] == 1
    assert res2["rules_fired"] == 0  # user:2 has no rules
    assert store.list_alerts("user:2") == []


def test_ack_alert(tmp_path):
    store, audit = make(tmp_path)
    store.upsert("user:1", "storm", "capture_storm", {"threshold": 2})
    seed_captures(audit, "user:1", "Amex", 2)
    res = run_risk_pass(store, audit, "user:1")
    alert_id = res["alert_ids"][0]
    assert store.ack_alert("user:1", alert_id)
    assert not store.ack_alert("user:2", alert_id)
    assert store.list_alerts("user:1")[0]["acked"] == 1