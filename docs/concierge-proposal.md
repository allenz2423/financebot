# Concierge Layer — Design Proposal (Working Draft)

Status: **design frozen (2026-09-17), not built.** Discussion-only record of the
"compensate for her speed + give Delilah real credentials" proposal. Nothing here is
implemented; nothing here is safe to assume exists in code. Remaining open items (§4)
are implementation/product parameters, not reasons to reopen the architecture.

Hard constraints this design must respect:

- **No per-user fine-tuning / QLoRA, ever.** Open-source project; the code stays generic
  and tenant-agnostic. (Permanent decision.)
- **Free-tier-first.** LLM routing stays on the local OmniRoute gateway; paid fallback only
  when the free tiers can't do the job.
- **Safe by default in the public repo.** Any concierge capability ships behind
  `ENABLE_CONCIERGE_*` flags; with flags off, zero concierge processes run.

---

## 1. The proposal (recap)

Two problems, one system:

1. **Latency** — turns take a while (68s+ cloud round-trips when tools error); users click
   off before the first token lands.
2. **Concierge capability** — give Delilah real credentials (credit card, merchant accounts)
   so she can *do* things, not just answer — behind approval gates and strict per-tenant
   isolation.

### The three workstreams

| Workstream | One-liner | Status |
|---|---|---|
| **A — Latency playbook** | Compensate for speed without new models: full-context injection with a size gate, claim-folding scheduler on the existing digest machinery, first-token discipline (ack + plan within seconds, heavy reasoning async) | Outline only |
| **B — Concierge layer** | Draft → Approved → Scheduled execution of real-world actions, approval gates, per-tenant vault, sandboxed browser, visual audit trail | **Specified below** |
| **C — Request-cap economics** | Semantic dedupe (don't re-ask what's in the KG), local-first escalation routing, overnight batching; mail rollups (algebra over ~14.7k emails instead of raw chunking) | Outline only |

A and C are parked — this document specifies **B**.

---

## 2. B — The concierge layer

**Shape:** Delilah gets a second, authority-gated execution path. She can draft and propose
real actions, but nothing touches money/accounts until an authorized human signs it.
Defense-in-depth around one state machine.

### 2.1 Action taxonomy — three risk tiers

| Tier | Examples | Approval |
|---|---|---|
| **read** | order status, tracking, price watch, account balance | none (still sandboxed + audited) |
| **write** | send an email, schedule, fill a form you saw | one-tap approve |
| **spend** | buy on a merchant, pay a bill, move money | full approve + caps + TTL |

Splits the build: read-tier is where you learn (cheap, safe); spend-tier is where you trust.

### 2.2 The state machine (the authority boundary)

```
draft → approved → executing → confirmed
         └→ expired (TTL ~10 min)
         └→ denied
         └→ escalated ─────────────────┐   (captcha / 2FA / risk wall, see 2.5)
                                        ▼
                              human solves live → re-verify → executing
```

Rules:

- **Draft comes from the model, never executes anything.** The only LLM tool is
  `request_concierge_action(action_type, args)`, which *always* returns a `draft_id` and
  renders a Discord approval card. There is no execute path reachable from a chat turn.
- **Approval is informed, fast, and short-lived.** Card shows: what, vendor, price, total
  impact, fallback. One tap. The yes **expires (~10 min)** — a stale yes must not
  authorize today's action at today's price.
- **Execute re-validates everything at execution time** (`src/services/concierge/executor.py`):
  price still matches, budget still available, target domain still allowlisted, approver
  still authorized, tenant still enabled.
- **No auto-approve under a cap, ever (v1).** A daily cap shortens approval to one tap;
  it never skips it. Silent auto-execute + one stale approval + one injected page = money
  gone.

### 2.3 Approval UX

Discord `View`/button cards — the same machinery already hardened for the questionnaire
forms (`form_flow.py`): `[✅ Approve] [✕ Deny]`, owner-checked, timeout/TTL, ephemeral
follow-ups. Screenshots attach to the card so approval happens with eyes open.

