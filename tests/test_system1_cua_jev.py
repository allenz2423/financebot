"""Tests for System 1 fast form scoring and autonomous action agent (CUA-S1-FORMS + Jev)."""

import os
import time
import pytest
from unittest.mock import MagicMock, patch

from src.services.concierge.system1_agent import (
    CuaS1Scorer,
    JevSystem1Client,
    System1FormAgent,
    _encode_bytes,
)


def test_encode_bytes():
    text = "hello world"
    ids, mask = _encode_bytes(text, 16)
    assert len(ids) == 16
    assert len(mask) == 16
    assert ids[0] == ord("h") + 1
    assert bool(mask[0]) is True
    # Padding bytes should be 0 and mask False
    assert ids[len(text)] == 0
    assert bool(mask[len(text)]) is False


def test_cua_s1_scorer_inference():
    scorer = CuaS1Scorer()
    assert scorer.is_available, "CUA-S1-FORMS ONNX model should be available in data/models/cua-s1"

    # Evaluate an email element
    email_elem = {
        "tag": "input",
        "type": "email",
        "label": "Email or mobile number",
        "name": "login_email",
        "placeholder": "Enter your email",
        "value": "",
    }
    t0 = time.perf_counter()
    pred_email = scorer.evaluate_element(
        email_elem,
        vault_labels=["email", "password"],
        form_title="Sign in to your account",
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    print(f"CUA-S1 Email Inference Latency: {latency_ms:.2f}ms")

    assert pred_email["action"] in {"fill", "click", "check", "skip"}
    assert "confidence" in pred_email
    assert 0.0 <= pred_email["confidence"] <= 1.0
    # Latency should be sub-50ms on modern CPU
    assert latency_ms < 100.0

    # Evaluate a submit button
    btn_elem = {
        "tag": "button",
        "type": "submit",
        "label": "Sign In",
        "name": "submit_btn",
        "value": "",
    }
    pred_btn = scorer.evaluate_element(
        btn_elem,
        vault_labels=["email", "password"],
        form_title="Sign in to your account",
    )
    assert pred_btn["action"] in {"click", "skip", "fill"}


def test_jev_client_graceful_fallback():
    # Without TYPESAFE_API_KEY, client reports unavailable and falls back cleanly
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
        client = JevSystem1Client()
        assert not client.is_available
        assert client.choose_target("log in", [{"handle": "e1"}]) is None
        assert client.is_auth_challenge("Enter verification code sent to phone") is None


def test_system1_form_agent_planning():
    mock_actuator = MagicMock()
    mock_vault = MagicMock()
    # Mock records in Vault
    mock_vault.list_records.return_value = [
        {
            "kind": "secret",
            "vault_ref": "s_123456",
            "consumer_scope": ["example.com"],
        }
    ]
    mock_vault.resolve.return_value = b'{"email|login": "user@example.com", "password|value": "SecretPass123"}'

    agent = System1FormAgent(
        cdp_actuator=mock_actuator,
        tenant="user:123",
        domain="example.com",
        vault=mock_vault,
    )

    vault_fields = agent._resolve_vault_fields()
    assert "email" in vault_fields or "login" in vault_fields
    assert "password" in vault_fields
    assert vault_fields["password"] == "SecretPass123"

    # Plan for username/email input
    email_ctrl = {
        "handle": "e1",
        "tag": "input",
        "type": "text",
        "label": "Username or Email",
        "name": "username",
        "secret": False,
        "otp": False,
    }
    plan_email = agent.plan_element_action(email_ctrl, vault_fields, form_title="Example Login")
    assert plan_email["action"] == "type"
    assert plan_email["handle"] == "e1"
    assert "example.com" in plan_email["value"]

    # Plan for password input
    pw_ctrl = {
        "handle": "e2",
        "tag": "input",
        "type": "password",
        "label": "Password",
        "name": "passwd",
        "secret": True,
        "otp": False,
    }
    plan_pw = agent.plan_element_action(pw_ctrl, vault_fields, form_title="Example Login")
    assert plan_pw["action"] == "type"
    assert plan_pw["handle"] == "e2"
    assert plan_pw["value"] == "SecretPass123"

    # Plan for OTP input
    otp_ctrl = {
        "handle": "e3",
        "tag": "input",
        "type": "text",
        "label": "Enter 6-digit OTP code",
        "name": "otp",
        "secret": False,
        "otp": True,
    }
    plan_otp = agent.plan_element_action(otp_ctrl, vault_fields, form_title="Example Login")
    assert plan_otp["action"] == "otp_challenge"

    # Plan for submit button
    btn_ctrl = {
        "handle": "e4",
        "tag": "button",
        "type": "submit",
        "label": "Log In",
        "name": "submit",
        "secret": False,
        "otp": False,
    }
    plan_btn = agent.plan_element_action(btn_ctrl, vault_fields, form_title="Example Login")
    assert plan_btn["action"] == "click"
    assert plan_btn["handle"] == "e4"


def test_system1_form_agent_autonomous_execution():
    # Simulate a full login form sequence
    mock_actuator = MagicMock()
    mock_actuator._handles = {
        "e1": {"handle": "e1", "tag": "input", "type": "text", "label": "Email", "name": "email", "secret": False, "otp": False},
        "e2": {"handle": "e2", "tag": "input", "type": "password", "label": "Password", "name": "pwd", "secret": True, "otp": False},
        "e3": {"handle": "e3", "tag": "button", "type": "submit", "label": "Sign In", "name": "login", "secret": False, "otp": False},
    }
    mock_actuator.interactive_summary.return_value = "e1: Email\ne2: Password\ne3: Sign In"
    mock_actuator.page_stage.return_value = "login"
    mock_actuator.get_text.return_value = "Please sign in to your dashboard"

    mock_vault = MagicMock()
    mock_vault.list_records.return_value = [
        {"kind": "secret", "vault_ref": "s_abc", "consumer_scope": ["portal.io"]}
    ]
    mock_vault.resolve.return_value = b'{"email|value": "alice@portal.io", "password|value": "mypass123"}'

    # Simulate after clicking submit, form clears / navigates
    def fake_click(handle):
        if handle == "e3":
            mock_actuator._handles = {}
            mock_actuator.page_stage.return_value = "complete"
            mock_actuator.get_text.return_value = "Welcome back, Alice! Dashboard Overview."

    mock_actuator.click.side_effect = fake_click

    agent = System1FormAgent(
        cdp_actuator=mock_actuator,
        tenant="user:456",
        domain="portal.io",
        vault=mock_vault,
    )

    result = agent.run_autonomous_step(max_steps=5)

    assert result["status"] == "ok"
    assert len(result["actions_executed"]) >= 2
    # Verify both type and click actions were dispatched to actuator
    assert mock_actuator.type_text.call_count >= 1
    assert mock_actuator.click.call_count >= 1
    assert result["latency_ms"] < 2000.0
    print(f"Full System 1 Autonomous Flow Latency: {result['latency_ms']}ms")


def test_system1_otp_blocking():
    # If an OTP field is on screen, System 1 halts and signals blocked stage
    mock_actuator = MagicMock()
    mock_actuator._handles = {
        "e1": {"handle": "e1", "tag": "input", "type": "text", "label": "Verification Code", "name": "otp", "secret": False, "otp": True},
    }
    mock_actuator.interactive_summary.return_value = "e1: Verification Code"
    mock_actuator.page_stage.return_value = "otp"
    mock_actuator.get_text.return_value = "Enter the 6-digit security code sent via SMS"

    mock_vault = MagicMock()
    mock_vault.list_records.return_value = []

    agent = System1FormAgent(
        cdp_actuator=mock_actuator,
        tenant="user:789",
        domain="secure.bank.com",
        vault=mock_vault,
    )

    result = agent.run_autonomous_step(max_steps=3)
    assert result["status"] == "blocked"
    assert result["stage"] == "otp"
    assert "OTP" in result["block_reason"] or "passcode" in result["block_reason"]
    # No blind clicks should occur
    assert mock_actuator.click.call_count == 0


def test_agentic_browser_step_auto_action():
    from src.bot.approval_views import AGENTIC_BROWSER_SESSIONS, agentic_browser_step

    mock_actuator = MagicMock()
    mock_actuator.interactive_summary.return_value = "e1: Email\ne2: Password\ne3: Log In"
    mock_actuator.get_text.return_value = "Sign in to App"
    mock_actuator.page_stage.return_value = "login"
    mock_actuator._handles = {
        "e1": {"handle": "e1", "tag": "input", "type": "email", "label": "Email", "secret": False, "otp": False},
        "e2": {"handle": "e2", "tag": "input", "type": "password", "label": "Password", "secret": True, "otp": False},
        "e3": {"handle": "e3", "tag": "button", "type": "submit", "label": "Log In", "secret": False, "otp": False},
    }
    mock_actuator.screenshot.return_value = None
    mock_actuator.storage_state.return_value = b'{"cookies": []}'
    mock_actuator.operation_lock = MagicMock()
    mock_actuator.operation_lock.__enter__ = MagicMock()
    mock_actuator.operation_lock.__exit__ = MagicMock()

    # Register in AGENTIC_BROWSER_SESSIONS
    mission_id = "test-mission-s1"
    AGENTIC_BROWSER_SESSIONS[mission_id] = mock_actuator

    mock_mission = {
        "mission_id": mission_id,
        "tenant": "user:101",
        "domain": "portal.io",
        "url": "https://portal.io/login",
        "kind": "fill_form",
        "state_vault_ref": None,
    }

    with patch("src.bot.approval_views.ApprovalStore") as MockStore, \
         patch("src.bot.approval_views.Vault") as MockVault:
        store_inst = MockStore.return_value
        store_inst.active_missions_for_tenant.return_value = [mock_mission]

        vault_inst = MockVault.return_value
        vault_inst.list_records.return_value = [
            {"kind": "secret", "vault_ref": "s_portal", "consumer_scope": ["portal.io"]}
        ]
        vault_inst.resolve.return_value = b'{"email|login": "user@portal.io", "password|val": "pass123"}'
        vault_inst.store_browser_state.return_value = {"vault_ref": "st_999"}

        res = agentic_browser_step(
            tenant="user:101",
            mission_id=mission_id,
            action="auto",
        )

        assert res["mission_id"] == mission_id
        assert res["action"] == "auto"
        assert "s1_result" in res
        assert res["s1_result"]["status"] == "ok"
        assert "[SYSTEM 1 AUTONOMOUS FORM RESULT]" in res["summary"]

