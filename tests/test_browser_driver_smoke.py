"""B2 browser_driver smoke tests: WS URL construction + import guard.

These tests don't spin a real Chromium; they validate the pure-Python
helpers that ``act_on_page`` / ``screenshot_page`` depend on, so a
Browserless/Playwright wiring mistake fails fast in CI.
"""

import importlib
import os

import pytest

from src.services.concierge import browser_driver


def test_ws_url_https_upgrade():
    browser_driver.BROWSERLESS_URL = "https://browserless:3000"
    assert browser_driver._ws_url() == "wss://browserless:3000/playwright"


def test_ws_url_http_upgrade():
    browser_driver.BROWSERLESS_URL = "http://browserless:3000/"
    assert browser_driver._ws_url() == "wss://browserless:3000/playwright"


def test_ws_url_preserves_existing_scheme():
    browser_driver.BROWSERLESS_URL = "wss://existing-host:443/edge"
    assert browser_driver._ws_url() == "wss://existing-host:443/edge/playwright"


def test_default_timeout_env():
    # DEFAULT_TIMEOUT_MS is read at import time; just sanity-check it's int.
    assert isinstance(browser_driver.DEFAULT_TIMEOUT_MS, int)
    assert browser_driver.DEFAULT_TIMEOUT_MS > 0


def test_playwright_import_guard():
    """The playwright async_api module must be importable (it's in requirements)."""
    importlib.import_module("playwright.async_api")


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