### 2.4 The visual gate (screenshots)

| Capture point | What it proves |
|---|---|
| **pre-state** (draft time) | the page as-is: item, price, cart state |
| **pre-click** (before submit) | the filled form, right before "Place Order" — last chance to see a wrong quantity or a flipped checkbox |
| **post-action** | the receipt page / outcome |

- Approval card = button view + pre-state PNG (`followup.send(..., files=[png])` works
  ephemeral).
- **Model self-verification**: a cheap VLM pass (free-tier vision model) on each capture
  must confirm *page price == draft price, item == cart item, button == expected* or the
  draft is refused/diffed. Two eyes: the VLM (reliability) and the human (trust).
- **Screenshots hash-chain into the audit log** — a visual, tamper-evident record of every
  action.

### 2.5 Bot-detection avoidance + human escalation

The honest frame: merchant bot defense is **account-level risk scoring**, not per-request.
Goal: never look like a different person than the account's normal human → **consistency
over cleverness**.

| Surface | Mitigation |
|---|---|
| `navigator.webdriver` / CDP artifacts | `patchright` (maintained undetected-chromedriver fork), not raw CDP |
| headless tells (UA, plugins, `window.chrome`) | **headed Chromium on Xvfb** — real browser, not headless |
| WebGL renderer = `SwiftShader` (software) | real GPU via `--use-gl=angle --use-angle=vulkan` (Mesa RADV) |
| fresh browser every time / cold profile | **persistent warm profile** volume per tenant |
| robotic behavior | humanized input: bezier mouse, scroll-then-read, realistic cadence |
| datacenter IP | residential egress (reference deployment: home host — already residential; a VPS/colo deployment must solve IP reputation *first*) |
| **captcha / 2FA / risk walls** | see escalation below — a wall is a **hard stop**, never auto-solved, never retried |

**Escalation handoff (noVNC):**

```
[executing] ── VLM sees wall in pre-click capture ──▶ [escalated]
   Delilah DMs: "⚠️ wall detected — solve it live: [noVNC link]" + screenshot + [✅ Done]
   User opens noVNC tab → drives the real X display → solves with own mouse/kbd
   User clicks [✅ Done] → re-capture → VLM confirms wall gone → resume
```

- Container runs Xvfb + a lightweight WM + patchright-headed Chromium + x11vnc + noVNC
  (web client).
- **Never auto-solve captchas.** Auto-solving is itself a stronger fraud signal than any
  other behavior, and it is the line past which the system is indefensible. Wall ⇒ pause ⇒
  human. 2FA/OTP-code walls use the same state + handoff.
- noVNC: bound to `127.0.0.1` by default, one-time URL token, listener lives only while a
  session is escalated (spawn on escalate, teardown on resolve). Remote solving = SSH
  tunnel / Tailscale, not an open port.

### 2.6 Acceleration: iGPU vs CPU (both first-class)

Config knob, per deployment:

```yaml
environment:
  - CONCIERGE_ACCEL=igpu        # igpu | cpu   (public repo default: cpu)
# mounted only when CONCIERGE_ACCEL=igpu:
devices:
  - /dev/dri/renderD129:/dev/dri/renderD129     # example node; varies per host
group_add:
  - "105"                                       # host render GID; varies
```

| mode | Chromium flags | WebGL reports | cost |
|---|---|---|---|
| `igpu` | `--use-gl=angle --use-angle=vulkan` (Mesa RADV) | real GPU renderer string | host must expose a DRM node |
| `cpu` | `--disable-gpu --use-gl=swiftshader` | `Google SwiftShader` | none — pure CPU |

- Xvfb, warm profile, noVNC, squid egress, container lifecycle are identical in both modes
  — only Chromium's GL path branches.
