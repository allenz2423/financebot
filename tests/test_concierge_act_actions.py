"""B2 write-tier executor tests: gating, masking, auditing, error capture."""

import pytest

from src.services.concierge.audit import AuditLog
from src.services.concierge.act_actions import (
    ACT_KINDS,
    ActActionError,
    Actuator,
    _act_outcome,
    _extract_receipt,
    perform_act,
)
from src.services.concierge.read_actions import _PAN_LIKE

ALLOW = ["example.com", "amazon.com"]


class StubActuator(Actuator):
    """Deterministic recorder implementing the Actuator contract."""

    def __init__(self, text="", screenshot=b"\x89PNG-stub", visible=True):
        self.text = text
        self.screenshot_data = screenshot
        self.visible = visible
        self.navigate_calls = []
        self.click_calls = []
        self.type_calls = []
        self.screenshot_calls = 0
        self.get_text_calls = 0

    def navigate(self, url):
        self.navigate_calls.append(url)

    def click(self, selector):
        self.click_calls.append(selector)

    def type_text(self, selector, value):
        self.type_calls.append((selector, value))

    def screenshot(self):
        self.screenshot_calls += 1
        return self.screenshot_data

    def get_text(self):
        self.get_text_calls += 1
        return self.text

    def element_visible(self, selector):
        return self.visible


def _plan(domain="example.com"):
    return {
        "domain": domain,
        "steps": [{"action": "navigate", "sel": "https://example.com/login"}],
    }


def test_act_kind_allowlist():
    assert ACT_KINDS == frozenset({"send_email", "schedule_event", "fill_form"})


def test_perform_act_writes_act_audit_row_and_footprint(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="Login successful for order ORD-9", screenshot=b"\x89PNG-x")
    res = perform_act(
        "fill_form", _plan(), ALLOW, "user:1", act, audit=audit,
    )
    assert res["status"] == "ok"
    assert res["kind"] == "fill_form"
    assert res["url"] == "https://example.com"
    assert isinstance(res["text_hash16"], str) and len(res["text_hash16"]) == 16
    assert res["screenshot_png"] == b"\x89PNG-x"
    assert res["error"] is None
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_fill_form"
    assert rows[0]["tenant"] == "user:1"
    assert rows[0]["detail"]["url"] == "https://example.com"
    assert rows[0]["detail"]["ok"] is True
    assert rows[0]["detail"]["text_hash16"] == res["text_hash16"]


def test_perform_act_runs_step_verb_chain(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="done")
    plan = {
        "domain": "example.com",
        "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "click", "sel": "#login"},
            {"action": "type", "sel": "#user", "value": "alice"},
            {"action": "assert_text", "sel": "#welcome"},
            {"action": "screenshot"},
        ],
    }
    res = perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    assert res["status"] == "ok"
    assert act.click_calls == ["#login"]
    assert act.type_calls == [("#user", "alice")]
    assert act.screenshot_calls == 3  # initial + step screenshot + final post-screenshot


def test_perform_act_masks_pan_in_summary_and_step_values(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="Card 4111111111111111 charged for order ORD-1")
    plan = {
        "domain": "example.com",
        "steps": [{"action": "type", "sel": "#cc", "value": "4111111111111111"}],
    }
    res = perform_act("send_email", plan, ALLOW, "user:1", act, audit=audit)
    assert _PAN_LIKE.search(res["summary"]) is None
    masked_values = [s.get("value") for s in res["steps"]]
    assert "4111111111111111" not in masked_values


def test_perform_act_masks_secret_bearing_selector_values(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="ok")
    plan = {
        "domain": "example.com",
        "steps": [
            {"action": "type", "sel": "#password", "value": "hunter2"},
            {"action": "type", "sel": "#api_key", "value": "sk-1234567890"},
        ],
    }
    res = perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    by_sel = {s["sel"]: s["value"] for s in res["steps"]}
    assert by_sel["#password"] == "\u2022" * 4
    assert by_sel["#api_key"] == "\u2022" * 4


def test_perform_act_refusal_is_audited_and_raised(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="x")
    with pytest.raises(ActActionError, match="not allowed"):
        perform_act(
            "fill_form", _plan(domain="evil.example.org"), ALLOW, "user:1",
            act, audit=audit,
        )
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"
    assert rows[0]["subject"] == "fill_form"
    assert "not allowed" in rows[0]["detail"]["reason"]
    # actuator never drives the disallowed target
    assert act.navigate_calls == []


def test_perform_act_unknown_kind_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    with pytest.raises(ActActionError, match="write-tier only"):
        perform_act(
            "order_status", _plan(), ALLOW, "user:1", StubActuator(), audit=audit,
        )


