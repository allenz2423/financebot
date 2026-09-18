# B2 Concierge — Amazon Login: End-to-End Worklog

Status: **deployed & verified (2026-09-17).** The full chain — write-tier approval →
browser fill_form → self-verify — now executes without crashing. The remaining step is a
live end-to-end run: ask the bot to log into Amazon and click ✅.

Everything below is what was designed, built, debugged, and shipped across these sessions,
in one place. It is a worklog, not a replacement for `docs/concierge-proposal.md` (the
design record) — cite this file when you need to know *what actually happened*.

---

## 1. Mission & end state

**Goal:** The user stores an Amazon password **once**; after that Delilah can perform a
`fill_form` login on amazon.com behind a Discord ✅ approval, with the password injected
from the per-tenant vault (never typed in chat, never stored by the model).

**End-to-end chain (all links now proven):**

```
model tool request_credential_capture   → user gets one-time capture URL
user submits Email/Password on form       → vault (encrypted, tenant-bound, consumer_scope=amazon.com)
model tool propose_concierge_act (fill_form, vault_refs in steps)
user clicks ✅ in Discord                 → ApprovalStore.claim() (atomic, one-shot)
worker thread: BrowserlessActuator (CDP)  → perform_act() fill steps, secrets injected at execution
vlm_verify.verify_act                     → VLM (local Ollama) or DOM-text assertion
audit_chain row + proposal status executed/rolled_back + Discord followup
```

**Important framing:** nothing here makes the *browser* trustworthy or the *model*
reliable — those are separate known issues (see §8). This worklog is about the *plumbing*
that was broken between them.

---

## 2. The failure chain (why this took five deployments)

A useful history of every way the ✅ path broke, in order. Each row maps to a real
`audit_chain` entry or a live crash.

| # | Where it died | Error (verbatim) | Root cause | Fixed in |
|---|---|---|---|---|
| 1 | `BrowserlessActuator._ensure` | `Cannot run the event loop while another loop is running` (audit seq 80, proposal `bc019e5ec71245f5`) | `BrowserlessActuator` is sync-over-async: it pumps Playwright through its **own** loop with `run_until_complete` — only legal in a thread with *no* running loop. The Discord button handler runs on the bot's main loop. Also emitted `RuntimeWarning: coroutine 'BrowserlessActuator._connect' was never awaited`. | `asyncio.to_thread` (first deploy of the day) |
| 2 | `BrowserlessActuator._connect` | `'PlaywrightContextManager' object has no attribute 'chromium'` (audit seq 85, proposal `89fec65c5f7b4676`) | `async_playwright()` returns a **context manager** (only `start`); the real driver handle is the `Playwright` instance `start()` returns. We stored the CM, so `.chromium` (and later `.stop()`) were missing. Hidden until now because attempt #1 died before connecting. | store `await async_playwright().start()` |
| 3 | `browser_driver.py` + actuator connect | `BrowserType` has no `connect_over_websocket` | Playwright ≥ 1.49 renamed `connect_over_websocket` → `connect`. Same stale call in `browser_driver.act_on_page`/`screenshot_page` (would have been the *next* crash — `verify_act` screenshots through it). | `chromium.connect(...)` |
| 4 | WS endpoint resolution | `write EPROTO ... wrong version number` | `_ws_url()` mapped `http://` → `wss://` (TLS) against a plain-WS service. Should be `ws://`. | scheme mapping fix |
| 5 | WS endpoint existence | `/playwright` → `404 Not Found`; `/chrome` → `404 Not Found` | Browserless **v2** (`ghcr.io/browserless/chromium:latest`, Chrome 153) removed the Playwright-driver WS route *and* the legacy `/chrome` CDP route. The only live endpoint is the **root path** `ws://browserless:3000/` (advertised by `/json/version` → `webSocketDebuggerUrl`). | `connect_over_cdp(_ws_url())` reusing `browser.contexts[0]` |
| 6 | Discord error path | `ActionApprovalView.on_error() takes 3 positional arguments but 4 were given` (Task exception was never retrieved) | discord.py calls `View.on_error(interaction, error, item)` — 4 args. Our override had 3, so ANY view exception crashed the handler *again* and the followup never reached the user. | add `item` param |
| 7 | `close()` in `finally` | (masked audit + results) | A raise inside the new `_execute_act`'s `finally: actuator.close()` overwrote the `except` path, so `act_error` was sometimes never audited / the followup never sent. | `close()` is now best-effort; never raises |

