"""B2 Discord approval view for concierge write-tier actions.

Reuses the ``LoginSessionView`` owner-check + timeout + cleanup pattern from
``src.bot.concierge_login``. On ✅ the proposal is executed via the
Browserless headless browser (act_actions.perform_act + BrowserlessActuator),
then self-verified via VLM (vlm_verify.verify_act). On ✕ it is rejected +
audited. The Discord message is edited to remove buttons on completion.
"""

from __future__ import annotations

import asyncio
import base64 as b64
import io
import json
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import discord

from src.services.concierge.act_actions import Actuator, perform_act, _act_outcome
from src.services.concierge.approval_store import ApprovalStore
from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import validate_target_url
from src.services.concierge.tenants import TenantStore
from src.services.concierge.vlm_verify import verify_act
from src.services.concierge.browser_driver import DEFAULT_TIMEOUT_MS
from src.services.concierge.browser_sessions import BrowserSessionStore
from src.security.vault import DEFAULT_DB_PATH as _DB, Vault

# Approved browser missions stay attached to a live page.  The model receives
# observations and chooses the next interaction instead of submitting a
# guessed multi-step selector script up front.
AGENTIC_BROWSER_SESSIONS: Dict[str, "BrowserlessActuator"] = {}

_AUTH_CHALLENGE_MARKERS = (
    "unable to send an sms",
    "verify using whatsapp",
    "one-time code",
    "one time code",
    "mobile verification",
    "enter the code",
    "security check",
    "captcha",
    "verify you're a human",
    "verify you are a human",
    "verify that you are human",
    "confirm you're human",
    "confirm you are human",
    "confirm that you are human",
    "i'm not a robot",
    "prove you're human",
    "are you a robot",
    "visual verification",
    "press and hold",
    "checking your browser",
    "just a moment",
    "unusual traffic",
    "datadome",
    "px-captcha",
    "enable javascript and cookies to continue",
)

# Frame URLs that merely embed an *invisible* risk-scoring widget. reCAPTCHA
# Enterprise v3 runs on ordinary pages (PayPal loads it even on the logged-in
# home page), and its URL contains the substring "captcha" — so a bare URL
# match falsely reported a human-verification wall. A real challenge is caught
# by its visible text ("I'm not a robot", "Confirm you're human", …); these
# fragments only suppress the URL-only false positive.
_BENIGN_CHALLENGE_URL_FRAGMENTS = (
    "recaptcha/enterprise",
    "recaptcha.net/recaptcha",
    "google.com/recaptcha",
    "gstatic.com/recaptcha",
    "grcenterprise",
    "recaptcha_v3",
)

# Consent/interstitial labels the actuator may click on its own to clear an
# overlay. Deliberately excludes generic "continue"/"submit"/"next" so this
# can never advance a form or submit data by accident.
_CONSENT_LABELS = (
    "accept all",
    "accept all cookies",
    "accept cookies",
    "accept",
    "allow all",
    "allow all cookies",
    "allow cookies",
    "allow",
    "i agree",
    "agree",
    "got it",
    "ok",
    "okay",
    "close",
    "reject all",
    "decline",
    "continue to site",
    "confirm my choices",
    "dismiss",
)


# Text the invisible reCAPTCHA Enterprise v3 widget injects into the page
# itself. The widget registers as the JS namespace ``recaptcha.<frame>.Main.init``
# and renders the "protected by reCAPTCHA" badge; frame-aware ``get_text``
# lifts both into the summary. Both contain the substring "captcha" and so
# tripped the challenge detector on ordinary, unauthenticated pages (PayPal's
# logged-in home page carries the widget). These fragments are pure widget
# scaffolding — never a wall — while a genuine challenge keeps its own wording
# ("I'm not a robot", "Verify you're a human", DataDome, px-captcha).
_RECAPTCHA_WIDGET_NOISE_RE = re.compile(
    r"recaptcha\.(?:\w+\.)*main\.init\s*\(.{0,4000}?\"\)\s*;?"
    r"|protected by\s+recaptcha"
    r"|\[embedded frame\s*\]",
    re.IGNORECASE | re.DOTALL,
)

# Frames that only host an *invisible* risk-scoring widget (reCAPTCHA Enterprise
# v3, or a `size=invisible` anchor). They render nothing the user can act on, so
# frame-aware text extraction skips them entirely rather than feeding their
# bootstrap script into the model's observation. A *visible* reCAPTCHA checkbox
# (``api2/anchor``, no ``size=invisible``) is deliberately not listed here so a
# real, user-solvable challenge still surfaces.
_INVISIBLE_CHALLENGE_FRAME_FRAGMENTS = (
    "recaptcha/enterprise",
    "grcenterprise",
    "size=invisible",
)


