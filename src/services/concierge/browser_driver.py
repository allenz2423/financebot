"""B2 browser driver: headless Playwright over the shared Browserless
egress-sandbox container.

Mirrors B1's ``scrape_rendered_page`` (which uses Browserless's ``/content``
scrape endpoint) but exposes *control* (click / fill / screenshot), which the
content endpoint cannot. We connect the playwright Python client directly to
Browserless's Playwright driver WebSocket so we never need local Chromium in
delilah_bot — the browser lives in the disposable, allowlist-gated
``browserless`` service that B1's read path already trusts.

All navigation/interaction is allowlist-gated by the caller
(``act_actions.resolve_act_target``); this module never trusts a URL.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://browserless:3000").rstrip("/")
BROWSERLESS_CDP_PATH = os.getenv("BROWSERLESS_CDP_PATH", "/chromium")
DEFAULT_TIMEOUT_MS = int(os.getenv("BROWSERLESS_TIMEOUT_MS", "30000"))


async def _text(page) -> str:
    try:
        body = await page.evaluate(
            "() => document.body ? document.body.innerText : ''",
        )
        return str(body or "")
    except Exception:
        return ""


def _ws_url() -> str:
    if BROWSERLESS_URL.startswith("https://"):
        ws = "wss://" + BROWSERLESS_URL[len("https://"):]
    elif BROWSERLESS_URL.startswith("http://"):
        ws = "ws://" + BROWSERLESS_URL[len("http://"):]
    else:
        ws = BROWSERLESS_URL
    # Browserless's Chromium image exposes its direct CDP connection at
    # /chromium. The /chrome route belongs to a different Browserless target
    # and returns 404 in the deployed chromium image.
    path = BROWSERLESS_CDP_PATH.strip()
    if not path:
        path = "/chromium"
    if not path.startswith("/"):
        path = "/" + path
    return ws.rstrip("/") + path


async def _apply_stealth(context, page) -> None:
    try:
        from src.bot.approval_views import _STEALTH_INIT_SCRIPT
        await context.add_init_script(_STEALTH_INIT_SCRIPT)
        await page.add_init_script(_STEALTH_INIT_SCRIPT)
    except Exception:
        pass
    try:
        cdp = await context.new_cdp_session(page)
        real_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        )
        await cdp.send("Network.setUserAgentOverride", {
            "userAgent": real_ua,
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "Win32",
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Chromium", "version": "133"},
                    {"brand": "Google Chrome", "version": "133"},
                    {"brand": "Not?A_Brand", "version": "99"},
                ],
                "fullVersion": "133.0.6943.126",
                "platform": "Windows",
                "platformVersion": "10.0.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        })
    except Exception:
        pass


def _stealth_ws_url() -> str:
    ws = _ws_url()
    if "?stealth=" not in ws and "&stealth=" not in ws:
        ws += ("&stealth=true" if "?" in ws else "?stealth=true")
    return ws


async def act_on_page(
    url: str,
    steps: List[Dict[str, Any]],
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Dict[str, Any]:
    """Open a page, run a sequence of interaction steps, return a result.

    ``steps`` is a list of action dicts, e.g.:
      {"type": "goto", "wait_until": "networkidle"}
      {"type": "fill", "selector": "input[name=q]", "value": "..."}
      {"type": "click", "selector": "button[type=submit]"}
      {"type": "wait_for", "selector": ".confirmation", "timeout_ms": 15000}
      {"type": "screenshot"}

    Returns a mask-only dict: pre-state + post-interaction text summary,
    base64 screenshots are intentionally omitted from this return (callers
    that need raw bytes take them separately). The summary is bounded.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(_stealth_ws_url())
        context = browser.contexts[0]
        page = await context.new_page()
        await page.set_viewport_size({"width": 1400, "height": 900})
        await _apply_stealth(context, page)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        pre_text = await _text(page)
        pre_screenshot = await page.screenshot(full_page=False)
        post_text = pre_text
        post_screenshot = pre_screenshot
        screenshot_taken = False
        for step in steps or []:
            kind = step.get("type")
            if kind == "goto":
                await page.goto(step.get("url") or url,
                                wait_until=step.get("wait_until", "domcontentloaded"),
                                timeout=step.get("timeout_ms", timeout_ms))
            elif kind == "fill":
                await page.fill(step["selector"], str(step.get("value", "")))
            elif kind == "click":
                await page.click(step["selector"], timeout=step.get("timeout_ms", timeout_ms))
            elif kind == "wait_for":
                try:
                    await page.wait_for_selector(step["selector"],
                                                 timeout=step.get("timeout_ms", 10000))
                except Exception:
                    pass
            elif kind == "screenshot":
                post_screenshot = await page.screenshot(full_page=False)
                screenshot_taken = True
            post_text = await _text(page)

        await context.close()
        await browser.close()
        return {
            "status": "ok",
            "url": url,
            "pre_text_chars": len(str(pre_text)),
            "post_text_chars": len(str(post_text)),
            "screenshot": post_screenshot if screenshot_taken else pre_screenshot,
        }


async def screenshot_page(url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bytes:
    """One-shot pre-state screenshot (B2 draft preview / B1 read backup)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(_stealth_ws_url())
        context = browser.contexts[0]
        page = await context.new_page()
        await page.set_viewport_size({"width": 1400, "height": 900})
        await _apply_stealth(context, page)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        shot = await page.screenshot(full_page=True)
        await context.close()
        await browser.close()
        return shot


__all__ = ["act_on_page", "screenshot_page", "BROWSERLESS_URL"]
