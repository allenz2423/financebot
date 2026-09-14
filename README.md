# Delilah Finance Bot

> **⚠️ This thing was vibecoded and hacked together.**
>
> I would not expect the highest code quality out of this since it was a
> project made on a whim. If you have bugs please raise them and let me
> know about them or if you have a fix.
> This project is vibecoded slop. Do not complain if functionalities are
> broken. Just fork and fix. I'm sure your agentic harness could cobble
> up some slop fix too.

A personal finance operating system for Discord. Delilah connects your live bank data, local/cloud LLMs, web search, Stirling PDF form engines, headless Chromium browsers via MCP, and an isolated code execution sandbox to provide **deterministic financial verification alongside proactive, conversational advisory**.

---

## Architecture

```
Discord ──► Delilah Bot (Discord interface, advisor loop, deterministic monitor)
                │
                ├──► Ollama / OpenRouter (LLM backend)
                ├──► Active World Model  (Entity-Claim-Dossier Knowledge Graph)
                ├──► Progressive Disclosure (3-layer context priming & on-demand retrieval)
                ├──► MCP Web Browser     (FastMCP headless Chromium & page scraper)
                ├──► Stirling PDF        (Self-hosted fillable government & tax PDF engine)
                ├──► SearXNG             (Private multi-engine web search)
                ├──► Qdrant Vector Store (Semantic memory & knowledge embeddings)
                └──► Sandbox Execution   (Isolated Landlock Python/Shell execution)
```

### Core Philosophy
1. **Progressive Disclosure (Claude-Mem Pattern)**: Rather than polluting the LLM's prompt with hundreds of unprompted ledger rows on every turn, Delilah injects a **Layer 1 State Index** (~250–350 tokens) showing domain summaries, estimated token retrieval costs, and on-demand tool mappings. The agent selectively fetches Layer 2/3 details only when relevant.
2. **Deterministic Background Monitor**: A standalone engine evaluates declarative financial rules (runway alerts, budget thresholds, unusual charges) directly against SQLite on a schedule. No chat message or LLM invocation is required.
3. **Active World Model**: Relational facts, account schedules, constraints, and debts are tracked via an explicit Entity-Claim Knowledge Graph rather than ephemeral chat memory.
4. **Model Context Protocol (MCP)**: Heavy, stateful services (such as browser rendering and dynamic DOM scraping) run as isolated FastMCP microservices communicating over standard SSE transports.

---

## Features

- **Discord-Based Financial Assistant** — Conversational financial reasoning grounded in real-time verified data.
- **Plaid Synchronization** — Automated and on-demand sync for checking, savings, credit cards, loans, and investment accounts.
- **Deterministic Database Invariants** — `!dbstatus`, `!integrity`, `!txaudit`, `!corrections`, and `!snapshot` query SQLite directly, completely bypassing LLM hallucination.
- **Progressive Context Priming** — High-efficiency prompt architecture with token-cost awareness and on-demand retrieval.
- **MCP Web Browsing & Scraping** — Native FastMCP web scraper container (`mcp-web-browser`) over SSE with CDP fallback to Browserless.
- **Government & Financial PDF Automation** — `find_government_forms` and `fill_pdf_form` integrated with self-hosted Stirling PDF.
- **Executive LaTeX & PDF Generation** — Instant publication-grade financial audits compiled in-sandbox via `pdflatex` with modern typography and executive summaries.
- **Active Financial Monitor** — Autonomous alerts for cash flow deviations, overdue income, upcoming bill pressure, and impulse spending.
- **Gmail Integration** — Read-only OAuth access for tracking order confirmations, merchant invoices, and price drop refunds.
- **Credit & Debt Optimizer** — Revolving utilization tracking, payoff simulators (Avalanche/Snowball), and card reward tier matching.
- **Voice Memos** — In-channel voice note transcription (local Whisper GPU or OpenAI Whisper).

---

## Stack & Services

The stack is containerized with Docker Compose:

| Service | Port | Description |
|---|---|---|
| `delilah` | `8000` | Core Discord bot, advisor loop, and background watchdog tasks |
| `ollama` | `11434` | Local LLM inference server (GPU-accelerated) |
| `searxng` | `8080` | Privacy-respecting meta-search engine |
| `mcp-web-browser` | `3000` | FastMCP browser automation & scraping server (SSE) |
| `browserless` | `3002` | Headless Chromium instance for Playwright / CDP |
| `stirling-pdf` | `8085` | PDF form field detection and auto-filling engine |
| `qdrant` | `6333` | Vector database for semantic search & knowledge retrieval |
| `sandbox` | `8100` | Landlock-isolated container for Python/shell execution |
| `pypi-proxy` | `3128` | Squid caching proxy for isolated sandbox package installs |

