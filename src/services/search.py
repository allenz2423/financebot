import json
import os
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
from datetime import datetime, timedelta
from dotenv import load_dotenv
import discord
from discord.ext import commands
import plaid_sync
import sandbox_client
import base64
from io import BytesIO
from pathlib import Path
from PIL import Image

from src.core.state import *
from src.utils.helpers import *


# ============================================================
# SearXNG — Enhanced web search pipeline (Open WebUI-inspired)
#
#   1. Multi-engine SearXNG queries with smart variant expansion
#   2. Reciprocal Rank Fusion (RRF) across engines
#   3. BM25 relevance scoring of scraped content
#   4. Multi-strategy content extraction (Jina → static → rendered)
#   5. Passage-level content windowing
#   6. Structured citations with quality signals
# ============================================================
import math
from collections import Counter

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "need", "must",
    "this", "that", "these", "those", "it", "its", "he", "she", "they",
    "them", "his", "her", "their", "we", "our", "you", "your", "i", "me",
    "my", "not", "no", "nor", "so", "if", "then", "than", "too", "very",
    "just", "about", "above", "after", "again", "all", "also", "am",
    "any", "as", "because", "before", "between", "both", "down", "during",
    "each", "few", "further", "here", "how", "into", "more", "most",
    "other", "out", "over", "own", "same", "some", "such", "there",
    "through", "under", "until", "up", "what", "when", "where", "which",
    "while", "who", "whom", "why", "store", "shop", "official", "website",
    "retail", "usd", "price", "buy", "location", "business", "type",
}

_search_semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
_scrape_semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
WEB_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}
WEB_PAGE_CACHE: dict[str, tuple[float, str, bool]] = {}

_USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAVE_BS4 = True
except ImportError:
    BeautifulSoup = None
    _HAVE_BS4 = False

_GENERIC_PRODUCT_WORDS = {
    "power", "bank", "charger", "portable", "battery", "fragrance",
    "perfume", "cologne", "eau", "parfum", "spray", "edp", "extrait",
    "official", "website", "retail", "price", "prices", "cost", "purchase",
    "buy", "sale", "deal", "cheapest", "near", "close", "current", "today",
    "store", "shop", "online", "site", "usd", "dollar", "dollars", "worth",
    "value", "how", "much", "where", "selling", "msrp", "product",
}

_RESEARCH_JUNK_HOSTS = {
    "cambridge.org", "dictionary.cambridge.org", "frenchdictionary.com",
    "collinsdictionary.com", "wiktionary.org", "wikipedia.org",
    "reddit.com", "www.reddit.com", "scamadviser.com", "scamadviser.net", "pinterest.com", "tiktok.com", "instagram.com", "facebook.com", "twitter.com", "yelp.com",
}

_PRODUCT_PATH_MARKERS = (
    "/products/", "/product/", "/item/", "/items/", "/p/", "/dp/",
    "/pd/", "/shop/", "/catalog/", "/collections/", "/category/", "/brands/",
)

_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "msclkid", "gclid", "fbclid", "ref", "source", "campaign",
}

_PRICE_WORDS = {
    "price", "prices", "cost", "costs", "retail", "msrp", "buy", "purchase",
    "how", "much", "worth", "value", "deal", "deals", "sale", "selling",
    "usd", "dollar", "dollars", "official", "website", "site", "online",
    "store", "shop", "where", "cheapest", "best", "current", "today",
}

_UNIT_RE = re.compile(r"^\d+(?:\.\d+)?(?:ml|oz|g|kg|mah|wh|w)$", re.IGNORECASE)

_DOCUMENT_HINT_WORDS = {
    "pdf", "calendar", "schedule", "form", "syllabus", "policy", "handbook",
    "manual", "faq", "application", "deadline", "catalog", "bulletin",
}

