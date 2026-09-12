import json
import os
TIMEZONE = os.getenv('TIMEZONE', 'America/New_York')
import re
import mimetypes
import sqlite3
import asyncio
import contextvars
import hashlib
import ipaddress
import random
import socket
import time
import math
from collections import Counter
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode
from html import unescape
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import discord
from discord.ext import commands
import base64
from io import BytesIO
from pathlib import Path
from PIL import Image



# ============================================================
# Configuration
# ============================================================
load_dotenv()
app = FastAPI(title="Delilah Financial OS")

@app.get("/health")
async def health_check():
    return {"status": "ok"}

SUPPORTED_PDF_CONTENT_TYPES = {"application/pdf"}
PDF_MAX_PAGES = int(os.getenv("PDF_MAX_PAGES", "6"))
PDF_RENDER_SCALE = float(os.getenv("PDF_RENDER_SCALE", "2.0"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set. Add it to your .env file.")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "1539128341301301320"))

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080/search")
ADVISOR_MODEL = os.getenv("ADVISOR_MODEL", "gemma4:26b")
MODEL_KEEP_ALIVE = os.getenv("MODEL_KEEP_ALIVE", "30m")
CHAT_HISTORY_TURNS = int(os.getenv("CHAT_HISTORY_TURNS", "100"))
ADVISOR_NUM_CTX = int(os.getenv("ADVISOR_NUM_CTX", "256000"))
ADVISOR_NUM_PREDICT = int(os.getenv("ADVISOR_NUM_PREDICT", "32768"))
ADVISOR_FINAL_NUM_PREDICT = int(os.getenv("ADVISOR_FINAL_NUM_PREDICT", "16384"))
ADVISOR_TOOL_NUM_PREDICT = int(os.getenv("ADVISOR_TOOL_NUM_PREDICT", "16384"))

# NO generation timeout — removed per user request.
# Turn timeout is a very high safety net only (2 hours).
# No automatic advisor turn timeout. Use !pause or !cancel for human control.
MAX_TOOL_ROUNDS = 0  # 0 = unlimited
MAX_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_TOOL_CALLS_PER_ROUND", "0"))  # 0 = unlimited
MAX_TOTAL_TOOL_CALLS = int(os.getenv("MAX_TOTAL_TOOL_CALLS", "100"))  # 0 = unlimited

MAX_SEARCH_ATTEMPTS_PER_ITEM = int(os.getenv("MAX_SEARCH_ATTEMPTS_PER_ITEM", "8"))
MAX_RESEARCH_FETCHES_PER_SEARCH = int(os.getenv("MAX_RESEARCH_FETCHES_PER_SEARCH", "6"))
MAX_RESEARCH_CRAWL_PAGES_PER_ITEM = int(
    os.getenv("MAX_RESEARCH_CRAWL_PAGES_PER_ITEM", "12")
)
MAX_RESEARCH_CRAWL_DEPTH = int(os.getenv("MAX_RESEARCH_CRAWL_DEPTH", "2"))
MAX_RESEARCH_LEDGER_ITEMS = int(os.getenv("MAX_RESEARCH_LEDGER_ITEMS", "32"))

DELILAH_BUILD = "2026-08-24-deterministic-verification-v2"

# Runtime identity marker: proves exactly which source file the running process loaded.
try:
    _RUNTIME_SOURCE = os.path.abspath(__file__)
    _RUNTIME_SOURCE_SHA256 = hashlib.sha256(Path(_RUNTIME_SOURCE).read_bytes()).hexdigest() if os.path.exists(_RUNTIME_SOURCE) else "missing"
except Exception:
    _RUNTIME_SOURCE = "<unknown>"
    _RUNTIME_SOURCE_SHA256 = "<unavailable>"
print(f" [RUNTIME] source={_RUNTIME_SOURCE} sha256={_RUNTIME_SOURCE_SHA256} build={DELILAH_BUILD}")

SEARCH_CONCURRENCY = int(os.getenv("SEARCH_CONCURRENCY", "4"))
PLAYWRIGHT_ENABLED = os.getenv("PLAYWRIGHT_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "18000"))
PLAYWRIGHT_WAIT_MS = int(os.getenv("PLAYWRIGHT_WAIT_MS", "900"))
PLAYWRIGHT_CONCURRENCY = int(os.getenv("PLAYWRIGHT_CONCURRENCY", "2"))
SEARCH_HTTP_TIMEOUT = float(os.getenv("SEARCH_HTTP_TIMEOUT", "45"))
SEARCH_CACHE_TTL_SECONDS = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "300"))
RESEARCH_PAGE_CACHE_TTL_SECONDS = int(
    os.getenv("RESEARCH_PAGE_CACHE_TTL_SECONDS", "900")
)
MAX_SEARCH_ENGINES_PER_QUERY = int(os.getenv("MAX_SEARCH_ENGINES_PER_QUERY", "4"))
MAX_SEARCH_RESULTS_PER_ENGINE = int(os.getenv("MAX_SEARCH_RESULTS_PER_ENGINE", "7"))
MAX_SEARCH_UNIQUE_RESULTS = int(os.getenv("MAX_SEARCH_UNIQUE_RESULTS", "20"))
MAX_RESEARCH_LINKS_PER_PAGE = int(os.getenv("MAX_RESEARCH_LINKS_PER_PAGE", "10"))
MAX_RESEARCH_QUEUE_SIZE = int(os.getenv("MAX_RESEARCH_QUEUE_SIZE", "32"))

# ------------------------------------------------------------
# Open WebUI-style search tunables
# ------------------------------------------------------------
SEARCH_SCRAPE_TOP_N = int(os.getenv("SEARCH_SCRAPE_TOP_N", "5"))
SEARCH_SCRAPE_MAX_CHARS = int(os.getenv("SEARCH_SCRAPE_MAX_CHARS", "3500"))
SEARCH_TIME_RANGE = os.getenv("SEARCH_TIME_RANGE", "").strip().lower()
JINA_API_KEY = os.getenv("JINA_API_KEY", "").strip()

# ============================================================
# Discord Setup
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ============================================================
# Live advisor task control / visibility
# ============================================================
ACTIVE_ADVISOR_TASKS: dict[str, asyncio.Task] = {}
ADVISOR_STATUS: dict[str, dict] = {}
ADVISOR_STATUS_LOCK = asyncio.Lock()
ADVISOR_TASK_REGISTRATION_LOCK = asyncio.Lock()
USER_INTERRUPTS: dict[str, str] = {}
STATUS_MESSAGES: dict[str, discord.Message] = {}
STATUS_UPDATE_TASKS: dict[str, asyncio.Task] = {}

def _advisor_status_snapshot(uid: str) -> dict:
    state = ADVISOR_STATUS.get(str(uid), {})
    return dict(state)

async def _set_advisor_status(uid: str, **updates) -> None:
    uid = str(uid)
    async with ADVISOR_STATUS_LOCK:
        state = ADVISOR_STATUS.setdefault(uid, {})
        state.update(updates)
        state["updated_at"] = time.monotonic()

# ============================================================
# Transaction Queue
# ============================================================
tx_queue = asyncio.Queue()

# ============================================================
# Database
# ============================================================
DB_PATH = "data/finances.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("PRAGMA busy_timeout=10000")
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")

import threading
from contextlib import contextmanager

_thread_local = threading.local()

@contextmanager
def get_db():
    """Thread-safe context manager for SQLite connection."""
    if not hasattr(_thread_local, "conn"):
        local_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
        local_conn.execute("PRAGMA busy_timeout=10000")
        local_conn.execute("PRAGMA journal_mode=WAL")
        local_conn.execute("PRAGMA synchronous=NORMAL")
        _thread_local.conn = local_conn
    
    yield _thread_local.conn

# The original transaction table is Plaid-managed after the initial baseline is
# established. This mutable state is exposed to SQLite triggers through a
# connection-level function so ONLY the Plaid sync path can write to it.
PLAID_SYNC_STATE = {"active": False}
conn.create_function(
    "plaid_sync_active",
    0,
    lambda: 1 if PLAID_SYNC_STATE.get("active", False) else 0,
)

c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    transaction_id TEXT UNIQUE,
    message_id INTEGER,
    date TEXT,
    merchant TEXT,
    clean_merchant TEXT,
    category TEXT,
    amount REAL,
    account_used TEXT,
    total_liquid REAL,
    net_cash REAL,
    all_balances TEXT,
    status TEXT DEFAULT 'Pending Evaluation',
    judgment TEXT,
    raw_embed TEXT
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS transaction_correction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    transaction_row_id INTEGER NOT NULL,
    transaction_id TEXT,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'correct',
    source TEXT,
    reason TEXT,
    changes TEXT,
    before_state TEXT,
    after_state TEXT
)
""")

c.execute("""
CREATE INDEX IF NOT EXISTS idx_transaction_correction_log_occurred
ON transaction_correction_log (occurred_at DESC)
""")

c.execute("""
CREATE INDEX IF NOT EXISTS idx_transaction_correction_log_transaction
ON transaction_correction_log (transaction_row_id, occurred_at DESC)
""")

# ============================================================
# Known Merchant Registry
# ============================================================
c.execute("""
CREATE TABLE IF NOT EXISTS known_merchants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_key TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL,
    category TEXT,
    confidence TEXT NOT NULL DEFAULT 'high',
    notes TEXT,
    evidence_url TEXT,
    source TEXT NOT NULL DEFAULT 'curated',
    updated_at TEXT NOT NULL
)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_known_merchants_key
ON known_merchants (merchant_key)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS merchant_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    known_merchant_id INTEGER NOT NULL,
    alias_key TEXT NOT NULL UNIQUE,
    notes TEXT,
    source TEXT NOT NULL DEFAULT 'curated',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (known_merchant_id) REFERENCES known_merchants(id)
)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_merchant_aliases_key
ON merchant_aliases (alias_key)
""")

def _merchant_key(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


# Per-advisor authorization set. A correction for an unknown merchant is only
# permitted after search_web has researched that merchant during the same turn.
_RESEARCHED_MERCHANTS = contextvars.ContextVar("researched_merchants", default=None)

def _research_set() -> set[str]:
    value = _RESEARCHED_MERCHANTS.get()
    if value is None:
        value = set()
        _RESEARCHED_MERCHANTS.set(value)
    return value

def _resolve_known_merchant(merchant: str | None):
    key = _merchant_key(merchant)
    if not key:
        return None

    # Try the original normalized key first.
    candidate_keys = [key]

    # Safe deterministic variants only. These are intentionally NOT fuzzy:
    # - normalize punctuation
    # - remove common legal suffixes
    # - remove a terminal website domain suffix
    # This lets transaction strings such as "FIRMOO.COM" or "Firmoo, Inc."
    # resolve to a known "Firmoo" entry without risking arbitrary fuzzy matches.
    normalized = re.sub(r"[^a-z0-9]+", " ", key).strip()
    normalized = re.sub(r"\s+", " ", normalized)

    domainless = re.sub(
        r"\.(?:com|net|org|co|io|us|shop|store)$",
        "",
        key,
        flags=re.IGNORECASE,
    ).strip()

    legal_stripped = re.sub(
        r"\s+(?:incorporated|inc|llc|ltd|limited|corp|corporation|co|company)\.?$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()

    for variant in (domainless, normalized, legal_stripped):
        variant = _merchant_key(variant)
        if variant and variant not in candidate_keys:
            candidate_keys.append(variant)

    def _lookup_known(candidate: str):
        c.execute(
            """SELECT id, canonical_name, category, confidence, notes, evidence_url, source, updated_at
            FROM known_merchants WHERE merchant_key = ? LIMIT 1""",
            (candidate,),
        )
        row = c.fetchone()
        if row:
            return {
                "known_merchant_id": row[0], "canonical_name": row[1], "category": row[2],
                "confidence": row[3], "notes": row[4], "evidence_url": row[5],
                "source": row[6], "updated_at": row[7], "matched_by": "exact",
            }

        c.execute(
            """SELECT km.id, km.canonical_name, km.category, km.confidence, km.notes,
                      km.evidence_url, km.source, km.updated_at
            FROM merchant_aliases ma
            JOIN known_merchants km ON km.id = ma.known_merchant_id
            WHERE ma.alias_key = ? LIMIT 1""",
            (candidate,),
        )
        row = c.fetchone()
        if row:
            return {
                "known_merchant_id": row[0], "canonical_name": row[1], "category": row[2],
                "confidence": row[3], "notes": row[4], "evidence_url": row[5],
                "source": row[6], "updated_at": row[7], "matched_by": "alias",
            }

        return None

    for candidate in candidate_keys:
        resolved = _lookup_known(candidate)
        if resolved:
            if candidate != key:
                resolved["matched_by"] = "normalized"
                resolved["matched_key"] = candidate
            return resolved

    # Last-resort fuzzy matching.
    #
    # This is intentionally read-only: fuzzy resolution never creates,
    # modifies, merges, or deletes merchant records. It only helps an
    # unusually mangled transaction descriptor resolve to an existing
    # known merchant.
    #
    # We compare against both canonical names and merchant keys. A few
    # aggressive normalization passes make strings such as:
    #
    #   "FIRMOOONLIN"
    #   "firmoo online"
    #   "FIRMOO.COM"
    #   "deepseerwea 18658850911 ch"
    #
    # much easier to recognize without making the database itself fuzzy.

    def _fuzzy_tokens(value: str) -> list[str]:
        value = value.lower()
        value = re.sub(r"[^a-z0-9]+", " ", value)
        value = re.sub(r"([a-z])([0-9])", r"\\1 \\2", value)
        value = re.sub(r"([0-9])([a-z])", r"\\1 \\2", value)
        return [x for x in value.split() if x]

    def _fuzzy_compact(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    def _fuzzy_score(source: str, candidate: str) -> float:
        a = _fuzzy_compact(source)
        b = _fuzzy_compact(candidate)

        if not a or not b:
            return 0.0

        if a == b:
            return 1.0

        # One compact string contained in the other is useful for
        # descriptors such as "firmooonlin" vs "firmoo".
        shorter, longer = sorted((a, b), key=len)
        containment = len(shorter) / len(longer) if shorter in longer else 0.0

        # Sequence similarity catches substitutions/transpositions while
        # remaining deterministic and dependency-free.
        import difflib
        ratio = difflib.SequenceMatcher(None, a, b).ratio()

        # Token overlap is useful when the descriptor contains extra
        # merchant/payment metadata.
        at = set(_fuzzy_tokens(source))
        bt = set(_fuzzy_tokens(candidate))
        token_overlap = (
            len(at & bt) / len(at | bt)
            if at and bt
            else 0.0
        )

        return max(containment, ratio, token_overlap)

    c.execute("""
        SELECT
            id,
            merchant_key,
            canonical_name,
            category,
            confidence,
            notes,
            evidence_url,
            source,
            updated_at
        FROM known_merchants
        WHERE canonical_name IS NOT NULL
          AND TRIM(canonical_name) != ''
    """)
    known_rows = c.fetchall()

    best = None
    second_best = None

    for row in known_rows:
        (
            merchant_id,
            merchant_key,
            canonical_name,
            category,
            confidence,
            notes,
            evidence_url,
            source,
            updated_at,
        ) = row

        # Compare against both the stored transaction descriptor and the
        # canonical identity. Keep the strongest score for this merchant.
        key_score = _fuzzy_score(key, merchant_key or "")
        name_score = _fuzzy_score(key, canonical_name or "")
        score = max(key_score, name_score)

        candidate = {
            "known_merchant_id": merchant_id,
            "canonical_name": canonical_name,
            "category": category,
            "confidence": confidence,
            "notes": notes,
            "evidence_url": evidence_url,
            "source": source,
            "updated_at": updated_at,
            "matched_by": "fuzzy",
            "fuzzy_score": round(score, 4),
            "matched_key": merchant_key,
        }

        if best is None or score > best["fuzzy_score"]:
            second_best = best
            best = candidate
        elif second_best is None or score > second_best["fuzzy_score"]:
            second_best = candidate

    if best:
        score = best["fuzzy_score"]
        second_score = second_best["fuzzy_score"] if second_best else 0.0

        # Very strong match: accept directly.
        #
        # For weaker matches, require a meaningful margin over the next
        # candidate. This prevents generic descriptors from accidentally
        # resolving to the wrong merchant.
        strong_match = score >= 0.90
        ambiguous_strong_match = (
            score >= 0.82
            and (score - second_score) >= 0.08
        )

        if strong_match or ambiguous_strong_match:
            return best

    return None

c.execute("""
CREATE TABLE IF NOT EXISTS cash_inflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    type TEXT,
    source TEXT,
    gross_amount REAL,
    deductions REAL,
    net_expected REAL,
    hours_worked REAL,
    hourly_rate REAL,
    status TEXT DEFAULT 'Pending',
    notes TEXT,
    created_at TEXT,
    expected_date TEXT,
    account TEXT,
    recurrence TEXT,
    matched_transaction_row_id INTEGER,
    matched_at TEXT,
    confidence REAL
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS savings_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    name TEXT,
    target_amount REAL,
    current_amount REAL DEFAULT 0.0,
    UNIQUE(user_id, name)
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS roundup_settings (
    user_id TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    target_bucket_name TEXT NOT NULL DEFAULT 'Emergency Fund',
    multiplier REAL DEFAULT 1.0,
    whole_dollar_roundup REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS bucket_contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    bucket_name TEXT NOT NULL,
    amount REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    transaction_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_bucket_contrib_user ON bucket_contributions(user_id)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    merchant TEXT,
    last_amount REAL,
    last_date TEXT,
    status TEXT DEFAULT 'Active',
    UNIQUE(user_id, merchant)
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS budget_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    weekly_discretionary_limit REAL DEFAULT 150.00,
    impulse_threshold REAL DEFAULT 50.00
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    role TEXT,
    content TEXT,
    created_at TEXT
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS nudge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    trigger_signature TEXT,
    last_sent_at TEXT,
    UNIQUE(user_id, trigger_signature)
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS pending_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_row_id INTEGER,
    old_category TEXT,
    proposed_category TEXT,
    reasoning TEXT,
    discord_message_id INTEGER,
    requester_user_id TEXT,
    status TEXT DEFAULT 'awaiting'
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS planned_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    direction TEXT NOT NULL,
    merchant TEXT,
    description TEXT,
    expected_amount REAL,
    actual_amount REAL,
    expected_date TEXT,
    posted_date TEXT,
    account_used TEXT,
    status TEXT DEFAULT 'Expected',
    source TEXT DEFAULT 'user',
    notes TEXT,
    matched_transaction_row_id INTEGER,
    matched_at TEXT,
    completed_at TEXT,
    created_at TEXT
)
""")

c.execute("""
CREATE INDEX IF NOT EXISTS idx_planned_transactions_status
ON planned_transactions(status)
""")

for _col, _typ in (("context_tag", "TEXT"), ("context_note", "TEXT")):
    try:
        c.execute(f"ALTER TABLE planned_transactions ADD COLUMN {_col} {_typ}")
    except sqlite3.OperationalError as _exc:
        if "duplicate column name" not in str(_exc).lower():
            raise

for _col, _typ in (("tax_deductible", "INTEGER DEFAULT 0"), ("tax_category", "TEXT")):
    try:
        c.execute(f"ALTER TABLE transactions ADD COLUMN {_col} {_typ}")
    except sqlite3.OperationalError as _exc:
        if "duplicate column name" not in str(_exc).lower():
            raise

c.execute("""
CREATE INDEX IF NOT EXISTS idx_planned_transactions_expected_date
ON planned_transactions(expected_date)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS plaid_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    plaid_account_id TEXT UNIQUE,
    name TEXT,
    official_name TEXT,
    mask TEXT,
    user_id TEXT,
    type TEXT,
    subtype TEXT,
    institution_name TEXT,
    currency TEXT DEFAULT 'USD',
    current_balance REAL,
    available_balance REAL,
    credit_limit REAL,
    last_synced_at TEXT,
    active INTEGER DEFAULT 1
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    captured_at TEXT NOT NULL,
    account_id TEXT,
    account_name TEXT,
    account_type TEXT,
    account_subtype TEXT,
    current_balance REAL,
    available_balance REAL,
    credit_limit REAL,
    liquid_balance REAL,
    debt_balance REAL,
    net_worth_contribution REAL
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS financial_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    captured_at TEXT NOT NULL,
    checking_balance REAL,
    total_liquid REAL,
    total_credit_debt REAL,
    net_cash REAL,
    total_depository REAL,
    total_investment REAL,
    total_credit REAL,
    total_loan REAL,
    net_worth REAL
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS user_debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    balance REAL NOT NULL,
    apr REAL NOT NULL DEFAULT 0.0,
    min_payment REAL NOT NULL DEFAULT 0.0,
    plaid_account_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_user_debts_user ON user_debts(user_id)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS transaction_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    transaction_row_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    quantity REAL DEFAULT 1.0,
    unit_price REAL,
    total_price REAL,
    category TEXT,
    source TEXT DEFAULT 'gmail_receipt',
    raw_receipt_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_transaction_items_tx ON transaction_items(transaction_row_id)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_transaction_items_user ON transaction_items(user_id)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS subscription_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    trial_end_date TEXT NOT NULL,
    projected_cost REAL NOT NULL DEFAULT 0.0,
    billing_cycle TEXT DEFAULT 'monthly',
    cancellation_url TEXT,
    notes TEXT,
    status TEXT DEFAULT 'active',
    notified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_subscription_trials_user ON subscription_trials(user_id)