def test_perform_act_missing_domain_arg_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    plan = {"steps": [{"action": "navigate", "sel": "https://example.com"}]}
    with pytest.raises(ActActionError, match="requires 'domain'"):
        perform_act("fill_form", plan, ALLOW, "user:1", StubActuator(), audit=audit)


def test_perform_act_empty_steps_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    with pytest.raises(ActActionError, match="non-empty 'steps'"):
        perform_act("fill_form", {"domain": "example.com", "steps": []}, ALLOW, "user:1", StubActuator(), audit=audit)


def test_perform_act_bad_step_action_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    plan = {"domain": "example.com", "steps": [{"action": "rm", "sel": "#x"}]}
    with pytest.raises(ActActionError, match="permitted act verb"):
        perform_act("fill_form", plan, ALLOW, "user:1", StubActuator(), audit=audit)


def test_perform_act_step_missing_selector_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    plan = {"domain": "example.com", "steps": [{"action": "click"}]}
    with pytest.raises(ActActionError, match="requires a 'sel'"):
        perform_act("fill_form", plan, ALLOW, "user:1", StubActuator(), audit=audit)


def test_perform_act_type_value_too_long_raises(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    plan = {"domain": "example.com", "steps": [{"action": "type", "sel": "#x", "value": "A" * 5000}]}
    with pytest.raises(ActActionError, match="exceeds"):
        perform_act("fill_form", plan, ALLOW, "user:1", StubActuator(), audit=audit)


def test_perform_act_step_exception_captured_not_propagated(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="partial")
    plan = {"domain": "example.com", "steps": [{"action": "navigate", "sel": "https://example.com/go"}]}
    original = act.click

    def explode(selector):
        raise RuntimeError("boom")

    act.click = explode  # exercise the error-capture path
    plan["steps"] = [{"action": "navigate", "sel": "https://example.com/go"}, {"action": "click", "sel": "#c"}]
    res = perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    assert res["status"] == "error"
    assert "RuntimeError" in res["error"]
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_fill_form"
    assert rows[0]["detail"]["ok"] is False
    assert "boom" in rows[0]["detail"]["error"]


def test_perform_act_assert_text_failure_captured_as_error(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="", visible=False)
    plan = {"domain": "example.com", "steps": [{"action": "assert_text", "sel": "#banner"}]}
    res = perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    assert res["status"] == "error"
    assert "assert_text failed" in res["error"]


def test_perform_act_screenshot_disabled(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="ok", screenshot=b"\x89PNG-x")
    res = perform_act("fill_form", _plan(), ALLOW, "user:1", act, audit=audit, include_screenshot=False)
    assert res["screenshot_png"] is None
    assert act.screenshot_calls == 0


def test_perform_act_summary_bounded_and_masked():
    act = StubActuator(text="word " * 5000 + " 4111111111111111")
    res = perform_act("fill_form", _plan(), ALLOW, "user:1", act)
    assert len(res["summary"]) <= 2000
    assert _PAN_LIKE.search(res["summary"]) is None


def test_extract_receipt_finds_confirmation_and_total():
    text = "Thank you! Order #ORD-9X confirmed. Your total: $45.67 has been charged."
    rec = _extract_receipt(text)
    assert rec["confirmation"] == "ORD-9X"
    assert rec["total"] == "45.67"


def test_extract_receipt_none_when_absent():
    assert _extract_receipt("Card processed. Thank you for shopping.") == {}


def test_extract_receipt_masks_pan_in_confirmation():
    text = "Order CONF-4111111111111111 confirmed. Total $12.00"
    rec = _extract_receipt(text)
    assert "confirmation" in rec
    assert _PAN_LIKE.search(rec["confirmation"]) is None
    assert rec["total"] == "12.00"


def test_perform_act_attaches_receipt_to_result_and_audit(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="Order #ORD-9 confirmed. Total: $19.99 charged.")
    res = perform_act("fill_form", _plan(), ALLOW, "user:1", act, audit=audit)
    assert res["receipt"]["confirmation"] == "ORD-9"
    assert res["receipt"]["total"] == "19.99"
    rows = audit.tail(limit=5)
    detail = rows[0]["detail"]
    assert detail["receipt"]["confirmation"] == "ORD-9"
    assert detail["receipt"]["total"] == "19.99"


def test_act_outcome_executed_for_clean_act():
    assert _act_outcome({"status": "ok"}) == "executed"


def test_act_outcome_rolled_back_for_failed_act():
    assert _act_outcome({"status": "error", "error": "boom"}) == "rolled_back"


# --- B2 write-tier hardening (security audit findings) ---


def test_perform_act_navigate_to_non_allowlisted_host_refuses_before_nav(tmp_path):
    """Step-level SSRF gate: a navigate step to a host outside the allowlist is
    refused (audited act_refused) and the actuator never drives the target."""
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="x")
    plan = {
        "domain": "example.com",
        "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "navigate", "sel": "https://169.254.169.254/latest/meta-data/"},  # link-local / non-allowlisted
        ],
    }
    with pytest.raises(ActActionError, match="not allowed"):
        perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"
    # the plan is gated up front — the actuator never drives the disallowed target
    assert act.navigate_calls == []