# ────────────────────────────────────────────────────────────
# BM25 relevance scoring (replaces naive keyword counting)
# ────────────────────────────────────────────────────────────
class BM25Scorer:
    """Okapi BM25 scorer for ranking search results against a query."""
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def score(self, query: str, document: str) -> float:
        if not query or not document:
            return 0.0
        query_terms = [
            t for t in self._tokenize(query)
            if t not in _STOPWORDS and len(t) > 1
        ]
        if not query_terms:
            return 0.0
        doc_tokens = self._tokenize(document)
        if not doc_tokens:
            return 0.0
        doc_len = len(doc_tokens)
        avg_dl = max(doc_len, 1)  # single-doc mode
        tf_map = Counter(doc_tokens)
        score = 0.0
        for term in query_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            # IDF approximation for single-document scoring
            idf = math.log(1.0 + (avg_dl - tf + 0.5) / (tf + 0.5))
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * (doc_len / avg_dl))
            score += idf * (numerator / denominator)
        return score

    def score_passages(self, query: str, document: str, passage_size: int = 400) -> list[tuple[float, str]]:
        """Score individual passages within a document for better windowing."""
        words = document.split()
        if not words:
            return []
        passages = []
        for i in range(0, len(words), passage_size // 2):
            passage = " ".join(words[i:i + passage_size])
            if passage.strip():
                passages.append((self.score(query, passage), passage))
        passages.sort(key=lambda x: x[0], reverse=True)
        return passages

_bm25 = BM25Scorer()

# ────────────────────────────────────────────────────────────
# URL / query helpers (kept compatible with rest of bot)
# ────────────────────────────────────────────────────────────
def _host_is_junk(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return True
    return any(host == junk or host.endswith("." + junk) for junk in _RESEARCH_JUNK_HOSTS)

def _looks_like_url(value: str) -> bool:
    value = str(value or "").strip()
    return value.lower().startswith(("http://", "https://"))


def _canonical_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return url
        pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() not in _TRACKING_QUERY_KEYS:
                pairs.append((key, value))
        query = urlencode(pairs, doseq=True)
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/")
        return parsed._replace(path=path, query=query, fragment="").geturl()
    except Exception:
        return url

def _normalize_query(q: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", q.lower())
    tokens = [t for t in tokens if t not in _STOPWORDS]
    return " ".join(sorted(tokens))

def _query_fingerprint(query: str) -> str:
    return hashlib.sha256(_normalize_query(query).encode()).hexdigest()

def _random_browser_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENT_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.7",
    }

# ────────────────────────────────────────────────────────────
# Research token helpers (kept compatible)
# ────────────────────────────────────────────────────────────
def _research_core_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", query.lower())
    out: list[str] = []
    for token in tokens:
        if token in _STOPWORDS or token in _PRICE_WORDS or _UNIT_RE.match(token):
            continue
        if token.isdigit() or len(token) < 3:
            continue
        if token not in out:
            out.append(token)
    return out

def _research_spec_tokens(query: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(r"\b\d+(?:\.\d+)?(?:ml|oz|g|kg|mah|wh|w)\b", query.lower())
        )
    )

def _research_identity_tokens(query: str) -> list[str]:
    tokens = _research_core_tokens(query)
    return [t for t in tokens if t not in _GENERIC_PRODUCT_WORDS]


# Bank/card descriptors frequently contain non-merchant fields that poison
# web search: plan-fee markers, processor/location suffixes, country codes,
# transaction-channel markers, etc. Keep this strictly for merchant research;
# ordinary user search queries remain verbatim.
_MERCHANT_DESCRIPTOR_NOISE = {
    "plan", "fee", "payment", "payments", "pmt", "purchase",
    "pos", "ecom", "ec", "debit", "credit",
    "usa", "us", "ny", "nyc", "brooklyn",
    "gb", "uk", "ch", "ca",
    "inc", "llc", "corp", "co", "company",
}


def _merchant_search_query(query: str) -> str:
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not q:
        return ""

    tokens = re.findall(r"[A-Za-z0-9]+", q)
    kept = []

    for token in tokens:
        low = token.lower()

        if low in _MERCHANT_DESCRIPTOR_NOISE:
            continue

        # Remove transaction/reference numbers, but preserve short numeric
        # merchant names such as "7 eleven".
        if low.isdigit() and len(low) >= 4:
            continue

        if low not in kept:
            kept.append(low)

    return " ".join(kept) if kept else q


def _merchant_search_queries(query: str) -> list[str]:
    """Build progressively broader queries for noisy merchant descriptors."""
    raw = re.sub(r"\s+", " ", str(query or "")).strip()
    normalized = _merchant_search_query(raw)

    if not normalized:
        return []

    out = []

    def add(value: str):
        value = re.sub(r"\s+", " ", value).strip()
        if value and value.lower() not in {x.lower() for x in out}:
            out.append(value)

    tokens = normalized.split()

    # First try the cleaned descriptor.
    add(normalized)

    # Exact phrase often works much better for brands.
    add(f'"{normalized}"')

    # Remove short transaction/channel prefixes:
    # TST XING FU TANG HUDSON -> XING FU TANG HUDSON
    trimmed = tokens[:]
    while len(trimmed) >= 2 and len(trimmed[0]) <= 3:
        trimmed.pop(0)
        value = " ".join(trimmed)
        add(value)
        add(f'"{value}"')

    # Remove obvious location suffixes:
    locationish = {
        "avenel", "hudson", "brooklyn", "manhattan", "ny", "nyc",
        "usa", "us", "gb", "uk", "ca", "ch", "herald", "square",
        "7th", "ave",
    }

    trimmed = tokens[:]
    while len(trimmed) >= 2 and trimmed[-1].lower() in locationish:
        trimmed.pop()
        value = " ".join(trimmed)
        add(value)
        add(f'"{value}"')

    # Finally try progressively shorter multi-word phrases.
    trimmed = tokens[:]
    while len(trimmed) >= 3:
        value = " ".join(trimmed[:-1])
        if len(value.split()) >= 2:
            add(value)
            add(f'"{value}"')
        trimmed.pop()

    return out[:10]

def _merchant_search_queries(query: str) -> list[str]:
    """Build a small ordered set of progressively broader merchant queries."""
    raw = re.sub(r"\s+", " ", str(query or "")).strip()
    if not raw:
        return []

    normalized = _merchant_search_query(raw)
    if not normalized:
        return []

    candidates: list[str] = []

    def add(value: str):
        value = re.sub(r"\s+", " ", value).strip()
        if value and value.lower() not in {x.lower() for x in candidates}:
            candidates.append(value)

    # First choice: conservative descriptor cleanup.
    add(normalized)

    tokens = normalized.split()

    # Exact quoted merchant phrase.
    if len(tokens) >= 1:
        add(f'"{normalized}"')

    # For noisy multi-token descriptors, try progressively removing leading
    # short descriptor tokens. This handles things like:
    #   "TST XING FU TANG HUDSON" -> "XING FU TANG HUDSON"
    # without globally declaring "TST" to be noise.
    while len(tokens) >= 3 and len(tokens[0]) <= 3:
        tokens = tokens[1:]
        add(" ".join(tokens))
        add(f'"{" ".join(tokens)}"')

    # Also try the longest meaningful contiguous suffix. This helps descriptors
    # where the useful merchant is followed by location/channel noise.
    tokens = normalized.split()
    while len(tokens) >= 3:
        candidate = " ".join(tokens[:-1])
        if len(candidate.split()) >= 2:
            add(candidate)
        tokens = tokens[:-1]

    return candidates[:6]


def _research_terms(query: str) -> tuple[list[str], list[str]]:
    return _research_core_tokens(query), _research_spec_tokens(query)

def _research_subject(query: str) -> str:
    identity = _research_identity_tokens(query)
    return " ".join(identity[:10]) or " ".join(_research_core_tokens(query)[:10])

def _research_subject_matches(query: str, subject_tokens: set[str]) -> bool:
    tokens = set(_research_identity_tokens(query))
    if not tokens or not subject_tokens:
        return False
    if len(tokens) <= 2 or len(subject_tokens) <= 2:
        return tokens == subject_tokens
    overlap = len(tokens & subject_tokens)
    shorter = min(len(tokens), len(subject_tokens))
    return (
        tokens <= subject_tokens
        or subject_tokens <= tokens
        or (shorter >= 3 and overlap >= min(3, shorter))
    )

def _whole_token_hits(text: str, tokens: list[str]) -> int:
    lower = text.lower()
    return sum(1 for token in tokens if re.search(rf"\b{re.escape(token)}\b", lower))

def _identity_score(query: str, text: str) -> int:
    if not text:
        return 0
    identity = _research_identity_tokens(query)
    if not identity:
        return 0
    lower = re.sub(r"[^a-z0-9]+", " ", text.lower())
    hits = _whole_token_hits(lower, identity)
    phrase = " ".join(identity)
    phrase_score = 3 if phrase and phrase in lower else 0
    proximity_score = 0
    if len(identity) >= 2:
        positions = []
        for token in identity:
            m = re.search(rf"\b{re.escape(token)}\b", lower)
            if m:
                positions.append(m.start())
        if len(positions) >= 2 and max(positions) - min(positions) <= 500:
            proximity_score = 2
    return hits + phrase_score + proximity_score

# ────────────────────────────────────────────────────────────
# Query variant generation (improved)
# ────────────────────────────────────────────────────────────
def _quote_search_phrase(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return f'"{cleaned}"' if cleaned else ""

def _build_research_variants(
    query: str,
    prior_queries: list[str] | None = None,
    regression_tier: int = 0,
) -> list[str]:
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not q:
        return []
    previous = {
        re.sub(r"\s+", " ", str(x).lower()).strip() for x in (prior_queries or [])
    }
    if q.lower() in previous:
        return []

    # HARD CAP: never send queries longer than 200 chars to search engines
    # Long queries (like batch perfume lists) trigger instant bans
    if len(q) > 200:
        # Extract just the identity tokens (first 6 meaningful words)
        tokens = _research_identity_tokens(q)
        q = " ".join(tokens[:6]) if tokens else q[:200]

    variants = [q]
    identity = _research_identity_tokens(q)
    if len(identity) >= 2:
        identity_str = " ".join(identity[:6])  # cap identity tokens too
        if identity_str.lower() not in [v.lower() for v in variants]:
            variants.append(identity_str)
        phrase_str = f'"{identity_str}"'
        if phrase_str.lower() not in [v.lower() for v in variants]:
            variants.append(phrase_str)
    return variants[:3]  # max 3 variants, not unlimited

def _looks_like_document_research(query: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
    return bool(tokens & _DOCUMENT_HINT_WORDS)

def _looks_like_price_research(query: str) -> bool:
    q = query.lower()
    return any(
        term in q
        for term in (
            "price", "prices", "cost", "retail", "msrp", "how much",
            "worth", "buy", "purchase", "sale", "deal", "cheapest",
        )
    )

def _looks_like_specific_fact_research(query: str) -> bool:
    q = query.lower()
    return _looks_like_price_research(query) or any(
        term in q
        for term in (
            "notes", "ingredients", "specs", "specifications", "review",
            "details", "accords",
        )
    )

# ────────────────────────────────────────────────────────────
# Content extraction (improved readability)
# ────────────────────────────────────────────────────────────
_BOILERPLATE_TAGS = [
    "script", "style", "noscript", "template", "svg", "iframe",
    "nav", "footer", "header", "aside", "form", "button", "select",
    "figure", "figcaption", "video", "audio", "canvas", "map",
]

def extract_main_text(html: str) -> str:
    """Extract readable main content from HTML using readability heuristics.
    Prefers <main>/<article>, falls back to largest text-density block."""
    if not html:
        return ""
    if _HAVE_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            # Remove boilerplate
            for tag in soup(_BOILERPLATE_TAGS):
                tag.decompose()
            # Remove comments
            from bs4 import Comment
            for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
                comment.extract()
            # Prefer semantic containers
            root = None
            for selector in ["main", "article", '[role="main"]', ".content", "#content", ".post-content", ".entry-content"]:
                root = soup.select_one(selector)
                if root:
                    break
            if not root:
                root = soup.body or soup
            text = root.get_text(" ", strip=True)
            # Clean up excessive whitespace
            text = re.sub(r"\s+", " ", text).strip()
            return text
        except Exception:
            pass

    # Regex fallback
    stripped = re.sub(
        r"<(script|style|noscript|template|svg|iframe|nav|footer|header|aside|form)[^>]*>.*?</\1>",
        " ", html, flags=re.S | re.I,
    )
    text = unescape(re.sub(r"<[^>]+>", " ", stripped))
    return re.sub(r"\s+", " ", text).strip()

# ────────────────────────────────────────────────────────────
# Content windowing (passage-based, not first-hit)
# ────────────────────────────────────────────────────────────
def _smart_content_window(text: str, query: str, max_chars: int) -> str:
    """Extract the most relevant passages from page text using BM25 passage scoring."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    # Score passages and take top-scoring ones
    passages = _bm25.score_passages(query, text, passage_size=300)
    if not passages:
        return text[:max_chars] + " …[truncated]"

    # Accumulate top passages until we hit the char budget
    selected = []
    total_chars = 0
    for score, passage in passages:
        if score <= 0:
            break
        if total_chars + len(passage) > max_chars:
            break
        selected.append(passage)
        total_chars += len(passage)

    if not selected:
        # Fallback: center on first keyword hit
        identity = _research_identity_tokens(query) or re.findall(r"[a-z0-9]+", query.lower())
        lower = text.lower()
        first_pos = -1
        for token in identity:
            pos = lower.find(token)
            if pos != -1 and (first_pos == -1 or pos < first_pos):
                first_pos = pos
        if first_pos == -1:
            return text[:max_chars] + " …[truncated]"
        start = max(0, first_pos - max_chars // 4)
        prefix = "…" if start > 0 else ""
        suffix = " …[truncated]" if start + max_chars < len(text) else ""
        return prefix + text[start:start + max_chars] + suffix

    result = " […] ".join(selected)
    if len(result) > max_chars:
        result = result[:max_chars] + " …[truncated]"
    return result

# ────────────────────────────────────────────────────────────
# SearXNG engine query
# ────────────────────────────────────────────────────────────
async def _searxng_engine_query(
    client: httpx.AsyncClient, query: str, engine: str, time_range: str | None
) -> list[dict]:
    params = {
        "q": query,
        "engines": engine,
        "format": "json",
        "categories": "general",
        "language": "en",
        "pageno": 1,
        "safesearch": 0,
    }
    if time_range:
        params["time_range"] = time_range
    try:
        r = await client.get(
            SEARXNG_URL, params=params, headers=_random_browser_headers()
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as exc:
        print(f" [SearXNG] {engine} failed for {query!r}: {type(exc).__name__}: {exc}")
        return []
    out = []
    for rank, raw in enumerate(data.get("results") or []):
        if rank >= MAX_SEARCH_RESULTS_PER_ENGINE:
            break
        url = str(raw.get("url") or "")
        if not url:
            continue
        out.append({
            "title": str(raw.get("title") or "").strip(),
            "url": url,
            "snippet": re.sub(r"\s+", " ", str(raw.get("content") or "")).strip(),
            "engine_score": float(raw.get("score") or 0.0),
            "engine": engine,
            "engine_rank": rank + 1,  # position in this engine's results
            "source_query": query,
            "publishedDate": raw.get("publishedDate"),
        })
    return out

# ────────────────────────────────────────────────────────────
# Reciprocal Rank Fusion (RRF) — merges rankings from multiple engines
# ────────────────────────────────────────────────────────────
def _reciprocal_rank_fusion(candidates: list[dict], k: int = 60) -> dict[str, dict]:
    """Merge results from all engines using RRF. Each engine's rank contributes
    1/(k + rank) to the fused score. Higher = more engines agree it's good."""
    by_url: dict[str, dict] = {}
    for item in candidates:
        canonical = _canonical_url(item["url"])
        if canonical in by_url:
            entry = by_url[canonical]
        else:
            item = dict(item)
            item["url"] = canonical
            item["engines"] = set()
            item["rrf_score"] = 0.0
            item["engine_ranks"] = []
            entry = item
            by_url[canonical] = entry
        entry["engines"].add(item["engine"])
        entry["engine_ranks"].append(item.get("engine_rank", 99))
        entry["rrf_score"] += 1.0 / (k + item.get("engine_rank", 99))

        # Keep best snippet/title
        if item.get("snippet") and not entry.get("snippet"):
            entry["snippet"] = item["snippet"]
        if item.get("title") and not entry.get("title"):
            entry["title"] = item["title"]
        # Keep highest raw engine score
        if item.get("engine_score", 0) > entry.get("engine_score", 0):
            entry["engine_score"] = item["engine_score"]
        if item.get("publishedDate") and not entry.get("publishedDate"):
            entry["publishedDate"] = item["publishedDate"]
    return by_url

def _registrable_host_label(host: str) -> str:
    parts = [
        part
        for part in re.split(r"[^a-z0-9]+", str(host or "").lower())
        if part
    ]

    if len(parts) < 2:
        return parts[0] if parts else ""

    # firmoo.co.uk -> firmoo
    if (
        len(parts) >= 3
        and parts[-2] in {"co", "com", "net", "org", "gov", "ac", "edu"}
        and len(parts[-1]) == 2
    ):
        return parts[-3]

    # stores.bestbuy.com -> bestbuy
    return parts[-2]


def _merchant_domain_authority(query: str, host: str) -> float:
    label = _registrable_host_label(host)
    if not label:
        return 0.0

    tokens = [
        token.lower()
        for token in re.findall(r"[a-z0-9]+", str(query or ""))
        if len(token) >= 3
    ]

    # Remove only obviously generic vocabulary. Do NOT use the general
    # research identity tokenizer here because "buy" is meaningful in
    # merchant names such as Best Buy.
    generic = {
        "official", "website", "online", "store", "shop",
        "company", "corporation", "inc", "llc", "corp",
        "usa", "us", "ny", "nyc", "brooklyn", "uk", "gb", "ca",
    }
    tokens = [t for t in tokens if t not in generic]

    if not tokens:
        return 0.0

    label_compact = re.sub(r"[^a-z0-9]", "", label)

    # Exact registrable-domain identity.
    for token in tokens:
        if label == token or label_compact == token:
            return 8.0

    # Multi-word merchant:
    # best + buy -> bestbuy
    # alpine + cinemas -> alpinecinemas
    compact = "".join(tokens)
    if compact and label_compact == compact:
        return 8.0

    # Important: do NOT reward buy-tebex.io or scripts-tebex.io merely
    # because "tebex" occurs inside a larger domain label.
    return 0.0


def _compute_fused_rank(results: list[dict], query: str) -> list[dict]:
    """Final ranking: blend relevance, cross-engine consensus, and first-party authority."""

    identity = _research_identity_tokens(query)
    identity_set = {t.lower() for t in identity}

    def authority_score(item: dict) -> float:
        url = str(item.get("url") or "").lower()
        title = str(item.get("title") or "").lower()

        try:
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        except Exception:
            host = ""

        host_tokens = {
            t for t in re.findall(r"[a-z0-9]+", host)
            if t not in _GENERIC_PRODUCT_WORDS
        }

        if not host_tokens or not identity_set:
            return 0.0

        # Strong first-party signal:
        # the result's registrable-looking hostname contains all query identity
        # tokens, ignoring generic research/product words.
        token_match = identity_set <= host_tokens

        # Brand-like domains often concatenate words (bestbuy.com, 7-eleven.com,
        # deepseek.com), so also test the normalized hostname without separators.
        host_compact = re.sub(r"[^a-z0-9]+", "", host)
        identity_compact = re.sub(r"[^a-z0-9]+", "", "".join(identity))
        compact_match = bool(identity_compact) and identity_compact in host_compact

        # A query phrase matching the page title is useful, but much weaker than
        # an actual first-party domain match.
        title_identity_hits = sum(
            1 for token in identity_set
            if re.search(rf"\b{re.escape(token)}\b", title)
        )

        if token_match or compact_match:
            return 8.0

        # Partial hostname evidence gets only a modest bonus.
        overlap = len(identity_set & host_tokens)
        if overlap:
            return min(2.5, overlap * 1.0)

        # Exact brand wording in a title gets a small bonus.
        if title_identity_hits >= max(1, len(identity_set)):
            return 1.0

        return 0.0

    for r in results:
        snippet = r.get("snippet", "")
        title = r.get("title", "")
        combined_text = f"{title} {snippet}"
        bm25_score = _bm25.score(query, combined_text)
        rrf = r.get("rrf_score", 0.0)
        engine_count = len(r.get("engines", set()))

        r["relevance"] = round(bm25_score, 3)
        r["authority_score"] = authority_score(r)

        # Authority is intentionally strong enough to lift a genuine first-party
        # result above generic third-party pages, while relevance still matters.
        r["fused_score"] = round(
            rrf * 10.0
            + bm25_score * 2.0
            + engine_count * 0.5
            + r["authority_score"],
            4,
        )
        r["engines"] = sorted(r["engines"])

    results.sort(
        key=lambda x: (
            float(x.get("fused_score", 0.0)),
            float(x.get("authority_score", 0.0)),
        ),
        reverse=True,
    )
    return results

# ────────────────────────────────────────────────────────────
# Page scraping (multi-strategy: Jina → static → Playwright)
# ────────────────────────────────────────────────────────────
async def _scrape_result_page(client: httpx.AsyncClient, url: str) -> str:
    """Fetch one search-result URL and return its extracted main text.
    Strategy: Jina Reader (if key) → direct HTTP + extract_main_text."""
    parsed = urlparse(url)
    if not await _host_is_public(parsed.hostname or ""):
        return ""
    async with _scrape_semaphore:
        # Strategy 1: Jina Reader for clean extraction
        if JINA_API_KEY:
            try:
                headers = {
                    "Authorization": f"Bearer {JINA_API_KEY}",
                    "User-Agent": random.choice(_USER_AGENT_POOL),
                }
                r = await client.get(
                    f"https://r.jina.ai/{url}", headers=headers, timeout=30.0
                )
                if r.status_code == 200 and len(r.text.strip()) > 100:
                    return r.text.strip()
            except Exception:
                pass  # fall through to direct fetch

        # Strategy 2: Direct HTTP fetch + readability extraction
        try:
            r = await client.get(
                url, headers=_random_browser_headers(), follow_redirects=True
            )
            if r.status_code != 200:
                return ""
            content_type = (
                r.headers.get("content-type", "").split(";")[0].strip().lower()
            )
            if content_type not in {
                "text/html", "application/xhtml+xml", "text/plain", "",
            }:
                return ""
            return extract_main_text(r.text)
        except Exception as exc:
            print(f" [SCRAPE] {url} failed: {type(exc).__name__}: {exc}")
            return ""

# ────────────────────────────────────────────────────────────
# Result formatting (improved citations)
# ────────────────────────────────────────────────────────────
def _format_search_results(
    query: str, results: list[dict], engines: list[str], time_range: str | None
) -> str:
    if not results:
        return (
            "[relevance: none]\n"
            "Search engines returned no usable result for this concrete query. "
            "Do not infer facts from unrelated candidates."
        )
    header = f'[web search: "{query}"] {len(results)} source(s) via {", ".join(engines)}'
    if time_range:
        header += f" (time_range={time_range})"
    lines = [header, ""]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "Untitled"
        url = r.get("url", "")
        lines.append(f"[Source {i}] {title}")
        lines.append(f"  URL: {url}")
        # Quality indicators
        quality_parts = []
        engine_list = r.get("engines", [])
        if engine_list:
            quality_parts.append(f"engines={','.join(engine_list)}")
        fused = r.get("fused_score", 0)
        if fused:
            quality_parts.append(f"score={fused:.2f}")
        rel = r.get("relevance", 0)
        if rel:
            quality_parts.append(f"relevance={rel:.2f}")
        if r.get("publishedDate"):
            quality_parts.append(f"published={r['publishedDate']}")
        if quality_parts:
            lines.append(f"  Meta: {' | '.join(quality_parts)}")
        if r.get("snippet"):
            lines.append(f"  Snippet: {r['snippet']}")
        # Full page content intentionally omitted from model-facing search output.
        # Use crawl_deeper to fetch and inspect a promising source.
        lines.append("")
    return "\n".join(lines)

def _extract_result_urls(search_text: str) -> list[str]:
    """Extract URLs from formatted search results (handles both old and new formats)."""
    urls = re.findall(r"(?:\(URL:\s*|URL:\s*)(https?://[^)\s]+)", search_text or "")
    return list(dict.fromkeys(_canonical_url(u) for u in urls))

# ────────────────────────────────────────────────────────────
# Main search function
# ────────────────────────────────────────────────────────────
async def search_searxng(
    query: str,
    append_location_hint: bool = False,
    prior_queries: list[str] | None = None,
    excluded_urls: set[str] | None = None,
    regression_tier: int = 0,
    time_range: str | None = None,
    scrape: bool = True,
) -> dict:
    """
    Search SearXNG.

    A user-supplied URL is preserved as a direct URL instead of being
    submitted to SearXNG as a keyword search.

    Normal searches use per-engine ranking, RRF, BM25, and identity scoring.
    """
    q = re.sub(r"\s+", " ", str(query or "")).strip()

    if not q:
        return {
            "query": "",
            "results": [],
            "text": "[web search] Empty query.",
        }

    # Direct URL: do not search the URL string.
    if _looks_like_url(q):
        canonical = _canonical_url(q)
        print(f" [SEARCH DIRECT URL] {canonical}")

        return {
            "query": q,
            "direct_url": canonical,
            "results": [{
                "title": canonical,
                "url": canonical,
                "snippet": "User-supplied direct URL.",
                "engine": "direct-url",
                "engine_rank": 1,
                "engines": ["direct-url"],
                "rrf_score": 1.0,
                "relevance": 1.0,
                "identity_score": 1,
                "fused_score": 1.0,
            }],
            "text": (
                "[direct URL]\n"
                f"URL: {canonical}\n"
                "This URL was supplied directly by the user. "
                "Do not search this URL as keywords."
            ),
        }

    now = time.time()
    fingerprint = _query_fingerprint(q)

    cached = WEB_SEARCH_CACHE.get(fingerprint)
    if cached and now - cached[0] < SEARCH_CACHE_TTL_SECONDS:
        return cached[1]

    effective_time_range = (
        (time_range or SEARCH_TIME_RANGE or "").strip().lower() or None
    )
    if effective_time_range not in {None, "day", "week", "month", "year"}:
        effective_time_range = None

    engines = [
        "bing",
        "brave",
        "duckduckgo",
        "google",
    ][:max(1, MAX_SEARCH_ENGINES_PER_QUERY)]

    candidates: list[dict] = []

    async with _search_semaphore:
        async with httpx.AsyncClient(timeout=SEARCH_HTTP_TIMEOUT) as client:
            responses = await asyncio.gather(
                *[
                    _searxng_engine_query(
                        client,
                        q,
                        engine,
                        effective_time_range,
                    )
                    for engine in engines
                ],
                return_exceptions=True,
            )

    for response in responses:
        if isinstance(response, list):
            candidates.extend(response)

    excluded = {
        _canonical_url(str(u))
        for u in (excluded_urls or set())
    }

    if excluded:
        candidates = [
            item for item in candidates
            if _canonical_url(item.get("url", "")) not in excluded
        ]

    # Restore RRF ranking across engines.
    fused_map = _reciprocal_rank_fusion(candidates)
    results = list(fused_map.values())

    # Restore BM25 relevance + engine agreement.
    results = _compute_fused_rank(results, q)

    # Reject candidates that have no lexical identity evidence at all.
    # Returning unrelated pages is worse than returning zero results because
    # downstream classification treats web results as evidence.
    identity_tokens = _research_identity_tokens(q)
    if identity_tokens:
        relevant = []
        for item in results:
            evidence = " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("snippet") or ""),
                    str(item.get("url") or ""),
                ]
            )
            lower = re.sub(r"[^a-z0-9]+", " ", evidence.lower())
            hits = sum(
                1
                for token in identity_tokens
                if re.search(rf"\b{re.escape(token)}\b", lower)
            )
            if hits > 0:
                relevant.append(item)

        results = relevant

    # Exact identity matching is especially useful for niche entities.
    for item in results:
        evidence = " ".join([
            str(item.get("title") or ""),
            str(item.get("snippet") or ""),
            str(item.get("url") or ""),
        ])
        identity_score = _identity_score(q, evidence)
        item["identity_score"] = identity_score
        item["fused_score"] = round(
            float(item.get("fused_score", 0.0))
            + (identity_score * 1.5),
            4,
        )

    results.sort(
        key=lambda x: (
            float(x.get("fused_score", 0.0)),
            int(x.get("identity_score", 0)),
        ),
        reverse=True,
    )

    results = results[:MAX_SEARCH_UNIQUE_RESULTS]

    # Scrape only the best candidates.
    if scrape and results:
        targets = results[:SEARCH_SCRAPE_TOP_N]

        async with httpx.AsyncClient(
            timeout=SEARCH_HTTP_TIMEOUT + 10,
            follow_redirects=True,
        ) as client:
            pages = await asyncio.gather(
                *[
                    _scrape_result_page(client, item["url"])
                    for item in targets
                ],
                return_exceptions=True,
            )

        for item, page in zip(targets, pages):
            if isinstance(page, str) and page.strip():
                item["page_content"] = page[:SEARCH_SCRAPE_MAX_CHARS]

                evidence = " ".join([
                    str(item.get("title") or ""),
                    str(item.get("snippet") or ""),
                    str(item.get("page_content") or ""),
                ])

                page_identity = _identity_score(q, evidence)
                item["page_identity_score"] = page_identity
                item["fused_score"] = round(
                    float(item.get("fused_score", 0.0))
                    + page_identity,
                    4,
                )

        results.sort(
            key=lambda x: (
                float(x.get("fused_score", 0.0)),
                int(x.get("identity_score", 0)),
                int(x.get("page_identity_score", 0)),
            ),
            reverse=True,
        )

    payload = {
        "query": q,
        "results": results,
        "text": _format_raw_search_results(
            q,
            results,
            engines,
            effective_time_range,
        ),
    }

    # Remove expired cache entries.
    expired = [
        key
        for key, value in WEB_SEARCH_CACHE.items()
        if now - value[0] >= SEARCH_CACHE_TTL_SECONDS
    ]

    for key in expired:
        del WEB_SEARCH_CACHE[key]

    WEB_SEARCH_CACHE[fingerprint] = (now, payload)

    print(
        f" [SearXNG RANKED] {q!r} → "
        f"{len(results)} results returned"
    )

    for index, item in enumerate(results[:5], 1):
        print(
            f" [SEARCH RANK {index}] "
            f"score={item.get('fused_score', 0):.3f} "
            f"identity={item.get('identity_score', 0)} "
            f"title={item.get('title', '')!r}"
        )

    return payload


def _format_raw_search_results(query: str, results: list[dict], engines: list[str], time_range: str | None) -> str:
    """Return search evidence the model can actually use for merchant verification."""
    if not results:
        return f'[web search: "{query}"] 0 source(s) via {", ".join(engines)}\nSearch engines returned no results.'
    header = f'[web search: "{query}"] {len(results)} source(s) via {", ".join(engines)}'
    if time_range:
        header += f' (time_range={time_range})'
    lines = [header, ""]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "Untitled"
        url = r.get("url", "")
        snippet = re.sub(r"\s+", " ", str(r.get("snippet") or "")).strip()
        page_content = re.sub(r"\s+", " ", str(r.get("page_content") or "")).strip()
        if len(snippet) > 600:
            snippet = snippet[:600].rstrip() + "…"
        if len(page_content) > 1200:
            page_content = page_content[:1200].rstrip() + "…"
        lines.append(f"[{i}] {title}")
        lines.append(f"URL: {url}")
        if snippet:
            lines.append(f"Snippet: {snippet}")
        if page_content:
            lines.append(f"Page evidence: {page_content}")
        lines.append("")
    lines.append("Verify the merchant identity from the evidence above. If it is insufficient or off-target, use fetch_webpage on a returned URL or crawl_deeper before saving a merchant.")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# Host safety check
# ────────────────────────────────────────────────────────────
async def _host_is_public(hostname: str) -> bool:
    if not hostname:
        return False
    host = hostname.strip().lower().rstrip(".")
    blocked_names = {
        "localhost", "localhost.localdomain", "metadata.google.internal",
        "metadata", "host.docker.internal",
    }
    if host in blocked_names or host.endswith(".local"):
        return False
    try:
        infos = await asyncio.get_running_loop().run_in_executor(
            None, lambda: socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        )
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            return False
    return True

# ────────────────────────────────────────────────────────────
# Playwright renderer (kept compatible)
# ────────────────────────────────────────────────────────────
_PLAYWRIGHT_LOCK = asyncio.Lock()
_PLAYWRIGHT_INSTANCE = None
_PLAYWRIGHT_BROWSER = None
_PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(max(1, PLAYWRIGHT_CONCURRENCY))

async def _get_playwright_browser():
    global _PLAYWRIGHT_INSTANCE, _PLAYWRIGHT_BROWSER
    if not PLAYWRIGHT_ENABLED:
        return None
    async with _PLAYWRIGHT_LOCK:
        if _PLAYWRIGHT_BROWSER is not None:
            return _PLAYWRIGHT_BROWSER
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print(" Playwright unavailable; install playwright + Chromium to enable JS rendering.")
            return None
        _PLAYWRIGHT_INSTANCE = await async_playwright().start()
        _PLAYWRIGHT_BROWSER = await _PLAYWRIGHT_INSTANCE.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS
        )
        print(" [Playwright] Chromium coprocessor ready")
        return _PLAYWRIGHT_BROWSER

