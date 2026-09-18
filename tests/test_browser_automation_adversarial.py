"""Adversarial stress-test suite for BrowserlessActuator and browser automation.

Challenge Dimensions:
1. Multi-step login flows with delayed DOM transitions / unexpected element delays.
2. Dynamic element waiting and selector resolution resilience on unescaped numeric IDs,
   pseudo-selectors, and missing elements.
3. Iframe traversal for child iframe auth widgets, nested iframes, and frame switching.
4. Premature click / observation loop desynchronization resistance.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import patch

from playwright._impl._errors import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from src.bot.approval_views import (
    BrowserlessActuator,
    agentic_browser_step,
    browser_auth_challenge,
    _is_invisible_challenge_frame,
    _strip_recaptcha_widget_noise,
    AGENTIC_BROWSER_SESSIONS,
)
from src.services.concierge.approval_store import ApprovalStore
from src.security.vault import Vault
from src.services.concierge import browser_driver


CDP_TEST_URL = "ws://localhost:3002/"
MASTER_KEY = "test-adversarial-master-key-32bytes!"


@pytest.fixture(autouse=True)
def mock_public_host_and_env():
    """Ensure test domains pass SSRF gate and browserless connects to test container."""
    orig_url = browser_driver.BROWSERLESS_URL
    browser_driver.BROWSERLESS_URL = "http://localhost:3002"
    with patch("src.services.concierge.browser_gate.is_public_host", return_value=True):
        yield
    browser_driver.BROWSERLESS_URL = orig_url


# ============================================================================
# 1. Multi-Step Login Flows with Delayed DOM Transitions
# ============================================================================


def test_adversarial_multistep_delayed_dom_transition():
    """Stress-test identifier-first flow where step 2 is delayed by asynchronous server validation (800ms)."""
    domain = "adv-multistep.test"
    url = f"https://{domain}/login"

    html_content = """<!DOCTYPE html>
    <html>
    <head><title>Delayed MultiStep Login</title></head>
    <body>
      <main id="app">
        <div id="step-1">
          <h2>Step 1: Identify</h2>
          <input id="user-input" type="text" placeholder="Username" />
          <button id="btn-next" type="button">Next Step</button>
          <div id="loading" style="display:none;">Validating user account...</div>
        </div>
        <div id="step-2" style="display:none;">
          <h2>Step 2: Authenticate</h2>
          <input id="pass-input" type="password" placeholder="Password" />
          <button id="btn-auth" type="button">Sign In</button>
        </div>
        <div id="dashboard" style="display:none;">
          <h1>Welcome Authorized User</h1>
        </div>
      </main>
      <script>
        document.getElementById('btn-next').addEventListener('click', () => {
          const u = document.getElementById('user-input').value;
          if (u === 'challenger_user') {
            document.getElementById('loading').style.display = 'block';
            // Simulate 800ms server lookup before rendering step 2
            setTimeout(() => {
              document.getElementById('step-1').style.display = 'none';
              document.getElementById('step-2').style.display = 'block';
            }, 800);
          }
        });
        document.getElementById('btn-auth').addEventListener('click', () => {
          const p = document.getElementById('pass-input').value;
          if (p === 'SecureDelayedPass999') {
            document.getElementById('step-2').style.display = 'none';
            document.getElementById('dashboard').style.display = 'block';
          }
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html_content)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # Step 1: Type user and click next
        act.type_text("#user-input", "challenger_user")
        act.click("#btn-next")

        # Step 2: The password input appears after 800ms delay.
        # Actuator type_text must find it without timing out or failing.
        act.type_text("#pass-input", "SecureDelayedPass999")
        act.click("#btn-auth")

        text = act.get_text()
        assert "Welcome Authorized User" in text
    finally:
        act.close()


def test_adversarial_multistep_dom_replacement():
    """Step 1 replaces entire DOM subtree via innerHTML with 600ms delay."""
    domain = "adv-replace.test"
    url = f"https://{domain}/signin"

    html_content = """<!DOCTYPE html>
    <html>
    <body>
      <div id="root">
        <form id="id-form">
          <input id="user-email" type="email" />
          <button id="submit-email" type="button">Continue</button>
        </form>
      </div>
      <script>
        document.getElementById('submit-email').addEventListener('click', () => {
          const email = document.getElementById('user-email').value;
          document.getElementById('root').innerHTML = '<p id="spinner">Checking credentials...</p>';
          setTimeout(() => {
            document.getElementById('root').innerHTML = `
              <form id="pass-form">
                <input id="user-password" type="password" />
                <button id="submit-pass" type="button">Log In</button>
              </form>
            `;
            document.getElementById('submit-pass').addEventListener('click', () => {
              document.getElementById('root').innerHTML = '<h2>Access Granted</h2>';
            });
          }, 600);
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html_content)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        act.type_text("#user-email", "adv@replace.test")
        act.click("#submit-email")

        # Step 2: innerHTML was wiped and recreated
        act.type_text("#user-password", "secret")
        act.click("#submit-pass")

        text = act.get_text()
        assert "Access Granted" in text
    finally:
        act.close()


# ============================================================================
# 2. Selector Resolution Resilience: Numeric IDs, Pseudo-selectors, Missing
# ============================================================================


def test_adversarial_numeric_ids_and_special_attributes():
    """Verify actuator handles numeric IDs (#1001, #0, #999-code) in type and click."""
    domain = "adv-numeric.test"
    url = f"https://{domain}/form"

    html_content = """<!DOCTYPE html>
    <html>
    <body>
      <main>
        <!-- Pure numeric IDs -->
        <input id="1001" type="text" placeholder="Numeric ID Input" />
        <input id="0" type="password" placeholder="Zero ID Input" />
        <button id="2002" type="button">Submit Pure Numeric</button>

        <!-- Numeric prefix IDs -->
        <input id="999-code" type="text" placeholder="Prefixed Numeric" />
        <button id="888_action" type="button">Trigger Action</button>

        <div id="result">Initial State</div>
      </main>
      <script>
        document.getElementById('2002').addEventListener('click', () => {
          const v1 = document.getElementById('1001').value;
          const v0 = document.getElementById('0').value;
          document.getElementById('result').innerText = `Pure Numeric: ${v1}|${v0}`;
        });
        document.getElementById('888_action').addEventListener('click', () => {
          const code = document.getElementById('999-code').value;
          document.getElementById('result').innerText = `Prefixed: ${code}`;
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html_content)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # Pure numeric IDs: CSS #1001 is invalid, but actuator rewrites to [id="1001"]
        act.type_text("#1001", "val_1001")
        act.type_text("#0", "pass_zero")
        act.click("#2002")

        text = act.get_text()
        assert "Pure Numeric: val_1001|pass_zero" in text

        # Numeric-prefixed IDs
        act.type_text("#999-code", "secret_code_77")
        act.click("#888_action")

        text2 = act.get_text()
        assert "Prefixed: secret_code_77" in text2
    finally:
        act.close()


def test_adversarial_pseudo_selectors_and_syntax_error_vulnerability():
    """EMPIRICAL VULNERABILITY REPRO:

    In click(selector), missing/invalid selectors trigger a clean ValueError via semantic fallback.
    In type_text(selector), when _find_selector returns None, it falls back to target = selector
    and directly executes target_scope.fill(selector), which crashes with an unhandled Playwright
    SyntaxError instead of raising a clean ValueError!
    """
    domain = "adv-pseudo.test"
    url = f"https://{domain}/selectors"

    html_content = """<!DOCTYPE html>
    <html>
    <body>
      <div>
        <input type="text" name="username" id="valid-user" />
        <button id="real-submit" type="submit">Submit Form</button>
      </div>
      <div id="output">Waiting</div>
    </body>
    </html>
    """

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL, timeout_ms=2000)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html_content)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # Contrast 1: click() on invalid/missing selector raises clean ValueError
        with pytest.raises(ValueError, match="click target not found or not visible"):
            act.click("button:invalid-pseudo-xyz")

        # In remediated version: type_text() raises clean ValueError matching click()
        with pytest.raises(ValueError, match="type target not found or not visible"):
            act.type_text("input:invalid-pseudo-xyz", "testuser")
    finally:
        act.close()


def test_adversarial_missing_element_clean_failure():
    """Actuator click must fail cleanly on missing elements with descriptive ValueError."""
    domain = "adv-missing.test"
    url = f"https://{domain}/empty"

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body="<html><body><h1>Empty</h1></body></html>")
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # Missing click target must raise ValueError cleanly
        with pytest.raises(ValueError, match="click target not found or not visible"):
            act.click("#nonexistent-btn-999")

        # Ambiguous text selector must fail closed
        with pytest.raises(ValueError, match="ambiguous click target"):
            act.click("text=NonexistentTextTarget")
    finally:
        act.close()


# ============================================================================
# 3. Iframe Traversal: Nested Iframes, Cross-Domain, and Frame Switching
# ============================================================================


def test_adversarial_nested_iframes_traversal():
    """Actuator should locate and interact with inputs inside deeply nested iframes (2 levels).

    KNOWN DEFECT: In approval_views.py:423, frames = [page.main_frame] + ... is captured once.
    The inner iframe (created dynamically when mid-frame loads) is missing during attempt 0,
    and return None at line 489 prevents retries from refreshing page.frames.
    """
    domain = "adv-nested-frame.test"
    top_url = f"https://{domain}/top"
    mid_url = f"https://{domain}/mid"
    inner_url = f"https://{domain}/inner"

    top_html = """<!DOCTYPE html>
    <html>
    <body>
      <h1>Top Level Window</h1>
      <iframe id="mid-frame" src="__MID_URL__"></iframe>
    </body>
    </html>
    """.replace("__MID_URL__", mid_url)

    mid_html = """<!DOCTYPE html>
    <html>
    <body>
      <h2>Mid Level Frame</h2>
      <iframe id="inner-frame" src="__INNER_URL__"></iframe>
    </body>
    </html>
    """.replace("__INNER_URL__", inner_url)

    inner_html = """<!DOCTYPE html>
    <html>
    <body>
      <h3>Deeply Nested Auth</h3>
      <input id="nested-token" type="password" placeholder="Nested Token" />
      <button id="nested-btn" type="button">Authorize Nested</button>
      <div id="nested-status">Pending</div>
      <script>
        document.getElementById('nested-btn').addEventListener('click', () => {
          const val = document.getElementById('nested-token').value;
          if (val === 'DeepToken42') {
            document.getElementById('nested-status').innerText = 'Authorized';
          }
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(top_url, allowed_domains=[domain], cdp_url=CDP_TEST_URL, timeout_ms=3000)
    act._ensure(navigate=False)
    page = act._page

    async def setup_routes():
        async def handle(route):
            url = route.request.url
            if url.startswith(top_url):
                await route.fulfill(status=200, content_type="text/html", body=top_html)
            elif url.startswith(mid_url):
                await route.fulfill(status=200, content_type="text/html", body=mid_html)
            elif url.startswith(inner_url):
                await route.fulfill(status=200, content_type="text/html", body=inner_html)
            else:
                await route.continue_()
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_routes())
        act.navigate(top_url)

        # Type into deeply nested iframe input
        act.type_text("#nested-token", "DeepToken42")
        act.click("#nested-btn")

        # Check inner frame status
        assert act._target_frame is not None
        status = act._loop.run_until_complete(
            act._target_frame.inner_text("#nested-status")
        )
        assert status == "Authorized"
    finally:
        act.close()


def test_adversarial_frame_switching_back_to_parent():
    """Interacting with an iframe must not trap the actuator from interacting with the parent frame next."""
    domain = "adv-switch-frame.test"
    top_url = f"https://{domain}/parent"
    child_url = f"https://{domain}/child"

    top_html = """<!DOCTYPE html>
    <html>
    <body>
      <h1>Parent Form</h1>
      <iframe id="child-frame" src="__CHILD_URL__"></iframe>
      <div id="parent-controls">
        <button id="parent-finish" type="button">Finish In Parent</button>
        <div id="parent-status">Initial</div>
      </div>
      <script>
        document.getElementById('parent-finish').addEventListener('click', () => {
          document.getElementById('parent-status').innerText = 'Parent Completed';
        });
      </script>
    </body>
    </html>
    """.replace("__CHILD_URL__", child_url)

    child_html = """<!DOCTYPE html>
    <html>
    <body>
      <input id="child-input" type="text" />
      <button id="child-button" type="button">Child Action</button>
      <div id="child-status">Child Ready</div>
      <script>
        document.getElementById('child-button').addEventListener('click', () => {
          document.getElementById('child-status').innerText = 'Child Done: ' + document.getElementById('child-input').value;
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(top_url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_routes():
        async def handle(route):
            url = route.request.url
            if url.startswith(top_url):
                await route.fulfill(status=200, content_type="text/html", body=top_html)
            elif url.startswith(child_url):
                await route.fulfill(status=200, content_type="text/html", body=child_html)
            else:
                await route.continue_()
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_routes())
        act.navigate(top_url)

        # 1. Interact in child frame
        act.type_text("#child-input", "widget-data")
        act.click("#child-button")

        child_frame = act._target_frame
        assert child_frame != page.main_frame
        child_stat = act._loop.run_until_complete(child_frame.inner_text("#child-status"))
        assert "Child Done: widget-data" in child_stat

        # 2. Crucial test: Now interact with an element in the PARENT frame
        # Actuator must switch _target_frame back to main_frame or resolve in parent
        act.click("#parent-finish")

        parent_stat = act._loop.run_until_complete(page.main_frame.inner_text("#parent-status"))
        assert parent_stat == "Parent Completed"
    finally:
        act.close()


# ============================================================================
# 4. Premature Click / Observation Loop Desynchronization Resistance
# ============================================================================


def test_adversarial_click_async_network_settle():
    """Click triggering an AJAX fetch with 700ms latency must settle before click() returns."""
    domain = "adv-settle.test"
    page_url = f"https://{domain}/async-login"
    api_url = f"https://{domain}/api/do-auth"

    page_html = """<!DOCTYPE html>
    <html>
    <body>
      <h1>Async Auth Page</h1>
      <button id="ajax-login-btn">Log In With Server Check</button>
      <div id="auth-result">Unauthenticated</div>
      <script>
        document.getElementById('ajax-login-btn').addEventListener('click', async () => {
          document.getElementById('auth-result').innerText = 'Authenticating...';
          const resp = await fetch('__API_URL__', { method: 'POST' });
          const data = await resp.json();
          document.getElementById('auth-result').innerText = data.result;
        });
      </script>
    </body>
    </html>
    """.replace("__API_URL__", api_url)

    act = BrowserlessActuator(page_url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_routes():
        async def handle(route):
            req_url = route.request.url
            if req_url.startswith(page_url):
                await route.fulfill(status=200, content_type="text/html", body=page_html)
            elif req_url.startswith(api_url):
                # 700ms simulated server latency
                await asyncio.sleep(0.7)
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"result": "Session Established Successfully"}),
                )
            else:
                await route.continue_()
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_routes())
        act.navigate(page_url)

        # Click the button which fires the delayed fetch
        act.click("#ajax-login-btn")

        # Actuator.click() must have waited for networkidle/settle
        # Therefore, immediately after click(), text must reflect the resolved response!
        text = act.get_text()
        assert "Session Established Successfully" in text
        assert "Authenticating..." not in text
    finally:
        act.close()


def test_adversarial_observation_loop_no_false_block(tmp_path, monkeypatch):
    """Simulate the llm.py observation counter loop across multi-step login to verify no false blocks."""
    domain = "adv-loop.test"
    url = f"https://{domain}/portal"
    api_url = f"https://{domain}/api/verify-step"

    db_path = str(tmp_path / "adversarial_loop.db")
    store = ApprovalStore(db_path)
    vault = Vault(db_path, master_key=MASTER_KEY)

    html_content = """<!DOCTYPE html>
    <html>
    <body>
      <div id="step-container">
        <div id="step-user">
          <input id="user-field" type="text" />
          <button id="user-submit" type="button">Next</button>
        </div>
      </div>
      <script>
        document.getElementById('user-submit').addEventListener('click', async () => {
          const u = document.getElementById('user-field').value;
          const resp = await fetch('__API_URL__', { method: 'POST' });
          const res = await resp.json();
          document.getElementById('step-container').innerHTML = `
            <div id="step-pwd">
              <p>Welcome ${res.user}</p>
              <input id="pwd-field" type="password" />
              <button id="pwd-submit" type="button">Sign In</button>
            </div>
          `;
        });
      </script>
    </body>
    </html>
    """.replace("__API_URL__", api_url)

    tenant = "user:adversary"
    p_id = store.create(
        tenant=tenant,
        uid="u1",
        kind="fill_form",
        args={"domain": domain, "steps": [{"action": "observe"}]},
        url=url,
        steps=[{"action": "observe"}],
        allowed_domains=[domain],
    )
    store.claim(p_id)
    mission = store.grant_mission(p_id)
    mission_id = mission["mission_id"]

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_routes():
        async def handle(route):
            req_url = route.request.url
            if req_url.startswith(url):
                await route.fulfill(status=200, content_type="text/html", body=html_content)
            elif req_url.startswith(api_url):
                await asyncio.sleep(0.4)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"user": "Alice"}))
            else:
                await route.continue_()
        await page.route(f"https://{domain}/**", handle)

    act._loop.run_until_complete(setup_routes())
    act.navigate(url)

    AGENTIC_BROWSER_SESSIONS[mission_id] = act
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: store)
    monkeypatch.setattr("src.bot.approval_views.Vault", lambda db=None: vault)

    try:
        # Simulate llm.py loop tracking
        browser_observation_counts = {}
        browser_action_observation_counts = {}

        def record_turn(step_res, action, selector=""):
            obs_key = json.dumps({"url": step_res.get("url"), "summary": step_res.get("summary")}, sort_keys=True)
            browser_observation_counts[obs_key] = browser_observation_counts.get(obs_key, 0) + 1

            act_key = json.dumps({
                "mission_id": step_res.get("mission_id"),
                "action": action,
                "selector": selector,
                "url": step_res.get("url"),
                "observation": obs_key,
            }, sort_keys=True)
            browser_action_observation_counts[act_key] = browser_action_observation_counts.get(act_key, 0) + 1

            # Check llm.py block conditions. The observation-repeat guard is
            # gated on the action being observe: a click/type/scroll legitimately
            # leaves the page text unchanged (typed values are not in innerText),
            # so blocking those would abort an ordinary login.
            if action == "click" and browser_action_observation_counts[act_key] >= 2:
                return True, "repeated click blocked"
            if action == "observe" and browser_observation_counts[obs_key] >= 2:
                return True, "repeated observation blocked"
            return False, ""

        # Turn 1: Observe initial page
        res1 = agentic_browser_step(tenant, mission_id, "observe")
        blocked, reason = record_turn(res1, "observe")
        assert not blocked

        # Turn 2: Type username
        res2 = agentic_browser_step(tenant, mission_id, "type", selector="#user-field", value="Alice")
        blocked, reason = record_turn(res2, "type", selector="#user-field")
        assert not blocked

        # Turn 3: Click Next
        res3 = agentic_browser_step(tenant, mission_id, "click", selector="#user-submit")
        blocked, reason = record_turn(res3, "click", selector="#user-submit")
        assert not blocked, f"Turn 3 blocked unexpectedly: {reason}"

        # Turn 4: Observe page after click — MUST have changed to password step
        assert "Welcome Alice" in res3["summary"]
        assert "pwd-field" in res3["summary"] or "pwd-submit" in res3["summary"]

        # Turn 5: Type password
        res4 = agentic_browser_step(tenant, mission_id, "type", selector="#pwd-field", value="Secret123")
        blocked, reason = record_turn(res4, "type", selector="#pwd-field")
        assert not blocked, f"Turn 5 blocked unexpectedly: {reason}"

    finally:
        act_saved = AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)
        if act_saved:
            act_saved.close()


# ============================================================================
# 5. Consent overlays and cross-origin iframe observation (regression)
# ============================================================================


def test_adversarial_consent_overlay_is_dismissed_then_click_proceeds():
    """A fixed cookie/consent overlay intercepts pointer events; the actuator
    must dismiss it and retry, not fail the click after a long timeout."""
    domain = "adv-consent.test"
    url = f"https://{domain}/home"

    html = """<!DOCTYPE html>
    <html><body>
      <div id="consent" style="position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:9999">
        <div style="position:absolute;bottom:0;width:100%;background:#fff;padding:16px">
          <button id="acceptAllButton" type="button"
                  onclick="document.getElementById('consent').remove()">Accept all</button>
        </div>
      </div>
      <main>
        <h1>Home</h1>
        <a id="signin" href="/signin" style="display:block;padding:40px;background:#eee">Log In</a>
      </main>
    </body></html>"""

    act = BrowserlessActuator(
        url, allowed_domains=[domain], cdp_url=CDP_TEST_URL, timeout_ms=5000
    )
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # The consent button is visible/clickable; the link is underneath the
        # overlay and a raw Playwright click would time out.
        assert act.element_visible("#acceptAllButton")

        act.click("#signin")  # must self-heal (dismiss consent) and proceed
        assert act._page.url.rstrip("/").endswith("/signin")
    finally:
        act.close()


def test_adversarial_iframe_content_is_observed_and_challenge_detected():
    """Login forms / bot challenges rendered in a cross-origin child iframe must
    be visible to get_text/interactive_summary/page_stage and to the challenge
    detector, even when the main document body is empty."""
    domain = "adv-frame.test"
    widget_host = "widget.adv-frame.test"
    url = f"https://{domain}/home"

    main_html = """<!DOCTYPE html><html><body>
      <h1>Loading…</h1>
      <iframe src="https://widget.adv-frame.test/form"
              style="width:640px;height:420px"></iframe>
    </body></html>"""

    widget_html = """<!DOCTYPE html><html><body>
      <main>
        <h1>Confirm you're human</h1>
        <input id="user" type="email" placeholder="Email" />
        <input id="pass" type="password" placeholder="Password" />
        <button id="go" type="button">Sign In</button>
      </main>
    </body></html>"""

    act = BrowserlessActuator(
        url, allowed_domains=[domain, widget_host], cdp_url=CDP_TEST_URL, timeout_ms=8000
    )
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            target = route.request.url
            body = widget_html if widget_host in target else main_html
            await route.fulfill(status=200, content_type="text/html", body=body)
        await page.route("**/*", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)
        act._loop.run_until_complete(asyncio.sleep(0.8))

        text = act.get_text()
        assert "EMBEDDED FRAME" in text, f"frame not surfaced in text: {text!r}"
        assert "human" in text.lower()

        summary = act.interactive_summary()
        assert "#pass" in summary or "password" in summary.lower(), summary

        # A password field living only in the child frame must drive the stage.
        assert act.page_stage() == "password"

        frame_urls = " ".join(f.url for f in act._page.frames)
        assert browser_auth_challenge(text, frame_urls) is True
    finally:
        act.close()


# ============================================================================
# 6. Secret hygiene and stale-selector fallback (regression)
# ============================================================================


def test_adversarial_secret_value_is_not_leaked_in_summary():
    """A typed password/OTP must never appear in interactive_summary: values
    are resolved from the vault and must not be echoed to the model."""
    domain = "adv-secret.test"
    url = f"https://{domain}/login"

    html = """<!DOCTYPE html><html><body>
      <main>
        <input id="user" type="email" placeholder="Email" />
        <input id="pass" type="password" placeholder="Password" />
        <input id="otp" type="text" name="one_time_code" placeholder="" />
        <button id="go" type="submit">Sign In</button>
      </main>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        act.type_text("#user", "person@adv-secret.test")
        act.type_text("#pass", "S3cr3tPa55word!")
        act.type_text("#otp", "918273")

        summary = act.interactive_summary()
        assert "S3cr3tPa55word!" not in summary, "password leaked into summary"
        assert "918273" not in summary, "OTP leaked into summary"
        # The selector must still be surfaced so the model can target the field.
        assert "pass" in summary.lower()
    finally:
        act.close()


def test_adversarial_hidden_stale_selector_falls_back_to_visible_submit():
    """A selector that exists but is hidden must not be returned as the click
    target (it would time out); the actuator must fall back to the visible
    submit control for a semantic continue/submit request."""
    domain = "adv-stale.test"
    url = f"https://{domain}/checkout"

    html = """<!DOCTYPE html><html><body>
      <main>
        <h1>Checkout</h1>
        <button id="continueButton" type="button"
                style="display:none">Continue</button>
        <form action="/done" method="get">
          <button id="real-submit" type="submit">Continue</button>
        </form>
      </main>
    </body></html>"""

    act = BrowserlessActuator(
        url, allowed_domains=[domain], cdp_url=CDP_TEST_URL, timeout_ms=6000
    )
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # The hidden stale id must not be selected; the visible submit is used.
        act.click("#continueButton")
        assert "/done" in act._page.url
    finally:
        act.close()


# ============================================================================
# 7. Challenge detection must ignore invisible reCAPTCHA widgets (regression)
# ============================================================================


def test_adversarial_invisible_recaptcha_url_is_not_a_challenge():
    """PayPal embeds invisible reCAPTCHA Enterprise v3 on ordinary pages; its
    frame URL contains "captcha" and must NOT be read as a verification wall."""
    recaptcha_urls = (
        "https://www.paypalobjects.com/web/res/320/6fb5de4b/recaptcha/grcenterprise_v3.html "
        "https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=x&size=invisible "
        "https://www.recaptcha.net/recaptcha/enterprise/bframe?hl=en"
    )
    assert browser_auth_challenge("Pay, Send and Save Money with PayPal", recaptcha_urls) is False
    # A bare recaptcha URL with no page text is still not a challenge.
    assert browser_auth_challenge("", recaptcha_urls) is False
    # get_text embeds child-frame URLs *inside* the summary text; that must not
    # trip the detector either (the real PayPal false positive).
    summary = (
        "[MAIN CONTENT]\nPay, send, and save smarter\n\n"
        "[EMBEDDED FRAME https://www.paypalobjects.com/web/res/320/x/recaptcha/grcenterprise_v3.html]\n"
        "[EMBEDDED FRAME https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&size=invisible]"
    )
    assert browser_auth_challenge(summary, "") is False
    # The widget also injects scaffolding *text* into the page, which the
    # frame-aware get_text lifts into the summary. Both fragments contain the
    # substring "captcha" yet are not a wall (the other half of the live
    # PayPal false positive).
    widget_text = (
        "[NAVIGATION]\nPersonal\nBusiness\n\n"
        '[EMBEDDED FRAME ]\nrecaptcha.frame.Main.init("[\\x22finput\\x22,null,[\\x22conf\\x22]]");\n'
        '[EMBEDDED FRAME ]\nrecaptcha.anchor.Main.init("[\\x22ainput\\x22]");\n'
        "You are verifiedprotected by reCAPTCHA"
    )
    assert browser_auth_challenge(widget_text, "") is False


def test_adversarial_real_challenge_is_still_detected():
    """A genuine wall (DataDome interstitial URL, or visible challenge text)
    must still be reported."""
    assert browser_auth_challenge("", "https://geo.ddc.paypal.com/captcha") is True
    assert browser_auth_challenge("Confirm you're human", "") is True
    assert browser_auth_challenge("Please complete the security check", "") is True


def test_adversarial_widget_noise_is_stripped_from_observation():
    """The invisible widget's bootstrap JS and badge are removed from the text
    fed to the model, so the observation is not half-consumed by scaffolding."""
    noisy = (
        '[EMBEDDED FRAME ]\nrecaptcha.frame.Main.init("[\\x22finput\\x22,null,[\\x22conf\\x22]]");\n'
        "Real page content\nprotected by reCAPTCHA"
    )
    clean = _strip_recaptcha_widget_noise(noisy)
    assert "captcha" not in clean.lower()
    assert "Real page content" in clean


def test_adversarial_invisible_challenge_frames_are_skipped_by_url():
    """Only invisible/enterprise widget frames are skipped; a *visible*
    reCAPTCHA checkbox frame must still be surfaced to the model."""
    assert _is_invisible_challenge_frame(
        "https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=x&size=invisible"
    ) is True
    assert _is_invisible_challenge_frame(
        "https://www.paypalobjects.com/web/res/320/x/recaptcha/grcenterprise_v3.html"
    ) is True
    assert _is_invisible_challenge_frame("https://www.google.com/recaptcha/api2/anchor?k=x") is False
    assert _is_invisible_challenge_frame("") is False


# ============================================================================
# 8. DOM inventory: forms + stable element ids (no selector guessing)
# ============================================================================


def test_adversarial_inventory_groups_form_and_numbers_controls():
    """Observe must enumerate every control with a stable id (eN), group the
    form's fields under its [FORM], and keep secret fields value-free."""
    domain = "adv-inventory.test"
    url = f"https://{domain}/login"

    html = """<!DOCTYPE html><html><body>
      <main>
        <h1>Sign in</h1>
        <form action="/session" method="post" name="login">
          <input id="email" type="email" name="email" placeholder="Email" />
          <input id="pw" type="password" name="password" placeholder="Password" />
          <button type="submit">Sign in</button>
        </form>
        <a href="/reset">Forgot password?</a>
      </main>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        summary = act.interactive_summary()
        assert "[FORM action=/session]" in summary, summary
        assert "[OTHER CONTROLS]" in summary
        # Every control is numbered and addressable by id.
        for handle in ("e1", "e2", "e3", "e4"):
            assert handle in summary, summary
        # The form's fields carry their type/name and a fallback selector.
        assert 'type=email' in summary
        assert 'name="password"' in summary
        assert "SECRET: type with an empty value" in summary
        # Password value never leaks.
        assert "hunter2" not in summary
    finally:
        act.close()


def test_adversarial_act_by_element_id_resolves_tagged_control():
    """A bare observation id (eN) must click/type the exact element the observe
    step numbered, without the model writing any CSS selector."""
    domain = "adv-handle.test"
    url = f"https://{domain}/login"

    html = """<!DOCTYPE html><html><body>
      <main>
        <form action="/session" method="get" name="login">
          <input id="email" type="email" name="email" placeholder="Email" />
          <input id="pw" type="password" name="password" placeholder="Password" />
          <button type="submit">Sign in</button>
        </form>
      </main>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        summary = act.interactive_summary()
        # The email field is e1, the password e2, the submit e3.
        assert "e1 <input type=email>" in summary, summary
        assert "e2 <input type=password>" in summary, summary
        assert 'e3 <button type=submit>' in summary, summary

        # Handle-only actions resolve to the field the observation named.
        act.type_text("e1", "person@adv-handle.test")
        act.type_text("e2", "hunter2")
        assert act._loop.run_until_complete(page.input_value("#email")) == "person@adv-handle.test"
        assert act._loop.run_until_complete(page.input_value("#pw")) == "hunter2"
        assert act.element_visible("e1") is True

        act.click("e3")
        assert "/session" in act._page.url
    finally:
        act.close()


def test_adversarial_handle_resolution_is_exact_and_passes_through_css():
    """``eN`` maps to the recorded selector; a real selector is untouched so
    the existing CSS-selector contract still works."""
    act = BrowserlessActuator("https://x.test/", allowed_domains=["x.test"], cdp_url=CDP_TEST_URL)
    # With no observation record, an id falls back to the live tag attribute.
    assert act._resolve_handle("e3") == '[data-concierge-handle="e3"]'
    assert act._handle_record("e3") is None
    # With a record, the id resolves to the recorded (most-stable) selector.
    act._handles = {"e3": {"handle": "e3", "selector": "#email", "selectors": ["#email"]}}
    assert act._resolve_handle("e3") == "#email"
    assert act._handle_record("e3")["selector"] == "#email"
    # Anything that is not an observed id passes through untouched.
    assert act._resolve_handle("#email") == "#email"
    assert act._resolve_handle("text=Sign in") == "text=Sign in"
    assert act._handle_record("#email") is None
    assert act._resolve_handle("") == ""
    act.close()


def test_adversarial_explicit_value_on_secret_field_is_honored(tmp_path, monkeypatch):
    """A secret-flagged field must accept a value the caller supplies instead
    of being forced through vault resolution (which raises when the domain has
    no stored credential). An empty value still means "resolve from vault"."""
    domain = "adv-explicit.test"
    url = f"https://{domain}/login"
    db_path = str(tmp_path / "adv_explicit.db")
    store = ApprovalStore(db_path)
    vault = Vault(db_path, master_key=MASTER_KEY)

    html = """<!DOCTYPE html><html><body>
      <main><form action="/session" method="post">
        <input id="email" type="email" name="email" />
        <input id="pw" type="password" name="password" />
        <button type="submit">Sign in</button>
      </form></main>
    </body></html>"""

    tenant = "user:adv-explicit"
    p_id = store.create(
        tenant=tenant, uid="u1", kind="fill_form",
        args={"domain": domain, "steps": [{"action": "observe"}]},
        url=url, steps=[{"action": "observe"}], allowed_domains=[domain],
    )
    store.claim(p_id)
    mission_id = store.grant_mission(p_id)["mission_id"]

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    act._loop.run_until_complete(setup_route())
    act.navigate(url)
    AGENTIC_BROWSER_SESSIONS[mission_id] = act
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: store)
    monkeypatch.setattr("src.bot.approval_views.Vault", lambda db=None: vault)

    try:
        obs = agentic_browser_step(tenant, mission_id, "observe")
        assert "SECRET: type with an empty value" in obs["summary"], obs["summary"]

        # e1=email, e2=password, e3=submit. The explicit value on the secret
        # field must be typed verbatim (previously forced through the vault).
        agentic_browser_step(tenant, mission_id, "type", selector="e2", value="ExplicitPass1")
        assert act._loop.run_until_complete(page.input_value("#pw")) == "ExplicitPass1"

        # An empty value still routes through vault resolution, which has no
        # credential for this domain and must fail loudly rather than type blank.
        with pytest.raises(ValueError, match="no credential is available"):
            agentic_browser_step(tenant, mission_id, "type", selector="e2", value="")
    finally:
        act_saved = AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)
        if act_saved:
            act_saved.close()


def test_adversarial_choice_control_is_activated_despite_overlay():
    """A radio/checkbox whose real <input> is covered by an overlay (PayPal's
    styled 2FA picker, framework choice groups) must still be selected instead
    of hanging on Playwright's actionability check for 30s."""
    domain = "adv-choice.test"
    url = f"https://{domain}/2fa"

    html = """<!DOCTYPE html><html><body>
      <form>
        <h1>Two-step verification</h1>
        <label for="email-challenge-option">email</label>
        <input id="email-challenge-option" type="radio" name="selectedChallengeType" value="email" />
        <label for="sms-challenge-option">sms</label>
        <input id="sms-challenge-option" type="radio" name="selectedChallengeType" value="sms" />
        <div id="state">none</div>
      </form>
      <!-- transparent overlay: the raw radio is not the hit target -->
      <span style="position:absolute;inset:0;z-index:10"></span>
      <script>
        document.querySelectorAll('input[name=selectedChallengeType]')
          .forEach(el => el.addEventListener('change', () => {
            document.getElementById('state').textContent = el.value;
          }));
      </script>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL, timeout_ms=2500)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # Click by raw id: the overlay would time out a plain Playwright click.
        act.click("#email-challenge-option")
        assert act._loop.run_until_complete(
            page.evaluate("() => document.getElementById('email-challenge-option').checked")
        ) is True
        assert act._loop.run_until_complete(page.inner_text("#state")) == "email"

        # And by the observed element id, for the other choice.
        summary = act.interactive_summary()
        assert "radio" in summary
        sms_id = next(
            line.split()[0] for line in summary.splitlines()
            if "sms-challenge-option" in line or ('<input type=radio>' in line and '"sms"' in line)
        )
        act.type_text(sms_id, "")  # typing a choice selects it
        assert act._loop.run_until_complete(
            page.evaluate("() => document.getElementById('sms-challenge-option').checked")
        ) is True
        assert act._loop.run_until_complete(page.inner_text("#state")) == "sms"
    finally:
        act.close()


def test_adversarial_navigate_refuses_element_id_and_accepts_path(tmp_path, monkeypatch):
    """Passing a bare observation id as a navigate target must fail loudly, not
    build 'https://<domain>/e1'; a real path still navigates."""
    domain = "adv-nav.test"
    url = f"https://{domain}/home"
    db_path = str(tmp_path / "adv_nav.db")
    store = ApprovalStore(db_path)
    vault = Vault(db_path, master_key=MASTER_KEY)

    html = '<!DOCTYPE html><html><body><h1>Home</h1><a id="go" href="/next">Next</a></body></html>'

    tenant = "user:adv-nav"
    p_id = store.create(
        tenant=tenant, uid="u1", kind="fill_form",
        args={"domain": domain, "steps": [{"action": "observe"}]},
        url=url, steps=[{"action": "observe"}], allowed_domains=[domain],
    )
    store.claim(p_id)
    mission_id = store.grant_mission(p_id)["mission_id"]

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    act._loop.run_until_complete(setup_route())
    act.navigate(url)
    AGENTIC_BROWSER_SESSIONS[mission_id] = act
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: store)
    monkeypatch.setattr("src.bot.approval_views.Vault", lambda db=None: vault)

    try:
        with pytest.raises(ValueError, match="navigate requires a url"):
            agentic_browser_step(tenant, mission_id, "navigate", selector="e1")
        # A genuine path is still accepted.
        step = agentic_browser_step(tenant, mission_id, "navigate", url="/home")
        assert "Home" in step["summary"]
    finally:
        act_saved = AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)
        if act_saved:
            act_saved.close()


def test_adversarial_mission_id_resolved_from_proposal_id(tmp_path, monkeypatch):
    """The model only ever sees *proposal* ids in concierge_act_status, so it
    frequently passes one as the mission_id. That must resolve to the mission
    the proposal created instead of killing the whole mission with 'browser
    mission is not active'. A single live mission is also adopted when no id
    matches, and an unresolvable id with several live missions must name them."""
    domain = "adv-mission.test"
    url = f"https://{domain}/home"
    db_path = str(tmp_path / "adv_mission.db")
    store = ApprovalStore(db_path)
    vault = Vault(db_path, master_key=MASTER_KEY)

    html = '<!DOCTYPE html><html><body><h1>Dashboard</h1></body></html>'
    tenant = "user:adv-mission"
    p_id = store.create(
        tenant=tenant, uid="u1", kind="fill_form",
        args={"domain": domain, "steps": [{"action": "observe"}]},
        url=url, steps=[{"action": "observe"}], allowed_domains=[domain],
    )
    store.claim(p_id)
    real_mission_id = store.grant_mission(p_id)["mission_id"]

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    act._loop.run_until_complete(setup_route())
    act.navigate(url)
    AGENTIC_BROWSER_SESSIONS[real_mission_id] = act
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: store)
    monkeypatch.setattr("src.bot.approval_views.Vault", lambda db=None: vault)

    try:
        # The proposal id (not the mission id) must resolve via origin_proposal_id.
        step = agentic_browser_step(tenant, p_id, "observe")
        assert step["mission_id"] == real_mission_id
        assert "Dashboard" in step["summary"]

        # A stale/garbage id with exactly one live mission is adopted.
        step = agentic_browser_step(tenant, "deadbeefdeadbeef", "observe")
        assert step["mission_id"] == real_mission_id

        # Two live missions and no id match: fail loudly, naming the missions.
        other = store.create(
            tenant=tenant, uid="u1", kind="fill_form",
            args={"domain": "adv-other.test", "steps": [{"action": "observe"}]},
            url="https://adv-other.test/home", steps=[{"action": "observe"}],
            allowed_domains=["adv-other.test"],
        )
        store.claim(other)
        other_mission_id = store.grant_mission(other)["mission_id"]
        with pytest.raises(ValueError, match="not active"):
            agentic_browser_step(tenant, "deadbeefdeadbeef", "observe")
        with pytest.raises(ValueError, match=real_mission_id):
            agentic_browser_step(tenant, "deadbeefdeadbeef", "observe")
        assert other_mission_id  # the second mission is reported in the error text
    finally:
        act_saved = AGENTIC_BROWSER_SESSIONS.pop(real_mission_id, None)
        if act_saved:
            act_saved.close()


def test_adversarial_missing_mission_error_is_actionable(tmp_path, monkeypatch):
    """With no active mission the error must tell the model to propose one —
    not the bare 'mission is not active' that made it give up and ask the user
    for permission. It must also say a proposal_id / `profile_…` ref is not a
    mission_id (the model passed `profile_70ed58ac0d4f` in production)."""
    store = ApprovalStore(str(tmp_path / "none.db"))
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: store)

    with pytest.raises(ValueError) as exc:
        agentic_browser_step("user:none", "profile_70ed58ac0d4f", "observe")
    msg = str(exc.value)
    assert "request_concierge_action" in msg
    assert "fill_form" in msg
    assert "profile_" in msg


def test_adversarial_handle_survives_id_rename_via_fingerprint():
    """Site-agnostic recovery: if the page re-renders and the id the observation
    recorded disappears, the control must still be found by its semantic
    fingerprint (tag + name), not just the recorded selector."""
    domain = "adv-rename.test"
    url = f"https://{domain}/login"

    # The email input's id changes on re-render, but its name stays.
    html = """<!DOCTYPE html><html><body>
      <main>
        <form action="/session" method="post">
          <input id="email-render-2" type="email" name="login_email" placeholder="Email" />
          <button id="go" type="submit">Continue</button>
        </form>
      </main>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # Simulate an observation of the ORIGINAL id, then a re-render that
        # keeps only the name.
        act._handles = {"e1": {
            "handle": "e1", "tag": "input", "type": "email",
            "name": "login_email", "label": "",
            "selector": "#email-render-1",
            "selectors": ["#email-render-1", 'input[name="login_email"]'],
        }}
        # The stale id is gone, but the name-based candidate still matches.
        act.type_text("e1", "person@adv-rename.test")
        assert act._loop.run_until_complete(page.input_value("#email-render-2")) == "person@adv-rename.test"

        # Now the name is gone too: only the semantic fingerprint can rescue it.
        act._handles = {"e1": {
            "handle": "e1", "tag": "input", "type": "email",
            "name": "login_email", "label": "",
            "selector": "#gone",
            "selectors": ["#gone"],
        }}
        act._loop.run_until_complete(page.evaluate(
            "() => { const i = document.querySelector('#email-render-2'); "
            "i.removeAttribute('name'); i.id = 'email-render-3'; }"
        ))
        # tag+type still identify it uniquely.
        act.type_text("e1", "second@adv-rename.test")
        assert act._loop.run_until_complete(page.input_value("#email-render-3")) == "second@adv-rename.test"
    finally:
        act.close()


def test_adversarial_handle_survives_rerender_that_wipes_the_tag():
    """Regression for the live PayPal failure: an SPA re-render replaces the
    form's nodes, discarding the data-concierge-handle attribute the observe
    stamped, while the stable ``#id`` survives. Acting by id after the
    re-render must still reach the field."""
    domain = "adv-tagwipe.test"
    url = f"https://{domain}/signin"

    html = """<!DOCTYPE html><html><body>
      <main><form action="/signin" method="post">
        <input id="email" type="email" name="login_email" />
        <input id="password" type="password" name="login_password" />
        <button id="btnNext" type="submit">Next</button>
      </form></main>
      <script>
        // Simulate a framework re-render a moment after the observation: the
        // form's nodes are rebuilt from a template that never carried the
        // observer's injected attribute.
        setTimeout(() => {
          document.querySelector('form').innerHTML =
            '<input id="email" type="email" name="login_email" />' +
            '<input id="password" type="password" name="login_password" />' +
            '<button id="btnNext" type="submit">Next</button>';
        }, 400);
      </script>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        summary = act.interactive_summary()
        assert "e1 <input type=email>" in summary, summary
        # The re-render fires and wipes the tag before the action arrives.
        act._loop.run_until_complete(asyncio.sleep(0.8))
        gone = act._loop.run_until_complete(page.evaluate(
            "() => document.querySelector('[data-concierge-handle]') === null"
        ))
        assert gone is True, "attribute should have been wiped by the re-render"

        # The recorded selector still resolves the field by id.
        act.type_text("e1", "person@adv-tagwipe.test")
        assert act._loop.run_until_complete(page.input_value("#email")) == "person@adv-tagwipe.test"
    finally:
        act.close()


def test_adversarial_handle_recovers_control_inside_reloaded_iframe():
    """A login form in a child iframe that reloads between observe and act must
    still be reachable by id through the recorded candidates."""
    domain = "adv-iframe-recover.test"
    widget = "widget.adv-iframe-recover.test"
    url = f"https://{domain}/home"

    main_html = """<!DOCTYPE html><html><body>
      <iframe src="https://widget.adv-iframe-recover.test/form"
              style="width:640px;height:420px"></iframe>
    </body></html>"""
    widget_html = """<!DOCTYPE html><html><body>
      <main>
        <input id="user" type="email" name="login_email" placeholder="Email" />
        <button id="go" type="submit">Sign In</button>
      </main>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain, widget], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            body = widget_html if widget in route.request.url else main_html
            await route.fulfill(status=200, content_type="text/html", body=body)
        await page.route("**/*", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)
        act._loop.run_until_complete(asyncio.sleep(0.6))

        summary = act.interactive_summary()
        assert "e1 <input type=email>" in summary, summary
        # The control lives in the child frame; typing by id must reach it.
        act.type_text("e1", "person@adv-iframe-recover.test")
        frame = [f for f in page.frames if widget in f.url][0]
        got = act._loop.run_until_complete(frame.evaluate(
            "() => document.querySelector('#user').value"
        ))
        assert got == "person@adv-iframe-recover.test"
    finally:
        act.close()


