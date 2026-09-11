# Delilah Financial OS

> **⚠️ This thing was vibecoded and hacked together.**
>
> I would not expect the highest code quality out of this since it was a
> project made on a whim. If you have bugs please raise them and let me
> know about them or if you have a fix.

A personal finance operating system for Discord. Delilah connects to your
bank data, an LLM, web search, and an isolated sandbox to give you
deterministic financial verification alongside conversational advice.

---

# Guide

## 1. What is this?

Delilah is a Discord bot that gives you financial assistance inside your
server. It reads your bank transactions, runs them through an LLM, and
answers questions about your money. A separate deterministic monitor
watches your ledger in the background and fires alerts even when no one
is talking to the bot.

## 2. Architecture

```
Discord ──► delilah bot (Discord interface, advisor loop, financial monitor)
                │
                ├──► ollama          (LLM backend)
                ├──► searxng         (web search)
                └──► sandbox         (isolated Python/shell execution)
```

The **financial monitor** is a separate, deterministic engine that runs as
its own background task. It evaluates declarative rules against your ledger
on a schedule — no chat message, no LLM required. If the advisor is down,
the monitor keeps watching.

## 3. Features

- **Discord-based financial assistant** — talk to Delilah in your server
- **Plaid synchronization** — bank accounts, transactions, balances
- **Deterministic database verification** — `!dbstatus`, `!integrity`,
  `!txaudit`, `!corrections`, and more bypass the LLM and query SQLite
  directly
- **LLM-powered conversations** — conversational advice grounded in your
  actual ledger data
- **Web search** — SearXNG with multi-engine ranking and content extraction
- **Sandboxed Python execution** — run analysis code in an isolated,
  Landlock-protected container
- **Persistent financial monitor** — rules like "alert if projected balance
  falls below $1,000 within 14 days" run autonomously
- **Gmail integration** — read-only OAuth access to your email
- **Push notifications** — ntfy alerts delivered to your phone
- **Voice memos** — send audio attachments and Delilah transcribes them
  before answering

---

# Installation

## 4. Requirements

- Docker and Docker Compose
- An NVIDIA GPU (for local speech-to-text; optional)
- A Discord application token
- A Plaid account (optional, for bank sync)
- A Gmail account (optional)

## 5. Clone the repo

```bash
git clone https://github.com/allenz2423/financebot.git
cd financebot
```

## 6. Configure your environment

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with at minimum:

- `DISCORD_TOKEN` — from https://discord.com/developers/applications
- `DISCORD_CHANNEL_ID` — the channel you want Delilah to listen in
- `SANDBOX_INTERNAL_TOKEN` — a shared secret for the sandbox container

## LLM setup

Delilah needs an LLM to reason about your finances. The advisor and the
wakeup provider are configured separately — the advisor does the heavy
thinking, the wakeup provider handles lightweight intent detection.

### Ollama (local, default)

Ollama runs as its own container. Pull a model first:

```bash
docker exec ollama ollama pull delilah-gemma-iq4nl:latest
```

Then set in `.env`:

```bash
LLM_PROVIDER=ollama
ADVISOR_MODEL=delilah-gemma-iq4nl:latest
WAKEUP_PROVIDER=ollama
WAKEUP_MODEL=delilah-gemma-iq4nl:latest
```

### OpenAI / OpenRouter (cloud)