- Reference deployment: host has a dGPU (olama's) + an AMD APU iGPU; the browser gets the
  iGPU (`/dev/dri/renderD129`), zero contention with inference.
- `igpu` is recommended for real merchants (real renderer string, smooth canvas); `cpu` is
  the portable OSS default. README note for self-hosters: `ls /dev/dri` → one line to
  upgrade.

### 2.7 Execution model: disposable container per session

Container lifecycle == action lifecycle:

```
base image: concierge-browser (Xvfb + patchright + noVNC + mesa) — immutable
[action starts]  spawn container: --device <accel> · mounts: decrypted tenant
                 profile (tmpfs, see 2.10), scratch, audit · network: per-tenant
                 squid allowlist only
[normal]         done → container exits → removed
[escalated]      container pinned alive while the human solves (noVNC lives inside it)
[failure/wall]   container dies → removed → state back to draft
```

Why: rollback becomes free (base is pristine; next action starts identical + warm profile),
merchant POV is indistinguishable from a browser restart, the host stays clean (nothing
running when flags are off), and cross-tenant contamination becomes structurally impossible
(the mount table is the isolation). The warm profile carries identity — not the container
instance — and it exists on disk only inside the vault, encrypted (2.10); the container
sees a tmpfs copy that dies with it.

### 2.8 Multitenancy (hard rules)

> **Tenant identity is never model-supplied.** It derives from the Discord interaction
> (`interaction.user.id` → `tenant_id`), is stamped on every artifact, and validated at
> every boundary. A model-emitted foreign tenant id is dropped and the interaction's used,
> or the call is refused.

Per-tenant partitions:

| Layer | Unit |
|---|---|
| browser state | encrypted tenant profile in the vault, decrypted to tmpfs only inside the action container (2.10) — a shared profile is a cross-tenant login leak |
| credentials | vault rows keyed `owner_uid`, encrypted at rest, reads always filtered |
| approval | owner-checked Discord buttons (existing `_is_owner` pattern) |
| audit | `concierge-audit/{tenant}/` — events + screenshots, hash-chained |
| budget/caps | per-tenant config row |
| egress allowlist | per-tenant squid ACLs |
| escalation | per-tenant noVNC session, token-scoped (two tenants never share a display) |

Concurrency: two tenants acting at once = two containers, two profiles, two token-scoped
VNC links. Contamination requires mounting the wrong volume — a single line to review.

Prerequisite: **KG rows on `user:current` must become real UIDs** before tenant isolation
is provable (known dataset wart).

Test pillar (B0): cross-tenant attempts fail hard — tenant B clicking tenant A's approve
button, tenant B reading tenant A's audit, model emitting a foreign tenant — each is a test,
each ends in `denied` + an audit row.

### 2.9 Admin command plane

TTY-style prefix commands (deterministic bot code, **not** an LLM tool — the model can
never be prompted into provisioning access; the message path short-circuits before the
LLM for anything with the command prefix).

```
!concierge enable  @user          — provision tenant (read-tier only, by default)
!concierge disable @user          — kill active sessions, freeze vault, deny drafts
!concierge tier    @user read|write|spend   — raise/lower authority
!concierge limit   @user $X/day   — spend cap
!concierge allow   @user <domain> — extend egress allowlist
!concierge status  [@user]        — provisioning, tier, caps, active sessions
!concierge kill    @user          — emergency: kill every live session, drop containers
!concierge audit   @user          — pull the tenant's hash-chained trail (incl. screenshots)
```

Rules:

1. **Admin comes from config only** (`CONCIERGE_ADMINS` in env). Chat can never elevate
   anyone; there is no admin-add path. A hijacked model can at most *suggest* a command —
   the handler still checks authorship.
2. **Tenant id comes from the mention** (`ctx.message.mentions[0].id`), never from text.
3. **Enable grants the minimum**: a provisioned tenant starts at read-tier, empty
   allowlist, `$0` cap. `tier` / `limit` / `allow` are separate, explicit, audited actions.
4. **Every admin action is audited**: `admin_uid, verb, target_uid, timestamp` on the
   admin's own chain and the tenant's.
5. **Disable is a hard stop**: kills containers, blanks the vault namespace to denial,
   refuses tool calls. Re-enable is a fresh provisioning decision.

What `enable` provisions: tenant record (`owner_uid, tier=read, daily_cap=0, state,
enabled_by, enabled_at`), profile namespace, vault namespace, default read-tier allowlist,
audit chain.

### 2.10 The vault (credentials + cards)

Greenfield: the codebase currently has **no crypto anywhere** (only a web-session
`SECRET_KEY` in `src/config/settings.py`, with an env-or-generate bootstrap precedent).
New code lives in `src/security/` (currently just `__init__.py` + `utils.py`).

**Master key:** bootstrapped like `SECRET_KEY` — env-or-generate, written once to a file
readable only by the bot process. Not in `.env`/compose env (too visible).

**Login info — cookies/tokens, never passwords (where avoidable):** MFA makes password
login impractical, so merchant creds are session cookies/tokens held as a per-tenant
browser profile. The profile is **encrypted at rest inside the vault** and materialized
into the disposable container's `tmpfs` only at spawn — the container dies, the cookies
die with it.

**Credit cards:**
- **Ingress is a self-hosted web page, never Discord** (2.10c) — the PAN never
  transits Discord's platform, chat, modals, tool args, or the model at any point.
- **Tokens + masked last4 wherever a wallet/token path exists** (Amazon wallet,
  PayPal, Stripe/Plaid tokens for bill pay) — the merchant holds the PAN, we hold a
  pointer. Zero PAN storage on those.
- **Encrypted-PAN fallback** (AES-GCM) only for merchants without a token path.
- **CVV is never stored. Ever.** PCI non-retention; forcing re-entry per purchase is
  itself a fraud brake.
- **Confinement rule:** a PAN exists in exactly two places — the vault, and a
  memory-mapped decrypt inside the ephemeral action container at execution time. Its
  only exit is a POST to the merchant's payment endpoint (the squid allowlist is the
  enforcement). It can never appear in model context, tool output, chat, audit logs, or
  screenshots. UI shows masks only (`•••• 4242`).

