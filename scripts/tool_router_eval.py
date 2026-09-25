#!/usr/bin/env python3
"""Method-agnostic semantic tool-router evaluation (v3).

v2 fixed the catalog and labels but still mixed methods: the headline compared
embedding / lexical / hybrid, yet safety and the no-tool floor were computed on
the *embedding* ranking only. v3 runs every metric — retrieval, safety,
calibration, detailed rankings — separately per method, and also after the
proposed read/mutate partition and the browser domain gate.

Also adds the reviewer's adversarial negatives, per-tool ranks/flags in the CSV,
and hybrid top-8 detail for browser cases, safety violations, and failures.

Run inside the container:

    docker exec -w /app -e PYTHONPATH=/app delilah_bot \
        python3 /app/scripts/tool_router_eval.py --recreate
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.llm import BOT_TOOLS_SCHEMA, MUTATION_TOOLS  # noqa: E402
from src.services.qdrant_client import (  # noqa: E402
    _get_embedding_backend,
    _get_embedding_vector_size,
    _get_qdrant_url,
    ensure_collection,
    get_embedding,
    upsert_points,
)

COLLECTION = "delilah_tool_router_eval"
DEFAULT_OUTPUT_DIR = Path("data/tool-router-poc")
METHODS = ("embedding", "lexical", "hybrid")

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------
CONDITIONAL_TOOLS = {
    "request_concierge_action", "concierge_browser_step", "request_credential_capture",
    "request_user_form", "run_python_sandbox", "run_shell",
}
ACTION_EXTRA = {
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
}
STRICT_MUTATE = set(MUTATION_TOOLS) | ACTION_EXTRA


def classify(name: str) -> str:
    if name in CONDITIONAL_TOOLS:
        return "conditional"
    if name in STRICT_MUTATE:
        return "mutate"
    return "read"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
BROWSER_CUE_RE = re.compile(
    r"("
    r"\blog(?:ged|s|ging)?\s*(?:me\s*)?(?:in|into|out)\b"
    r"|\bsign\s*(?:ed|ing)?\s*in\b"
    r"|\blog ?in\b|\bmy login\b"
    r"|\bcheck\s*out\b|\bcheckout\b|\bmy cart\b|\bcart\b"
    r"|\border status\b|\bonline order\b|\bmy orders?\b"
    r"|\btracking\b|\btrack\s+(?:my|the|this|a)\s+(?:package|order|parcel|shipment)\b"
    r"|\b(?:package|parcel|shipment)\b|\bship(?:ped|ping)\b|\bdelivery\b"
    r"|\bprice\s*drop\b|\bprice\s*watch\b"
    r"|\bwatch\s+(?:this|the)\s+(?:product|item|price)\b"
    r"|\bon\s+(?:the\s+)?(?:merchant\s+)?site\b"
    r"|\badd to cart\b|\bwhere is my\b"
    r")",
    re.IGNORECASE,
)
BROWSER_DOMAIN = {
    "request_concierge_action", "concierge_browser_step", "concierge_act_status",
    "request_credential_capture", "scrape_rendered_page", "fetch_webpage", "crawl_deeper",
}
# The deterministic gate targets login/order/checkout/price-watch flows only, so
# "gate-intent" is derived from the concierge tools — not generic page fetch.
GATE_DOMAIN = {
    "request_concierge_action", "concierge_browser_step", "concierge_act_status",
    "request_credential_capture",
}


def browser_gate(query: str) -> bool:
    return bool(BROWSER_CUE_RE.search(query or ""))


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------
E: list[dict[str, Any]] = []


def L(q: str, kind: str, primary: str | None, *accept: str, expect_browser: bool | None = None) -> None:
    acc = set(accept) | ({primary} if primary else set())
    if expect_browser is None:
        expect_browser = bool(acc & GATE_DOMAIN)
    E.append({"q": q, "kind": kind, "primary": primary, "accept": acc, "expect_browser": expect_browser})


# --- spending ---
L("How much did I spend on restaurants last month?", "read", "query_spending", "get_spending_breakdown", "compare_period_spending", "get_daily_spending_average")
L("Break down my spending by category for the last 90 days.", "read", "get_spending_breakdown", "query_spending", "get_category_budget_pacing")
L("How much did I spend at coffee shops this year?", "read", "query_spending", "get_spending_breakdown", "compare_period_spending")
L("What did I spend my money on this month?", "read", "query_spending", "get_spending_breakdown", "get_daily_spending_average")
L("Where am I overspending compared with my budget?", "read", "check_budget_status", "get_budget_pacing_and_forecast", "get_category_budget_pacing")
L("Am I on track with my grocery budget?", "read", "check_budget_status", "get_category_budget_pacing", "get_budget_pacing_and_forecast")
L("Show my spending trend over the past six months.", "read", "query_spending", "get_spending_breakdown", "compare_period_spending", "render_financial_chart")
L("What are my total monthly expenses?", "read", "query_spending", "get_spending_breakdown", "get_cash_flow_summary")
L("How does this month compare to last month on spending?", "read", "compare_period_spending", "query_spending", "get_spending_breakdown")
L("What is my average daily spend?", "read", "get_daily_spending_average", "query_spending", "get_spending_breakdown")
# --- balances ---
L("What is my checking balance right now?", "read", "get_current_financial_position", "get_accounts_overview", "get_unified_net_worth")
L("Give me an overview of all my accounts.", "read", "get_accounts_overview", "get_current_financial_position")
L("What is my total liquid cash?", "read", "get_current_financial_position", "get_accounts_overview", "get_safe_to_spend_metrics")
L("Pull the latest balances from my linked accounts.", "write", "sync_plaid_accounting", "pull_live_financial_data")
L("Sync my bank and credit-card data.", "write", "sync_plaid_accounting", "pull_live_financial_data")
L("What is my current net worth?", "read", "get_unified_net_worth", "get_current_financial_position", "get_net_worth_history")
L("Show my net-worth history by month.", "read", "get_net_worth_history", "get_unified_net_worth")
L("How safe am I to spend right now?", "read", "get_safe_to_spend_metrics", "get_current_financial_position")
# --- transactions: read ---
L("List my most recent transactions.", "read", "get_recent_transactions", "search_transactions", "get_transaction_ledger", "get_transaction_detail")
L("Find transactions from Whole Foods.", "read", "search_transactions", "get_recent_transactions", "search_merchants_and_aliases", "get_transaction_ledger")
L("Search for charges containing Uber.", "read", "search_transactions", "get_recent_transactions", "search_merchants_and_aliases")
L("Which transactions are still unlocked?", "read", "get_unlocked_transactions")
L("Show me transactions I previously locked.", "read", "get_locked_transactions")
L("Find duplicate or suspicious transactions.", "read", "get_duplicate_transactions", "detect_unusual_bill_increases")
L("Get the line items for this transaction.", "read", "get_transaction_items", "get_transaction_detail")
L("Show the ledger of all my transactions.", "read", "get_transaction_ledger", "get_recent_transactions", "search_transactions")
L("What are all my transactions in one place?", "read", "get_transaction_ledger", "get_recent_transactions")
# --- transactions: write ---
L("Add a cash transaction for 45 dollars at the farmers market.", "write", "add_transaction")
L("Delete the duplicate transaction from yesterday.", "write", "delete_transaction")
L("Correct the merchant name on this transaction.", "write", "correct_transaction", "batch_correct_transactions")
L("Fix these five incorrectly categorized transactions.", "write", "batch_correct_transactions", "correct_transaction")
L("Lock the reviewed transactions.", "write", "lock_transaction", "batch_lock_transactions")
L("Undo the correction I made to that transaction.", "write", "clear_transaction_correction")
# --- gmail ---
L("Search my email for the latest electric bill.", "read", "search_gmail", "read_gmail_message", "read_gmail_thread")
L("Find receipts from Amazon in my inbox.", "read", "search_gmail", "parse_and_attach_receipt", "parse_text_receipt")
L("Read the email thread about my refund.", "read", "read_gmail_thread", "read_gmail_message", "search_gmail")
L("Open the message from my landlord.", "read", "read_gmail_message", "read_gmail_thread", "search_gmail")
L("Find the confirmation email for my flight.", "read", "search_gmail", "read_gmail_message", "read_gmail_thread")
L("What does my latest email from the bank say?", "read", "read_gmail_message", "search_gmail", "read_gmail_thread")
# --- concierge / browser ---
L("Check the status of my online order.", "read", "request_concierge_action", "concierge_browser_step", "concierge_act_status")
L("Find the tracking information for my package.", "read", "request_concierge_action", "concierge_browser_step", "concierge_act_status")
L("Look up my order status on the merchant site.", "read", "request_concierge_action", "concierge_browser_step", "concierge_act_status")
L("Watch this product for a price drop.", "read", "request_concierge_action", "concierge_browser_step", "concierge_act_status")
L("Where is my package, has it shipped yet?", "read", "request_concierge_action", "concierge_act_status")
L("Log into amazon and checkout my cart.", "write", "request_concierge_action", "concierge_browser_step")
L("Am I logged in to paypal?", "read", "concierge_browser_step", "concierge_act_status")
L("Log me into my paypal account.", "write", "request_concierge_action", "concierge_browser_step", "request_credential_capture")
L("Set up my login for this site.", "write", "request_credential_capture", "request_concierge_action")
L("Scrape the pricing table off this page.", "read", "scrape_rendered_page", "fetch_webpage", expect_browser=False)
L("Fetch the product page for this item.", "read", "fetch_webpage", "scrape_rendered_page", expect_browser=False)
L("Crawl the site for more detail.", "read", "crawl_deeper", "fetch_webpage", expect_browser=False)
# --- reminders ---
L("Schedule a reminder tomorrow at 9 AM to review my budget.", "write", "schedule_reminder")
L("Show my scheduled reminders.", "read", "get_scheduled_reminders")
L("Delete the reminder about calling the bank.", "write", "delete_scheduled_reminder")
L("Move my reminder to Friday afternoon.", "write", "update_scheduled_reminder")
# --- monitor ---
L("List my active monitoring rules.", "read", "monitor_list_rules")
L("Create a rule when my checking balance falls below 1000 dollars.", "write", "monitor_create_natural_rule", "monitor_add_rule")
L("Show my recent financial alerts.", "read", "monitor_list_alerts")
L("Run the monitoring pass now.", "write", "monitor_run_pass")
L("Acknowledge the unusual-spending alert.", "write", "monitor_ack_alert")
# --- web research ---
L("Search the web for the best current price on this laptop.", "read", "search_web")
L("Look up the return policy for this retailer.", "read", "search_web", "fetch_webpage")
L("What do we know about this merchant?", "read", "get_known_merchant", "search_merchants_and_aliases")
L("Save this merchant for future reference.", "write", "save_known_merchant", "add_merchant_alias")
# --- world model / memory ---
L("Search my stored financial memories for my insurance deductible.", "read", "search_vector_memory", "query_knowledge_base", "search_world_model")
L("What do I know about my car loan?", "read", "search_world_model", "get_world_model_entity", "list_world_model_claims", "search_vector_memory")
L("Record that my emergency fund target is 10000 dollars.", "write", "assert_world_model_claim", "set_savings_goal")
L("Show the dossier for this account.", "read", "get_world_model_dossier", "get_world_model_entity")
L("List every claim you hold about me.", "read", "list_world_model_claims", "search_world_model")
# --- analysis / sandbox ---
L("Run a Python calculation comparing these two budgets.", "write", "run_python_sandbox")
L("Simulate my cash flow if rent rises by 10 percent.", "read", "simulate_cash_flow_scenario", "simulate_what_if_scenario", "simulate_stochastic_cash_flow")
L("Compare debt snowball and debt avalanche for me.", "read", "calculate_debt_snowball_vs_avalanche", "simulate_debt_payoff")
L("Generate a financial digest for this month.", "write", "generate_financial_digest", "generate_weekly_financial_briefing", "render_financial_chart")
L("How long until I pay off my loans?", "read", "calculate_loan_amortization", "simulate_debt_payoff", "get_debt_overview")
# --- adversarial negatives for the browser gate (should NOT fire) ---
L("Am I on track with my budget?", "read", "check_budget_status", "get_budget_pacing_and_forecast", "get_category_budget_pacing", expect_browser=False)
L("Track my spending this month.", "read", "query_spending", "get_spending_breakdown", "compare_period_spending", expect_browser=False)
L("What is the price of this transaction?", "read", "get_transaction_detail", "get_recent_transactions", "search_transactions", expect_browser=False)
L("What merchant is this charge from?", "read", "get_known_merchant", "get_transaction_detail", "search_merchants_and_aliases", expect_browser=False)
L("Am I logged in to my bank app?", "read", "get_current_financial_position", "get_accounts_overview", expect_browser=False)
# --- no-tool turns ---
for _q in [
    "What is 30% off of 84 dollars?", "What's the capital of France?", "Hey, how are you?",
    "Thanks, that's helpful.", "Explain how compound interest works.", "What day is it today?",
    "Tell me a joke.", "Who wrote Hamlet?", "What's the weather like?", "Can you speak Spanish?",
    "Good morning!", "What can you do?", "Are you an AI?", "What is 2 plus 2?",
    "How do I spell 'reconcile'?", "What's your favorite color?", "Nice work, thank you.",
    "Is it going to rain tomorrow?", "Translate 'hello' to French.", "What timezone am I in?",
    "Give me a fun fact about space.", "What's the tallest mountain?", "Are you there?",
    "Let's chat for a bit.", "Who is the president?", "What year did WW2 end?",
    "How many ounces in a cup?", "Sing me a song.", "What's a good book to read?",
    "Define opportunity cost.",
]:
    L(_q, "none", None)
# --- ambiguous terse follow-ups ---
for _q in ["yeah do that", "the second one", "show me", "do it", "and the other one"]:
    L(_q, "ambiguous", None)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------
def tool_cards() -> list[dict[str, Any]]:
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
    return cards


async def recreate_collection() -> None:
    base = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.delete(f"{base}/collections/{COLLECTION}")


async def index_cards(cards: list[dict[str, Any]]) -> None:
    await ensure_collection(COLLECTION)
    vectors: list[list[float]] = []
    for start in range(0, len(cards), 16):
        chunk = cards[start:start + 16]
        vectors.extend(await asyncio.gather(*(get_embedding(c["text"]) for c in chunk)))
    points = [
        {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tool-eval:{c['name']}")), "vector": v,
         "payload": {"domain": "tool_eval", "name": c["name"], "class": c["class"]}}
        for c, v in zip(cards, vectors)
    ]
    await upsert_points(points, collection_name=COLLECTION)


async def embed_all(query: str, cards: list[dict[str, Any]]) -> dict[str, float]:
    base = _get_qdrant_url()
    payload = {
        "vector": await get_embedding(query), "limit": len(cards), "with_payload": True,
        "filter": {"must": [{"key": "domain", "match": {"value": "tool_eval"}}]},
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{base}/collections/{COLLECTION}/points/search", json=payload)
        resp.raise_for_status()
    return {r["payload"]["name"]: r["score"] for r in resp.json().get("result", [])}


def _tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


class BM25:
    def __init__(self, names: list[str], docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.names = names
        self.docs = [_tok(f"{n.replace('_', ' ')} {n.replace('_',' ')} {d}") for n, d in zip(names, docs)]
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


def _ranked(scores: dict[str, float], allow: set[str] | None = None) -> list[str]:
    items = [(n, s) for n, s in scores.items() if allow is None or n in allow]
    return [n for n, _ in sorted(items, key=lambda kv: kv[1], reverse=True)]


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.0f}%" if d else "n/a"


async def main(args: argparse.Namespace) -> None:
    cards = tool_cards()
    names = [c["name"] for c in cards]
    cls = {c["name"]: c["class"] for c in cards}
    if args.recreate:
        await recreate_collection()
    await index_cards(cards)
    print(f"Indexed {len(cards)} tools")
    bm = BM25(names, [c["text"] for c in cards])

    # pass 1: raw scores
    raw: list[tuple[dict[str, Any], dict[str, float], dict[str, float]]] = []
    for item in E:
        raw.append((item, await embed_all(item["q"], cards), bm.scores(item["q"])))

    # global lexical normalisation so the blend and its floor are cross-query comparable
    all_lex = [v for _, _, lex in raw for v in lex.values()]
    lo, hi = (min(all_lex), max(all_lex)) if all_lex else (0.0, 1.0)
    span = (hi - lo) or 1.0

    per_item: list[dict[str, Any]] = []
    csv_rows: list[dict[str, Any]] = []
    for item, emb, lex in raw:
        lexn = {n: (v - lo) / span for n, v in lex.items()}
        hybrid = {n: 0.5 * emb.get(n, 0.0) + 0.5 * lexn.get(n, 0.0) for n in names}
        scores = {"embedding": emb, "lexical": lex, "hybrid": hybrid}
        ranks = {m: _ranked(scores[m]) for m in METHODS}
        allow = ({n for n in names if cls[n] in ("read", "conditional")} if item["kind"] == "read"
                 else {n for n in names if cls[n] in ("mutate", "conditional")} if item["kind"] == "write"
                 else set(names))
        ranks_part = {m: _ranked(scores[m], allow) for m in METHODS}
        p = {**item, "scores": scores, "ranks": ranks, "ranks_part": ranks_part, "cls": cls}
        per_item.append(p)
        for n in names:
            csv_rows.append({
                "query": item["q"], "kind": item["kind"], "tool": n, "class": cls[n],
                "is_primary": int(n == item["primary"]), "is_acceptable": int(n in item["accept"]),
                "score_embedding": round(emb.get(n, 0), 5),
                "score_lexical": round(lex.get(n, 0), 5),
                "score_hybrid": round(hybrid.get(n, 0), 5),
                "rank_embedding": ranks["embedding"].index(n) + 1,
                "rank_lexical": ranks["lexical"].index(n) + 1,
                "rank_hybrid": ranks["hybrid"].index(n) + 1,
            })

    labeled = [p for p in per_item if p["kind"] in ("read", "write")]
    n_read = [p for p in per_item if p["kind"] == "read"]
    none_items = [p for p in per_item if p["kind"] == "none"]
    ambig = [p for p in per_item if p["kind"] == "ambiguous"]
    n = len(labeled)

    def retrieval(m: str, part: bool = False) -> dict[str, float]:
        key = "ranks_part" if part else "ranks"
        def rank(p): return p[key][m]
        top1 = sum(1 for p in labeled if p["primary"] and rank(p) and rank(p)[0] == p["primary"])
        acc = {k: sum(1 for p in labeled if any(t in p["accept"] for t in rank(p)[:k])) for k in (1, 3, 5, 8)}
        rr = 0.0
        for p in labeled:
            if p["primary"] and p["primary"] in rank(p):
                rr += 1.0 / (rank(p).index(p["primary"]) + 1)
        return {"primary_top1": top1, **{f"acc{k}": v for k, v in acc.items()},
                "mrr": rr / max(1, sum(1 for p in labeled if p["primary"]))}

    def safety(m: str, part: bool = False) -> dict[str, float]:
        key = "ranks_part" if part else "ranks"
        def ranking(p): return p[key][m]
        u1 = sum(1 for p in n_read if any(cls[t] == "mutate" for t in ranking(p)[:1]))
        u3 = sum(1 for p in n_read if any(cls[t] == "mutate" for t in ranking(p)[:3]))
        u8 = sum(1 for p in n_read if any(cls[t] == "mutate" for t in ranking(p)[:8]))
        share = statistics.mean([sum(1 for t in ranking(p)[:8] if cls[t] == "mutate") / 8 for p in n_read])
        return {"top1": u1, "top3": u3, "top8": u8, "share": share}

    def calibrate(m: str) -> tuple[float, tuple]:
        pos = [p["scores"][m][p["ranks"][m][0]] for p in labeled if p["ranks"][m]]
        neg = [p["scores"][m][p["ranks"][m][0]] for p in none_items if p["ranks"][m]]
        if not pos or not neg:
            return 0.0, (0.0, 0.0, 0.0, 0.0, 0.0)
        lo_s, hi_s = min(pos + neg), max(pos + neg)
        best = None
        for i in range(0, 101):
            t = lo_s + (hi_s - lo_s) * i / 100.0
            tp = sum(1 for s in pos if s >= t)
            fp = sum(1 for s in neg if s >= t)
            prec = tp / (tp + fp) if (tp + fp) else 1.0
            rec = tp / len(pos)
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            if best is None or f1 > best[4]:
                best = (t, prec, rec, fp / len(neg), f1)
        return best[0], best

    m_ret = {m: retrieval(m) for m in METHODS}
    m_ret_p = {m: retrieval(m, part=True) for m in METHODS}
    m_safe = {m: safety(m) for m in METHODS}
    m_safe_p = {m: safety(m, part=True) for m in METHODS}
    m_cal = {m: calibrate(m) for m in METHODS}

    # gate
    browser_items = [p for p in labeled if p["expect_browser"]]
    non_browser = [p for p in labeled if not p["expect_browser"]]
    gate_recall = sum(browser_gate(p["q"]) for p in browser_items)
    gate_fp = [p for p in non_browser if browser_gate(p["q"])]
    # gate-adjusted: for gate-positive browser items, restrict pool to browser domain
    gate_fixed = sum(
        1 for p in browser_items
        if browser_gate(p["q"]) and p["primary"]
        and _ranked(p["scores"]["hybrid"], GATE_DOMAIN) and _ranked(p["scores"]["hybrid"], GATE_DOMAIN)[0] == p["primary"]
    )

    # ---- report ----
    md: list[str] = []
    md.append("# Tool-router evaluation — v3 (method-agnostic)\n")
    md.append(f"- Catalog: **{len(cards)}** tools (`BOT_TOOLS_SCHEMA`).")
    md.append(f"- Battery: {n} labelled, {len(none_items)} no-tool, {len(ambig)} ambiguous; "
              f"{len(browser_items)} browser-intent, {len(non_browser)} non-browser.")
    md.append("- Every metric is computed separately for **embedding / lexical / hybrid**.\n")
    md.append("## Reproducibility\n")
    md.append("| field | value |")
    md.append("|---|---|")
    md.append(f"| embedding backend | `{_get_embedding_backend()}` |")
    md.append(f"| embedding model | `{os.getenv('EMBEDDING_LOCAL_MODEL', '?')}` |")
    md.append(f"| vector size | {_get_embedding_vector_size()} |")
    md.append(f"| collection | `{COLLECTION}` |")
    md.append(f"| catalog hash | `{hashlib.sha256(chr(10).join(sorted(names)).encode()).hexdigest()[:16]}` |")
    md.append(f"| query-set hash | `{hashlib.sha256(chr(10).join(e['q'] for e in E).encode()).hexdigest()[:16]}` |")
    md.append(f"| run at | {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} |")
    md.append("")
    md.append("## Retrieval (all methods)\n")
    md.append("| method | primary top-1 | acc@1 | acc@3 | acc@5 | acc@8 | MRR |")
    md.append("|---|---|---|---|---|---|---|")
    for m in METHODS:
        r = m_ret[m]
        md.append(f"| {m} | {r['primary_top1']}/{n} = {_pct(r['primary_top1'], n)} | "
                  f"{_pct(r['acc1'], n)} | {_pct(r['acc3'], n)} | {_pct(r['acc5'], n)} | {_pct(r['acc8'], n)} | {r['mrr']:.3f} |")
    md.append("")
    md.append("## Retrieval after read/mutate partition (search only the permitted side)\n")
    md.append("| method | primary top-1 | acc@3 | acc@8 | MRR |")
    md.append("|---|---|---|---|---|")
    for m in METHODS:
        r = m_ret_p[m]
        md.append(f"| {m} | {_pct(r['primary_top1'], n)} | {_pct(r['acc3'], n)} | {_pct(r['acc8'], n)} | {r['mrr']:.3f} |")
    md.append("")
    md.append("## Safety: mutating candidate on read queries (per method)\n")
    md.append("| method | top-1 | top-3 | top-8 | mean shortlist share |")
    md.append("|---|---|---|---|---|")
    for m in METHODS:
        s = m_safe[m]
        md.append(f"| {m} | {s['top1']}/{len(n_read)} | {s['top3']}/{len(n_read)} | {s['top8']}/{len(n_read)} | {s['share']:.2f} |")
    md.append("")
    md.append("**After read/mutate partition** (mutating reads should be 0):\n")
    for m in METHODS:
        s = m_safe_p[m]
        md.append(f"- {m}: top-1 {s['top1']}, top-3 {s['top3']}, top-8 {s['top8']}, share {s['share']:.2f}")
    md.append("")
    md.append("## No-tool floor (per method, in-sample)\n")
    md.append("| method | best-F1 floor | precision | recall | FPR |")
    md.append("|---|---|---|---|---|")
    for m in METHODS:
        t, (t2, prec, rec, fpr, f1) = m_cal[m]
        md.append(f"| {m} | {t:.4f} | {prec:.2f} | {rec:.2f} | {fpr:.2f} |")
    md.append("")
    md.append("## Browser gate\n")
    md.append(f"- recall on {len(browser_items)} browser-intent queries: **{gate_recall}/{len(browser_items)}** "
              f"({_pct(gate_recall, len(browser_items))})")
    md.append(f"- false positives on {len(non_browser)} non-browser queries: **{len(gate_fp)}/{len(non_browser)}**")
    if gate_fp:
        for p in gate_fp:
            md.append(f"    - FP: `{p['q']}`")
    md.append(f"- **gate + browser-domain restriction**: {gate_fixed} of {sum(browser_gate(p['q']) for p in browser_items)} "
              f"gate-positive browser queries route to their primary tool (hybrid).")
    md.append("")
    md.append("## Browser cases — hybrid top-8\n")
    for p in browser_items:
        md.append(f"- `{p['q']}` → {p['ranks']['hybrid'][:8]}")
    md.append("")
    md.append("## Safety violations — hybrid top-1 mutating on read\n")
    viol = [p for p in n_read if cls[p["ranks"]["hybrid"][0]] == "mutate"]
    md.append("- none" if not viol else "")
    for p in viol:
        md.append(f"- `{p['q']}` → `{p['ranks']['hybrid'][0]}` (want {sorted(p['accept'])})")
    md.append("")
    md.append("## Representative hybrid failures (primary not top-1)\n")
    fails = [p for p in labeled if p["primary"] and p["ranks"]["hybrid"][0] != p["primary"]][:25]
    for p in fails:
        md.append(f"- `{p['q']}` → top-1 `{p['ranks']['hybrid'][0]}`, want `{p['primary']}` "
                  f"(primary at #{p['ranks']['hybrid'].index(p['primary']) + 1 if p['primary'] in p['ranks']['hybrid'] else '>8'})")
    md.append("")
    md.append("## Per-query detail (all methods, top-8)\n")
    for p in per_item:
        md.append(f"### {p['q']}  _({p['kind']})_")
        for m in METHODS:
            md.append(f"**{m}**: " + ", ".join(
                f"{t}{'★' if t == p['primary'] else '✓' if t in p['accept'] else ''}"
                f"{'⚠' if p['kind'] == 'read' and cls[t] == 'mutate' else ''}"
                for t in p["ranks"][m][:8]))
        md.append("")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "tool_router_eval_v3.md").write_text("\n".join(md), encoding="utf-8")
    with (args.output_dir / "tool_router_eval_v3.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(csv_rows[0]))
        w.writeheader()
        w.writerows(csv_rows)

    print(json.dumps({
        "tools": len(cards),
        "retrieval": {m: {"primary_top1": m_ret[m]["primary_top1"], "acc8": m_ret[m]["acc8"], "mrr": round(m_ret[m]["mrr"], 3)} for m in METHODS},
        "safety": {m: {"top1": m_safe[m]["top1"], "top3": m_safe[m]["top3"], "top8": m_safe[m]["top8"]} for m in METHODS},
        "safety_partitioned": {m: {"top1": m_safe_p[m]["top1"], "top8": m_safe_p[m]["top8"]} for m in METHODS},
        "floors": {m: round(m_cal[m][0], 4) for m in METHODS},
        "gate": {"recall": f"{gate_recall}/{len(browser_items)}", "fp": f"{len(gate_fp)}/{len(non_browser)}",
                 "gate_fixed": f"{gate_fixed}/{sum(browser_gate(p['q']) for p in browser_items)}"},
    }, indent=2))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--recreate", action="store_true")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))