Set the provider and point it at any OpenAI-compatible endpoint:

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-or-v1-...
OPENAI_MODEL=openrouter/auto-beta
OPENAI_URL=https://openrouter.ai/api/v1/chat/completions
```

OpenRouter is configured with a free-first fallback: Delilah tries free
models before spending credits. Adjust with:

```bash
OPENROUTER_FREE_FIRST=1
OPENROUTER_MAX_FREE_ATTEMPTS=4
OPENROUTER_FREE_MODELS=nvidia/nemotron-3-super-120b-a12b:free,dots-studio/dots-3-note-preview:free
OPENROUTER_PAID_MODEL=openrouter/auto-beta
```

### Verify

Restart the bot and check the boot banner — it shows which model is
loaded:

```bash
docker compose restart delilah
docker logs delilah_bot | grep -i "Conversational Advisor"
```

Optional credentials:

- `OPENAI_API_KEY` — for OpenAI Whisper transcription (alternative to local)
- `PLAID_CLIENT_ID` / `PLAID_SECRET` — for bank synchronization
- `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` — for Gmail access
- `JINA_API_KEY` — improves webpage content extraction
- `NTFY_TOPIC` — for push notifications to your phone

## 7. Set up Gmail OAuth (optional)

If you want `!gmail connect`, copy your Google OAuth credentials into the
secrets folder. The repo ships with a placeholder; replace it with your
real file:

```bash
cp /path/to/your/gmail-client.json secrets/gmail-client.json
```

The real file is gitignored and never committed.

## 8. Start the stack

```bash
docker compose up -d --build
docker logs delilah_bot -f
```

This starts six services: the bot, Ollama, SearXNG, the sandbox, the
sandbox installer, and a Squid proxy. The bot binds to port 8000,
SearXNG to 8080, and Ollama to 11434.

## 9. Invite the bot

Add the bot to your Discord server. Run `!help` in-channel to see the
full command reference.

---

# GPU speech-to-text (optional)

## 10. Local transcription on your GPU

Voice memos work out of the box using the OpenAI Whisper API. If you want
local transcription on your GPU instead, build with CUDA enabled:

```bash
docker compose build --build-arg USE_CUDA=1 delilah
docker compose up -d --force-recreate delilah
```

Then set in `.env`:

```bash
STT_BACKEND=local
STT_LOCAL_MODEL=base
STT_LOCAL_DEVICE=cuda
STT_LOCAL_COMPUTE=int8
```

## 11. CPU fallback

Without a GPU, set `STT_LOCAL_DEVICE=cpu` and it transcribes on CPU —
slower but fully functional.

---

# Configuration

All configuration lives in `.env`. See `.env.example` for every available
variable and its purpose. Never commit `.env` — it contains your Discord
token, Plaid credentials, and Gmail OAuth secrets.

---

# Commands

Run `!help` in-channel for the full interactive reference. It's paginated
with Previous/Next/Close buttons, and you can jump to a section directly:

```
!help           # full reference, paginated
!help monitor   # jump to the Financial Monitor section
!help gmail     # jump to the Gmail section
```

### Advisor controls
`!status` `!peek` `!auditstatus` `!pause` `!cancel` `!switch` `!model`

### Database verification (deterministic, no LLM)
`!dbstatus` `!unlocked` `!locked` `!override` `!corrections`
`!merchantstatus` `!researchstatus` `!snapshot` `!integrity` `!txaudit`

### Financial monitor
`!monitor list` `!monitor add` `!monitor run` `!monitor alerts`
`!monitor ack` `!monitor on/off` `!monitor del`

### Ledger utilities
`!chart` `!export` `!clear` `!clearchat` `!setbudget` `!checknow`
`!plaidstatus` `!plaidsetup` `!plaidlink` `!fixbank` `!testpush`
`!ntfysetup` `!gmail connect/status/disconnect`

---

# Financial monitor rules

The monitor supports eight rule kinds. Each can be created with
`!monitor add <kind> <name> <json-config>`:

| Kind | Fires when |
|---|---|
| `projected_balance_low` | Projected balance drops below a threshold |
| `category_spend_exceeded` | Spending in a category exceeds a limit |
| `income_overdue` | Expected income hasn't arrived past its due date |
| `subscription_price_changed` | A recurring charge jumps by a percentage |
| `unusual_transaction` | A charge deviates from your normal pattern |
| `recurring_bill_missing` | A known recurring bill hasn't posted |
| `cash_flow_change` | Monthly cash flow shifts significantly |
| `large_deposit` | A deposit above a threshold lands |

---

# Testing

```bash
pytest -q
```

Tests cover the financial monitor engine, financial-correctness invariants,
and the security utilities (SSRF protection, path traversal, SQL injection
prevention, rate limiting).

---

# Security

- All financial mutations go through parameterized, user-scoped queries
- SSRF protection blocks localhost, private IPs, and internal domains
- The sandbox runs read-only with Landlock, rlimits, and dropped
  capabilities
- Secrets live in `.env` and are never committed

---

# License

This is a personal project. See `SECURITY.md` for the security policy.