---

## Quick Start

### 1. Requirements
- Docker & Docker Compose v2+
- Discord Bot Token & Application ID
- NVIDIA GPU (Optional, for local Ollama and local Whisper STT)

### 2. Clone & Configure
```bash
git clone https://github.com/allenz2423/financebot.git
cd financebot

cp .env.example .env
```

Edit `.env` with your minimum required credentials:
```env
DISCORD_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=your_discord_channel_id
SANDBOX_INTERNAL_TOKEN=generate_a_random_token_here
```

### 3. LLM Configuration

#### Local Inference (Ollama)
```bash
# Pull your desired model
docker exec -it financebot-ollama-1 ollama pull delilah-gemma-iq4nl:latest
```

In `.env`:
```env
LLM_PROVIDER=ollama
ADVISOR_MODEL=delilah-gemma-iq4nl:latest
```

#### Cloud Inference (OpenRouter / OpenAI)
```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-or-v1-...
OPENAI_MODEL=openrouter/auto-beta
OPENAI_URL=https://openrouter.ai/api/v1/chat/completions
```

### 4. Vector Embeddings Configuration (Qdrant)
Delilah supports both **100% free local CPU embeddings** and **cloud embeddings**:

```env
# Choose: "local" (Ollama 100% CPU, 0 MB VRAM) or "cloud" (OpenRouter/OpenAI) or "auto"
EMBEDDING_BACKEND=local

# When local:
EMBEDDING_LOCAL_MODEL=nomic-embed-text-cpu

# When cloud:
EMBEDDING_CLOUD_MODEL=text-embedding-3-small
```

### 5. Run the Stack
```bash
docker compose up -d --build
docker logs delilah_bot -f
```

---

## Command Reference

Run `!help` in your Discord server for the interactive reference with button pagination:

```
!help           # Full interactive reference
!help monitor   # Jump to Financial Monitor section
!help gmail     # Jump to Gmail integration section
```

### Advisor & Brain
- `!status` — View current advisor state, active model, and token usage
- `!peek` — Inspect live tool-call trace and thought stream for the current turn
- `!pause` / `!cancel` — Intercept or halt running advisor loops
- `!switch` / `!model` — Toggle between local Ollama models and cloud providers

### Deterministic Database Tools (No LLM)
- `!dbstatus` — Report account balances, ledger counts, and database sync status
- `!integrity` — Run database consistency checks and transaction audits
- `!txaudit` — Audit transaction categorization and ledger integrity
- `!corrections` — Inspect human overrides and correction logs
- `!snapshot` — View point-in-time financial balances

### Financial Monitor
- `!monitor list` — Display all registered monitoring rules
- `!monitor add <kind> <name> <config_json>` — Add an autonomous rule
- `!monitor run` — Trigger an immediate evaluation pass
- `!monitor alerts` — View recent firing history
- `!monitor ack <id>` — Acknowledge an active alert

### Ledger & Integrations
- `!chart [spending|networth]` — Generate visual trend charts
- `!setbudget <weekly_limit> [impulse_threshold]` — Configure spending limits
- `!checknow` — Force an immediate Plaid bank synchronization
- `!gmail connect` / `!gmail status` — Manage read-only Gmail access
- `!export` — Export ledger records as CSV/JSON

---

## Testing & Quality Assurance

The test suite covers deterministic ledger invariants, security filters (SSRF, SQL injection, path traversal), financial calculations, and MCP integrations:

```bash
# Run the full test suite (253 tests)
pytest tests/ -q
```

---

## Security Architecture

- **User-Scoped Database Queries**: All SQL mutations use parameterized, user-scoped queries.
- **Multi-Tenant Security Boundaries**: Financial context is strictly isolated and withheld during Gmail-only operations.
- **Strict SSRF Mitigation**: Outbound web requests and MCP endpoints block loopback, RFC 1918 private subnets, and cloud metadata IPs.
- **Isolated Sandbox**: Sandboxed code execution drops Linux capabilities and runs read-only with Landlock filesystem protection.

---

## License

Personal project. See [SECURITY.md](SECURITY.md) for security disclosures.