def test_perform_act_navigate_internal_host_fails_closed(tmp_path):
    """SSRF gate is checked at propose AND re-checked at execute: a plan whose
    navigate targets the internal metadata host is refused even when the plan's
    own 'domain' field was allowlisted."""
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="x")
    plan = {
        "domain": "example.com",
        "steps": [{"action": "navigate", "sel": "http://169.254.169.254/latest/meta-data/"}],
    }
    with pytest.raises(ActActionError, match="not allowed"):
        perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    assert act.navigate_calls == []
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_refused"


def test_perform_act_resolves_vault_ref_secret_into_type_value(tmp_path):
    """Executor-side secret injection: a type step carrying a vault_ref is
    resolved through the caller-provided resolver and the plaintext never
    leaves the resolver boundary (result steps stay masked)."""
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="done")
    plan = {
        "domain": "example.com",
        "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "type", "sel": "#user", "value": "alice"},
            {"action": "type", "sel": "#password", "value": "\u2022" * 4,
             "vault_ref": "vref-1"},
        ],
    }

    def _resolver(ref):
        assert ref == "vref-1"
        return "SuperSecret-Passw0rd"

    res = perform_act(
        "fill_form", plan, ALLOW, "user:1", act, audit=audit,
        resolve_secret=_resolver,
    )
    assert res["status"] == "ok"
    assert act.type_calls == [
        ("#user", "alice"), ("#password", "SuperSecret-Passw0rd"),
    ]
    by_sel = {s["sel"]: s["value"] for s in res["steps"] if s.get("action") == "type"}
    assert by_sel["#password"] == "\u2022" * 4
    rows = audit.tail(limit=5)
    assert "SuperSecret-Passw0rd" not in str(rows[0]["detail"])


def test_perform_act_secret_shaped_value_without_ref_fails_closed(tmp_path):
    """A secret-bearer field that reaches execution with a live value and no
    vault_ref must fail closed: the plaintext is never typed into the site."""
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="done")
    plan = {
        "domain": "example.com",
        "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "type", "sel": "#password", "value": "hunter2"},  # no vault_ref
        ],
    }
    res = perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    assert res["status"] == "error"
    assert "vault_ref" in res["error"]
    assert act.type_calls == []  # plaintext never left the executor boundary
    rows = audit.tail(limit=5)
    assert rows[0]["action"] == "act_fill_form"
    assert rows[0]["detail"]["ok"] is False


def test_perform_act_assert_text_value_mismatch_is_error(tmp_path):
    """assert_text semantics: element must be visible AND the expected value
    must appear in the page text; a mismatch is captured, not swallowed."""
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="Welcome back!", visible=True)
    plan = {
        "domain": "example.com",
        "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "assert_text", "sel": "#banner", "value": "Unauthorized"},
        ],
    }
    res = perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    assert res["status"] == "error"
    assert "expected" in res["error"]
    rows = audit.tail(limit=5)
    assert rows[0]["detail"]["ok"] is False


def test_perform_act_assert_text_value_present_passes(tmp_path):
    """assert_text passes only when the element is visible AND the expected
    value is present in the page text (not just element existence)."""
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="Welcome back, alice!", visible=True)
    plan = {
        "domain": "example.com",
        "steps": [
            {"action": "navigate", "sel": "https://example.com/login"},
            {"action": "assert_text", "sel": "#banner", "value": "Welcome back"},
        ],
    }
    res = perform_act("fill_form", plan, ALLOW, "user:1", act, audit=audit)
    assert res["status"] == "ok"


def test_perform_act_receipt_masks_token_like_values(tmp_path):
    """Receipt extraction must mask token-like secrets (sk-/ghp_/glpat...)."""
    audit = AuditLog(str(tmp_path / "a.db"))
    act = StubActuator(text="Invoice ref: sk-1234567890abcdef confirmed. Total: $9.99")
    res = perform_act("fill_form", _plan(), ALLOW, "user:1", act, audit=audit)
    rec = res.get("receipt") or {}
    for v in rec.values():
        assert "sk-1234567890abcdef" not in str(v)
    rows = audit.tail(limit=5)
    assert "sk-1234567890abcdef" not in str(rows[0]["detail"])