async def _shutdown_playwright():
    global _PLAYWRIGHT_INSTANCE, _PLAYWRIGHT_BROWSER
    if _PLAYWRIGHT_BROWSER is not None:
        await _PLAYWRIGHT_BROWSER.close()
        _PLAYWRIGHT_BROWSER = None
    if _PLAYWRIGHT_INSTANCE is not None:
        await _PLAYWRIGHT_INSTANCE.stop()
        _PLAYWRIGHT_INSTANCE = None

# ────────────────────────────────────────────────────────────
# Structured data extraction (JSON-LD)
# ────────────────────────────────────────────────────────────
def _extract_jsonld_prices(html: str) -> list[str]:
    findings: list[str] = []
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html[:50000], flags=re.DOTALL | re.IGNORECASE,
    )
    def walk(obj):
        if isinstance(obj, dict):
            typ = obj.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(t in {"Product", "Offer", "AggregateOffer"} for t in types if isinstance(t, str)):
                name = obj.get("name") or obj.get("sku") or "product"
                brand_obj = obj.get("brand")
                brand = brand_obj.get("name") if isinstance(brand_obj, dict) else brand_obj or ""
                sku = obj.get("sku") or ""
                identity_name = " ".join(
                    str(part).strip() for part in (brand, name, sku) if str(part).strip()
                )
                offers = obj.get("offers")
                candidates = (
                    offers if isinstance(offers, list)
                    else [offers] if isinstance(offers, dict) else []
                )
                for offer in candidates:
                    if not isinstance(offer, dict):
                        continue
                    price = offer.get("price")
                    currency = offer.get("priceCurrency")
                    if price is not None:
                        findings.append(
                            f"JSON-LD price: {identity_name} — {price} {currency or ''}".strip()
                        )
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for raw in blocks:
        try:
            walk(json.loads(raw.strip()))
        except Exception:
            continue
    return list(dict.fromkeys(findings))[:50]