""")
c.execute("""
CREATE INDEX IF NOT EXISTS idx_subscription_trials_end ON subscription_trials(trial_end_date)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS transaction_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    transaction_row_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    note TEXT,
    created_at TEXT
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS lifestyle_context_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    state TEXT NOT NULL,
    note TEXT,
    starts_at TEXT,
    ends_at TEXT,
    created_at TEXT
)
""")

# ------------------------------------------------------------
# General-purpose Delilah memories — durable notes the advisor
# saves proactively about user habits, preferences, recurring
# situations, or anything worth remembering.
# ------------------------------------------------------------
c.execute("""
CREATE TABLE IF NOT EXISTS delilah_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    category TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    importance TEXT DEFAULT 'normal',
    source TEXT DEFAULT 'bot',
    entity_key TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT,
    is_active INTEGER DEFAULT 1
)
""")

c.execute(
    "CREATE INDEX IF NOT EXISTS idx_delilah_memories_category ON delilah_memories(category)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_delilah_memories_created ON delilah_memories(created_at)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_transaction_context_tx ON transaction_context(transaction_row_id)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_transaction_context_tag ON transaction_context(tag)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_balance_snapshots_captured ON balance_snapshots(captured_at)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_balance_snapshots_account ON balance_snapshots(account_id)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_financial_snapshots_captured ON financial_snapshots(captured_at)"
)
conn.commit()

SESSION_HISTORY: dict[str, list[dict]] = {}
SESSION_HISTORY_MAX_TURNS = 200

# Per-user audit state persists across "continue" turns.
AUDIT_SESSION_STATE: dict[str, dict] = {}

def _audit_remaining_from_result(result: str) -> int | None:
    """Extract deterministic remaining-count from either total or paginated getter output."""
    if not isinstance(result, str):
        return None
    # Prefer explicit total headers when present.
    match = re.search(r"(?im)^\s*(?:Unlocked|Locked) transactions:\s*(\d+)\b", result)
    if match:
        return int(match.group(1))
    # Pagination output: "Showing 1–25 of 117 transactions."
    match = re.search(r"(?im)Showing\s+\d+\s*[–-]\s*\d+\s+of\s+(\d+)\s+transactions", result)
    if match:
        return int(match.group(1))
    # Alternate compact forms used by some getter variants.
    match = re.search(r"(?im)of\s+(\d+)\s+(?:unlocked|locked)?\s*transactions", result)
    if match:
        return int(match.group(1))
    return None

def _decode_b64_arg(args: dict, key: str, legacy_key: str) -> str:
    raw = args.get(key)
    if not raw:
        legacy = args.get(legacy_key)
        if legacy:
            raw = legacy
        else:
            raise ValueError(f"'{key}' is required.")
    raw = str(raw)
    # If it contains characters outside the base64 alphabet, it's raw code.
    if re.search(r"[^A-Za-z0-9+/=\s]", raw):
        return raw
    try:
        return base64.b64decode(raw, validate=False).decode("utf-8")
    except Exception:
        # If it fails to decode, just return it as raw code.
        return raw

def load_history_on_boot(limit: int = 20):
    global SESSION_HISTORY
    c.execute("SELECT DISTINCT user_id FROM chat_history")
    user_ids = [row[0] for row in c.fetchall()]
    for uid in user_ids:
        c.execute(
            "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (str(uid), limit),
        )
        rows = c.fetchall()[::-1]
        SESSION_HISTORY[str(uid)] = [
            {"role": role, "content": content} for role, content in rows
        ]
    print(f" [BOOT] Preloaded context into RAM for {len(user_ids)} user(s).")

# ============================================================
# Database Migration Safety
# ============================================================
def _ensure_column(table: str, column: str, coltype: str):
    c.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in c.fetchall()}
    if column not in existing:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

_ensure_column("transactions", "clean_merchant", "TEXT")
_ensure_column("transactions", "pending_transaction_id", "TEXT")
_ensure_column("transactions", "override_amount", "REAL")
_ensure_column("transactions", "override_reason", "TEXT")
_ensure_column("transactions", "override_at", "TEXT")
_ensure_column("transactions", "override_source", "TEXT")
_ensure_column("transactions", "is_locked", "INTEGER DEFAULT 0")
_ensure_column("transactions", "category", "TEXT")

# ============================================================
# Plaid-Managed Original Transaction Baseline

# ============================================================
# transactions_original is the durable record of the transaction identity
# supplied by Plaid. It starts with the initial ledger baseline, then only the
# Plaid sync path may append/update/remove rows as Plaid itself changes them.
# Local/model corrections NEVER write this table.
c.execute("""
CREATE TABLE IF NOT EXISTS transactions_original AS
SELECT * FROM transactions WHERE 0
""")

c.execute("SELECT COUNT(*) FROM transactions_original")
_original_count = c.fetchone()[0]
if _original_count == 0:
    # One-time startup baseline seed. This is the sole legitimate non-sync
    # writer of transactions_original, so it must briefly hold the same
    # "Plaid sync active" flag the sync path uses, or the guard triggers
    # (transactions_original_plaid_only_*) will reject it and crash boot.
    PLAID_SYNC_STATE["active"] = True
    try:
        c.execute("""
            INSERT INTO transactions_original
            SELECT * FROM transactions
        """)
        print(f" [DB] Created initial Plaid baseline: {c.rowcount} transaction(s)")
    finally:
        PLAID_SYNC_STATE["active"] = False

c.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_original_id
ON transactions_original (id)
""")
c.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_original_transaction_id
ON transactions_original (transaction_id)
WHERE transaction_id IS NOT NULL
""")

# Replace the old permanently-immutable triggers with guarded triggers.
# INSERT/UPDATE/DELETE are allowed ONLY while the Plaid sync connection flag is
# active. Any other write source receives a hard SQLite error.
c.execute("DROP TRIGGER IF EXISTS transactions_original_no_insert")
c.execute("DROP TRIGGER IF EXISTS transactions_original_no_update")
c.execute("DROP TRIGGER IF EXISTS transactions_original_no_delete")
c.execute("DROP TRIGGER IF EXISTS transactions_original_plaid_only_insert")
c.execute("DROP TRIGGER IF EXISTS transactions_original_plaid_only_update")
c.execute("DROP TRIGGER IF EXISTS transactions_original_plaid_only_delete")

c.execute("""
CREATE TRIGGER transactions_original_plaid_only_insert
BEFORE INSERT ON transactions_original
BEGIN
    SELECT CASE
        WHEN plaid_sync_active() != 1
        THEN RAISE(ABORT, 'transactions_original is Plaid-managed; only Plaid sync may write it')
    END;
