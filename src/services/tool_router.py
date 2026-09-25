"""Semantic tool-router retrieval with staged live authorization.

The default mode is observational: it ranks the *full callable catalog* for one
turn and appends a JSONL record per model round. An explicit
``TOOL_ROUTER_LIVE=1`` promotion enables a bounded authorization set derived
from the same proposal. Retrieval failure fails open to the controller's
existing schema/permission gates; it never grants a tool that those gates did
not already offer. The design contract lives in
``docs/tool-router-loop-contract.md``.

Heavy dependencies (httpx, qdrant_client, the tool catalog in llm.py) are
imported lazily inside functions so that merely importing this module is cheap
and the flag-off loop path performs zero router work.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SHADOW_COLLECTION = "delilah_tool_router_shadow"
SCHEMA_VERSION = 1
ROUTER_VERSION = "shadow-v1"
TOP_K_DEFAULT = 8
PROPOSE_TIMEOUT_S = 1.5
INDEX_TIMEOUT_S = 20.0
_CACHE_MAX = 512
_LOG_DIR = Path("data/tool-router-shadow")

# Conditional tools sit across the read/mutate line: they are offered in both
# partitions but the decision is made at call time by argument-aware gates.
CONDITIONAL_TOOLS = frozenset({
    "request_user_form", "run_python_sandbox", "run_shell",
})

GATE_DOMAIN = frozenset()

# --------------------------------------------------------------------------
# Flag + sampling
# --------------------------------------------------------------------------

def shadow_enabled() -> bool:
    """True when shadow instrumentation is switched on (default off)."""
    raw = str(os.getenv("TOOL_ROUTER_SHADOW", "0")).strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def live_enabled() -> bool:
    """True only when live semantic authorization was explicitly promoted."""
    raw = str(os.getenv("TOOL_ROUTER_LIVE", "0")).strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def live_min_score() -> float:
    """Minimum normalized retrieval score for an optional live candidate.

    The default is deliberately permissive because the existing schema and
    execution grant remain the authority. Raising this value is a rollout
    tuning knob, not a security boundary.
    """
    try:
        return max(0.0, min(1.0, float(os.getenv("TOOL_ROUTER_LIVE_MIN_SCORE", "0"))))
    except (TypeError, ValueError):
        return 0.0


def shadow_sample_n() -> int:
    """1-in-N turn sampling; 1 logs every sampled turn."""
    try:
        n = int(os.getenv("TOOL_ROUTER_SHADOW_SAMPLE", "1"))
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def sample_hit(seq: int) -> bool:
    n = shadow_sample_n()
    return n <= 1 or (seq % n) == 0


# --------------------------------------------------------------------------
# Classification + cards (lazy catalog import)
# --------------------------------------------------------------------------

def _mutation_tools() -> set[str]:
    from src.services.llm import MUTATION_TOOLS
    return set(MUTATION_TOOLS)


_ACTION_EXTRA = frozenset({
    "send_push_alert", "monitor_add_rule", "monitor_delete_rule",
    "monitor_clear_all_rules", "monitor_run_pass", "refresh_knowledge_base",
    "pin_knowledge_immutable", "index_financial_snapshot_to_qdrant",
    "save_to_knowledge_base", "retract_world_model_claim", "set_savings_goal",
    "delete_savings_goal", "allocate_next_best_dollar", "manage_subscription",
    "cancel_subscription", "remove_subscription", "add_recurring_bill",
    "add_merchant_alias", "sync_plaid_accounting", "pull_live_financial_data",
    "reconcile_expected_and_planned_transactions", "fill_pdf_form",
    "install_python_package", "scan_and_auto_tag_deductions",
    "canonicalize_transactions", "auto_reconcile_ledger",
    "prioritize_savings_goals", "fund_savings_goal",
})


def classify(name: str) -> str:
    if name in CONDITIONAL_TOOLS:
        return "conditional"
    if name in (_mutation_tools() | _ACTION_EXTRA):
        return "mutate"
    return "read"


_cards_cache: Optional[list[dict[str, Any]]] = None


def tool_cards() -> list[dict[str, Any]]:
    """One card per callable tool: name + description + parameter names."""
    global _cards_cache
    if _cards_cache is not None:
        return _cards_cache
    from src.services.llm import BOT_TOOLS_SCHEMA
    cards, seen = [], set()
    for entry in BOT_TOOLS_SCHEMA:
        fn = entry.get("function") or {}
        name = fn.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        desc = (fn.get("description") or "").strip()
        props = list(((fn.get("parameters") or {}).get("properties") or {}).keys())
        text = f"{name.replace('_', ' ')}. {desc}"
        if props:
            text += f" Parameters: {', '.join(props)}."
        cards.append({"name": name, "text": text[:2000], "class": classify(name)})
    _cards_cache = cards
    return cards


def catalog_hash() -> str:
    cards = tool_cards()
    blob = "\n".join(f"{c['name']}:{c['text']}:{c['class']}" for c in cards)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def known_names() -> set[str]:
    return {c["name"] for c in tool_cards()}


def tool_class(name: str) -> str:
    for c in tool_cards():
        if c["name"] == name:
            return c["class"]
    return "unknown"


# --------------------------------------------------------------------------
# Lexical retrieval (BM25)
# --------------------------------------------------------------------------

def _tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


class BM25:
    def __init__(self, names: list[str], docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.names = names
        self.docs = [_tok(f"{n.replace('_', ' ')} {n.replace('_', ' ')} {d}") for n, d in zip(names, docs)]
        self.k1, self.b = k1, b
        self.avgdl = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        self.tf = [Counter(d) for d in self.docs]
        df: Counter = Counter()
        for d in self.docs:
            for t in set(d):
                df[t] += 1
        self.N, self.df = len(self.docs), df

    def _idf(self, t: str) -> float:
        n = self.df.get(t, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def scores(self, query: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, d, tf in zip(self.names, self.docs, self.tf):
            dl, s = len(d), 0.0
            for t in _tok(query):
                f = tf.get(t)
                if f:
                    s += self._idf(t) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out[name] = s
        return out


def _minmax(scores: dict[str, float], names: list[str]) -> dict[str, float]:
    vals = [scores.get(n, 0.0) for n in names]
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 0.0)
    span = (hi - lo) or 1.0
    return {n: (scores.get(n, 0.0) - lo) / span for n in names}


# --------------------------------------------------------------------------
# Embedding index (lazy, lock-guarded)
# --------------------------------------------------------------------------

_index_lock: Optional[asyncio.Lock] = None
_index_hash: Optional[str] = None


def _get_lock() -> asyncio.Lock:
    global _index_lock
    if _index_lock is None:
        _index_lock = asyncio.Lock()
    return _index_lock


async def _index_cards(cards: list[dict[str, Any]]) -> None:
    from src.services.qdrant_client import ensure_collection, get_embeddings, upsert_points
    await ensure_collection(SHADOW_COLLECTION)
    texts = [c["text"] for c in cards]
    try:
        vectors = await get_embeddings(texts)
    except Exception:
        vectors = [await _one(c["text"]) for c in cards]  # pragma: no cover
    points = [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tool-router-shadow:{c['name']}")),
            "vector": v,
            "payload": {"domain": "tool_router", "name": c["name"], "class": c["class"]},
        }
        for c, v in zip(cards, vectors)
    ]
    await upsert_points(points, collection_name=SHADOW_COLLECTION)


async def _one(text: str):
    from src.services.qdrant_client import get_embedding
    return await get_embedding(text)


async def ensure_index() -> None:
    """Build/refresh the tool-card index once per catalog revision.

    Called by the loop *outside* the per-round timeout budget: a cold build
    embeds the whole catalog and must not be cancelled mid-flight.
    """
    global _index_hash
    cards = tool_cards()
    h = catalog_hash()
    if _index_hash == h:
        return
    async with _get_lock():
        if _index_hash == h:
            return
        await _index_cards(cards)
        _index_hash = h


def index_ready() -> bool:
    return _index_hash is not None


_INDEX_RETRY_COOLDOWN_S = 120.0
_index_task: Optional["asyncio.Task"] = None
_index_next_attempt: float = 0.0


async def _index_runner() -> None:
    try:
        await ensure_index()
    except Exception:
        logger.debug("tool-router shadow index build failed", exc_info=True)


def schedule_index() -> None:
    """Ensure the card index is being built *without blocking the caller*.

    ``ensure_index`` embeds the whole catalog and talks to Qdrant; awaiting it on
    a turn's critical path means a hung backend makes every sampled turn pay the
    index timeout. Here the build runs as a background task: while it is in
    flight (or hasn't started) ``propose`` degrades to lexical. After a failed
    build we wait a cooldown before retrying so a persistently broken backend
    cannot spawn an attempt every turn.
    """
    global _index_task, _index_next_attempt
    if index_ready():
        return
    now = time.monotonic()
    if now < _index_next_attempt:
        return
    if _index_task is not None and not _index_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _index_next_attempt = now + _INDEX_RETRY_COOLDOWN_S
    _index_task = loop.create_task(_index_runner())


async def _emb_scores(embed_text: str, limit: int) -> dict[str, float]:
    import httpx
    from src.services.qdrant_client import _get_qdrant_url, get_embedding
    base = _get_qdrant_url()
    payload = {
        "vector": await get_embedding(embed_text),
        "limit": limit,
        "with_payload": True,
        "filter": {"must": [{"key": "domain", "match": {"value": "tool_router"}}]},
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{base}/collections/{SHADOW_COLLECTION}/points/search", json=payload)
        resp.raise_for_status()
    return {r["payload"]["name"]: r["score"] for r in resp.json().get("result", [])}


# --------------------------------------------------------------------------
# Ranking cache
# --------------------------------------------------------------------------

_cache: dict[tuple, dict] = {}
_bm25_cache: Optional[tuple[str, "BM25"]] = None


def _bm25() -> "BM25":
    """Catalog-keyed BM25 index (tokenising 178 cards is not free per call)."""
    global _bm25_cache
    cards = tool_cards()
    h = catalog_hash()
    if _bm25_cache is not None and _bm25_cache[0] == h:
        return _bm25_cache[1]
    bm = BM25([c["name"] for c in cards], [c["text"] for c in cards])
    _bm25_cache = (h, bm)
    return bm


def _cache_get(key: tuple) -> Optional[dict]:
    return _cache.get(key)


def _cache_put(key: tuple, value: dict) -> None:
    if len(_cache) >= _CACHE_MAX:
        for k in list(_cache.keys())[: _CACHE_MAX // 4]:
            _cache.pop(k, None)
    _cache[key] = value


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def browser_gate(query: str) -> bool:
    return False


def _hash_text(s: str, n: int = 16) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:n]


def mode_signature(mode: str, gate: bool) -> str:
    return f"mode={mode};browser_gate={int(bool(gate))}"


async def propose(query: str, mode_signature: str = "", top_k: int = TOP_K_DEFAULT) -> dict[str, Any]:
    """Rank the full catalog for ``query`` (hybrid; lexical fallback).

    Returns a bounded candidate set plus the read/write partitions. Never raises
    out to the caller's loop for retrieval problems: embedding/Qdrant failures
    degrade to BM25 only with ``fallback_reason`` set.
    """
    t0 = time.perf_counter()
    cards = tool_cards()
    names = [c["name"] for c in cards]
    cls = {c["name"]: c["class"] for c in cards}
    gate = browser_gate(query)
    embed_text = f"{query}\n{mode_signature}".strip() if mode_signature else (query or "")
    key = (catalog_hash(), _hash_text(embed_text, 32), top_k)

    cached = _cache_get(key)
    if cached is not None:
        out = dict(cached)
        out["cache_hit"] = True
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out

    method, fallback_reason, emb = "hybrid", None, None
    if not index_ready():
        # The loop builds the index before proposing; if it is not ready yet
        # (cold start, or the build failed) rank lexically rather than block.
        method, fallback_reason = "lexical", "index_not_ready"
    else:
        try:
            emb = await _emb_scores(embed_text, len(cards))
        except Exception as exc:  # degrade, never fail the turn
            method = "lexical"
            fallback_reason = f"{type(exc).__name__}: {exc}"[:200]

    bm = _bm25()
    lex = _minmax(bm.scores(query), names)
    if emb is not None:
        scores = {n: 0.5 * emb.get(n, 0.0) + 0.5 * lex.get(n, 0.0) for n in names}
    else:
        scores = lex

    ranked = sorted(names, key=lambda n: scores[n], reverse=True)
    raw_topk = [
        {"name": n, "rank": i + 1, "score": round(scores[n], 6), "class": cls[n]}
        for i, n in enumerate(ranked[:top_k])
    ]
    read_ranked = [n for n in ranked if cls[n] in ("read", "conditional")]
    write_ranked = [n for n in ranked if cls[n] in ("mutate", "conditional")]
    domain_ranked = [n for n in ranked if not gate or n in GATE_DOMAIN]
    out = {
        "method": method,
        "fallback_reason": fallback_reason,
        "browser_gate": gate,
        "mode_signature": mode_signature,
        "raw_topk": raw_topk,
        "read_topk": read_ranked[:top_k],
        "write_topk": write_ranked[:top_k],
        "domain_topk": domain_ranked[:top_k],
        "top1_score": round(scores[ranked[0]], 6) if ranked else None,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "cache_hit": False,
    }
    _cache_put(key, out)
    return out


def authorize_live_proposal(
    proposal: Optional[dict[str, Any]],
    *,
    offered_names: set[str] | frozenset[str],
    required_tools: set[str] | frozenset[str] = frozenset(),
    always_allow: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Convert one proposal into a bounded, auditable live authorization.

    This function is intentionally not a standalone permission system. It can
    only narrow the controller's already-offered names. Required/controller
    tools remain available, while retrieval candidates are restricted to the
    proposal's top-k partitions and score threshold. Missing/invalid proposals
    fail open so a transient embedding/index outage cannot strand a legitimate
    workflow; the caller records that decision in the round trace.
    """
    offered = {str(name) for name in offered_names if name}
    required = {str(name) for name in required_tools if name}
    keep = {str(name) for name in always_allow if name}
    if not isinstance(proposal, dict):
        return {
            "decision": "fail_open",
            "reason": "proposal_unavailable",
            "allowed_names": offered,
            "candidate_names": set(),
        }

    threshold = live_min_score()
    candidates: set[str] = set()
    for item in proposal.get("raw_topk") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        if name and score >= threshold:
            candidates.add(name)
    for key in ("read_topk", "write_topk", "domain_topk"):
        candidates.update(str(name) for name in (proposal.get(key) or []) if name)

    allowed = (candidates | required | keep) & offered
    # These controller primitives are necessary to make a rejected or
    # incomplete route recoverable without bypassing the live gate.
    allowed |= {name for name in {"end_turn", "load_tool_schemas", "enable_reasoning"} if name in offered}
    return {
        "decision": "authorized",
        "reason": "proposal_topk",
        "allowed_names": allowed,
        "candidate_names": candidates & offered,
        "threshold": threshold,
    }


# --------------------------------------------------------------------------
# Per-turn state + provenance
# --------------------------------------------------------------------------

_turn_seq = itertools.count(1)


def begin_turn(prompt_text: str, intent_seed: Optional[set] = None, sampled: Optional[bool] = None) -> dict[str, Any]:
    """Create per-turn shadow state. Provenance is a per-turn map because the
    dynamic tool set is a lossy union (re-seeding an already-loaded tool mutates
    nothing, so a snapshot+diff cannot recover explicit loads)."""
    seq = next(_turn_seq)
    return {
        "seq": seq,
        "turn_id": str(uuid.uuid4()),
        "query_hash": _hash_text(prompt_text or "", 64),
        "prompt_text": prompt_text or "",
        "intent_seed": set(intent_seed or ()),
        "explicit_loads": set(),
        "rounds": 0,
        "sampled": sample_hit(seq) if sampled is None else bool(sampled),
    }


def observe_load_tool_schemas(turn: Optional[dict], names: Any) -> None:
    """Record explicit ``load_tool_schemas`` names even when the set union is a
    no-op (tool was already seeded)."""
    if not turn:
        return
    loads = turn.setdefault("explicit_loads", set())
    if isinstance(names, str):
        names = [names]
    for n in names or ():
        if n:
            loads.add(str(n))


def _provenance(turn: dict, name: str, in_offering: bool) -> str:
    seed = name in turn.get("intent_seed", ())
    load = name in turn.get("explicit_loads", ())
    if seed and load:
        return "both"
    if load:
        return "explicit_load"
    if seed:
        return "intent_seed"
    if in_offering:
        return "core_or_baseline"
    return "unobserved"


def build_record(
    *,
    turn: dict,
    round_id: int,
    mode: str,
    restricted: bool,
    chosen: list[str],
    offered_names: set,
    required_tools: Optional[list],
    proposed: Optional[dict],
    router_inert: bool = False,
    error: Optional[str] = None,
    end_turn: bool = False,
) -> dict[str, Any]:
    """Assemble one JSONL record (no prompt text, page text, args, or results)."""
    cards = tool_cards()
    catalog_size = len(cards)
    known = known_names()
    cls = {c["name"]: c["class"] for c in cards}
    seed = turn.get("intent_seed", set())
    loads = turn.get("explicit_loads", set())
    offered_names = set(offered_names or ())

    topk = []
    if proposed:
        topk = [c["name"] for c in proposed.get("raw_topk", ())]
    topk_set = set(topk)
    rank_of = {c["name"]: c["rank"] for c in proposed.get("raw_topk", ())} if proposed else {}
    domain_topk = set(proposed.get("domain_topk", ())) if proposed else set()
    partition_topk = set()
    if proposed:
        read_topk = set(proposed.get("read_topk", ()))
        write_topk = set(proposed.get("write_topk", ()))
        for n in chosen:
            if cls.get(n) == "mutate":
                if n in write_topk:
                    partition_topk.add(n)
            elif cls.get(n) in ("read", "conditional"):
                if n in read_topk:
                    partition_topk.add(n)

    chosen_records = [
        {
            "name": n,
            "class": cls.get(n, "unknown"),
            "in_offering": n in offered_names,
            "provenance": _provenance(turn, n, n in offered_names),
            "in_topk": n in topk_set,
            "in_domain_topk": n in domain_topk,
            "in_partition_topk": n in partition_topk,
            "in_counterfactual_topk": (
                n in partition_topk and (not proposed.get("browser_gate") or n in domain_topk)
            ) if proposed else False,
            "rank": rank_of.get(n),
        }
        for n in chosen
    ]

    # Projection: in canary the intent seed `I` is replaced by router candidates,
    # but the preserved set `F = C ∪ L` (core ∪ explicit loads) survives — the
    # router may not erase a tool the model explicitly loaded. Only the seed is
    # subtracted. Candidate additions are the *domain-gated* set (`T ∩ G`),
    # matching `O_router = F ∪ (T ∩ G ∩ E)`. Documented approximation
    # (see contract "Projected schema reduction").
    core_est = (offered_names - seed) | loads
    gated_topk = set()
    if proposed:
        gated_topk = (
            set(proposed.get("domain_topk", ()))
            if proposed.get("browser_gate")
            else topk_set
        )
    projected_offered = core_est | gated_topk
    reduction = 0.0
    if offered_names:
        reduction = round(100.0 * (len(offered_names) - len(projected_offered)) / len(offered_names), 1)

    return {
        "schema_version": SCHEMA_VERSION,
        "router_version": ROUTER_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "turn_id": turn["turn_id"],
        "round_id": round_id,
        "query_hash": turn["query_hash"],
        "catalog_hash": catalog_hash(),
        "catalog_size": catalog_size,
        "mode": mode,
        "restricted": bool(restricted),
        "router_inert": bool(router_inert),
        "browser_gate": bool(proposed.get("browser_gate")) if proposed else browser_gate(turn.get("prompt_text", "")),
        "mode_signature": proposed.get("mode_signature") if proposed else None,
        "required_tools": list(required_tools or ()),
        "required_in_baseline": [bool(t in offered_names) for t in (required_tools or ())],
        "intent_seed_count": len(seed),
        "explicit_load_count": len(loads),
        "offered_count": len(offered_names),
        "end_turn": bool(end_turn),
        "chosen": chosen_records,
        "method": proposed.get("method") if proposed else None,
        "fallback_reason": proposed.get("fallback_reason") if proposed else None,
        "candidate_count": len(topk),
        "gated_candidate_count": len(gated_topk),
        "raw_topk": proposed.get("raw_topk", []) if proposed else [],
        "read_topk": proposed.get("read_topk", []) if proposed else [],
        "write_topk": proposed.get("write_topk", []) if proposed else [],
        "domain_topk": proposed.get("domain_topk", []) if proposed else [],
        "chosen_any_in_topk": any(c["in_topk"] for c in chosen_records),
        "chosen_any_in_domain_topk": any(c["in_domain_topk"] for c in chosen_records),
        "chosen_any_in_partition_topk": any(c["in_partition_topk"] for c in chosen_records),
        "chosen_any_in_counterfactual_topk": any(
            c["in_counterfactual_topk"] for c in chosen_records
        ),
        "projected_core_count": len(core_est),
        "projected_offered_count": len(projected_offered),
        "projected_reduction_pct": reduction,
        "projected_discovery_reduction_pct": reduction,
        # Names the model chose that are not in the catalog (hallucinated/unknown).
        # This is NOT the baseline dispatch-refusal count the contract asks for;
        # that needs instrumentation at the dispatch site and is deferred.
        "chosen_unknown_names": [n for n in chosen if n not in known],
        "latency_ms": proposed.get("latency_ms") if proposed else None,
        "cache_hit": bool(proposed.get("cache_hit")) if proposed else False,
        "top1_score": proposed.get("top1_score") if proposed else None,
        "error": error,
    }


def write_record(record: dict[str, Any]) -> None:
    """Append one record to ``data/tool-router-shadow/YYYY-MM-DD.jsonl``."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = _LOG_DIR / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    except Exception as exc:  # logging must never break a turn
        logger.debug("tool-router shadow write failed: %s", exc)


def reset_for_tests() -> None:
    global _index_hash, _cards_cache, _bm25_cache, _turn_seq, _index_task, _index_next_attempt
    _index_hash = None
    _cards_cache = None
    _bm25_cache = None
    _index_task = None
    _index_next_attempt = 0.0
    _cache.clear()
    _turn_seq = itertools.count(1)