def _is_invisible_challenge_frame(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(frag in lowered for frag in _INVISIBLE_CHALLENGE_FRAME_FRAGMENTS)


def _without_benign_challenge_urls(blob: str) -> str:
    """Drop tokens that are benign invisible-reCAPTCHA widget URLs.

    The frame-aware ``get_text`` embeds child-frame URLs *inside* the summary
    text (``[EMBEDDED FRAME <url>]``), so a URL that merely hosts an invisible
    reCAPTCHA Enterprise v3 widget contains the substring "captcha" and would
    otherwise be read as a challenge.
    """
    return " ".join(
        piece for piece in str(blob or "").split()
        if not any(frag in piece.lower() for frag in _BENIGN_CHALLENGE_URL_FRAGMENTS)
    )


def _strip_recaptcha_widget_noise(blob: str) -> str:
    """Remove invisible-reCAPTCHA widget scaffolding from extracted text."""
    return _RECAPTCHA_WIDGET_NOISE_RE.sub(" ", str(blob or ""))


def _without_recaptcha_product_name(blob: str) -> str:
    """Drop whitespace tokens that name Google's reCAPTCHA product.

    Ordinary pages carry the invisible reCAPTCHA Enterprise v3 widget, which
    injects the JS namespace ``recaptcha.<frame>.Main.init(...)`` and renders a
    "protected by reCAPTCHA" badge. Both contain the challenge-marker substring
    "captcha", so pages that merely *host* the widget were misread as a
    verification wall. A genuine challenge keeps its own wording ("I'm not a
    robot", "Verify you're a human", DataDome, px-captcha), and a real
    "complete the captcha" instruction (no "re" prefix) still matches — so this
    cannot mask a human-solvable wall.
    """
    return " ".join(
        piece for piece in str(blob or "").split()
        if "recaptcha" not in piece.lower()
    )


def browser_auth_challenge(text: str, url: str = "") -> bool:
    """Return whether the page requires user-owned authentication input."""
    haystack = _without_recaptcha_product_name(
        _without_benign_challenge_urls(text)
    ).lower()
    if any(marker in haystack for marker in _AUTH_CHALLENGE_MARKERS):
        return True
    clean_url = _without_recaptcha_product_name(
        _without_benign_challenge_urls(url)
    ).lower()
    return any(marker in clean_url for marker in _AUTH_CHALLENGE_MARKERS)


_STEALTH_INIT_SCRIPT = """
(() => {
    // 1. Mask navigator.webdriver
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
            configurable: true,
        });
        delete Object.getPrototypeOf(navigator).webdriver;
    } catch (e) {}

    // 2. Standardize userAgent and appVersion by removing HeadlessChrome
    try {
        const ua = navigator.userAgent.replace(/HeadlessChrome/gi, 'Chrome');
        Object.defineProperty(navigator, 'userAgent', {
            get: () => ua,
            configurable: true,
        });
        const appVer = navigator.appVersion.replace(/HeadlessChrome/gi, 'Chrome');
        Object.defineProperty(navigator, 'appVersion', {
            get: () => appVer,
            configurable: true,
        });
    } catch (e) {}

    // 3. Ensure window.chrome exists
    try {
        if (!window.chrome) {
            window.chrome = {};
        }
        if (!window.chrome.runtime) {
            window.chrome.runtime = {};
        }
    } catch (e) {}

    // 4. Standardize navigator.plugins
    try {
        if (!navigator.plugins || navigator.plugins.length === 0) {
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
                configurable: true,
            });
        }
    } catch (e) {}

    // 5. Standardize languages
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
            configurable: true,
        });
    } catch (e) {}
})();
"""


# Observation hands the model a numbered handle per visible control (e1, e2,
# …) and tags the live element with this attribute, so an action can name an
# element exactly instead of inventing a CSS selector and guessing the order
# of controls. The tag lives only in the page DOM (never persisted) and is
# re-applied on every observation.
_HANDLE_ATTR = "data-concierge-handle"
_HANDLE_RE = re.compile(r"^\s*e(\d+)\s*$")


def _format_control(item: Dict[str, Any]) -> str:
    """One inventory line: ``e3 <button> "Sign in" selector=...``."""
    tag = item.get("tag") or "el"
    typ = item.get("type") or ""
    head = f"{item.get('handle')} <{tag}"
    if typ and tag in {"input", "button"}:
        head += f" type={typ}"
    head += ">"
    parts = [head]
    if item.get("label"):
        parts.append(json.dumps(item["label"]))
    if item.get("name"):
        parts.append("name=" + json.dumps(item["name"]))
    if item.get("otp"):
        parts.append("ONE-TIME CODE: type the code you retrieved as value (do NOT type an empty value)")
    elif item.get("secret"):
        parts.append("SECRET: type with an empty value")
    parts.append("selector=" + json.dumps(item.get("selector") or ""))
    return " ".join(parts)


def _format_inventory(items: List[Dict[str, Any]]) -> str:
    """Render the observed controls grouped by owning form, then the rest."""
    if not items:
        return ""
    lines = [
        "[INTERACTIVE ELEMENTS] Every control the bot can act on, each with a "
        "stable id. Act on one by its id: click with selector=\"eN\", or type "
        "into it with selector=\"eN\" and (for a credential field) an empty "
        "value. Prefer the id over writing a CSS selector.",
    ]
    forms: Dict[Any, List[Dict[str, Any]]] = {}
    order: List[Any] = []
    others: List[Dict[str, Any]] = []
    for item in items:
        if item.get("form_action") or item.get("form"):
            key = (item.get("_frame"), item.get("form_action"), item.get("form"))
            if key not in forms:
                forms[key] = []
                order.append(key)
            forms[key].append(item)
        else:
            others.append(item)
    for key in order:
        action = key[1] or ""
        lines.append("[FORM" + (f" action={action}" if action else "") + "]")
        for item in forms[key]:
            lines.append("  " + _format_control(item))
    if others:
        lines.append("[OTHER CONTROLS]")
        for item in others:
            lines.append("  " + _format_control(item))
    return "\n".join(lines)


class BrowserlessActuator(Actuator):
    """Sync Actuator backed by a Browserless/Playwright WS session.

    Playwright is async; each method drives a single page via a dedicated
    event loop so the sync ``perform_act`` contract is preserved.

    SSRF hardening: the actuator carries the tenant allowlist and RE-VALIDATES
    the live page URL (allowlist + DNS-public, via ``validate_target_url``)
    after every navigation and before every interaction, including responses
    to HTTP redirects and JS-driven main-frame navigations (framenavigated).
    A page that leaves the gated scope fails closed mid-act.
    """

    def __init__(self, url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 allowed_domains: Optional[List[str]] = None,
                 cdp_url: Optional[str] = None):
        self._url = url
        self._timeout = timeout_ms
        self._allowed = list(allowed_domains or [])
        self._cdp_url = cdp_url
        self._shared_browser = bool(cdp_url)
        self._loop = asyncio.new_event_loop()
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._nav_violation = None
        self._target_frame = None
        # Handle -> control record from the last observation (e.g. {"selector":
        # "#email", ...}). Resolvers map an action's ``eN`` id back to the exact
        # selector that observation saw. A live data attribute is ALSO stamped
        # on each control, but single-page sites (PayPal's auth flow) re-render
        # and wipe it within seconds — long before a slow model issues its next
        # action — so the recorded selector, not the attribute, is the durable
        # handle.
        self._handles: Dict[str, Dict[str, Any]] = {}
        # Playwright is driven by a dedicated loop. Discord commands and the
        # advisor continuation can arrive concurrently, so never let two
        # threads drive that loop at the same time.
        self.operation_lock = threading.RLock()

    def _ensure(self, navigate: bool = True) -> None:
        if self._page is not None:
            return
        self._loop.run_until_complete(self._connect(navigate=navigate))

    async def _connect(self, navigate: bool = True) -> None:
        from playwright.async_api import async_playwright
        # ``async_playwright()`` is a context manager (only ``start``); the
        # real driver handle is the Playwright instance ``start()`` returns.
        # Browserless v2 only exposes the root CDP endpoint (no Playwright
        # driver WS), so we attach via ``connect_over_cdp`` and reuse its
        # default context — no ``new_context`` over CDP.
        self._playwright = await async_playwright().start()
        try:
            browser = await self._playwright.chromium.connect_over_cdp(
                self._cdp_url or _ws_url()
            )
        except Exception:
            # A completed VNC session can outlive its Docker container (host
            # reboot, manual cleanup, or an expired test session). Never roll
            # back a valid act solely because that stale shared-browser hint
            # is dead; fall back to the normal Browserless session.
            if not self._cdp_url:
                raise
            self._cdp_url = None
            self._shared_browser = False
            browser = await self._playwright.chromium.connect_over_cdp(_ws_url())
        self._browser = browser
        self._context = browser.contexts[0]
        # Navigate only when requested; restore_storage_state needs cookies and
        # localStorage loaded into context BEFORE first page navigation.
        await self._init_page(self._url if navigate else None)

    def storage_state(self) -> bytes:
        """Return encrypted-at-rest-ready Playwright storage state."""
        self._ensure()
        state = self._loop.run_until_complete(self._context.storage_state())
        # Playwright storage_state does not include the active document URL.
        # Persist it alongside cookies so a rehydrated mission returns to the
        # sign-in/verification page instead of always reopening the homepage.
        state["__current_url"] = self._page.url if self._page is not None else self._url
        return json.dumps(state, separators=(",", ":")).encode("utf-8")

    def restore_storage_state(self, state_bytes: bytes) -> None:
        """Restore cookies and localStorage before navigating to target URL."""
        # Connect to browser without navigating to initial_url unauthenticated
        self._ensure(navigate=False)
        state = json.loads(state_bytes.decode("utf-8"))
        cookies = state.get("cookies") or []
        if cookies:
            self._loop.run_until_complete(self._context.add_cookies(cookies))
        origins = state.get("origins") or []
        if origins:
            async def _apply_origins():
                for origin_entry in origins:
                    origin_url = origin_entry.get("origin")
                    storage_items = origin_entry.get("localStorage") or []
                    if origin_url and storage_items:
                        init_code = f"""
                        (() => {{
                            try {{
                                if (window.location.origin === {json.dumps(origin_url)}) {{
                                    const items = {json.dumps(storage_items)};
                                    for (const item of items) {{
                                        if (item.name && item.value !== undefined) {{
                                            window.localStorage.setItem(item.name, item.value);
                                        }}
                                    }}
                                }}
                            }} catch (e) {{}}
                        }})();
                        """
                        try:
                            await self._context.add_init_script(init_code)
                        except Exception:
                            pass
            self._loop.run_until_complete(_apply_origins())

        # Safe navigation sequence: visit target URL only AFTER cookies and
        # localStorage are installed into the browser context.
        current_url = str(state.get("__current_url") or self._url)
        validate_target_url(current_url, self._allowed)
        self._loop.run_until_complete(
            self._page.goto(current_url, wait_until="domcontentloaded", timeout=self._timeout)
        )
        async def _ensure_local_storage():
            try:
                current_origin = await self._page.evaluate("() => window.location.origin")
                for origin_entry in origins:
                    if origin_entry.get("origin") == current_origin:
                        for item in origin_entry.get("localStorage") or []:
                            await self._page.evaluate(
                                "([k, v]) => { try { window.localStorage.setItem(k, v); } catch(e){} }",
                                [item.get("name"), item.get("value")],
                            )
            except Exception:
                pass
        self._loop.run_until_complete(_ensure_local_storage())
        self._assert_on_scope()

    async def _init_page(self, initial_url: Optional[str] = None) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[0] if self._shared_browser and pages else await self._context.new_page()
        self._nav_violation = None
        self._target_frame = None
        await self._page.set_viewport_size({"width": 1400, "height": 900})

        # Inject anti-bot stealth scripts into page and context
        try:
            await self._page.add_init_script(_STEALTH_INIT_SCRIPT)
        except Exception:
            pass

        async def _guard_frame(frame) -> None:
            try:
                url = frame.url or ""
                if frame == self._page.main_frame and url.lower().startswith(
                    ("http://", "https://")
                ):
                    validate_target_url(url, self._allowed)
            except Exception as exc:  # noqa: BLE001 — record, raise at next op
                self._nav_violation = exc

        self._page.on("framenavigated", _guard_frame)
        if initial_url and (not self._shared_browser or not self._page.url or self._page.url == "about:blank"):
            await self._page.goto(initial_url, wait_until="load", timeout=self._timeout)

    def _assert_on_scope(self) -> None:
        """Fail closed if the live page left the gated allowlist scope."""
        if self._nav_violation is not None:
            raise self._nav_violation
        if self._page is not None:
            validate_target_url(self._page.url, self._allowed)

    def navigate(self, url: str) -> None:
        self._ensure()

        async def _nav():
            try:
                resp = await self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)
                if resp is not None and int(resp.status) >= 400:
                    raise ValueError(
                        f"navigation returned HTTP {resp.status} for {url!r}"
                    )
            except Exception as e:
                # If aborted due to another navigation or client redirect, wait for load
                if "ERR_ABORTED" in str(e):
                    await self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout)
                    resp = None
                else:
                    raise

        self._loop.run_until_complete(_nav())
        self._assert_on_scope()

    def heal(self) -> bool:
        """Dismiss a clearly labelled checkpoint interstitial, if present."""
        self._ensure()
        self._assert_on_scope()

        async def _heal() -> bool:
            if self._page is None or self._page.is_closed():
                return False
            clicked_any = False
            for _ in range(3):
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                try:
                    text = str(await self._page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    ) or "")
                except Exception:
                    return clicked_any
                low = text.lower()
                want = ""
                for label in ("continue", "continue shopping", "verify", "try again"):
                    if label in low:
                        want = label
                        break
                if not want:
                    return clicked_any
                clicked = await self._click_visible_text(want, ("a", "button", "input[type='submit']"))
                if not clicked:
                    return clicked_any
                clicked_any = True
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout)
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            return clicked_any

        return self._loop.run_until_complete(_heal())

    async def _click_visible_text(self, text: str, tags: tuple) -> bool:
        """Click the first visible element whose text contains ``text``."""
        try:
            frames = [self._page.main_frame] + [
                f for f in self._page.frames if f != self._page.main_frame
            ] if (self._page and not self._page.is_closed()) else []
            for frame in frames:
                try:
                    clicked = bool(await frame.evaluate(
                        """(args) => {
                            const want = args.text.toLowerCase().trim();
                            const els = document.querySelectorAll(args.tags.join(','));
                            for (const el of els) {
                                const t = (el.innerText || el.value || '').toLowerCase().trim();
                                if (t.includes(want)) {
                                    const r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {
                                        el.click();
                                        return true;
                                    }
                                }
                            }
                            return false;
                        }""",
                        {"text": text, "tags": list(tags)},
                    ))
                    if clicked:
                        return True
                except Exception:
                    continue
            return False
        except Exception:  # noqa: BLE001 — heal is best-effort
            return False

    async def _dismiss_consent(self) -> bool:
        """Best-effort dismiss a cookie/consent dialog covering the page.

        Many sites render a fixed consent overlay that sits on top of every
        control; Playwright then refuses the click with
        ``... intercepts pointer events`` until the overlay is gone. Only
        clicks a *visible* control whose label is an unambiguous consent
        action, so it can never submit a form or advance a flow by accident.
        """
        if self._page is None or self._page.is_closed():
            return False
        frames = [self._page.main_frame] + [
            f for f in self._page.frames if f != self._page.main_frame
        ]
        for frame in frames:
            try:
                clicked = bool(await frame.evaluate(
                    """(labels) => {
                        const vis = (el) => {
                            const s = getComputedStyle(el), r = el.getBoundingClientRect();
                            return s.visibility !== 'hidden' && s.display !== 'none' &&
                                r.width > 0 && r.height > 0;
                        };
                        const norm = (t) => String(t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        for (const el of document.querySelectorAll(
                            'button,a,[role="button"],input[type="button"],input[type="submit"]'
                        )) {
                            if (!vis(el)) continue;
                            const t = norm(el.innerText || el.value || el.getAttribute('aria-label'));
                            if (!t) continue;
                            if (labels.some((l) => t === l || t.startsWith(l))) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    list(_CONSENT_LABELS),
                ))
            except Exception:
                continue
            if clicked:
                try:
                    await self._wait_for_settle()
                except Exception:
                    pass
                return True
        return False

    async def _wait_for_settle(self, timeout_ms: Optional[int] = None) -> None:
        """Wait for DOM mutations and network settling after an interaction."""
        if self._page is None or self._page.is_closed():
            return
        timeout = timeout_ms or min(self._timeout, 3000)
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass
        try:
            await self._page.wait_for_load_state("networkidle", timeout=min(timeout, 2000))
        except Exception:
            pass
        try:
            await asyncio.sleep(0.5)
        except Exception:
            pass

    async def _find_selector(self, selector: str) -> Optional[str]:
        self._target_frame = None

        def _fieldy(s: str) -> bool:
            low = s.lower()
            return "pass" in low or "email" in low or "password" in low

        def _get_candidates_for(sel: str) -> List[str]:
            cands = [sel]
            # Numeric ID fix: #123 is invalid CSS syntax, [id="123"] is valid
            m = re.match(r"^#(\d.*)$", sel.strip())
            if m:
                cands.append(f'[id="{m.group(1)}"]')
            return cands

        for attempt in range(3):
            try:
                if self._page is None or self._page.is_closed():
                    return None

                frames = [self._page.main_frame] + [
                    f for f in self._page.frames if f != self._page.main_frame
                ]

                # 1. Try exact selector (and normalized numeric ID variations)
                for cand in _get_candidates_for(selector):
                    for frame_idx, frame in enumerate(frames):
                        try:
                            # Dynamic waiting on the primary selector
                            wait_ms = min(self._timeout, 1500) if frame_idx == 0 else min(self._timeout, 600)
                            el = None
                            try:
                                el = await frame.wait_for_selector(
                                    cand,
                                    state="visible" if _fieldy(cand) else "attached",
                                    timeout=wait_ms,
                                )
                            except Exception as e:
                                err_str = str(e).lower()
                                if "syntaxerror" in err_str or "is not a valid selector" in err_str:
                                    break
                                if "timeout" in err_str:
                                    el = None
                                elif "execution context was destroyed" in err_str or "navigating" in err_str:
                                    raise
                            if el is not None:
                                # Require a genuinely visible match. A stale or
                                # display:none element that merely "attaches"
                                # must not satisfy the lookup: returning it here
                                # suppressed the semantic fallback below, so the
                                # model kept clicking a hidden control and the
                                # mission stalled on a repeated failure.
                                try:
                                    if await el.is_visible():
                                        self._target_frame = frame
                                        return cand
                                except Exception:
                                    pass
                        except Exception as exc:
                            err_msg = str(exc).lower()
                            if "syntaxerror" in err_msg or "is not a valid selector" in err_msg:
                                break
                            if "execution context was destroyed" in err_msg or "navigating" in err_msg:
                                raise

                # 2. Semantic fallback candidates for common controls
                s_low = selector.lower()
                candidates = []
                if "email" in s_low or "user" in s_low or "login" in s_low:
                    candidates = [
                        "input[type='email']",
                        "input[autocomplete*='username' i]",
                        "input[name*='email' i]",
                        "input[name*='user' i]",
                        "input[type='text']",
                    ]
                elif "pass" in s_low:
                    candidates = ["input[type='password']", "input[name*='password' i]"]
                elif any(word in s_low for word in ("submit", "continue", "next", "proceed", "sign")):
                    # Deliberately exclude a bare ``button`` / ``[role=button]``:
                    # taking the first visible one on the page can silently
                    # click an unrelated control (e.g. "Accept all cookies")
                    # and then report success. Text-matched resolution in
                    # ``click`` handles untyped buttons by their label.
                    candidates = ["input[type='submit']", "button[type='submit']"]

                for c in candidates:
                    for frame in frames:
                        try:
                            el = await frame.query_selector(c)
                            if el:
                                try:
                                    if await el.is_visible():
                                        self._target_frame = frame
                                        return c
                                except Exception:
                                    pass
                        except Exception as exc:
                            err_msg = str(exc).lower()
                            if "syntaxerror" in err_msg or "is not a valid selector" in err_msg:
                                continue
                            if "execution context was destroyed" in err_msg or "navigating" in err_msg:
                                raise
                if attempt < 2:
                    await asyncio.sleep(0.4)
                    continue
                return None
            except Exception as exc:
                if attempt < 2 and ("execution context was destroyed" in str(exc).lower() or "navigating" in str(exc).lower()):
                    try:
                        await self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout)
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                    continue
                err_msg = str(exc).lower()
                if "syntaxerror" in err_msg or "is not a valid selector" in err_msg:
                    return None
                raise

    def _handle_record(self, selector: str) -> Optional[Dict[str, Any]]:
        """The observation record for a bare id (``e3``), or ``None``.

        ``None`` for anything that is not an id we observed — a real CSS
        selector, a ``text=`` request, or an id from an observation the page
        has since replaced.
        """
        s = str(selector or "").strip()
        if not _HANDLE_RE.match(s):
            return None
        return self._handles.get(s)

    def _resolve_handle(self, selector: str) -> str:
        """Best single selector for an observed id — used as a field hint.

        This is the *primary* (most-stable) selector the observation recorded
        for the control.  Element lookup itself goes through ``_locate``,
        which tries every recorded candidate and then re-finds the control by
        its semantic fingerprint, so a renamed id or wiped attribute on any
        site is still resolved.  A non-id passes through untouched.
        """
        s = str(selector or "").strip()
        if not _HANDLE_RE.match(s):
            return selector
        record = self._handles.get(s)
        if record and record.get("selector"):
            return str(record["selector"])
        return f'[{_HANDLE_ATTR}="{s}"]'

    async def _fingerprint_selector(self, record: Dict[str, Any]) -> Optional[str]:
        """Re-find a control by what it *is*, after its selectors went stale.

        A re-rendering site can drop an id or renumber a generated path
        between observation and action.  The control's semantic identity —
        tag + type/name, or its visible label — usually survives; locate the
        one visible element that matches, re-tag it, and return its selector.
        Runs in every frame so it also recovers controls inside reloaded
        iframes.
        """
        if self._page is None or self._page.is_closed():
            return None
        handle_id = str(record.get("handle") or "")
        fp = {
            "id": handle_id,
            "sel": f'[{_HANDLE_ATTR}="{handle_id}"]',
            "tag": record.get("tag") or "",
            "type": record.get("type") or "",
            "name": record.get("name") or "",
            "placeholder": record.get("placeholder") or "",
            "label": record.get("label") or "",
        }
        if not (fp["name"] or fp["label"] or fp["placeholder"] or fp["type"]):
            return None

        async def _scan(frame) -> str:
            try:
                return str(await asyncio.wait_for(frame.evaluate("""(fp) => {
                    const visible = (el) => {
                        const s = getComputedStyle(el), r = el.getBoundingClientRect();
                        return s.visibility !== 'hidden' && s.display !== 'none' &&
                            r.width > 0 && r.height > 0;
                    };
                    const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
                    const nodes = [...document.querySelectorAll(
                        'a,button,input,textarea,select,[role="button"],[role="link"]'
                    )].filter(visible);
                    const tag = (el) => el.tagName.toLowerCase();
                    const attr = (el, a) => el.getAttribute(a) || '';
                    const type = (el) => attr(el, 'type').toLowerCase();
                    const label = (el) => clean(
                        el.innerText || attr(el, 'aria-label') || attr(el, 'placeholder') || ''
                    );
                    // Try the most identifying signal first; accept a match
                    // only when it is UNIQUE, so a generic fallback (tag+type)
                    // can never silently pick the wrong control.
                    const strategies = [
                        (el) => fp.name && attr(el, 'name') === fp.name && tag(el) === fp.tag,
                        (el) => fp.label && label(el) === fp.label && tag(el) === fp.tag,
                        (el) => fp.placeholder && attr(el, 'placeholder') === fp.placeholder && tag(el) === fp.tag,
                        (el) => fp.type && type(el) === fp.type && tag(el) === fp.tag,
                    ];
                    for (const match of strategies) {
                        const hits = nodes.filter(match);
                        if (hits.length === 1) {
                            try { hits[0].setAttribute('data-concierge-handle', fp.id); } catch (e) {}
                            return fp.sel;
                        }
                    }
                    return '';
                }""", fp), timeout=5.0) or "")
            except Exception:
                return ""

        frames = [self._page.main_frame] + [
            f for f in self._page.frames if f != self._page.main_frame
        ]
        for frame in frames:
            found = await _scan(frame)
            if found:
                return found
        return None

    async def _quick_visible(self, selector: str) -> Optional[str]:
        """One no-retry pass: the first visible match in any frame, if present.

        Used to try a control's candidate selectors cheaply; the retrying
        ``_find_selector`` remains the final fallback for slow-rendering pages.
        """
        if self._page is None or self._page.is_closed():
            return None
        cands = [selector]
        m = re.match(r"^#(\d.*)$", str(selector).strip())
        if m:
            cands.append(f'[id="{m.group(1)}"]')
        frames = [self._page.main_frame] + [
            f for f in self._page.frames if f != self._page.main_frame
        ]
        for cand in cands:
            for frame in frames:
                try:
                    el = await frame.query_selector(cand)
                except Exception:
                    continue
                if el is None:
                    continue
                try:
                    if await el.is_visible():
                        self._target_frame = frame
                        return cand
                except Exception:
                    pass
        return None

    async def _locate(self, selector: str) -> Optional[str]:
        """Resolve an action target to a live, visible selector.

        A bare observation id (``e3``) is resolved site-agnostically: try each
        selector the observation recorded for that control (id first, then
        name/testid/aria-label/placeholder, then the structural path), and if
        every one has gone stale, re-find the control by its semantic
        fingerprint.  A normal CSS selector takes the ordinary lookup path.
        """
        record = self._handle_record(selector)
        if record is None:
            return await self._find_selector(selector)
        for cand in record.get("selectors") or []:
            found = await self._quick_visible(str(cand))
            if found:
                return found
        fp = await self._fingerprint_selector(record)
        if fp:
            return await self._find_selector(fp)
        # Last resort: let the retrying lookup handle a slow-rendering page.
        return await self._find_selector(str(record.get("selector") or selector))

    async def _activate_control(self, scope, sel: str) -> None:
        """Click a located control, routing radio/checkbox activation through
        the native ``.click()`` when the input itself is not directly
        actionable.

        Framework UIs (PayPal's 2FA method picker, styled radio/checkbox
        groups) hide the real ``<input>`` behind a zero-size/``opacity:0``
        element, so Playwright's actionability check waits on it forever and
        times out after 30s. Routing the click through the associated
        ``<label>`` — or the element's own ``click()``, which still fires the
        ``change`` event — selects it immediately. Site-agnostic: any
        ``input[type=radio|checkbox]`` benefits, with no per-site branch.
        """
        kind = None
        try:
            kind = await scope.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    return {tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase()};
                }""",
                sel,
            )
        except Exception:
            kind = None
        if kind and kind.get("tag") == "input" and kind.get("type") in {"radio", "checkbox"}:
            try:
                await scope.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        let label = null;
                        if (el.id)
                            label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        if (!label) label = el.closest('label');
                        (label || el).click();
                        return true;
                    }""",
                    sel,
                )
                return
            except Exception:
                pass
        await scope.click(sel, timeout=self._timeout)

    def click(self, selector: str) -> None:
        self._ensure()
        self._assert_on_scope()

        async def _click():
            s_low = selector.lower()
            for attempt in range(3):
                try:
                    # Never resolve a bare text action by taking the first
                    # matching element. Shopping carts and similar pages
                    # commonly contain several identical controls (for
                    # example one Delete link per item). A guessed text
                    # selector must fail closed so the model is forced to
                    # inspect the page and provide a unique/row-scoped target.
                    if re.match(r"^\s*text\s*=", selector, re.IGNORECASE):
                        wanted = re.sub(r"^\s*text\s*=\s*", "", selector, flags=re.IGNORECASE).strip()
                        match_count = await self._page.evaluate(
                            """(wanted) => {
                                const needle = String(wanted || '').toLowerCase();
                                let n = 0;
                                for (const el of document.querySelectorAll('a,button,input,[role="button"]')) {
                                    const text = (el.innerText || el.value || '').trim().toLowerCase();
                                    const r = el.getBoundingClientRect();
                                    if (text.includes(needle) && r.width > 0 && r.height > 0) n++;
                                }
                                return n;
                            }""",
                            wanted,
                        )
                        if int(match_count or 0) != 1:
                            raise ValueError(
                                f"ambiguous click target {selector!r}: found "
                                f"{int(match_count or 0)} visible matches; use a unique selector"
                            )
                    sel = await self._locate(selector)
                    if sel:
                        target_scope = self._target_frame or self._page
                        await self._activate_control(target_scope, sel)
                        await self._wait_for_settle()
                    else:
                        # Models often describe a control semantically when a
                        # generated selector is stale (e.g. ``#continueButton``).
                        # Resolve that against visible controls generically;
                        # never silently report a missing click as success.
                        raw_token = re.sub(r"([a-z])([A-Z])", r"\1 \2", selector)
                        token = re.sub(r"[^a-z0-9 ]+", " ", raw_token.lower())
                        token = re.sub(r"\b(button|btn|link)\b", " ", token)
                        token = " ".join(token.split())
                        semantic = next(
                            (
                                word for word in (
                                    "continue", "next", "proceed", "submit",
                                    "sign in", "log in", "login",
                                )
                                if word in token
                            ),
                            None,
                        )
                        semantic_clicked = False
                        if semantic:
                            # Some forms expose an unlabeled submit input;
                            # its purpose is still unambiguous when the model
                            # requested a semantic submit/continue action.
                            for candidate in (
                                "button[type='submit']",
                                "input[type='submit']",
                            ):
                                for frame in self._page.frames:
                                    try:
                                        control = await frame.query_selector(candidate)
                                        if control and await control.is_visible():
                                            await control.click(timeout=self._timeout)
                                            semantic_clicked = True
                                            break
                                    except Exception:
                                        continue
                                if semantic_clicked:
                                    break
                        if not semantic_clicked and semantic and await self._click_visible_text(
                            semantic, ("button", "input", "a", "[role='button']")
                        ):
                            semantic_clicked = True
                        if semantic_clicked:
                            await self._wait_for_settle()
                        else:
                            raise ValueError(f"click target not found or not visible: {selector}")
                    return
                except Exception as exc:
                    msg = str(exc).lower()
                    if attempt < 2 and ("execution context was destroyed" in msg or "navigating" in msg):
                        try:
                            await self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout)
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)
                        continue
                    # A fixed cookie/consent banner sitting over the target makes
                    # Playwright refuse the click ("... intercepts pointer
                    # events") until the banner is dismissed. Clear it and retry
                    # the same target once instead of failing the whole mission.
                    if attempt < 2 and "intercept" in msg:
                        try:
                            if await self._dismiss_consent():
                                await asyncio.sleep(0.3)
                                continue
                        except Exception:
                            pass
                    raise

        self._loop.run_until_complete(_click())
        # A click can follow an <a> or trigger a client-side redirect. The
        # pre-click scope check is not enough: validate the resulting page
        # before reporting the action as successful. This prevents an
        # allowlisted page from sending the browser to an unapproved host.
        self._assert_on_scope()

    def scroll(self, value: str = "down", selector: str = "") -> None:
        """Scroll the live page or bring a generic selector into view."""
        self._ensure()
        self._assert_on_scope()

        async def _scroll() -> None:
            if self._page is None or self._page.is_closed():
                return
            if selector:
                target = await self._locate(selector)
                if target:
                    target_scope = self._target_frame or self._page
                    await target_scope.locator(target).scroll_into_view_if_needed()
                    return
            raw = str(value or "down").strip().lower()
            if raw == "top":
                await self._page.evaluate("() => window.scrollTo(0, 0)")
            elif raw == "bottom":
                await self._page.evaluate(
                    "() => window.scrollTo(0, document.documentElement.scrollHeight)"
                )
            else:
                try:
                    delta = int(float(raw))
                except (TypeError, ValueError):
                    delta = -700 if raw in {"up", "back", "previous"} else 700
                await self._page.mouse.wheel(0, max(-3000, min(3000, delta)))
            await asyncio.sleep(0.2)

        self._loop.run_until_complete(_scroll())

    def type_text(self, selector: str, value: str) -> None:
        self._ensure()
        self._assert_on_scope()

        async def _type():
            s_low = selector.lower()
            for attempt in range(3):
                try:
                    sel = await self._locate(selector)
                    if not sel:
                        raise ValueError(f"type target not found or not visible: {selector}")
                    target_scope = self._target_frame or self._page
                    # "Typing" into a radio/checkbox means selecting it; fill()
                    # refuses these inputs outright. Activate the choice instead
                    # of failing, so a choice control named by the observation
                    # works with either click or type.
                    try:
                        _kind = await target_scope.evaluate(
                            """(sel) => {
                                const el = document.querySelector(sel);
                                if (!el) return null;
                                return {tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase()};
                            }""",
                            sel,
                        )
                    except Exception:
                        _kind = None
                    if _kind and _kind.get("tag") == "input" and _kind.get("type") in {"radio", "checkbox"}:
                        await self._activate_control(target_scope, sel)
                        return
                    await target_scope.fill(sel, str(value), timeout=self._timeout)
                    return
                except ValueError:
                    raise
                except Exception as exc:
                    err_msg = str(exc).lower()
                    if "syntaxerror" in err_msg or "is not a valid selector" in err_msg:
                        raise ValueError(f"type target not found or not visible: {selector}") from exc
                    if attempt < 2 and ("execution context was destroyed" in err_msg or "navigating" in err_msg):
                        try:
                            await self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout)
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)
                        continue
                    # A consent overlay over the field blocks fill() the same way
                    # it blocks click(); clear it and retry once.
                    if attempt < 2 and "intercept" in err_msg:
                        try:
                            if await self._dismiss_consent():
                                await asyncio.sleep(0.3)
                                continue
                        except Exception:
                            pass
                    raise

        self._loop.run_until_complete(_type())

    def screenshot(self, fast: bool = False) -> bytes:
        self._ensure()
        self._assert_on_scope()

        async def _shot() -> bytes:
            try:
                if self._page is None or self._page.is_closed():
                    return b""
                # Challenge/auth pages can keep network activity open; a
                # screenshot must not depend on the page reaching a load state.
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                # Wait for any primary dynamic container or auth portal element if present
                for sel in (
                    "main",
                    "form",
                    "body",
                ):
                    try:
                        if self._page.is_closed():
                            return b""
                        await self._page.wait_for_selector(sel, state="visible", timeout=2000)
                        break
                    except Exception:
                        continue
                # Explicit user screenshots get a settling delay for a clean
                # frame. Internal recovery screenshots prioritize latency.
                if not fast:
                    await asyncio.sleep(1.5)
                if self._page.is_closed():
                    return b""
                if fast:
                    return await self._page.screenshot(
                        type="jpeg", quality=55, full_page=False
                    )
                return await self._page.screenshot(full_page=False)
            except Exception as exc:
                print(f" [CONCIERGE SCREENSHOT] capture failed: {type(exc).__name__}: {exc}", flush=True)
                return b""

        return self._loop.run_until_complete(_shot())


    def get_text(self) -> str:
        self._ensure()
        self._assert_on_scope()

        async def _frame_text(frame) -> str:
            try:
                return str(await asyncio.wait_for(frame.evaluate("""() => {
                    const visible = (el) => {
                        const s = getComputedStyle(el), r = el.getBoundingClientRect();
                        return s.visibility !== 'hidden' && s.display !== 'none' &&
                            r.width > 0 && r.height > 0;
                    };
                    const clean = (value) => String(value || '').replace(/\\n{3,}/g, '\\n\\n').trim();
                    const regions = [
                        ['STATUS', '[role="alert"], [role="status"], .alert, .flash, .notice'],
                        ['MAIN CONTENT', 'main, [role="main"]'],
                        ['FORM CONTENT', 'form'],
                        ['COMPLEMENTARY CONTENT', 'aside, [role="complementary"]'],
                        ['NAVIGATION', 'nav, [role="navigation"]'],
                        ['HEADER', 'header, [role="banner"]'],
                        ['FOOTER', 'footer, [role="contentinfo"]'],
                    ];
                    const chunks = [];
                    const seen = new Set();
                    for (const [label, selector] of regions) {
                        const values = [];
                        for (const el of document.querySelectorAll(selector)) {
                            if (!visible(el) || seen.has(el)) continue;
                            seen.add(el);
                            const value = clean(el.innerText);
                            if (value) values.push(value);
                        }
                        if (values.length) chunks.push(`[${label}]\\n${values.join('\\n\\n')}`);
                    }
                    if (chunks.length) {
                        return chunks.join('\\n\\n');
                    }
                    return clean(document.body ? document.body.innerText : '');
                }"""), timeout=5.0) or "")
            except Exception:
                return ""

        async def _eval() -> str:
            try:
                if self._page is None or self._page.is_closed():
                    return ""
                # Preserve generic DOM landmarks instead of flattening the
                # whole document. A flattened page makes sidebar
                # recommendations, navigation, and the actual task content
                # indistinguishable to the model. No site names or selectors
                # are assumed here; pages without landmarks still fall back
                # to their visible body text.
                frames = [self._page.main_frame] + [
                    f for f in self._page.frames if f != self._page.main_frame
                ]
                chunks: List[str] = []
                for idx, frame in enumerate(frames):
                    if idx and _is_invisible_challenge_frame(getattr(frame, "url", "")):
                        continue
                    text = await _frame_text(frame)
                    if not text.strip():
                        continue
                    if idx == 0:
                        chunks.append(text)
                    else:
                        # Login forms, consent walls, and bot challenges are
                        # frequently rendered inside an (often cross-origin)
                        # iframe; without this the main document looks empty
                        # and the model is blind to the only content on-page.
                        chunks.append(f"[EMBEDDED FRAME {str(frame.url)[:120]}]\n{text}")
                return _strip_recaptcha_widget_noise("\n\n".join(chunks))
            except Exception:
                return ""

        return self._loop.run_until_complete(_eval())

    def interactive_summary(self, limit: int = 80) -> str:
        """Return every visible control the bot can act on, as it is observed.

        Each control is tagged in the DOM with ``data-concierge-handle`` and
        handed to the model as a numbered id (e1, e2, …), grouped under its
        owning form.  The model acts by id — ``click selector="e3"`` — so it
        never has to invent a selector or guess the order of a form's fields.
        ``_find_selector`` resolves the tag in whichever frame owns it.
        """
        self._ensure()
        self._assert_on_scope()

        async def _frame_controls(frame, start: int, remaining: int) -> List[Dict[str, Any]]:
            try:
                raw = await asyncio.wait_for(frame.evaluate("""({start, limit}) => {
                    const visible = (el) => {
                        const s = getComputedStyle(el), r = el.getBoundingClientRect();
                        return s.visibility !== 'hidden' && s.display !== 'none' &&
                            r.width > 0 && r.height > 0;
                    };
                    const quote = (v) => String(v || '').replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
                    // Every way we can name this element, most-stable first.
                    // An action tries them in order, so a site that re-renders
                    // (dropping an id) or renames a generated path still has a
                    // fallback that matches what the observation actually saw.
                    const selectors = (el) => {
                        const out = [];
                        if (el.id) out.push(`#${CSS.escape(el.id)}`);
                        for (const attr of ['data-testid', 'name', 'aria-label', 'placeholder']) {
                            const value = el.getAttribute(attr);
                            if (value) out.push(`${el.tagName.toLowerCase()}[${attr}="${quote(value)}"]`);
                        }
                        const parts = [];
                        let node = el;
                        while (node && node.nodeType === 1 && node !== document.body && parts.length < 6) {
                            let part = node.tagName.toLowerCase();
                            if (node.parentElement) {
                                const same = [...node.parentElement.children].filter(x => x.tagName === node.tagName);
                                if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
                            }
                            parts.unshift(part);
                            node = node.parentElement;
                        }
                        if (parts.length) out.push(parts.join(' > '));
                        return out;
                    };
                    const controls = [];
                    let n = start;
                    for (const el of document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="link"]')) {
                        if (!visible(el)) continue;
                        // Never surface a secret field's current value to the
                        // model. Typed passwords/OTPs would otherwise be echoed
                        // in plaintext into the tool result (and on to the
                        // provider), violating the vault-only secret contract.
                        const secretMeta = ((el.name || '') + ' ' + (el.id || '') + ' ' +
                            (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('autocomplete') || '')).toLowerCase();
                        // A one-time / verification code is a secret we must not
                        // echo, but it is NOT resolvable from the vault: the
                        // model has to supply the value it retrieved from email
                        // or the user. Track it separately so the inventory can
                        // say "type the code" instead of "type with an empty
                        // value" (which would trigger vault resolution).
                        const isOtp = el.tagName === 'INPUT' && (
                            /otp|one-?time|verification|security-?code|passcode|\\bpin\\b/.test(secretMeta) ||
                            el.getAttribute('autocomplete') === 'one-time-code'
                        );
                        const isSecret = el.tagName === 'INPUT' && (
                            el.type === 'password' || isOtp ||
                            /password|passwd|passcode|otp|one-?time|verification|security-?code|\\bcvv\\b|\\bcvc\\b|\\bpin\\b/.test(secretMeta)
                        );
                        const label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || (isSecret ? '' : el.value) || '').replace(/\\s+/g, ' ').trim();
                        if (!label && !['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) continue;
                        n += 1;
                        const handle = 'e' + n;
                        // Tag the live element so an action can name it exactly
                        // (selector [data-concierge-handle="eN"]) without the
                        // model guessing a CSS path. Best-effort: a node that
                        // rejects the attribute still has its selector.
                        try { el.setAttribute('data-concierge-handle', handle); } catch (e) {}
                        const form = el.closest ? el.closest('form') : null;
                        const isSubmit = el.tagName === 'BUTTON'
                            ? ((el.getAttribute('type') || 'submit').toLowerCase() === 'submit')
                            : (el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'submit');
                        const cands = selectors(el);
                        controls.push({
                            handle: handle,
                            tag: el.tagName.toLowerCase(),
                            type: (el.getAttribute('type') || '').toLowerCase(),
                            name: el.getAttribute('name') || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            label: label.slice(0, 140),
                            selector: cands.length ? cands[0] : '',
                            selectors: cands,
                            secret: isSecret,
                            otp: isOtp,
                            submit: isSubmit,
                            form: form ? (form.getAttribute('name') || form.id || '') : '',
                            form_action: form ? (form.getAttribute('action') || '') : '',
                        });
                        if (controls.length >= limit) break;
                    }
                    return controls;
                }""", {"start": start, "limit": remaining}), timeout=5.0)
                return list(raw or [])
            except Exception:
                return []

        async def _eval() -> str:
            try:
                if self._page is None or self._page.is_closed():
                    return ""
                # Controls can live in a child frame (embedded auth widgets,
                # consent walls). `_find_selector` already searches every
                # frame, so a handle tagged here still resolves at action time.
                frames = [self._page.main_frame] + [
                    f for f in self._page.frames if f != self._page.main_frame
                ]
                items: List[Dict[str, Any]] = []
                for idx, frame in enumerate(frames):
                    if idx and _is_invisible_challenge_frame(getattr(frame, "url", "")):
                        continue
                    remaining = limit - len(items)
                    if remaining <= 0:
                        break
                    found = await _frame_controls(frame, len(items), remaining)
                    for item in found:
                        item["_frame"] = idx
                        items.append(item)
                # Remember the id -> selector mapping so a later click/type can
                # name a control by id and still resolve it after the page has
                # re-rendered (which discards the live data attribute).
                self._handles = {it["handle"]: it for it in items}
                return _format_inventory(items)
            except Exception:
                return ""

        return self._loop.run_until_complete(_eval())

    def page_stage(self) -> str:
        """Classify the visible interaction stage using generic form controls."""
        self._ensure()
        self._assert_on_scope()

        async def _frame_stage(frame) -> str:
            try:
                return str(await asyncio.wait_for(frame.evaluate("""() => {
                    const visible = (el) => {
                        const s = getComputedStyle(el), r = el.getBoundingClientRect();
                        return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                    };
                    if ([...document.querySelectorAll('input[type="password"]')].some(visible)) return 'password';
                    if ([...document.querySelectorAll('input[type="email"], input[autocomplete*="username" i], input[name*="email" i], input[name*="user" i]')].some(visible)) return 'identifier';
                    if ([...document.querySelectorAll('input, textarea')].some(visible)) return 'form';
                    return 'page';
                }"""), timeout=5.0) or "page")
            except Exception:
                return "page"

        async def _stage() -> str:
            try:
                if self._page is None or self._page.is_closed():
                    return "unavailable"
                frames = [self._page.main_frame] + [
                    f for f in self._page.frames if f != self._page.main_frame
                ]
                best = "page"
                for frame in frames:
                    stage = await _frame_stage(frame)
                    if stage == "password":
                        return "password"
                    if stage == "identifier":
                        best = "identifier"
                    elif stage == "form" and best == "page":
                        best = "form"
                return best
            except Exception:
                return "unknown"

        return str(self._loop.run_until_complete(_stage()) or "unknown")

    def element_visible(self, selector: str) -> bool:
        self._ensure()
        self._assert_on_scope()

        async def _visible() -> bool:
            try:
                if self._page is None or self._page.is_closed():
                    return False
                sel = await self._locate(selector)
                if not sel:
                    return False
                target_scope = self._target_frame or self._page
                el = await target_scope.query_selector(sel)
                if not el:
                    return False
                return await el.is_visible()
            except Exception:
                return False

        return self._loop.run_until_complete(_visible())

    def close(self) -> None:
        """Best-effort teardown; never raises (a failure here must not mask
        the act result — the current close() runs inside ``_execute_act``'s
        ``finally``)."""
        try:
            try:
                if self._page is not None and not self._shared_browser:
                    self._loop.run_until_complete(self._context.close())
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass
            try:
                if self._browser is not None and not self._shared_browser:
                    self._loop.run_until_complete(self._browser.close())
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass
            try:
                if self._playwright is not None:
                    self._loop.run_until_complete(self._playwright.stop())
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass
        finally:
            try:
                if not self._loop.is_running():
                    self._loop.close()
            except RuntimeError:
                pass


def _ws_url() -> str:
    from src.services.concierge.browser_driver import _ws_url as _orig
    return _orig()


def _normalize_mission_navigation_url(raw: str, mission_url: str, domain: str) -> str:
    """Normalize model-friendly navigation input into a gated absolute URL."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("navigate requires a URL or path")
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        candidate = value
    elif value.startswith("/"):
        candidate = "https://" + str(domain).strip().lower().lstrip(".") + value
    elif "://" not in value and not parsed.scheme:
        first = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "." in first or first.lower().startswith("www."):
            candidate = "https://" + value
        else:
            candidate = "https://" + str(domain).strip().lower().lstrip(".") + "/" + value
    else:
        raise ValueError("navigate target must use http:// or https://, or be a path")
    # Keep the mission URL in the signature for an explicit, stable contract:
    # the domain is the authoritative host gate, while the URL identifies the
    # approved mission that supplied it.
    _ = mission_url
    return validate_target_url(candidate, [str(domain).strip().lower()])