**Rules:**
- Every entry keyed `owner_uid`; every read `WHERE owner_uid = ?`.
- **The model never sees credential values** — the browser holds them; tool outputs
  redact; there is no "dump session" path.
- Audit logs record actions, never secrets. Rotation/revocation on demand from the
  admin surface.

### 2.10b Credential & spend security ("no funny business")

The bar is **structural impossibility, not discouragement**. Two planes:

**Money plane** (stops the mistake/abuse before it exists):
- **Price-diff tolerance** — approve at $X, execute only within X±tol, else re-draft.
- **Caps** — per-action, per-day, per-merchant, per-tenant; hard-stop at the day cap.
- **Velocity flags** — first purchase on a fresh session, odd-hour purchases → flag,
  not execute.
- **Checkout-surprise detection** — pre-click screenshot + VLM catches flipped
  "Subscribe & Save" checkboxes and quantity changes.
- **Payee/recipient allowlist** for any money movement; merchant allowlist enforced at
  egress.

**Trust plane** (stops exfiltration of the credentials themselves):
- **No secret transits Discord** — credentials enter only via the self-hosted
  capture server (2.10c); nothing sensitive ever appears in chat, modals, tool
  args, or model context.
- **Capture redaction policy (decided):** secrets (PAN/CVV/password/OTP fields) are
  redacted **in audit-stored screenshots**; the **live noVNC session shows them raw**
  (it is the authorized human solving walls in real time). Redaction happens at capture,
  before anything is written to the audit chain.
- **Clipboard + download restricted** inside the concierge browser.
- **Autonomous risk monitor** — reuse `src/services/monitor.py` architecture:
  pure-Python rule allowlist (no LLM, no dynamic code, no eval), rules owned per
  `user_id`, watching the hash-chained audit for velocity/price-diff/cap anomalies →
  Discord alert. It cannot be prompt-injected because it is not an LLM path.
