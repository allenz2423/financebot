"""Bounded, source-preserving multi-query research packets.

This is deliberately not an LLM summarizer. It composes Delilah's existing
search and fetch primitives into one generic read-only operation that is useful
for technical research, fact checking, products, organizations, travel, and
merchant investigation without adding domain-specific assumptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SearchFunc = Callable[..., Awaitable[Mapping[str, Any]]]
FetchFunc = Callable[..., Awaitable[str]]


def _clean_queries(query: str | None, queries: Sequence[str] | None) -> list[str]:
    values: list[str] = []
    for value in [query, *(queries or ())]:
        clean = " ".join(str(value or "").split()).strip()
        if clean and clean.casefold() not in {item.casefold() for item in values}:
            values.append(clean)
    return values[:5]


def _canonical_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return raw
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _result_score(result: Mapping[str, Any], fallback_rank: int) -> float:
    for key in ("fused_score", "relevance", "identity_score", "rrf_score"):
        try:
            value = float(result.get(key))
            if value == value:
                return value
        except (TypeError, ValueError):
            pass
    return max(0.0, 1.0 / max(1, fallback_rank))


def _source_card(result: Mapping[str, Any], query: str, rank: int) -> dict[str, Any] | None:
    url = _canonical_url(str(result.get("url") or result.get("link") or ""))
    if not url.startswith(("http://", "https://")):
        return None
    return {
        "url": url,
        "title": str(result.get("title") or "").strip(),
        "snippet": str(result.get("snippet") or result.get("description") or "").strip(),
        "query": query,
        "rank": rank,
        "score": _result_score(result, rank),
        "engines": list(result.get("engines") or []) if isinstance(result.get("engines"), list) else [],
    }


async def build_research_packet(
    query: str | None = None,
    queries: Sequence[str] | None = None,
    *,
    search_func: SearchFunc,
    fetch_func: FetchFunc,
    max_sources: int = 6,
    fetch_top: int = 3,
    max_chars: int = 5000,
    time_range: str | None = None,
) -> dict[str, Any]:
    """Search and fetch a bounded set of sources while preserving provenance."""
    clean_queries = _clean_queries(query, queries)
    max_sources = max(1, min(int(max_sources or 6), 8))
    fetch_top = max(0, min(int(fetch_top or 0), max_sources))
    max_chars = max(1000, min(int(max_chars or 5000), 12000))
    if not clean_queries:
        return {"status": "invalid", "error": "query or queries is required", "sources": []}

    search_results = await asyncio.gather(
        *[
            search_func(
                q,
                time_range=time_range if time_range in {"day", "week", "month", "year"} else None,
                user_id=None,
            )
            for q in clean_queries
        ],
        return_exceptions=True,
    )
    by_url: dict[str, dict[str, Any]] = {}
    search_errors: list[dict[str, str]] = []
    for query_text, payload in zip(clean_queries, search_results):
        if isinstance(payload, Exception):
            search_errors.append({"query": query_text, "error": f"{type(payload).__name__}: {payload}"})
            continue
        results = payload.get("results", []) if isinstance(payload, Mapping) else []
        if not isinstance(results, list):
            continue
        for rank, result in enumerate(results, start=1):
            if not isinstance(result, Mapping):
                continue
            card = _source_card(result, query_text, rank)
            if not card:
                continue
            prior = by_url.get(card["url"])
            if prior is None or (card["score"], -card["rank"]) > (prior["score"], -prior["rank"]):
                by_url[card["url"]] = card

    sources = sorted(by_url.values(), key=lambda item: (-float(item["score"]), item["rank"], item["url"]))[:max_sources]

    async def fetch_card(card: dict[str, Any]) -> dict[str, Any]:
        try:
            text = await fetch_func(card["url"], max_chars=max_chars)
            excerpt = str(text or "")[:max_chars].strip()
            card.update({"fetch_status": "fetched", "fetched": True, "excerpt": excerpt})
        except Exception as exc:
            card.update({
                "fetch_status": "failed",
                "fetched": False,
                "excerpt": "",
                "fetch_error": f"{type(exc).__name__}: {exc}",
            })
        return card

    await asyncio.gather(*(fetch_card(card) for card in sources[:fetch_top]))
    for card in sources[fetch_top:]:
        card.update({"fetch_status": "not_requested", "fetched": False, "excerpt": ""})

    for index, card in enumerate(sources, start=1):
        card["source_id"] = f"source_{index}"
    evidence_text = "\n\n".join(
        f"[{card['source_id']}] {card['title']}\nURL: {card['url']}\n"
        f"Query: {card['query']}\nSnippet: {card['snippet']}\n"
        f"Fetched excerpt: {card['excerpt']}"
        for card in sources
    )
    return {
        "status": "success" if sources else "no_sources",
        "queries": clean_queries,
        "source_count": len(sources),
        "fetched_count": sum(1 for card in sources if card["fetched"]),
        "search_errors": search_errors,
        "sources": sources,
        "evidence_text": evidence_text,
        "interpretation": "Source packet only; Delilah must distinguish evidence from conclusions and report conflicts.",
    }


__all__ = ["build_research_packet"]