END
""")
c.execute("""
CREATE TRIGGER transactions_original_plaid_only_update
BEFORE UPDATE ON transactions_original
BEGIN
    SELECT CASE
        WHEN plaid_sync_active() != 1
        THEN RAISE(ABORT, 'transactions_original is Plaid-managed; only Plaid sync may write it')
    END;
END
""")
c.execute("""
CREATE TRIGGER transactions_original_plaid_only_delete
BEFORE DELETE ON transactions_original
BEGIN
    SELECT CASE
        WHEN plaid_sync_active() != 1
        THEN RAISE(ABORT, 'transactions_original is Plaid-managed; only Plaid sync may write it')
    END;
END
""")

c.execute("CREATE TABLE IF NOT EXISTS transaction_snapshot_metadata (key TEXT PRIMARY KEY, value TEXT)")
c.execute("""
INSERT OR IGNORE INTO transaction_snapshot_metadata (key, value)
VALUES ('created_at', datetime('now'))
""")

_ensure_column("cash_inflows", "hours_worked", "REAL")
_ensure_column("cash_inflows", "hourly_rate", "REAL")
_ensure_column("cash_inflows", "expected_date", "TEXT")
_ensure_column("cash_inflows", "account", "TEXT")
_ensure_column("cash_inflows", "recurrence", "TEXT")
_ensure_column("cash_inflows", "matched_transaction_row_id", "INTEGER")
_ensure_column("cash_inflows", "matched_at", "TEXT")
_ensure_column("cash_inflows", "confidence", "REAL")

# # # _ensure_column("transactions", "raw_embed", "TEXT")
_ensure_column("pending_corrections", "requester_user_id", "TEXT")

_ensure_column("delilah_memories", "source", "TEXT DEFAULT 'bot'")
_ensure_column("delilah_memories", "entity_key", "TEXT")
_ensure_column("delilah_memories", "updated_at", "TEXT")
_ensure_column("delilah_memories", "is_active", "INTEGER DEFAULT 1")
_ensure_column("delilah_memories", "is_pinned", "INTEGER DEFAULT 0")

conn.commit()

c.execute(
    "CREATE INDEX IF NOT EXISTS idx_delilah_memories_entity ON delilah_memories(entity_key)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_delilah_memories_active ON delilah_memories(is_active)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_delilah_memories_content ON delilah_memories(content)"
)
c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date)")
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions (clean_merchant)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions (category)"
)
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_chat_history_user_time ON chat_history (user_id, created_at)"
)
conn.commit()

c.execute("SELECT COUNT(*) FROM budget_settings")
if c.fetchone()[0] == 0:
    c.execute(
        "INSERT INTO budget_settings (weekly_discretionary_limit, impulse_threshold) VALUES (150.00, 50.00)"
    )
    conn.commit()

# ============================================================
# ACTIVE WORLD MODEL (AWM) — Minimal Viable Cognitive Substrate
# ============================================================
c.execute("""
CREATE TABLE IF NOT EXISTS kg_entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases TEXT,
    attributes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS kg_claims (
    claim_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES kg_entities(entity_id),
    predicate TEXT NOT NULL,
    object_id TEXT REFERENCES kg_entities(entity_id),
    scalar_value TEXT,
    provenance_type TEXT NOT NULL,
    source_authority INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    tx_asserted_at TEXT DEFAULT (datetime('now')),
    tx_retracted_at TEXT,
    parent_claim_ids TEXT,
    evidence_refs TEXT,
    is_scenario INTEGER DEFAULT 0
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS kg_contradictions (
    contradiction_id TEXT PRIMARY KEY,
    claim_id_a TEXT NOT NULL REFERENCES kg_claims(claim_id),
    claim_id_b TEXT NOT NULL REFERENCES kg_claims(claim_id),
    status TEXT DEFAULT 'UNRESOLVED',
    created_at TEXT DEFAULT (datetime('now'))
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS kg_dossiers (
    doc_id TEXT PRIMARY KEY,
    primary_entity_id TEXT REFERENCES kg_entities(entity_id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
)
""")

c.execute("""
CREATE VIRTUAL TABLE IF NOT EXISTS kg_search_fts USING fts5(
    target_id UNINDEXED,
    target_type UNINDEXED,
    title,
    content,
    tags,
    tokenize='porter unicode61'
)
""")

c.execute("CREATE INDEX IF NOT EXISTS idx_claims_subj ON kg_claims(subject_id, predicate)")
c.execute("CREATE INDEX IF NOT EXISTS idx_claims_obj ON kg_claims(object_id, predicate)")
c.execute("CREATE INDEX IF NOT EXISTS idx_claims_temporal ON kg_claims(subject_id, valid_from, valid_to, tx_retracted_at)")
c.execute("CREATE INDEX IF NOT EXISTS idx_claims_active ON kg_claims(subject_id, tx_retracted_at) WHERE tx_retracted_at IS NULL")
conn.commit()



__all__ = ['get_db', 'DB_PATH', 'SEARCH_CONCURRENCY', 'SEARCH_SCRAPE_MAX_CHARS', 'ACTIVE_ADVISOR_TASKS', 'MAX_SEARCH_ENGINES_PER_QUERY', 'STATUS_UPDATE_TASKS', 'SESSION_HISTORY_MAX_TURNS', 'DISCORD_TOKEN', 'DISCORD_CHANNEL_ID', '_ensure_column', 'MAX_SEARCH_RESULTS_PER_ENGINE', 'USER_INTERRUPTS', 'ADVISOR_FINAL_NUM_PREDICT', 'tx_queue', 'health_check', 'conn', 'SEARXNG_URL', 'app', 'PLAYWRIGHT_WAIT_MS', 'AUDIT_SESSION_STATE', '_decode_b64_arg', '_resolve_known_merchant', 'PDF_RENDER_SCALE', 'MAX_RESEARCH_LINKS_PER_PAGE', '_advisor_status_snapshot', 'load_history_on_boot', 'MAX_RESEARCH_FETCHES_PER_SEARCH', 'SUPPORTED_PDF_CONTENT_TYPES', 'ADVISOR_TASK_REGISTRATION_LOCK', 'CHAT_HISTORY_TURNS', 'MAX_TOTAL_TOOL_CALLS', 'ADVISOR_STATUS_LOCK', '_RESEARCHED_MERCHANTS', 'MAX_SEARCH_ATTEMPTS_PER_ITEM', 'ADVISOR_TOOL_NUM_PREDICT', 'ADVISOR_STATUS', 'MAX_TOOL_ROUNDS', 'ADVISOR_NUM_PREDICT', 'SEARCH_CACHE_TTL_SECONDS', 'ADVISOR_MODEL', 'OLLAMA_URL', '_merchant_key', 'DELILAH_BUILD', 'PLAYWRIGHT_ENABLED', 'JINA_API_KEY', 'MAX_TOOL_CALLS_PER_ROUND', 'bot', 'PLAID_SYNC_STATE', 'SESSION_HISTORY', 'MODEL_KEEP_ALIVE', 'PLAYWRIGHT_CONCURRENCY', 'c', 'MAX_RESEARCH_LEDGER_ITEMS', '_research_set', 'SEARCH_HTTP_TIMEOUT', 'PDF_MAX_PAGES', 'ADVISOR_NUM_CTX', 'RESEARCH_PAGE_CACHE_TTL_SECONDS', 'MAX_RESEARCH_CRAWL_PAGES_PER_ITEM', 'MAX_SEARCH_UNIQUE_RESULTS', 'SEARCH_SCRAPE_TOP_N', 'intents', '_set_advisor_status', 'MAX_RESEARCH_QUEUE_SIZE', 'PLAYWRIGHT_TIMEOUT_MS', 'STATUS_MESSAGES', 'MAX_RESEARCH_CRAWL_DEPTH', '_audit_remaining_from_result', '_original_count', 'PLAYWRIGHT_HEADLESS', 'SEARCH_TIME_RANGE']

import contextvars
CURRENT_USER_ID = contextvars.ContextVar('current_user_id', default='1')