- **Model never sees secrets** (2.10) — nothing to exfiltrate even in a total
  hijack.

### 2.10c Credential lifecycle: capture intents + describe/resolve

**Core principle (data plane): credentials are capabilities, not data available to
Delilah.** The manifest exposes *what capability exists* — never enough information to
reconstruct the secret. Invariant: **no API whose return type could contain plaintext
exists** — which is stronger than "the LLM isn't supposed to call resolve"; there is
simply no call to make.

**Core principle (human-consent plane): credential capture is an explicit
human-consent surface, not an agent capability.** Delilah may construct a typed
capture request, but she cannot collect, inspect, validate beyond the declared schema,
or receive the submitted secret. The capture UI presents the declared intent to the
user, and the user must explicitly provide or refuse the credential. Risk monitoring
independently observes capture activity for anomalous behavior.

These two principles describe the two halves of the system: the model cannot obtain
the secret, **and** it cannot silently compel the human to provide one.

**Ingress — a web page we own, never Discord.** Discord modals, chat commands, and
with-secrets tool args are all out: the PAN would transit Discord's platform, and chat
history/tool logs would retain it. Instead the bot's own FastAPI app (`app` in
`src/core/state.py`, uvicorn :8000) serves a capture page, following the existing
`/linkbank/{session_id}` precedent (token-gated GET page + server-side POST exchange).

**One generic engine — the page is the universal intake.** There are no per-kind
pages: a card, an Amazon login, a captcha answer, a Brightspace login all render from
the same validated `CaptureIntent`. Kind/types/retention are declared schema, not code
branches. Schema-forced rules: `card_cvv` and `captcha` fields must be
`retention: ephemeral` (CVV/captcha answers can never be vaulted); `card_pan`,
`card_exp`, `card_cvv` are only valid under `kind: payment`.

**CaptureIntent — the model can ask for anything.** The model proposes what it needs;
the system renders whatever the validated intent describes (the same normalizing role
`validate_form_schema` plays for questionnaires):

```python
CaptureIntent:
  tenant: str            # interaction-derived, never model-supplied
  token: str             # one-time, hashed at rest
  label: str             # "Brightspace login" — what the page says it's for
  explanation: str       # why Delilah is asking (rendered verbatim, consent-first)
  fields: [              # allowlisted types; UNKNOWN TYPES ARE REJECTED, never coerced
    {type: text|password|otp|card_pan|card_exp|card_cvv|captcha|token|url,
     label, required, retention: vault|ephemeral}
  ]
  kind: secret | payment | session_profile    # taxonomy below
  consumer_scope: [domains]                   # immutable after capture
  expires_at: ~10 min
```

**Credential taxonomy — kind is descriptive; delivery mode determines privilege:**

```
kind ──▶ delivery_mode        (assigned by the system/schema, never selected by the model)
  secret          resolve_plaintext
  payment         resolve_plaintext + PCI rules
  session_profile mount_only
```

| kind | example | delivery_mode | executor privilege |
|---|---|---|---|
| `secret` | API key, password, Brightspace login | `resolve_plaintext` | inject into the narrowest field/header the action allows |
| `payment` | card (token or encrypted PAN) | `resolve_plaintext` + PCI rules | autofill the merchant's payment field only; CVV never stored |
| `session_profile` | logged-in Amazon browser profile | `mount_only` | can only be mounted AS a browser profile; no plaintext path exists, not even internally |

**`delivery_mode` is assigned by the system/schema, validated by the executor — never
selected by the model.** A model cannot request `session_profile` and somehow ask for
`resolve_plaintext`; the mapping is fixed at schema time. `session_profile` is not
"another token" — it is a *session capability*: the executor cannot request its value;
it can only boot the disposable container with it mounted.

**Consent-first by construction:** the page renders the intent verbatim — what, why,
retention, consumer scope — and the tenant types or refuses on *our* page. The model
proposes; it can never collect. The risk monitor flags capture anomalies: repeats
(already-stored secrets), wide/no consumer scope, capture storms.