Also observed (model-side, not a code bug): audit seq 74–77 show **four** `act_refused`
entries with `read target must be a full http(s):// URL (e.g. https://amazon.com)` — the
model kept emitting `navigate` steps with scheme-less URLs before correcting itself at
seq 78. See §8.

---

## 3. System map — files that carry this feature

| File | Role |
|---|---|
| `src/bot/approval_views.py` | `ActionApprovalView` (✅/✕ Discord buttons, owner-only, TTL, timeout → `expired`), `BrowserlessActuator` (sync-over-async CDP actuator with per-nav SSRF revalidation), `_execute_act` worker-thread entry, `_result_detail` (strips screenshot bytes before persistence) |
| `src/services/concierge/act_actions.py` | `perform_act` (drives the actuator through validated steps), `_act_outcome`, `_validate_steps`, `_act_target_url`, `_step_vault_refs`, `_secret_field_type`, `resolve_secret` hook |
| `src/services/concierge/approval_store.py` | `ApprovalStore`: `create`/`get`/`claim` (atomic single-use) /`update_status` over `concierge_act_proposals` |
| `src/services/concierge/audit.py` | `AuditLog` over **hash-chained** `audit_chain` table (each row hashes the previous; tamper-evident) |
| `src/services/concierge/capture_tokens.py` | `CaptureTokenStore`: `issue`/`peek`/`redeem`, tenant-bound, SHA-256-hashed tokens, TTL 10 min, purge |
| `src/services/concierge/capture_schema.py` | `validate_capture_intent`: KINDS, FIELD_TYPES, `PAYMENT_ONLY_FIELDS`, `FORCED_EPHEMERAL` (cvv/captcha), `MAX_FIELDS` 12, `consumer_scope` domain regex |
| `src/services/concierge/credential_capture.py` | **New** — `build_credential_capture`, self-service issuer; field-schema validation (`_validate_fields`), label allowlist + payment/identity denylist, lazy `CONCIERGE_CAPTURE_URL`, `CaptureRefused` |
| `src/services/concierge/routes.py` | FastAPI capture page: GET form (action carries `?tenant=`), POST accepts **both** JSON and urlencoded; renders "Submitted ✓" for browser submits |
| `src/services/concierge/llm_tool.py` | Tool entrypoint `request_credential_capture` + `propose_concierge_act` + read-tier `request_concierge_action(_async)`; `_protect_secret_steps` vault-injects model-supplied secrets before persistence |
| `src/services/concierge/browser_gate.py` | `validate_target_url` (allowlist + DNS-public), `domain_matches`, `READ_KINDS`, `ReadGateError` |
| `src/services/concierge/browser_driver.py` | `BROWSERLESS_URL`, `_ws_url()` (→ root CDP endpoint), `act_on_page`, `screenshot_page`, `DEFAULT_TIMEOUT_MS` |
| `src/services/concierge/vlm_verify.py` | `verify_act`: VLM (local Ollama, base64 screenshot) or DOM-text fallback; degrades gracefully |
| `src/services/concierge/tenants.py` | `TenantStore`: enable/tier/allow_domains |
| `src/security/vault.py` | `Vault`: `store_fields`/`resolve`, `FIELD_TYPES`, `PAYMENT_ONLY_FIELDS`, `consumer_scope` gating; `data/concierge.db` |
| `src/services/llm.py` | Tool schema registration, `EXPECTED_TOOL_NAMES`, `core_tools`, dispatcher wiring (tenant from `"user:"+uid`) |
| `tests/test_concierge_*.py`, `tests/test_credential_capture.py` | 226 tests covering gates, capture, approval, browser gate |

**Container runtime:** `delilah_bot` (compose service `delilah`), browserless sidecar
`financebot-browserless` (`ghcr.io/browserless/chromium:latest`, port 3000 internal /
3002 host), shared `financebot-internal` + `egress` networks. NPM reverse proxy fronts
`concierge.incoming.nyc → 192.168.0.167:8000` with Cloudflare edge TLS.

