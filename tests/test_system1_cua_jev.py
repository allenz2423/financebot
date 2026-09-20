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


@pytest.mark.asyncio
async def test_cua_agent_shopping_turn_loop():
    from unittest.mock import MagicMock
    from src.services.concierge.system1_agent import CUAAgent

    mock_actuator = MagicMock()
    # Mock initial interactive summary with search input
    mock_actuator._handles = {
        "e1": {"handle": "e1", "tag": "input", "type": "search", "label": "Search Amazon", "name": "field-keywords"},
        "e2": {"handle": "e2", "tag": "button", "type": "submit", "label": "Go", "name": "submit"},
    }
    mock_actuator.interactive_summary.return_value = "e1: [input:search] 'Search Amazon'\ne2: [button:submit] 'Go'"
    mock_actuator.get_text.return_value = "Amazon.com. Spend less. Smile more."

    def fake_type(handle, val):
        if handle == "e1":
            # Search submitted, transition to search results page
            mock_actuator._handles = {
                "e3": {"handle": "e3", "tag": "a", "type": "link", "label": "Rotring 600 Mechanical Pencil Mint 0.5mm"},
                "e4": {"handle": "e4", "tag": "a", "type": "link", "label": "Rotring 800 Silver Pencil"},
            }
            mock_actuator.interactive_summary.return_value = (
                "e3: [a:link] 'Rotring 600 Mechanical Pencil Mint 0.5mm'\n"
                "e4: [a:link] 'Rotring 800 Silver Pencil'"
            )
            mock_actuator.get_text.return_value = "Results for rotring 600 mint"

    def fake_click(handle):
        if handle == "e3":
            # Product details page with Add to Cart button
            mock_actuator._handles = {
                "e5": {"handle": "e5", "tag": "button", "type": "submit", "label": "Add to Cart"},
            }
            mock_actuator.interactive_summary.return_value = "e5: [button:submit] 'Add to Cart'"
            mock_actuator.get_text.return_value = "Rotring 600 Mint in stock. Add to Cart."
        elif handle == "e5":
            # Item added to cart
            mock_actuator._handles = {}
            mock_actuator.interactive_summary.return_value = ""
            mock_actuator.get_text.return_value = "Added to Cart. Cart subtotal: $37.33"

    mock_actuator.type_text.side_effect = fake_type
    mock_actuator.click.side_effect = fake_click
    mock_actuator.screenshot.return_value = None

    cua = CUAAgent(cdp_actuator=mock_actuator)
    # Test parse goal
    parsed = cua.parse_goal("add a rotring 600 in a nice blue adjacent color preferably mint to my cart")
    assert "rotring 600" in parsed["search_query"]
    assert "mint" in parsed["preferred_colors"]

    # Test autonomous turn loop
    res = await cua.run_goal_async("add a rotring 600 in a nice blue adjacent color preferably mint to my cart", max_turns=5)
    assert res["status"] == "completed"
    assert len(res["actions_executed"]) >= 2
    assert any(a["action"] == "type" for a in res["actions_executed"])
    assert any(a["action"] == "click" for a in res["actions_executed"])


@pytest.mark.asyncio
async def test_cua_agent_banking_turn_loop():
    """Verify CUA functions identically on banking portals without any shopping assumptions."""
    from unittest.mock import MagicMock
    from src.services.concierge.system1_agent import CUAAgent

    mock_actuator = MagicMock()
    mock_actuator._handles = {
        "e1": {"handle": "e1", "tag": "a", "type": "link", "label": "360 Checking ...1234 - $4,520.10"},
        "e2": {"handle": "e2", "tag": "button", "type": "button", "label": "Transfer Money"},
    }
    mock_actuator.interactive_summary.return_value = "e1: [a:link] '360 Checking ...1234'\ne2: [button] 'Transfer Money'"
    mock_actuator.get_text.return_value = "Capital One Accounts Dashboard. Welcome Alice."

    def fake_click(handle):
        if handle == "e2":
            mock_actuator._handles = {
                "e3": {"handle": "e3", "tag": "input", "type": "text", "label": "Amount", "name": "amount"},
                "e4": {"handle": "e4", "tag": "button", "type": "submit", "label": "Confirm Transfer"},
            }
            mock_actuator.interactive_summary.return_value = "e3: [input:text] 'Amount'\ne4: [button:submit] 'Confirm Transfer'"
            mock_actuator.get_text.return_value = "Transfer Funds form"

    mock_actuator.click.side_effect = fake_click
    mock_actuator.screenshot.return_value = None

    cua = CUAAgent(cdp_actuator=mock_actuator)
    res = await cua.run_goal_async("Transfer Money", max_turns=3)
    assert res["status"] == "completed"
    assert any(a["action"] == "click" and a["target"] == "e2" for a in res["actions_executed"])


@pytest.mark.asyncio
async def test_cua_agent_challenge_blocking():
    """Verify CUA halts cleanly when an OTP or security challenge is presented."""
    from unittest.mock import MagicMock
    from src.services.concierge.system1_agent import CUAAgent

    mock_actuator = MagicMock()
    mock_actuator._handles = {
        "e1": {"handle": "e1", "tag": "input", "type": "text", "label": "Enter security code sent via SMS", "name": "otp"},
    }
    mock_actuator.interactive_summary.return_value = "e1: [input:text] 'Enter security code sent via SMS'"
    mock_actuator.get_text.return_value = "Two-step verification. Enter the verification code sent to your phone."

    cua = CUAAgent(cdp_actuator=mock_actuator)
    res = await cua.run_goal_async("Check recent transactions", max_turns=3)
    assert res["status"] == "blocked"
    assert res["stage"] == "challenge"
    assert "challenge" in res["block_reason"].lower() or "verification" in res["block_reason"].lower()
    assert len(res["actions_executed"]) == 0