**Invariants (each one is a B0 test):**
1. Unknown field types → rejected. Coercion is forbidden — it could silently break the
   masking promise (a `password` asked as `text`).
2. Token is single-use.
3. Token is tenant-bound.
4. Token is intent-bound.
5. Token expires (~10 min).
6. Submitted values never enter normal application logs — nor error messages, audit
   metadata, or DM text. Logs say `card added: •••• 4242`, never more.
7. `ephemeral` values cannot become vault records (no promotion path).
8. `vault` values are encrypted before persistence.
9. Consumer scope is immutable after capture; the executor refuses to mount a secret
   outside its scope.
10. The model receives only success/failure + capability metadata (masks) — **never the
    submitted value, even incidentally**. "The user entered OTP 123456" in a tool
    response is a leak and is forbidden.

**Login intake** is Discord-free: an "add login" request spawns a fresh profile
container with noVNC; the user logs into the merchant live on our own screen; cookies
land in the profile, which encrypts into the vault as a `session_profile` on
completion. Passwords are typed only on the merchant's own site.

**Reachability (decided):** the capture page is public via a **domain + Let's Encrypt**
reverse proxy (Caddy) in front of :8000. Only capture + existing flows are proxied;
rate-limited; access logs never capture POST bodies.

**Retrieval — the describe/resolve split.** The model reasons over a mask-only
manifest injected into tool context:

```json
{"cards":[{"vault_ref":"card_7","type":"payment","scope":["amazon.com"],
           "display":"•••• 4242","policy":"payment_policy/default"}]}
```

- **Manifest is live-derived per turn** — recomputed from the vault on every turn:
  additions appear on the next turn; revocation/rotation/disable takes effect on the
  next turn; the executor refuses a `vault_ref` that is revoked or foreign to the
  tenant, even if it lingers in model context.
- **Draft:** `request_concierge_action(..., vault_ref=...)` — chosen from the manifest,
  never invented.
- **Resolve (executor-only):** re-validates tenant, kind, delivery mode, consumer
  scope, and an **approved draft exists**; decrypts **inside the disposable action
  container only**; secret/payment → narrowest autofill; session_profile → mounted.
  Container dies; plaintext dies with it.
- **No resolve tool is exposed to the model** — there is no call whose return type
  could contain plaintext (invariant above).
- **Policy beats preference:** a per-tenant `payment_policy` (default card, cap,
  allowed merchants) lives in the vault; the executor enforces it and the manifest
  reflects it. The model can suggest a card; policy decides.

Full lifecycle: **intent → human supplies on our page → vault (encrypted) or
ephemeral → manifest (masks) → draft (vault_ref) → approved → resolve inside
disposable container → gone.** The model never touches a raw value; a raw value never
touches Discord; plaintext never outlives the action container.

### 2.11 Network topology

- Scraper browser (`browserless`, shared, general egress) stays **separate** from the
  concierge browser — the scrape path ingests untrusted content; the concierge acts on
  approved content. Blurring that boundary is the injection hole.
- Concierge browser on a proxy-only network: squid with per-tenant allowlists; no general
  egress. A fully prompt-injected model still can't exfiltrate — there's nowhere for the
  bytes to go. Squid logs are themselves an audit trail.

---

## 3. Build order (all flag-gated)