def _extract_discovered_links(html: str, base_url: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in re.finditer(
        r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html, flags=re.DOTALL | re.IGNORECASE,
    ):
        href, anchor = match.groups()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, unescape(href)).split("#", 1)[0]
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        absolute = _canonical_url(absolute)
        anchor_text = re.sub(r"<[^>]+>", " ", anchor)
        anchor_text = re.sub(r"\s+", " ", unescape(anchor_text)).strip()
        found.append((absolute, anchor_text))
    dedup: dict[str, str] = {}
    for url, anchor in found:
        if url not in dedup or len(anchor) > len(dedup[url]):
            dedup[url] = anchor
    return list(dedup.items())[:150]

def _price_evidence_found(text: str, query: str | None = None) -> bool:
    if not text or not query:
        return False
    identity = _research_identity_tokens(query)
    specs = _research_spec_tokens(query)
    if not identity:
        return False
    for line in text.splitlines():
        if "json-ld price:" not in line.lower():
            continue
        if not re.search(r"(?:\$|USD\s*)\d", line, flags=re.IGNORECASE):
            if not re.search(r"(?:price:\s*[^—]+—\s*)\d", line, flags=re.IGNORECASE):
                continue
        if _identity_score(query, line) < max(2, min(4, len(identity))):
            continue
        if specs and _whole_token_hits(line, specs) < len(specs):
            continue
        return True
    haystack = text.lower()
    for match in re.finditer(
        r"(?:\$|USD\s*)\d{1,6}(?:,\d{3})*(?:\.\d{2})?", haystack, flags=re.IGNORECASE
    ):
        start = max(0, match.start() - 650)
        end = min(len(haystack), match.end() + 650)
        window = haystack[start:end]
        required_identity_hits = (
            min(2, len(identity)) if len(identity) <= 2 else min(3, len(identity))
        )
        if _whole_token_hits(window, identity) < required_identity_hits:
            continue
        if specs and _whole_token_hits(window, specs) < len(specs):
            continue
        return True
    return False