def _shared_cdp_url(tenant: str, url: str) -> Optional[str]:
    """Find the user's completed headed/VNC browser for this target host."""
    host = (urlparse(str(url)).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    try:
        session = BrowserSessionStore(_DB).latest_ready_for_domain(tenant, host)
    except Exception:
        return None
    if not session:
        return None
    try:
        socket.gethostbyname(session["container_name"])
    except OSError:
        # SQLite may retain a completed session after Docker removed its
        # container. Treat that row as stale instead of returning a dead CDP
        # hostname to the actuator.
        return None
    return "http://" + session["container_name"] + ":9223"


class ActionApprovalView(discord.ui.View):
    """[✅ Approve] / [✕ Reject] for a pending concierge write-tier proposal.

    Only the user who triggered the original request (uid embedded in the
    proposal) can approve or reject. Modeled on ``LoginSessionView``.
    """

    def __init__(
        self,
        proposal_id: str,
        owner_uid: int,
        tenant: str,
        timeout: float = 600.0,
    ):
        super().__init__(timeout=timeout)
        self.proposal_id = proposal_id
        self.owner_uid = int(owner_uid)
        self.tenant = tenant

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_uid

    @discord.ui.button(label="✅ Approve & Execute", style=discord.ButtonStyle.success)
    async def _approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the person who requested this action can approve it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)

        # The whole approved-act lifecycle (claim → mission grant → healing
        # execution → VLM verify → audit → status) runs in one worker thread,
        # shared with the mission auto-pilot path; Discord messaging stays here.
        result = await asyncio.to_thread(
            execute_approved_act, self.proposal_id, self.tenant
        )
        proposal = ApprovalStore().get(self.proposal_id)
        if not result.get("ok") and result.get("unclaimed"):
            await interaction.followup.send(
                "⚠️ This approval request can no longer be executed (already "
                "handled, or its approval window expired).", ephemeral=True
            )
            for item in list(self.children):
                item.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            return
        files = []
        shot_bytes = None
        if result.get("ok"):
            res = result["res"]
            shot_bytes = res.get("screenshot_png") if isinstance(res, dict) else None
            verification = result["verification"]
            outcome = result["outcome"]
            execution_phase = result.get("execution_phase", "completed")
            started_only = execution_phase == "started"
            status_emoji = "🔎" if started_only else ("✅" if outcome == "executed" else "⚠️")
            outcome_word = "mission started — not verified" if started_only else (
                "executed" if outcome == "executed" else "rolled back"
            )
            rec = res.get("receipt") or {}
            receipt_line = (
                "\nReceipt: " + ", ".join(f"{k}={v}" for k, v in rec.items())
                if rec else ""
            )
            msg = (
                f"{status_emoji} **{proposal['kind']}** {outcome_word} on "
                f"{proposal['url']}\n"
                f"Verify: **{verification['verdict']}** "
                f"(conf {verification['confidence']:.2f})\n"
                f"Summary: {res.get('summary', '(no result)')[:300]}"
                f"{receipt_line}"
            )
            mission = result.get("mission")
            if mission:
                expires = str(mission.get("expires_at") or "")[:16].replace("T", " ")
                msg += (
                    f"\n🚀 **Auto-pilot active** — follow-up steps on "
                    f"`{mission['domain']}` run without re-approval until "
                    f"{expires} UTC."
                )
            if isinstance(shot_bytes, (bytes, bytearray)) and len(shot_bytes) > 0:
                files = [discord.File(io.BytesIO(shot_bytes), filename="concierge_browser.png")]
            await interaction.followup.send(msg, files=files, ephemeral=False)
            # Resume the advisor with the live page observation.  The original
            # turn ended at the permission boundary; without this continuation
            # the model can only guess what happened and cannot choose the next
            # browser action.
            if proposal.get("kind") == "fill_form" and mission:
                try:
                    from src.services.llm import chat_with_delilah
                    continuation = await interaction.channel.send(
                        "🔎 Permission granted. Inspecting the live page and continuing the approved browser mission…"
                    )
                    continuation_task = asyncio.create_task(chat_with_delilah(
                        "Permission was granted for the approved browser mission. "
                        "Use concierge_browser_step with the active mission_id to observe the current page, "
                        "then take exactly one appropriate next action at a time. Do not guess selectors; "
                        "inspect after every action. Stop and report if the page shows a challenge or failure.",
                        self.owner_uid,
                        continuation,
                        required_tools={"concierge_browser_step"},
                    ), name=f"advisor:continuation:{self.owner_uid}:{self.proposal_id}")
                    # Register the continuation so !cancel can find it.
                    from src.core.state import ACTIVE_ADVISOR_TASKS
                    ACTIVE_ADVISOR_TASKS[str(self.owner_uid)] = continuation_task
                except Exception as cont_err:
                    print(f" [CONCIERGE CONTINUATION] failed to start: {cont_err}")
        else:
            shot_bytes = result.get("screenshot_png")
            if isinstance(shot_bytes, (bytes, bytearray)) and len(shot_bytes) > 0:
                files = [discord.File(io.BytesIO(shot_bytes), filename="concierge_browser.png")]
            await interaction.followup.send(
                "⚠️ Execution failed — act rolled back: " + result["error"][:300],
                files=files,
                ephemeral=False,
            )
        for item in list(self.children):
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="✕ Reject", style=discord.ButtonStyle.danger)
    async def _reject(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the person who requested this action can reject it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        store = ApprovalStore()
        proposal = store.get(self.proposal_id)
        if proposal and proposal["status"] == "pending":
            store.update_status(self.proposal_id, "rejected")
            audit = AuditLog(_DB)
            audit.append(
                actor=self.tenant, action="act_rejected", tenant=self.tenant,
                subject=proposal["kind"], detail={"proposal_id": self.proposal_id},
            )
            await interaction.followup.send(
                "✕ Write-tier action rejected. No execution occurred.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "This request is no longer pending.", ephemeral=True
            )
        for item in list(self.children):
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def on_timeout(self) -> None:
        """Auto-expire after the approval TTL window; buttons go gray.

        Transitions the proposal to ``expired`` server-side (not just the
        Discord view timeout) — and the terminal transition revokes any vault
        secrets vaulted for it.
        """
        try:
            store = ApprovalStore()
            proposal = store.get(self.proposal_id)
            if proposal and proposal["status"] == "pending":
                store.update_status(
                    self.proposal_id, "expired",
                    result_detail={"reason": "approval prompt timed out"},
                )
                AuditLog(_DB).append(
                    actor=self.tenant, action="act_expired", tenant=self.tenant,
                    subject=proposal["kind"],
                    detail={"proposal_id": self.proposal_id},
                )
        except Exception:
            pass
        for item in list(self.children):
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    async def on_error(
        self, interaction: discord.Interaction, exc: Exception, item: Any
    ) -> None:
        print(f" [CONCIERGE APPROVAL VIEW] error: {type(exc).__name__}: {exc}")
        for child in list(self.children):
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass


def _make_resolver(
    tenant: str,
    vault: "Vault",
    url: str,
    field_hint: str = "",
) -> Callable[[Optional[str]], str]:
    """Vault-ref resolver for the approved act: tenant-scoped with a
    domain-scoped fallback to active tenant records (persistent captures)."""

    def _scope_host(val: str) -> str:
        s = str(val or "").strip().lower()
        if "://" in s:
            s = urlparse(s).netloc.lower()
        elif "/" in s:
            s = s.split("/", 1)[0]
        s = s.removeprefix("www.")
        if ":" in s:
            s = s.split(":", 1)[0]
        return s

    def _domain_matches(candidate_scope: str, target: str) -> bool:
        if not candidate_scope or not target:
            return False
        s = _scope_host(candidate_scope).lstrip(".*")
        if s.startswith("*."):
            s = s[2:]
        t = _scope_host(target)
        return t == s or t.endswith("." + s)

    def _resolve_secret(vault_ref: Optional[str] = None) -> str:
        raw = None
        target_domain = _scope_host(urlparse(url).netloc or url)
        if vault_ref:
            # Explicit refs are accepted only after the caller has established
            # the live page is on this domain (consumer_url is revalidated by
            # Vault). The value never leaves this executor closure.
            try:
                raw = vault.resolve(tenant, vault_ref, consumer_url=url)
            except Exception:
                raw = None
        else:
            # The model does not need to see credential refs. At the moment a
            # secret field is actually typed, select an active secret scoped
            # to the current visited domain or parent domain.
            candidates = []
            for rec in vault.list_records(tenant):
                if rec.get("kind") != "secret":
                    continue
                scopes = rec.get("consumer_scope") or []
                if any(_domain_matches(scope, target_domain) for scope in scopes):
                    candidates.append(rec)
            if len(candidates) >= 1:
                if len(candidates) > 1:
                    import logging
                    logging.warning(
                        "Multiple stored credentials exist for %s (tenant %s); "
                        "selecting latest active record %s",
                        target_domain, tenant, candidates[0].get("vault_ref"),
                    )
                chosen = max(
                    candidates,
                    key=lambda r: (str(r.get("updated_at") or ""), str(r.get("created_at") or "")),
                ) if any(r.get("created_at") or r.get("updated_at") for r in candidates) else candidates[0]
                try:
                    raw = vault.resolve(tenant, chosen["vault_ref"], consumer_url=url)
                except Exception:
                    raw = None
        if raw is None:
            raise ValueError(
                f"no credential is available for the visited domain {target_domain}"
            )
        try:
            payload = json.loads(raw)
        except Exception:
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        if isinstance(payload, dict):
            # Select the field matching the live control, while keeping the
            # payload entirely inside this executor closure.  A credential
            # record commonly contains both username and password; returning
            # the first/most-secret value makes login fail in a confusing way.
            # This is deliberately based on generic field types/labels, not
            # on any merchant's DOM or semantics.
            hint = str(field_hint or "").lower()
            preferred = []
            if any(x in hint for x in ("pass", "pwd", "secret")):
                preferred = ["password", "secret", "pass"]
            elif any(x in hint for x in ("display", "nickname", "screen_name")):
                preferred = ["display_name", "display", "nickname", "screen_name"]
            elif any(x in hint for x in ("user", "email", "login", "account", "identifier")):
                preferred = ["username", "user", "email", "login", "account", "identifier"]
            for wanted in preferred:
                for k, v in payload.items():
                    key = k.lower()
                    if wanted in key:
                        return str(v)
            # If there is no useful hint, preserve the safe historical
            # behavior of preferring a password-like field, then first value.
            for k, v in payload.items():
                if "pass" in k.lower() or "secret" in k.lower():
                    return str(v)
            values = list(payload.values())
            if values:
                return str(values[0])
        elif isinstance(payload, str):
            return payload
        raise ValueError(f"vault_ref {vault_ref} holds no value")

    return _resolve_secret


def execute_approved_act(
    proposal_id: str,
    tenant: str,
    store: Optional[ApprovalStore] = None,
    vault: Optional[Vault] = None,
    actuator_factory: Optional[Callable[[str, List[str]], Actuator]] = None,
    verify_factory: Optional[Callable] = None,
) -> dict:
    """Execute a claimed proposal: approval-view ✅ OR mission auto-pilot.

    Single source of truth for the approved-act lifecycle — the Discord
    approval view and the llm.py auto-pilot dispatcher both call this. Claims
    the proposal (pending→approved, TTL-enforced), grants/refreshes the
    auto-pilot mission for its (tenant, kind, domain) scope, runs
    ``perform_act`` in a healing BrowserlessActuator + VLM verification,
    audits, and transitions the proposal status. Returns ``{"ok": True,
    "outcome", "res", "verification", "mission"}`` or ``{"ok": False,
    "error", "screenshot_png"}`` (+ ``unclaimed`` when the proposal was
    already terminal or past its approval window). Runs in a worker thread
    via ``asyncio.to_thread`` — the actuator is sync-over-async and must
    never run on the bot's event loop.
    """
    store = store or ApprovalStore()
    vault = vault or Vault(_DB)
    if not store.claim(proposal_id):
        return {
            "ok": False,
            "unclaimed": True,
            "error": "Already handled or its approval window expired",
            "screenshot_png": None,
        }
    proposal = store.get(proposal_id)
    allowed = proposal.get("allowed_domains") or []
    audit = AuditLog(store.db_path)
    audit.append(
        actor=tenant, action="act_approved", tenant=tenant,
        subject=proposal["kind"], detail={"proposal_id": proposal_id},
    )
    mission = store.grant_mission(proposal_id)

    if actuator_factory is None:
        def actuator_factory(url: str, allowed_domains: List[str]) -> Actuator:
            return BrowserlessActuator(
                url, timeout_ms=DEFAULT_TIMEOUT_MS, allowed_domains=allowed_domains,
                cdp_url=_shared_cdp_url(tenant, url),
            )
    verify = verify_factory or verify_act
    actuator = actuator_factory(proposal["url"], allowed)
    keep_browser = proposal["kind"] == "fill_form"
    try:
        def resolve_secret_for_live_page(vault_ref: Optional[str] = None) -> str:
            live_url = getattr(getattr(actuator, "_page", None), "url", "") or proposal["url"]
            return _make_resolver(tenant, vault, live_url)(vault_ref)

        execution_args = dict(proposal["args"])
        if keep_browser:
            # Approval authorizes the mission, not a blind selector script.
            # Open the page and let the advisor inspect it before choosing
            # clicks/typing.  The original proposed steps remain in the audit
            # record and approval preview.
            execution_args["steps"] = [{"action": "screenshot"}]
        res = perform_act(
            kind=proposal["kind"],
            args=execution_args,
            allowed_domains=allowed,
            tenant=tenant,
            actuator=actuator,
            audit=audit,
            include_screenshot=True,
            resolve_secret=resolve_secret_for_live_page,
        )
        verification = asyncio.run(verify(
            proposal["url"], proposal["steps"],
            page_text=res.get("summary", "") or "",
            allowed_domains=allowed,
        ))
        outcome = _act_outcome(res)
        marker_only = (
            len(execution_args.get("steps") or []) == 1
            and execution_args["steps"][0].get("action") == "screenshot"
        )
        execution_phase = "started" if marker_only else "completed"
        audit.append(
            actor=tenant, action="act_" + outcome, tenant=tenant,
            subject=proposal["kind"],
            detail={
                "proposal_id": proposal_id,
                "status": res["status"],
                "verify_verdict": verification.get("verdict"),
                "verify_confidence": verification.get("confidence"),
            },
        )
        store.update_status(
            proposal_id, outcome,
            result_detail={
                "act_result": _result_detail(res),
                "verification": verification,
            },
        )
        if keep_browser and mission:
            mission_id = mission["mission_id"]
            mission_domain = mission.get("domain") or proposal.get("domain") or urlparse(proposal["url"]).netloc
            if hasattr(actuator, "storage_state") and callable(actuator.storage_state):
                try:
                    state_bytes = actuator.storage_state()
                    if state_bytes:
                        state_rec = vault.store_browser_state(
                            tenant, f"{mission_domain} browser session", state_bytes,
                            [mission_domain], existing_ref=mission.get("state_vault_ref"),
                        )
                        vault_ref = state_rec.get("vault_ref")
                        if vault_ref:
                            store.set_mission_state_ref(mission_id, tenant, vault_ref)
                            mission["state_vault_ref"] = vault_ref
                except Exception as exc:
                    print(f" [CONCIERGE STATE] initial mission snapshot failed: {exc}", flush=True)
            AGENTIC_BROWSER_SESSIONS[mission_id] = actuator
        return {
            "ok": True, "outcome": outcome, "res": res,
            "verification": verification, "mission": mission,
            "execution_phase": execution_phase,
        }
    except Exception as exc:  # noqa: BLE001 — surface, roll back, audit
        err_shot = None
        try:
            err_shot = actuator.screenshot()
        except Exception:
            pass
        audit.append(
            actor=tenant, action="act_error", tenant=tenant,
            subject=proposal["kind"],
            detail={"proposal_id": proposal_id, "error": str(exc)[:300]},
        )
        store.update_status(proposal_id, "rolled_back",
                            result_detail={"error": str(exc)[:300]})
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "screenshot_png": err_shot,
        }
    finally:
        if not (keep_browser and 'res' in locals() and res.get("status") == "ok" and mission):
            actuator.close()


