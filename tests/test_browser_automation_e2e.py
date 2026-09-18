"""Milestone 3: Automated Browser End-to-End Suite.

Hermetic, deterministic Playwright-based tests using route interception to mock
realistic HTML/HTTP interactions:
1. Single-step login flow:
   - Form with #username, #password, #login-btn.
   - Fills username and password, clicks submit, verifies authenticated dashboard and session cookies.
2. Multi-step / identifier-first login flow:
   - Modern Google/Amazon/PayPal style flow: step 1 (#username, #continue-btn) -> dynamic DOM transition -> step 2 (#password, #submit-btn).
   - Verifies completion without premature heal() clicks or timeout hangs.
3. Dynamic element waiting & DOM shifts:
   - Delayed DOM injection (e.g. 500ms setTimeout).
   - Verifies wait-based selector resolution and settlement.
   - Verifies numeric CSS ID fallback resilience (e.g. #98765).
4. Iframe-embedded auth widget:
   - Main page hosting child <iframe> with credentials input.
   - Verifies actuator traverses frames, finds selectors in child frame, and fills inputs.
5. Anti-bot stealth:
   - Verifies navigator.webdriver is undefined/masked and HeadlessChrome is absent in User-Agent.
6. Error recovery & challenge page handling:
   - Interstitial checkpoint detection and healing.
   - OTP wall detection via agentic_browser_step with blocked=True handoff.
   - Navigation HTTP 500 error graceful failure.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import patch

from src.bot.approval_views import (
    BrowserlessActuator,
    agentic_browser_step,
    browser_auth_challenge,
    AGENTIC_BROWSER_SESSIONS,
)
from src.services.concierge.approval_store import ApprovalStore
from src.security.vault import Vault
from src.services.concierge import browser_driver


CDP_TEST_URL = "ws://localhost:3002/"
MASTER_KEY = "test-browser-e2e-master-key-32bytes!"


@pytest.fixture(autouse=True)
def mock_public_host_and_env():
    """Ensure test domains pass SSRF gate and browserless connects to test container."""
    orig_url = browser_driver.BROWSERLESS_URL
    browser_driver.BROWSERLESS_URL = "http://localhost:3002"
    with patch("src.services.concierge.browser_gate.is_public_host", return_value=True):
        yield
    browser_driver.BROWSERLESS_URL = orig_url


# ============================================================================
# 1. Single-Step Login Flow
# ============================================================================


def test_e2e_single_step_login_flow():
    """Full single-step login: fills inputs, clicks login, verifies dashboard and cookies."""
    domain = "mockbank.test"
    base_url = f"https://{domain}"
    login_url = f"{base_url}/login"

    login_html = """<!DOCTYPE html>
    <html>
    <head><title>Mock Bank Login</title></head>
    <body>
      <main>
        <h1>Secure Customer Portal</h1>
        <form id="login-form">
          <label for="username">Username</label>
          <input id="username" name="username" type="text" />
          <label for="password">Password</label>
          <input id="password" name="password" type="password" />
          <button id="login-btn" type="button">Sign In</button>
        </form>
      </main>
      <script>
        document.getElementById('login-btn').addEventListener('click', () => {
          const u = document.getElementById('username').value;
          const p = document.getElementById('password').value;
          if (u === 'alice_customer' && p === 'BankPassword123!') {
            document.cookie = 'bank_session=sess_auth_token_999; path=/';
            window.localStorage.setItem('auth_status', 'authenticated');
            document.body.innerHTML = `
              <main>
                <h1>Account Dashboard</h1>
                <p>Welcome back, alice_customer!</p>
                <div id="account-balance">$24,500.00</div>
              </main>
            `;
          } else {
            document.body.innerHTML = '<p id="error">Invalid Credentials</p>';
          }
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(login_url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            if route.request.url.startswith(login_url):
                await route.fulfill(status=200, content_type="text/html", body=login_html)
            else:
                await route.continue_()
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(login_url)

        # Fill credentials
        act.type_text("#username", "alice_customer")
        act.type_text("#password", "BankPassword123!")

        # Click submit
        act.click("#login-btn")

        # Verify page transitioned to Dashboard
        text = act.get_text()
        assert "Account Dashboard" in text
        assert "Welcome back, alice_customer!" in text
        assert "$24,500.00" in text

        # Verify session cookie was persisted in browser context
        cookies = act._loop.run_until_complete(act._context.cookies())
        bank_cookie = next((c for c in cookies if c["name"] == "bank_session"), None)
        assert bank_cookie is not None
        assert bank_cookie["value"] == "sess_auth_token_999"

        # Verify localStorage
        ls_val = act._loop.run_until_complete(
            page.evaluate("() => window.localStorage.getItem('auth_status')")
        )
        assert ls_val == "authenticated"

    finally:
        act.close()


# ============================================================================
# 2. Multi-Step / Identifier-First Login Flow
# ============================================================================


def test_e2e_multistep_identifier_first_login_flow():
    """Modern identifier-first login (e.g. Google/Amazon): dynamic DOM transition to password step."""
    domain = "authflow.test"
    base_url = f"https://{domain}"
    signin_url = f"{base_url}/signin"

    multistep_html = """<!DOCTYPE html>
    <html>
    <head><title>Identifier-First Login</title></head>
    <body>
      <main id="auth-box">
        <h1>Sign In to Service</h1>
        <!-- Step 1: Identifier -->
        <div id="step-username">
          <input id="username" type="email" placeholder="Enter your email" />
          <button id="continue-btn" type="button">Continue</button>
        </div>
        <!-- Step 2: Password (hidden initially) -->
        <div id="step-password" style="display: none;">
          <p id="user-display"></p>
          <input id="password" type="password" placeholder="Enter your password" />
          <button id="submit-btn" type="button">Submit Password</button>
        </div>
        <!-- Step 3: Success -->
        <div id="step-success" style="display: none;">
          <h2>Portal Home</h2>
          <p>Authenticated as valid user</p>
        </div>
      </main>
      <script>
        document.getElementById('continue-btn').addEventListener('click', () => {
          const email = document.getElementById('username').value;
          if (email && email.includes('@')) {
            // DOM transition: reveal step 2
            document.getElementById('step-username').style.display = 'none';
            document.getElementById('step-password').style.display = 'block';
            document.getElementById('user-display').innerText = email;
          }
        });
        document.getElementById('submit-btn').addEventListener('click', () => {
          const pass = document.getElementById('password').value;
          if (pass === 'MultiStepSecretPass777') {
            document.getElementById('step-password').style.display = 'none';
            document.getElementById('step-success').style.display = 'block';
          }
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(signin_url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=multistep_html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(signin_url)

        # Step 1: Type username and click Continue
        act.type_text("#username", "bob@authflow.test")
        act.click("#continue-btn")

        # Step 2: Password field is now dynamically visible; type password and click submit
        act.type_text("#password", "MultiStepSecretPass777")
        act.click("#submit-btn")

        # Verify successful completion without hanging
        text = act.get_text()
        assert "Portal Home" in text
        assert "Authenticated as valid user" in text

    finally:
        act.close()


# ============================================================================
# 3. Dynamic Element Waiting & DOM Shifts
# ============================================================================


def test_e2e_dynamic_element_waiting_and_dom_shifts():
    """Actuator must wait resiliently for dynamically injected elements and handle numeric IDs."""
    domain = "dynamicapp.test"
    url = f"https://{domain}/overview"

    dynamic_html = """<!DOCTYPE html>
    <html>
    <head><title>Dynamic App</title></head>
    <body>
      <main>
        <h1>Async Data Loader</h1>
        <div id="loader">Fetching statement records...</div>
        <div id="target-container"></div>
      </main>
      <script>
        // Simulate asynchronous network/DOM mounting delay (400ms)
        setTimeout(() => {
          document.getElementById('loader').style.display = 'none';
          document.getElementById('target-container').innerHTML = `
            <div id="98765">
              <button id="view-records-btn">View Detailed Records</button>
            </div>
            <div id="content-status">Ready</div>
          `;
        }, 400);
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=dynamic_html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # 1. Click dynamic button: must wait for setTimeout to mount the button
        act.click("#view-records-btn")

        # 2. Test numeric ID syntax fallback resilience:
        # In CSS '#98765' is invalid syntax, but BrowserlessActuator falls back to '[id="98765"]'
        act.click("#98765")

        # Verify page settled cleanly
        text = act.get_text()
        assert "View Detailed Records" in text
        assert "Ready" in text

    finally:
        act.close()


# ============================================================================
# 4. Iframe-Embedded Auth Widget
# ============================================================================


def test_e2e_iframe_embedded_auth_widget():
    """Actuator must traverse child frames, finding selectors and typing into inputs inside iframes."""
    domain = "checkoutportal.test"
    main_url = f"https://{domain}/checkout"
    frame_url = f"https://{domain}/frames/secure-payment"

    main_html = f"""<!DOCTYPE html>
    <html>
    <head><title>Checkout Page</title></head>
    <body>
      <h1>Order Summary: $189.00</h1>
      <div id="frame-holder">
        <iframe id="payment-iframe" src="{frame_url}" width="400" height="300"></iframe>
      </div>
      <div id="order-complete" style="display: none;">Thank you for your order!</div>
    </body>
    </html>
    """

    frame_html = """<!DOCTYPE html>
    <html>
    <head><title>Payment Widget Frame</title></head>
    <body>
      <div id="card-form">
        <input id="cardholder-name" type="text" placeholder="Cardholder Name" />
        <input id="security-code" type="password" placeholder="CVV" />
        <button id="pay-now-btn" type="button">Confirm Payment</button>
      </div>
      <div id="frame-status">Awaiting Input</div>
      <script>
        document.getElementById('pay-now-btn').addEventListener('click', () => {
          const name = document.getElementById('cardholder-name').value;
          const cvv = document.getElementById('security-code').value;
          if (name && cvv === '987') {
            document.getElementById('frame-status').innerText = 'Payment Authorized Successfully';
          }
        });
      </script>
    </body>
    </html>
    """

    act = BrowserlessActuator(main_url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_routes():
        async def handle(route):
            req_url = route.request.url
            if req_url.startswith(main_url):
                await route.fulfill(status=200, content_type="text/html", body=main_html)
            elif req_url.startswith(frame_url):
                await route.fulfill(status=200, content_type="text/html", body=frame_html)
            else:
                await route.continue_()
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_routes())
        act.navigate(main_url)

        # Fill inputs residing inside the iframe
        act.type_text("#cardholder-name", "Fox Mulder")
        act.type_text("#security-code", "987")

        # Click button residing inside the iframe
        act.click("#pay-now-btn")

        # Verify child frame was targeted and executed
        assert act._target_frame is not None
        assert act._target_frame != page.main_frame
        assert frame_url in act._target_frame.url

        # Check that the frame DOM reacted to the click
        frame_status = act._loop.run_until_complete(
            act._target_frame.inner_text("#frame-status")
        )
        assert frame_status == "Payment Authorized Successfully"

    finally:
        act.close()


# ============================================================================
# 5. Anti-Bot Stealth Verification
# ============================================================================


def test_e2e_anti_bot_stealth():
    """Actuator stealth scripts must mask navigator.webdriver and HeadlessChrome."""
    domain = "stealthcheck.test"
    url = f"https://{domain}/verify"

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><body><h1>Stealth Verification</h1></body></html>",
            )
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # 1. navigator.webdriver must be undefined (masked)
        webdriver_val = act._loop.run_until_complete(
            page.evaluate("() => navigator.webdriver")
        )
        assert webdriver_val is None

        # 2. HeadlessChrome must be absent in User-Agent
        ua = act._loop.run_until_complete(page.evaluate("() => navigator.userAgent"))
        assert "HeadlessChrome" not in ua
        assert "Chrome" in ua

        # 3. HeadlessChrome must be absent in appVersion
        app_ver = act._loop.run_until_complete(page.evaluate("() => navigator.appVersion"))
        assert "HeadlessChrome" not in app_ver

        # 4. window.chrome must exist
        has_chrome = act._loop.run_until_complete(
            page.evaluate("() => typeof window.chrome === 'object' && window.chrome !== null")
        )
        assert has_chrome is True

        # 5. navigator.plugins must be populated
        plugins_len = act._loop.run_until_complete(
            page.evaluate("() => navigator.plugins ? navigator.plugins.length : 0")
        )
        assert plugins_len > 0

        # 6. navigator.languages must contain standard languages
        languages = act._loop.run_until_complete(page.evaluate("() => navigator.languages"))
        assert isinstance(languages, list)
        assert len(languages) >= 1
        assert "en-US" in languages or "en" in languages

    finally:
        act.close()


# ============================================================================
# 6. Error Recovery & Challenge Page Handling
# ============================================================================


def test_e2e_challenge_interstitial_detection_and_heal():
    """Actuator detects anti-bot challenge interstitial and heal() clicks verify."""
    domain = "challengeportal.test"
    url = f"https://{domain}/checkpoint"

    challenge_html = """<!DOCTYPE html>
    <html>
    <head><title>Security Checkpoint</title></head>
    <body>
      <main>
        <h1>Security Check</h1>
        <p>Verify that you are human before proceeding to your account.</p>
        <button id="verify-btn" type="button" onclick="document.body.innerHTML='<h1>Passed Verification</h1>'">
          Verify
        </button>
      </main>
    </body>
    </html>
    """

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=challenge_html)
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        act.navigate(url)

        # Text extraction contains challenge markers
        text = act.get_text()
        assert "Security Check" in text
        assert "Verify that you are human" in text

        # browser_auth_challenge recognizes this is an authentication/bot challenge
        assert browser_auth_challenge(text, url) is True

        # heal() successfully interacts with the 'Verify' button to dismiss checkpoint
        healed = act.heal()
        assert healed is True

        # Check healed DOM
        after_text = act.get_text()
        assert "Passed Verification" in after_text

    finally:
        act.close()


def test_e2e_challenge_otp_wall_agentic_step_blocked(tmp_path, monkeypatch):
    """Unresolvable challenge wall sets blocked=True in agentic_browser_step without crashing."""
    domain = "bankchallenge.test"
    url = f"https://{domain}/mfa"
    db_path = str(tmp_path / "challenge_test.db")
    store = ApprovalStore(db_path)
    vault = Vault(db_path, master_key=MASTER_KEY)

    otp_html = """<!DOCTYPE html>
    <html>
    <head><title>MFA Verification</title></head>
    <body>
      <main>
        <h1>Mobile Verification</h1>
        <p>Enter the one-time code sent to your phone to complete verification.</p>
        <input id="otp-code" type="text" />
      </main>
    </body>
    </html>
    """

    tenant = "user:challenger"
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

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=otp_html)
        await page.route(f"https://{domain}/**", handle)

    act._loop.run_until_complete(setup_route())
    act.navigate(url)

    # Attach to active sessions
    AGENTIC_BROWSER_SESSIONS[mission_id] = act
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: store)
    monkeypatch.setattr("src.bot.approval_views.Vault", lambda db=None: vault)

    try:
        step_res = agentic_browser_step(tenant, mission_id, "observe")
        # Step marks blocked=True without raising an unhandled crash
        assert step_res["blocked"] is True
        assert step_res["block_reason"] == "user authentication/verification is required"
        assert "Mobile Verification" in step_res["summary"]
    finally:
        act_saved = AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)
        if act_saved:
            act_saved.close()