def _research_digest(text: str, query: str, max_chars: int = 1800) -> str:
    lines = text.splitlines()
    structured = [line for line in lines if "JSON-LD price:" in line]
    links = [line for line in lines if line.startswith("- https://")][:30]
    identity = _research_identity_tokens(query)
    plain = re.sub(
        r"\s+", " ", " ".join(
            line for line in lines
            if not line.startswith("[discovered links]")
            and not line.startswith("- https://")
        ),
    ).strip()
    excerpts: list[str] = []
    lower = plain.lower()
    if plain and identity:
        positions = [lower.find(token) for token in identity if lower.find(token) >= 0]
        if positions:
            center = min(positions)
            start = max(0, center - 450)
            excerpts.append(plain[start:start + 1300])
    parts: list[str] = []
    if structured:
        parts.append("[PRICE EVIDENCE]\n" + "\n".join(structured[:12]))
    if excerpts:
        parts.append("[RELEVANT PAGE TEXT]\n" + " ".join(excerpts)[:1300])
    if links:
        parts.append("[DISCOVERED LINKS]\n" + "\n".join(links))
    digest = "\n".join(parts).strip()
    if not digest:
        digest = plain[:max_chars] if plain else text[:max_chars]
    return digest[:max_chars]

# ────────────────────────────────────────────────────────────
# Rendered page fetch (Playwright)
# ────────────────────────────────────────────────────────────
async def fetch_webpage_rendered(
    url: str,
    allowed_urls: set[str] | None = None,
    max_chars: int = 14000,
    direct_user_url: bool = False,
) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return " Invalid URL."
    if allowed_urls is not None and _canonical_url(url) not in {
        _canonical_url(u) for u in allowed_urls
    }:
        return " Refused URL: renderer only accepts URLs surfaced by search_web."
    if not await _host_is_public(urlparse(url).hostname or ""):
        return " Refused URL: non-public host."
    browser = await _get_playwright_browser()
    if browser is None:
        return " Playwright renderer unavailable."
    async with _PLAYWRIGHT_SEMAPHORE:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
            locale="en-US",
            java_script_enabled=True,
        )
        try:
            async def route_handler(route):
                if route.request.resource_type in {"image", "media", "font"}:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", route_handler)
            page = await context.new_page()
            page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
            # Do not require the navigation to reach DOMContentLoaded.
            # Bot-protected / heavily client-rendered sites may keep navigation
            # open indefinitely even though useful content is already available.
            try:
                response = await page.goto(
                    url, wait_until="commit", timeout=PLAYWRIGHT_TIMEOUT_MS
                )
            except Exception as exc:
                print(
                    f" [Playwright] Navigation exception for {url}: "
                    f"{type(exc).__name__}: {exc}"
                )
                response = None

            if response is not None and response.status >= 400:
                return f" Rendered fetch HTTP {response.status}."

            try:
                await page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=5000,
                )
            except Exception:
                # The page may still have useful rendered content; continue.
                pass

            if PLAYWRIGHT_WAIT_MS > 0:
                await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)

            initial_host = (urlparse(url).hostname or "").lower().removeprefix("www.")
            final_host = (urlparse(page.url).hostname or "").lower().removeprefix("www.")
            if initial_host != final_host and not direct_user_url:
                return " Refused redirect target: cross-site redirect."

            try:
                body = await page.locator("body").inner_text(timeout=5000)
            except Exception:
                body = ""
            html = await page.content()
            structured = _extract_jsonld_prices(html)
            links = []
            try:
                raw = await page.locator("a").evaluate_all(
                    "els => els.map(a => ({href: a.href || '', text: (a.innerText || a.textContent || '').trim()})).slice(0, 250)"
                )
                seen = set()
                for item in raw or []:
                    href = str(item.get("href", ""))
                    text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
                    if not href.startswith(("http://", "https://")):
                        continue
                    u = _canonical_url(href)
                    if u in seen:
                        continue
                    seen.add(u)
                    links.append((u, text))
            except Exception:
                pass
            parts = [
                f"[rendered page: {page.url}]",
                f"[page title: {await page.title()}]",
            ]
            if structured:
                parts.append("[structured product data]\n" + "\n".join(f"- {x}" for x in structured))
            if links:
                parts.append("[discovered links]\n" + "\n".join(f"- {u} | {a}" for u, a in links[:80]))
            if body:
                parts.append(body[:max_chars])
            return "\n".join(parts) if len(parts) > 1 else " Rendered page contained no readable text."
        except Exception as exc:
            return f" Playwright render failed: {type(exc).__name__}: {exc}"
        finally:
            await context.close()