def agentic_browser_step(
    tenant: str,
    mission_id: str,
    action: str = "observe",
    selector: str = "",
    value: str = "",
    vault_ref: Optional[str] = None,
    url: str = "",
) -> Dict[str, Any]:
    """Perform one observed browser interaction inside an approved mission."""
    store = ApprovalStore()
    active = store.active_missions_for_tenant(tenant, limit=50)
    requested = str(mission_id or "").strip()
    missions = [m for m in active if m["mission_id"] == requested]
    if not missions and requested:
        # The model frequently passes a *proposal* id (the only id it sees in
        # concierge_act_status) where a mission id is required. Resolve it to
        # the mission that proposal created rather than failing the whole
        # mission on an id mix-up.
        missions = [m for m in active if m.get("origin_proposal_id") == requested]
    if not missions and len(active) == 1:
        # A single live mission for this tenant is unambiguous: adopt it so a
        # missing/misremembered id (a fresh turn, a model that never saw the
        # mission context) does not stall a legitimately approved session.
        missions = list(active)
    if not missions:
        if active:
            ids = ", ".join(m["mission_id"] for m in active)
            raise ValueError(
                "browser mission " + repr(requested) + " is not active; the tenant's "
                "active mission id(s) are: " + ids + " (a proposal_id is NOT a "
                "mission_id — pass one of these)"
            )
        raise ValueError(
            "no active browser mission for this user. Start one by calling "
            "request_concierge_action with kind='fill_form' and args={'domain': '<site>'} "
            "(the user approves it once), then observe the live page. A mission_id is "
            "neither a proposal_id nor a vault_ref such as 'profile_…'."
        )
    # Canonicalize onto the resolved mission so the actuator registry and the
    # persisted state ref are keyed by the real id, not whatever the model sent.
    mission_id = missions[0]["mission_id"]
    actuator = AGENTIC_BROWSER_SESSIONS.get(mission_id)
    shared_cdp = _shared_cdp_url(tenant, missions[0]["url"])
    # A mission may have started on Browserless before the user opened the
    # VNC login browser. Rebind it once a live headed session appears, so the
    # next model turn is visible in the user's existing VNC window.
    if actuator is not None and shared_cdp and getattr(actuator, "_cdp_url", None) != shared_cdp:
        actuator.close()
        AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)
        actuator = None
    if actuator is None:
        # Rehydrate a fresh Browserless connection from the tenant-scoped,
        # encrypted storage state after a bot restart.
        state_ref = missions[0].get("state_vault_ref")
        if not state_ref:
            raise ValueError("browser mission is no longer attached and has no saved session state")
        actuator = BrowserlessActuator(
            missions[0]["url"], allowed_domains=[missions[0]["domain"]],
            cdp_url=shared_cdp,
        )
        try:
            actuator.restore_storage_state(Vault(_DB).resolve_browser_state(tenant, state_ref))
        except Exception:
            actuator.close()
            raise ValueError("saved browser session could not be restored; request a new approval")
        AGENTIC_BROWSER_SESSIONS[mission_id] = actuator
    action = str(action or "observe").lower()
    selector = "" if selector is None else selector
    # `hint` is the control's most-stable selector (when the model names a
    # control by its observed id), fed to the credential resolver's field
    # matching so it can choose username/email versus password.
    hint = actuator._resolve_handle(selector)
    with actuator.operation_lock:
        if action in {"observe", "screenshot"}:
            pass
        elif action == "navigate":
            # `url` is the navigation target. A bare observation id (eN) or a
            # CSS selector accidentally passed here is NOT a path — treating it
            # as one produced "https://<domain>/e1" and a 404, which then left
            # the model acting on a stale handle. Fail loudly instead.
            target = url or selector
            _sel_s = str(selector or "").strip()
            _looks_like_selector = bool(
                re.match(r"^(e\d+$|[.#\[])", _sel_s, re.IGNORECASE)
                or re.match(r"^[a-z0-9]+\s*\[", _sel_s, re.IGNORECASE)
            )
            if not str(url or "").strip() and _looks_like_selector:
                raise ValueError(
                    "navigate requires a url (http(s)://… or a /path); use click to act on a control"
                )
            actuator.navigate(_normalize_mission_navigation_url(
                target, missions[0]["url"], missions[0]["domain"]
            ))
        elif action == "click":
            # Pass the raw id through: `_locate` expands it against every
            # recorded candidate and the live fingerprint.
            actuator.click(selector)
        elif action == "type":
            # An explicit, non-empty value is authoritative: the user (or the
            # model relaying a value the user supplied in chat) chose it. The
            # stored credential is resolved only when no value is supplied
            # (an empty value means "fill the credential for this domain") or
            # when an explicit vault_ref is named. Resolving on the *secret*
            # flag even when a value was given broke generic logins: a secret
            # field on a site with no stored credential raised instead of
            # accepting the supplied value.
            if vault_ref:
                live_url = getattr(getattr(actuator, "_page", None), "url", "") or missions[0]["url"]
                value = _make_resolver(
                    tenant, Vault(_DB), live_url, field_hint=hint
                )(vault_ref)
            elif not value:
                # The generic field hint lets the resolver choose username/
                # email versus password without requiring the model to see
                # vault refs.
                live_url = getattr(getattr(actuator, "_page", None), "url", "") or missions[0]["url"]
                value = _make_resolver(
                    tenant, Vault(_DB), live_url, field_hint=hint
                )()
            actuator.type_text(selector, value)
        elif action == "scroll":
            actuator.scroll(value=value, selector=selector)
        else:
            raise ValueError("action must be observe, screenshot, navigate, click, type, or scroll")
        debug_shots = os.getenv("CONCIERGE_DEBUG_SCREENSHOTS", "0").lower() in {
            "1", "true", "yes", "on"
        }
        # Snapshot cookies/local storage after every turn. The raw state never
        # enters the model context or SQLite; only its encrypted Vault ref does.
        state_ref = missions[0].get("state_vault_ref")
        state_rec = Vault(_DB).store_browser_state(
            tenant, f"{missions[0]['domain']} browser session", actuator.storage_state(),
            [missions[0]["domain"]], existing_ref=state_ref,
        )
        if not state_ref:
            store.set_mission_state_ref(mission_id, tenant, state_rec["vault_ref"])

        def _sample_observation() -> "tuple[str, str]":
            """Interactive-element inventory (part 1) and page text (part 2).

            Part 1 lists every control the bot can act on, each addressed by a
            stable id, so the model never guesses a selector or the order of a
            form's fields.  Part 2 is the readable page text; a bot-check /
            human-verification wall is frequently rendered in a cross-origin
            child frame, so the frame URLs are sampled alongside the
            (frame-aware) page text.  Missing that made the bot report "page
            did not change" instead of naming the challenge.
            """
            controls = actuator.interactive_summary()
            text = (actuator.get_text() or "")[:4000]
            if controls:
                summary = (
                    f"{controls}\n\n[PAGE TEXT]\n{text}\n\n"
                    "[BROWSER PROGRESS] The page has already been observed. Choose one "
                    "control by its id (eN) and call click or type with "
                    "selector=\"eN\"; observing the same page again without an action is "
                    "not progress. For a credential field, use type with an empty value so "
                    "the executor can resolve the stored credential for this allowed domain."
                )[:6000]
            else:
                summary = text
            urls = ""
            live = getattr(actuator, "_page", None)
            if live is not None:
                try:
                    urls = " ".join(
                        str(getattr(f, "url", "") or "") for f in live.frames
                    )
                except Exception:
                    urls = ""
            return summary, urls

        summary, challenge_urls = _sample_observation()
        # A `type` action carries input the *user* supplied — a login field, or
        # a verification code the user pasted into chat. It is never a
        # bot-driven attempt to defeat a wall, so a challenge page must not
        # block it; otherwise the mission can never enter a code the user
        # provides and the login dead-ends. Every other action still hard-blocks
        # on a detected wall.
        blocked = action != "type" and browser_auth_challenge(summary, challenge_urls)
        if blocked:
            # A single sample can land while the page is still loading: an
            # empty/partial document or a frame mid-navigation can read as a
            # wall that is gone a moment later, which surfaces to the user as a
            # spurious "complete the verification manually". Re-sample once
            # after the page settles and report the block only if it persists —
            # a real wall is still there, a transient is not.
            time.sleep(0.8)
            settled_summary, settled_urls = _sample_observation()
            if not browser_auth_challenge(settled_summary, settled_urls):
                summary, blocked = settled_summary, False
        page_stage = actuator.page_stage()
        page_url = getattr(actuator, "_page", None).url if getattr(actuator, "_page", None) else missions[0]["url"]
        internal_shots = os.getenv("CONCIERGE_INTERNAL_SCREENSHOTS", "0").lower() in {
            "1", "true", "yes", "on"
        }
        needs_visual_recovery = internal_shots and len(summary.strip()) < 160
        user_requested_screenshot = action == "screenshot"
        shot = (
            actuator.screenshot(fast=not user_requested_screenshot)
            if user_requested_screenshot or debug_shots or needs_visual_recovery
            else None
        )
        return {
            "mission_id": mission_id,
            "action": action,
            "url": page_url,
            "summary": summary,
            "page_stage": page_stage,
            "blocked": blocked,
            "block_reason": "user authentication/verification is required" if blocked else "",
            "screenshot_png": shot,
            "screenshot_user_requested": user_requested_screenshot,
        }


def _result_detail(res: Dict[str, Any]) -> Dict[str, Any]:
    """Persistable (JSON-safe) view of an act result: no raw screenshot bytes.

    The screenshot blob is kept ephemeral (DM attachment path only) and is
    excluded from the SQLite ``result_detail`` so the status transition can
    never crash the approval callback on bytes serialization.
    """
    safe = dict(res)
    safe.pop("screenshot_png", None)
    return safe


__all__ = ["ActionApprovalView", "BrowserlessActuator", "agentic_browser_step", "browser_auth_challenge", "execute_approved_act"]