def test_e2e_transient_challenge_signal_is_not_reported_as_a_wall(tmp_path, monkeypatch):
    """A challenge seen once while the page is still settling must not be
    reported as a wall once the page is clean — otherwise the user is told to
    complete a verification step that does not exist."""
    domain = "transientchallenge.test"
    url = f"https://{domain}/home"
    db_path = str(tmp_path / "transient_test.db")
    store = ApprovalStore(db_path)
    vault = Vault(db_path, master_key=MASTER_KEY)

    clean_html = """<!DOCTYPE html>
    <html>
    <head><title>Welcome</title></head>
    <body>
      <main>
        <h1>Welcome back</h1>
        <p>You are signed in.</p>
        <button id="signout">Sign out</button>
      </main>
    </body>
    </html>
    """

    tenant = "user:transient"
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

    async def setup_route():
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=clean_html)
        await page.route(f"https://{domain}/**", handle)

    act._loop.run_until_complete(setup_route())
    act.navigate(url)

    # The very first text sample lands mid-load and reads as a wall; every
    # later sample sees the settled, clean page.
    real_get_text = act.get_text
    samples = []

    def flaky_get_text():
        samples.append(1)
        if len(samples) == 1:
            return "Please wait. Checking your browser before accessing the site."
        return real_get_text()

    monkeypatch.setattr(act, "get_text", flaky_get_text)

    AGENTIC_BROWSER_SESSIONS[mission_id] = act
    monkeypatch.setattr("src.bot.approval_views.ApprovalStore", lambda db=None: store)
    monkeypatch.setattr("src.bot.approval_views.Vault", lambda db=None: vault)

    try:
        step_res = agentic_browser_step(tenant, mission_id, "observe")
        assert len(samples) >= 2, "the transient must trigger a re-sample"
        assert step_res["blocked"] is False
        assert step_res["block_reason"] == ""
        assert "Welcome back" in step_res["summary"]
        assert "Checking your browser" not in step_res["summary"]
    finally:
        act_saved = AGENTIC_BROWSER_SESSIONS.pop(mission_id, None)
        if act_saved:
            act_saved.close()


def test_e2e_http_500_navigation_error_recovery():
    """Navigation encountering server HTTP 500 error raises ValueError cleanly."""
    domain = "errorbank.test"
    url = f"https://{domain}/broken"

    act = BrowserlessActuator(url, allowed_domains=[domain], cdp_url=CDP_TEST_URL)
    act._ensure(navigate=False)
    page = act._page

    async def setup_route():
        async def handle(route):
            await route.fulfill(
                status=500,
                content_type="text/html",
                body="<html><body><h1>500 Internal Server Error</h1></body></html>",
            )
        await page.route(f"https://{domain}/**", handle)

    try:
        act._loop.run_until_complete(setup_route())
        with pytest.raises(ValueError, match="navigation returned HTTP 500"):
            act.navigate(url)
    finally:
        act.close()
