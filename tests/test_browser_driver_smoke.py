"""B2 browser_driver smoke tests: WS URL construction + import guard.

These tests don't spin a real Chromium; they validate the pure-Python
helpers that ``act_on_page`` / ``screenshot_page`` depend on, so a
Browserless/Playwright wiring mistake fails fast in CI.
"""

import importlib
import os

import pytest

from src.services.concierge import browser_driver
from src.bot.approval_views import (
    _normalize_mission_navigation_url,
    browser_auth_challenge,
)


def test_ws_url_https_upgrade():
    browser_driver.BROWSERLESS_URL = "https://browserless:3000"
    assert browser_driver._ws_url() == "wss://browserless:3000/chromium"


def test_ws_url_http_upgrade():
    browser_driver.BROWSERLESS_URL = "http://browserless:3000/"
    assert browser_driver._ws_url() == "ws://browserless:3000/chromium"


def test_ws_url_preserves_existing_scheme():
    browser_driver.BROWSERLESS_URL = "wss://existing-host:443/edge"
    assert browser_driver._ws_url() == "wss://existing-host:443/edge/chromium"


def test_ws_url_supports_configured_path(monkeypatch):
    monkeypatch.setattr(browser_driver, "BROWSERLESS_URL", "http://browserless:3000")
    monkeypatch.setattr(browser_driver, "BROWSERLESS_CDP_PATH", "chromium")
    assert browser_driver._ws_url() == "ws://browserless:3000/chromium"


def test_default_timeout_env():
    # DEFAULT_TIMEOUT_MS is read at import time; just sanity-check it's int.
    assert isinstance(browser_driver.DEFAULT_TIMEOUT_MS, int)
    assert browser_driver.DEFAULT_TIMEOUT_MS > 0


def test_playwright_import_guard():
    """The playwright async_api module must be importable (it's in requirements)."""
    importlib.import_module("playwright.async_api")


def test_browser_auth_challenge_detects_otp_wall():
    assert browser_auth_challenge(
        "We're unable to send an SMS message. Verify using WhatsApp instead."
    )


def test_browser_auth_challenge_ignores_normal_page():
    assert not browser_auth_challenge("Welcome back. Your orders are listed here.")


def test_agentic_navigation_normalizes_path_and_bare_host(monkeypatch):
    monkeypatch.setattr(
        "src.bot.approval_views.validate_target_url",
        lambda url, allowed: url,
    )
    assert _normalize_mission_navigation_url(
        "/signin", "https://example.com", "example.com"
    ) == "https://example.com/signin"
    assert _normalize_mission_navigation_url(
        "example.com/signin", "https://example.com", "example.com"
    ) == "https://example.com/signin"
    assert _normalize_mission_navigation_url(
        "signin", "https://example.com", "example.com"
    ) == "https://example.com/signin"


def test_agentic_navigation_rejects_cross_host(monkeypatch):
    monkeypatch.setattr(
        "src.bot.approval_views.validate_target_url",
        lambda url, allowed: (_ for _ in ()).throw(ValueError("not allowed")),
    )
    with pytest.raises(ValueError, match="not allowed"):
        _normalize_mission_navigation_url(
            "https://evil.example", "https://example.com", "example.com"
        )


@pytest.fixture(autouse=True)
def restore_env():
    original = browser_driver.BROWSERLESS_URL
    original_timeout = os.environ.get("BROWSERLESS_TIMEOUT_MS")
    yield
    browser_driver.BROWSERLESS_URL = original
    if original_timeout is None:
        os.environ.pop("BROWSERLESS_TIMEOUT_MS", None)
    else:
        os.environ["BROWSERLESS_TIMEOUT_MS"] = original_timeout