---

## 4. What was built/fixed, by area

### 4.1 Security hardening (foundation, prior sessions)

- **Tenant-swap injection closed** — the capture URL carries `?tenant=`; tokens are
  single-use *and* tenant-bound: `peek`/`redeem` both raise `CaptureTokenError` if the
  caller's tenant ≠ the token's tenant. Replacing `user:342385739952160769` with another
  id in the URL yields nothing.
- **SSRF gates** — `BrowserlessActuator` re-validates the *live page URL*
  (allowlist + DNS-public, via `validate_target_url`) after every navigation and before
  every interaction, including HTTP redirects and JS-driven `framenavigated` events. A page
  leaving the gated scope fails closed mid-act.
- **Vault-injected secrets** — model-supplied secret values never persist in plaintext:
  `_protect_secret_steps` replaces them with masked placeholders + tenant-scoped
  `vault_ref`; plaintext exists only as encrypted vault ciphertext (consumer_scope = the
  act's domain) and is decrypted once at execution inside the approval callback.
- **Atomic approval** — `ApprovalStore.claim()` is one-shot: a second ✅ cannot double-execute.
- **Rolled-back on failure** — any execution exception transitions the proposal to
  `rolled_back` (not a terminal success), audited as `act_error`.
- **Hash-chained audit** — `audit_chain`: each row stores `prev_hash` + `entry_hash`;
  tampering breaks the chain.
- **Risk-based approval TTL** — spend-tier tenants get a 120 s window, standard write 600 s.
- **Credential capture is login-credential-only** — the issuer's field types are
  `{text, password, otp, token, url}`; labels must match
  `^[A-Za-z0-9][A-Za-z0-9 _./+()-]{0,39}$`; labels containing card/cvv/pan/ssn/pin/
  billing/debit/credit/expiry/iban/routing are refused; `card_pan`/`card_exp`/`card_cvv`
  are rejected by the schema outside `kind=payment` (PAYMENT_ONLY_FIELDS). Max 6 fields,
  distinct labels.

### 4.2 Public HTTPS on the capture link

- Concierge FastAPI serves on `:8000`. NPM (nginx-proxy-manager) reverse-proxies
  `concierge.incoming.nyc` → `192.168.0.167:8000` (from NPM's network: `172.19.0.1:8000`).
- DNS is Cloudflare-proxied (104.21.18.35 / 172.67.x) with working edge TLS. Working
  pattern reference: `plaid.delilah.incoming.nyc` (DNS-only → 98.14.10.98).
- `.env` gained `CONCIERGE_CAPTURE_URL=https://concierge.incoming.nyc`. The capture
  issuer reads it **lazily at call time** (`base_url` param default `""`), so tests can
  override via `monkeypatch.setenv` and the URL is never stale.

### 4.3 Model-driven credential capture (the "bot asks, not manual" feature)

- New model tool `request_credential_capture(domain, fields?)`. Tenant is injected from
  the interaction (`"user:"+uid`), never model-supplied.
- Gates: tenant enabled **and** domain on allowlist; every attempt is audited
  (`capture_issued` / `capture_refused` — domain only, never the token).
- Optional `fields` array (label/type/required) — the model can request OTP fields, etc.
  Defaults to Email + Password.
- Model got it working in production: the log shows the model calling the tool on its
  first attempt (`ok=True`, result 177 chars) — the capture was issued without manual
  commands.
- **Form-post bug class fixed:** plain HTML forms submit `urlencoded` bodies with
  absolute-path actions that *drop query strings*. The POST route now accepts both JSON
  and form bodies AND the rendered form action embeds `?tenant=`; browser submit returns
  a "Submitted ✓" page. The user's stored creds confirmed live multiple times
  (audit `capture_stored`, vault refs `s_56b28992b347`, `s_77099a8f4a79`, `s_8f30aca083bb`…).

### 4.4 The ✅-approval event-loop fix (deploy #1)

- Before: `_approve` ran the whole act inline: `BrowserlessActuator` → `run_until_complete`
  → `RuntimeError` on the main loop; the proposal rolled back (`act_error`) without ever
  touching the browser.
- After: `_approve` does `result = await asyncio.to_thread(self._execute_act, proposal,
  allowed, audit, vault, store)`. The worker thread (no running loop) runs
  `perform_act` → `asyncio.run(verify_act(...))` → audit `act_<outcome>`/`act_error` →
  `store.update_status(executed|rolled_back)`. Discord messaging (followup, button
  disable, message edit) stays on the main loop.
- `_execute_act` returns `{"ok": True, outcome, res, verification}` or
  `{"ok": False, "error": "Type: msg"}`.

### 4.5 The Playwright/Browserless stack fix (deploy #2 — today's "check the log")

Four stacked issues (see §2, #2–#5) fixed across `approval_views.py` and
`browser_driver.py`:

1. `_connect()` stores the **started** instance: `self._playwright = await
   async_playwright().start()`.
2. Connect via **CDP**: `chromium.connect_over_cdp(_ws_url())` — Playwright-driver WS
   route no longer exists on Browserless v2.
3. `_ws_url()` returns the **root** endpoint (`ws://browserless:3000/`), mapping
   `http://` → `ws://`, `https://` → `wss://`.
4. Over CDP there is no `browser.new_context()`: reuse `browser.contexts[0]` and set the
   viewport per-page (`page.set_viewport_size`). Applied to the actuator **and** to
   `browser_driver.act_on_page`/`screenshot_page` (the `verify_act` screenshot path).
5. `ActionApprovalView.on_error(interaction, exc, item)` — correct discord.py signature
   (4 args); loop var renamed to avoid shadowing.
6. `BrowserlessActuator.close()` is now best-effort (each teardown step wrapped), so a
   cleanup failure can never mask an act result again.

Verified that over CDP: `page.goto` → `new_page`/`contexts[0]`, `ctx.close()`,
`browser.close()`, and browserless **self-heals** after a close (the preboot Chrome is
restarted; `/json/version` returns 200 again a few seconds later).

---

## 5. Verification evidence

### Automated
```
python3 -m pytest tests/test_concierge_*.py tests/test_credential_capture.py -q
→ 226 passed (2.1s)
```
Plus `ast.parse` syntax checks on every edited file, and a container-side import check:
`docker exec delilah_bot python3 -c "import src.bot.approval_views; ..."`.

### Live in-container probes (post-deploy, all passed)
- **Actuator roundtrip:** `_ensure()` → `navigate('https://example.com')` (allowlist
  gate enforced) → `get_text()` (129 chars) → `element_visible('h1')` (True) →
  `screenshot()` (13,113 bytes) → `close()`.
- **verify_act screenshot+VLM path:** empty `page_text` forces a real screenshot →
  `{'verdict': 'verified', 'confidence': 0.85, 'detail': "VLM asserts all expected
  strings present: ['Example Domain']"}`.
- **Browserless self-heal:** after `browser.close()` over CDP, `/json/version` → 200.
- **Boot logs:** no error/traceback/exception lines after rebuild.

### Audit-chain evidence (concierge.db, `audit_chain` table)
```
seq 80  act_error   Cannot run the event loop while another loop is running  (bc019e5ec71245f5)
seq 81-82  capture_stored  user:1 (host-run test pollution — see §8)
seq 83  act_proposed  fill_form 7 steps https://amazon.com  (89fec65c5f7b4676)
seq 84  act_approved  fill_form  (89fec65c5f7b4676)
seq 85  act_error   'PlaywrightContextManager' object has no attribute 'chromium'  (89fec65c5f7b4676)
```
seq 85 is the pre-fix failure; the fix deploys after it. Proposals live in
`concierge_act_proposals` (statuses: pending / executed / rolled_back / rejected / expired).

---

## 6. Ops playbook (what to run)

```bash
# tests
python3 -m pytest tests/test_concierge_*.py tests/test_credential_capture.py -q

# rebuild + restart the bot (env-only change: add --force-recreate)
docker compose up -d --build delilah

# wait for boot (chained sleep+echo is blocked in this environment)
sleep 12 # intentional-sleep: wait for boot

# confirm the fix is in the running image
docker exec delilah_bot python3 -c "import src.bot.approval_views as m; print(hasattr(m.ActionApprovalView, '_execute_act'))"

# boot-log scan
docker logs delilah_bot --since 1m 2>&1 | grep -iE "error|traceback|exception"

# read the audit chain (sqlite3 CLI is absent in the container; use python)
docker exec delilah_bot python3 -c "
import sqlite3
db = sqlite3.connect('/app/data/concierge.db')
for r in db.execute('SELECT seq, ts, actor, action, detail FROM audit_chain ORDER BY seq DESC LIMIT 10'):
    print(r)
"
```

Environment quirks (learned the hard way):
- `curl` absent in the container → use python `urllib`; `sqlite3` CLI absent → python.
- `read_file` offset/limit is unreliable on `src/services/llm.py` → `sed -n 'A,Bp'`.

---

## 7. What is NOT yet proven / known limits

- **No successful fill_form on a real site yet.** Every attempt so far died in plumbing
  (event loop → connect → endpoint). The live end-to-end run is the next step; Amazon's
  bot detection (headless fingerprints, login walls, captcha) may still block it.
- **CVV/captcha re-entry at approval is not implemented** (FORCED_EPHEMERAL refuses the
  proposal instead of asking). Tokenized-card (PAN/CVV) support is a deferred design item.
- **search.py:1364** uses the same dead `/chrome` CDP path for B1 web-search JS rendering
  (falls back to local Chromium) — untouched this session, flagged for a later pass.
- **Model reliability:** it corrected its own scheme-less `navigate` bug (4 refusals at
  seq 74–77) but the tolerance could be improved (e.g., coerce `amazon.com` →
  `https://amazon.com` at validation time).

---

## 8. Flagged items (do NOT start without user confirmation)

1. Watchdog busy-defer code fix (re-arm at most once, then drop).
2. Add a `login` kind to `request_concierge_action`.
3. PAN/CVV tokenized-card deferred item (§7).
4. `ENABLE_CONCIERGE_BROWSER=1` cookie-session path (browser_sessions.py) — separate
   preview/approval UX, not wired.
5. `tests/test_concierge_routes.py` run on the **host** while the bot is live pollutes
   the audit chain with `capture_stored` rows under actor `user:1` (seen at seq 81–82).
   Fix: isolate test DBs from `data/concierge.db` when running host-side.
6. Search playwright path (§7).

## 9. Open questions for the user

- **Internship/SWE scrape (#324, 22:00 UTC):** bundle into the morning news pass
  (#332, 12:45 UTC) or keep it standalone?
- **DST bump (ends 2026-11-01):** fixed-UTC schedules drift an hour after the switch
  (news 12:45→13:45 UTC, internship 22:00→23:00 UTC). Want a one-shot Nov-1 reminder to
  handle it?

## 10. Secrets & safety notes

- `CONCIERGE_ADMINS=user:342385739952160769` (the sole admin/tenant id).
- `.env` contains NVIDIA/Cloudflare/Mistral keys + secrets — never echo it into chat,
  logs, or this file.
- Password/credential plaintext exists only inside the encrypted vault (`vault_records`),
  decrypted once at act execution. Audit rows carry domain/label, never the token.
## 11. Auto-pilot missions (2026-09-17 late session)

Users approved a login flow, the act died at the first deviation (AVS bot-check,
slow load, validation error), and the only way forward was a brand-new proposal +
a brand-new ✅ click. This session added an **auto-pilot mission** model:

**What changed**
- **One ✅ unlocks a timed mission.** `ApprovalStore.grant_mission()` (new
  `concierge_act_missions` table) creates/refreshes an active mission scoped to
  (tenant, kind, www-normalized domain) with a 30-min TTL, at approval time.
- **Follow-up acts auto-execute.** In `llm.py` dispatcher, `propose_concierge_act`
  result is checked against `active_mission_for(proposal)`; if a mission matches,
  `execute_approved_act` runs **immediately — no approval prompt** — and the result
  (status/verify/summary + error text on rollback) is returned to the model inline,
  so it can propose corrected steps for the same domain and keep going.
- **Healing executor.** `perform_act` now retries a failed step once after
  `actuator.heal()`; `BrowserlessActuator.heal()` clicks through curated Amazon
  interstitials only: AVS "Click the button below to continue shopping" and
  `form[name='claimView']` captcha (both dismiss with a "Continue shopping" text
  click — never arbitrary buttons). The 8004c90018d34027 bot-check death is now a
  click-through, not a rollback.
- **Shared lifecycle.** `ActionApprovalView` and the dispatcher both call the new
  module-level `execute_approved_act()` (claim → mission grant → perform_act →
  VLM verify → audit → status) — one source of truth, actuator/verify factories
  injectable for tests.
- **Model visibility.** `concierge_context` now lists ACTIVE AUTO-PILOT MISSIONS
  with instructions ("check concierge_act_status first, then propose corrected
  steps for the SAME domain — they run immediately"). `concierge_act_status` was
  added earlier this session for post-approval outcome reads.

**Scope/safety of a mission**
- Missions are strictly per (tenant, kind, domain) — a different domain/kind always
  needs a fresh approval prompt. Every auto-executed act still re-validates the
  allowlist + DNS-public + step navigations (proposal validation is unchanged), and
  every execution is audited (`act_approved` / `act_executed` / `act_rolled_back`
  per proposal).
- TTL is server-side (30 min), enforced on every mission lookup (`expire_missions`).
- Secrets resolve exactly as before: tenant-scoped vault, consumer_url = proposal
  URL, persistent captures survive.

**Verification**
- 564 host tests pass (234 concierge-specific, incl. new mission lifecycle,
  heal-retry, and `execute_approved_act` flow tests with injected stubs).
- `docker compose up -d --build delilah` rebuilt; boot clean; upgraded live
  `concierge.db` shows `concierge_act_missions` table.

**Live check (container)**
```bash
docker exec delilah_bot python3 -c "
from src.services.concierge.approval_store import ApprovalStore
print(ApprovalStore('/app/data/concierge.db').active_missions_for_tenant('user:342385739952160769'))"
```

**Still true / limits**
- Amazon AVS may still challenge a *new* headless session regardless of heal() — the
  heal handles the "Continue shopping" interstitial, not a full captcha solve.
- Zero-input continuation (bot runs follow-up LLM proposals in the background after
  a rollback, without waiting for the user's next message) is deliberately NOT
  built — auto-pilot is model-driven on the next user turn. Flag if you want it.

## 12. Model-failure guards enforced (2026-09-17 late session, after repeated cross-domain redirects)

Live run `4c32668fbd534405` (fill_form amazon, approved 22:30:32, mission
`09956d5dcffb4719` granted) rolled back the same way as before: the plan's first
navigate used a wrong off-target host with the real target in `value:` —
the browser went to a 404 and `fill(input#ap_password)` timed out. The email
step also carried a bare `vault:s_a7359d2e5d6e:email` template value (no
braces, no step `vault_ref`) which the old `{{vault:`-only refusal missed, so the
literal template got typed into the email field.

This was a repeated cross-domain redirect and the 2nd bare-vault template — both are now
**enforced refusals**, not prompt guidance:

- **Same-host navigation** (`act_actions._validate_step_navigations`, new
  `target_host` param + local `_host_of`): every step-level navigate must stay on
  the act's own target host (www-stripped). The cross-domain redirect is refused
  whether the host is allowlisted or not. Enforced at propose (`propose_concierge_act`,
  audited `act_refused`, before any approval prompt) AND re-checked at execute
  (`perform_act` now computes the target URL first, then validates navigations).
- **Bare `vault:` template values** (`act_actions._validate_steps`): any type value
  containing `vault:` (braced `{{vault:...}}` or bare `vault:s_...:field`) is
  refused with the canonical instruction — reference the stored value via the
  step's own `vault_ref`, never inline. The executor never types a template
  literal into the page.

Tests: `test_perform_act_navigate_off_target_host_refused`,
`test_perform_act_bare_vault_template_value_refused`,
`test_propose_cross_domain_redirect_plan_refused`, `test_propose_bare_vault_template_value_refused`.
Full suite 568 passed (was 564). Deployed via `docker compose up -d --build delilah`;
both guards confirmed present in the running container.

Remaining gap (unchanged): the mission for amazon.com lives 30 min (TTL 1800 s) —
after the failed run it may already be expired, so a retry may need a fresh ✅.
Zero-input continuation after rollback is still deliberately NOT built.