# ────────────────────────────────────────────────────────────
# Static + rendered page fetch
# ────────────────────────────────────────────────────────────
async def fetch_webpage(
    url: str,
    allowed_urls: set[str] | None = None,
    max_chars: int = 5000,
    discover_links: bool = False,
    prefer_rendered: bool = False,
    direct_user_url: bool = False,
) -> str:
    if not url or not url.lower().startswith(("http://", "https://")):
        return f" Not a valid http(s) URL: {url!r}"
    parsed = urlparse(url)
    if not await _host_is_public(parsed.hostname or ""):
        return " Refused URL: only publicly routable hosts may be fetched."

    key = _canonical_url(url)
    now = time.time()
    cached = WEB_PAGE_CACHE.get(key)
    if cached and now - cached[0] < RESEARCH_PAGE_CACHE_TTL_SECONDS and not prefer_rendered:
        return cached[1]

    static_result = None
    static_error = ""
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers=_random_browser_headers()
        ) as client:
            r = await client.get(url)
    except Exception as exc:
        r = None
        static_error = f" Fetch failed: {type(exc).__name__}: {exc}"

    if r is not None:
        final_host = (urlparse(str(r.url)).hostname or "").lower().removeprefix("www.")
        initial_host = (parsed.hostname or "").lower().removeprefix("www.")
        if not await _host_is_public(final_host):
            return " Refused redirect target: final host is not public."
        if final_host != initial_host and not direct_user_url:
            return " Refused redirect target: different host."
        if r.status_code == 200:
            content_type = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type == "application/pdf" or url.lower().endswith(".pdf"):
                parsed_pdf = await _extract_pdf_content(r.content)
                result = f"[fetched PDF: {r.url}]\n{parsed_pdf['text']}"
                if parsed_pdf["image_pages"]:
                    result += f"\n[{len(parsed_pdf['image_pages'])} page(s) had no usable text layer and were rendered as images.]"
                WEB_PAGE_CACHE[key] = (now, result, False)
                return result
            html = r.text
            structured = _extract_jsonld_prices(html)
            discovered = _extract_discovered_links(html, str(r.url)) if discover_links else []
            page_text = extract_main_text(html)

            # A non-empty response is not necessarily usable: many JS-heavy
            # sites return only a thin application shell until JavaScript runs.
            shell_markers = (
                "enable javascript",
                "javascript is required",
                "please enable javascript",
                "you need to enable javascript",
                "loading...",
                "just a moment...",
                "checking your browser",
            )
            normalized_text = re.sub(r"\s+", " ", page_text or "").strip().lower()
            likely_js_shell = (
                not page_text
                or len(page_text.strip()) < 250
                or any(marker in normalized_text for marker in shell_markers)
            )

            if (page_text or structured) and not likely_js_shell:
                parts = [f"[fetched page: {r.url}]"]
                if structured:
                    parts.append("[structured product data]\n" + "\n".join(f"- {x}" for x in structured))
                if discovered:
                    parts.append("[discovered links]\n" + "\n".join(f"- {u} | {a}" for u, a in discovered[:100]))
                if page_text:
                    parts.append(page_text[:max_chars] + (" …[truncated]" if len(page_text) > max_chars else ""))
                static_result = "\n".join(parts)
                WEB_PAGE_CACHE[key] = (now, static_result, False)
                if not prefer_rendered:
                    return static_result

    if static_result is None:
        static_error = f" Fetched {url} but found no readable text content (likely JS-only)."
    else:
        static_error = f" {url} returned HTTP {r.status_code if r else 'N/A'}."

    if prefer_rendered or static_error.startswith(" Fetched"):
        rendered = await fetch_webpage_rendered(
            url,
            allowed_urls=allowed_urls,
            max_chars=max(12000, max_chars * 2),
            direct_user_url=direct_user_url,
        )
        if rendered and not rendered.startswith((" ", " Refused", " Playwright renderer unavailable", " Playwright render failed")):
            WEB_PAGE_CACHE[key] = (now, rendered, True)
            return rendered

    if static_result is not None:
        return static_result
    return static_error or " Page fetch failed."