def test_adversarial_otp_field_is_flagged_as_one_time_code_not_vault_secret():
    """A one-time/verification code input must NOT be described as a vault
    secret ("type with an empty value"): the model has to supply the code it
    retrieved from email/user. It must still keep its typed value out of the
    summary. Regression for the live PayPal OTP wall where every otpCode-N
    digit box was labelled SECRET."""
    domain = "adv-otp.test"
    url = f"https://{domain}/verify"

    html = """<!DOCTYPE html><html><body>
      <main>
        <form action="/verify" method="post" name="otp">
          <input id="ci-otpCode-0" type="number" name="otpCode-0" aria-label="1-6" />
          <input id="ci-otpCode-1" type="number" name="otpCode-1" aria-label="2-6" />
          <button id="securityCodeSubmit" type="submit" name="submitSecurityCode">Submit</button>
        </form>
        <input id="pw" type="password" name="password" placeholder="Password" />
      </main>
    </body></html>"""

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        summary = act.interactive_summary()
        # OTP boxes are described as one-time-code fields, not vault secrets.
        otp_lines = [ln for ln in summary.splitlines() if "otpCode" in ln]
        assert otp_lines, summary
        assert all("ONE-TIME CODE" in ln for ln in otp_lines), summary
        assert all("SECRET" not in ln for ln in otp_lines), summary
        # A genuine password field is still a vault secret.
        pw_lines = [ln for ln in summary.splitlines() if 'name="password"' in ln]
        assert pw_lines and all("SECRET: type with an empty value" in ln for ln in pw_lines), summary
        # The typed OTP value must never leak into the summary.
        act.type_text("#ci-otpCode-0", "918273")
        summary2 = act.interactive_summary()
        assert "918273" not in summary2, summary2
    finally:
        act.close()