| Phase | Scope | Exit criteria |
|---|---|---|
| **B0** | Admin command plane; tenant records; audit log (append-only, hash-chained); draft/approve state machine; executor skeleton with untrusted-data rejection; cross-tenant + admin-gating test pillars; fix `user:current` wart; **vault bootstrap** (master key + `src/security/vault.py` + encryption module + credential-confinement tests); **generic capture-intent page + one-time tokens** (CaptureIntent schema, allowlisted types + unknown-type rejection, vault/ephemeral retention, immutable consumer scope, kind taxonomy `secret|payment|session_profile` with schema-assigned `delivery_mode` (`resolve_plaintext`/`mount_only`), no-value-in-response invariant — FastAPI route, `/linkbank` precedent, TLS, no-body-logging) **with the 10 invariant tests**; **capture-redaction pipeline** (audit-redacted / VNC-raw) with unit tests | suite green; no real integrations |
| **B1** | Read-tier through the egress sandbox: order status / tracking / price watch; stateless screenshot capture into DMs; **risk-monitor skeleton** (`monitor.py` rule kinds over concierge audit events); logged-in read-tier (requires the vault) | proves vault + sandbox + audit + visual capture + anomaly alerts with zero money risk |
| **B2** | Write-tier: email / schedule / forms with one-tap approve + pre-state/pre-click screenshots + VLM self-verify | visual approval gate live |
| **B3** | Spend-tier: merchant actions with caps, price-diff tolerance, TTL'd approval, receipts; headed/Xvfb + iGPU profile; tokenized cards + encrypted-PAN fallback (2.10); CVV re-entry via the capture page (never stored); snapshot-free rollback via disposable containers | end-to-end approved spend |

---

## 4. Open decisions

**Resolved (2026-09-17):**
- **Card storage** — tokens + encrypted-PAN fallback; CVV never stored (2.10). ✅
- **Risk monitor** — autonomous `monitor.py`-pattern anomaly watcher is in scope (2.10b). ✅
- **Capture redaction** — redact in audit-stored screenshots, raw in the live noVNC
  session (2.10b). ✅
- **Credential ingress + capture reach** — self-hosted capture page (never Discord),
  reached via **domain + Let's Encrypt** (2.10c). ✅

Still open:
1. **Auto-approve under a daily cap** — ruled *no on v1* (2.2). Revisit only with a
   receipts + rollback story.
2. **Merchant adapter strategy** — deferred until a second executor backend exists:
   browser automation is the first (and only) backend; introduce a `MerchantAdapter`
   abstraction only when a materially different backend shows up.
3. **Approval TTL strictness** — 10 min proposed; too-annoying approvals defeat the
   concierge point.
4. **VLM self-verify model tier** — free vision model (weaker) vs paid for spend-tier only.
5. **VNC exposure model** — `127.0.0.1` + tunnel default; revisit if remote solving from
   phone becomes the norm.

---

## 5. Current-stack touchpoints (for implementation)

- `docker-compose.yml`: `browserless` (shared scraper — keep separate), `sandbox`
  (read-only, no egress — a template for the concierge image), `pypi-proxy` (squid —
  pattern for the allowlist proxy), `egress` vs `sandbox-egress` networks, GPU device
  reservation pattern (`deploy.resources.reservations.devices`).
- `src/bot/commands.py`: `@bot.command(...)` prefix-command pattern (`checknow`) +
  message short-circuit on `bot.command_prefix` — the `!concierge` integration point.
- `src/bot/form_flow.py`: the View/button + owner-check + ephemeral follow-up machinery —
  the approval-card pattern.
- `src/services/form_flow.py`: session/state-layer separation (pure logic, Discord-free) —
  the shape for `src/services/concierge/*`.
- `src/services/monitor.py`: LLM-free, rule-allowlist, per-`user_id`-owned monitoring
  engine with Discord alert delivery — the architecture the concierge risk monitor reuses
  (2.10b).
- `src/security/` (greenfield): home for `vault.py` + encryption; the
  env-or-generate bootstrap precedent is `SECRET_KEY` in `src/config/settings.py`.
- `src/core/state.py`: the FastAPI app (`app`, uvicorn :8000) — the credential-capture
  route lives here, following the `/linkbank/{session_id}` token-gated GET+POST pattern
  in `src/bot/commands.py` (bank creds already enter this way today).
- TLS/reach: Caddy + Let's Encrypt reverse proxy in front of :8000 for the public
  capture path — only capture + existing flows proxied, rate-limited, access logs never
  capture POST bodies.