# ────────────────────────────────────────────────────────────
# Recursive crawl (kept compatible)
# ────────────────────────────────────────────────────────────
async def crawl_research_pages(
    query: str,
    search_result: str,
    allowed_urls: set[str],
    visited_urls: set[str] | None = None,
) -> tuple[str, bool, str | None, int, bool]:
    urls = [u for u in _extract_result_urls(search_result) if not _host_is_junk(u)]
    if not urls:
        return (
            "[RECURSIVE PAGE CRAWL]\nNo relevant crawl seeds survived search filtering.",
            False, None, 0, False,
        )
    identity = _research_identity_tokens(query)
    specs = _research_spec_tokens(query)
    visited = visited_urls if visited_urls is not None else set()

    def rank_seed(url):
        low = url.lower()
        ih = _whole_token_hits(low, identity)
        sh = _whole_token_hits(low, specs)
        product = any(m in low for m in _PRODUCT_PATH_MARKERS)
        category = any(m in low for m in ("/collections/", "/category/", "/brands/"))
        return ih * 12 + sh * 8 + (14 if product else 0) - (5 if category else 0)

    seeds = [u for u in dict.fromkeys(urls) if _canonical_url(u) not in visited][:6]
    queue = [(u, 0, "search result") for u in seeds]
    queued = {_canonical_url(u) for u in seeds}
    seen = set()
    blocks = []
    pages = 0
    resolved = None

    for seed in seeds:
        allowed_urls.add(seed)

    print(
        f" [CRAWL START] {query!r} seeds={len(seeds)} max_pages={MAX_RESEARCH_CRAWL_PAGES_PER_ITEM} max_depth={MAX_RESEARCH_CRAWL_DEPTH}"
    )

    while queue and pages < MAX_RESEARCH_CRAWL_PAGES_PER_ITEM:
        url, depth, anchor = queue.pop(0)
        key = _canonical_url(url)
        if key in seen or key in visited:
            continue
        seen.add(key)
        visited.add(key)
        productish = any(
            m in url.lower()
            for m in ("/products/", "/product/", "/item/", "/p/", "/dp/", "/pd/")
        )
        fetched = await fetch_webpage(
            url, allowed_urls=allowed_urls, max_chars=12000,
            discover_links=True, prefer_rendered=False,
        )
        pages += 1
        if productish and PLAYWRIGHT_ENABLED and not _price_evidence_found(fetched, query):
            rendered = await fetch_webpage(
                url, allowed_urls=allowed_urls, max_chars=14000,
                discover_links=True, prefer_rendered=True,
            )
            if not rendered.startswith(""):
                fetched = rendered
        digest = _research_digest(fetched, query, max_chars=2600)
        blocks.append(f"[crawl page depth={depth} url={url}]\n{digest}")
        print(f" [CRAWL PAGE {pages}/{MAX_RESEARCH_CRAWL_PAGES_PER_ITEM}] depth={depth} url={url}")
        if fetched.startswith(""):
            continue
        if _price_evidence_found(fetched, query):
            resolved = url
            print(f" [RESEARCH RESOLVED] {query!r} via {url}")
            break
        if depth >= MAX_RESEARCH_CRAWL_DEPTH:
            continue
        page_host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        discovered = re.findall(r"- (https?://[^\s|]+) \| ([^\n]*)", fetched)
        for du, da in discovered:
            du = _canonical_url(du)
            if du in seen or du in queued or du in visited or _host_is_junk(du):
                continue
            dh = (urlparse(du).hostname or "").lower().removeprefix("www.")
            if dh != page_host:
                continue
            if len(queue) >= MAX_RESEARCH_QUEUE_SIZE:
                break
            allowed_urls.add(du)
            queued.add(du)
            queue.append((du, depth + 1, da))

    result = "[RECURSIVE PAGE CRAWL]\n" + "\n---\n".join(blocks)
    return result, bool(resolved), resolved, pages, True



async def search_merchant(query: str, *, scrape: bool = True) -> dict:
    """
    Merchant-focused search with controlled query fallbacks.

    Ordinary search_web remains verbatim. This helper exists only for noisy
    transaction descriptors and stops at the first query producing evidence
    that actually overlaps the merchant identity.
    """
    candidates = _merchant_search_queries(query)
    if not candidates:
        return {
            "query": "",
            "results": [],
            "text": "[relevance: none]\nEmpty merchant query.",
        }

    best = None

    for candidate in candidates:
        result = await search_searxng(candidate, scrape=scrape)
        results = result.get("results") or []

        if results:
            identity = _research_identity_tokens(candidate)
            if identity:
                relevant = []
                for item in results:
                    evidence = " ".join(
                        [
                            str(item.get("title") or ""),
                            str(item.get("snippet") or ""),
                            str(item.get("url") or ""),
                        ]
                    ).lower()

                    hits = sum(
                        1
                        for token in identity
                        if re.search(
                            rf"\b{re.escape(token)}\b",
                            re.sub(r"[^a-z0-9]+", " ", evidence),
                        )
                    )

                    if hits:
                        relevant.append(item)

                if relevant:
                    result["results"] = relevant
                    result["text"] = _format_raw_search_results(
                        candidate,
                        relevant,
                        sorted(
                            {
                                engine
                                for item in relevant
                                for engine in item.get("engines", [])
                            }
                        ),
                        result.get("time_range"),
                    )
                    print(
                        f" [MERCHANT SEARCH] {query!r} -> {candidate!r} "
                        f"({len(relevant)} relevant results)"
                    )
                    return result

            else:
                return result

        best = result

    print(f" [MERCHANT SEARCH] No relevant evidence for {query!r}")
    return {
        "query": query,
        "results": [],
        "text": (
            f'[web search: "{query}"] [relevance: none]\n'
            "No search candidate contained usable merchant-identity evidence."
        ),
    }


__all__ = ['_normalize_query', '_research_subject_matches', '_format_search_results', '_canonical_url', '_PRICE_WORDS', '_query_fingerprint', '_DOCUMENT_HINT_WORDS', '_format_raw_search_results', '_GENERIC_PRODUCT_WORDS', 'WEB_SEARCH_CACHE', '_research_terms', 'fetch_webpage_rendered', 'BM25Scorer', '_looks_like_document_research', '_research_digest', '_shutdown_playwright', '_quote_search_phrase', '_bm25', '_looks_like_price_research', '_PLAYWRIGHT_SEMAPHORE', 'fetch_webpage', '_scrape_result_page', 'crawl_research_pages', '_PLAYWRIGHT_INSTANCE', '_whole_token_hits', '_compute_fused_rank', '_price_evidence_found', '_PLAYWRIGHT_BROWSER', '_PRODUCT_PATH_MARKERS', '_research_spec_tokens', '_research_identity_tokens', '_BOILERPLATE_TAGS', '_smart_content_window', '_research_core_tokens', '_get_playwright_browser', '_UNIT_RE', '_extract_result_urls', '_host_is_junk', '_USER_AGENT_POOL', '_host_is_public', '_TRACKING_QUERY_KEYS', '_build_research_variants', 'WEB_PAGE_CACHE', 'search_searxng', '_RESEARCH_JUNK_HOSTS', 'extract_main_text', '_reciprocal_rank_fusion', '_STOPWORDS', '_random_browser_headers', '_research_subject', '_PLAYWRIGHT_LOCK', '_searxng_engine_query', '_extract_jsonld_prices', '_looks_like_specific_fact_research', '_identity_score', '_scrape_semaphore', '_extract_discovered_links', '_search_semaphore']
