#!/usr/bin/env python3
"""Tiny Qdrant proof of concept for semantic tool-candidate retrieval.

This is intentionally standalone and read-only with respect to Delilah's
runtime. It indexes the existing capability registry into a separate Qdrant
collection, searches it for sample/user queries, and writes CSV + TXT output.

Examples:
    python scripts/tool_router_poc.py
    python scripts/tool_router_poc.py --query "How much did I spend on restaurants?"
    python scripts/tool_router_poc.py --output-dir /tmp/tool-router-poc
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

# Make `python scripts/tool_router_poc.py` work from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.discovery import CAPABILITY_REGISTRY
from src.services.qdrant_client import (
    _get_qdrant_url,
    ensure_collection,
    get_embedding,
    upsert_points,
)


COLLECTION = "delilah_tool_router_poc"
DEFAULT_OUTPUT_DIR = Path("data/tool-router-poc")
SAMPLE_QUERIES = [
    "How much did I spend on restaurants last month?",
    "Break down my spending by category for the last 90 days.",
    "How much did I spend at coffee shops this year?",
    "Show me my largest discretionary expenses.",
    "What are my total monthly expenses?",
    "Where am I overspending compared with my budget?",
    "Am I on track with my grocery budget?",
    "Check the status of all my category budgets.",
    "Which spending categories increased this month?",
    "Show my spending trend over the past six months.",
    "How much did I spend on transportation last week?",
    "What is my checking balance right now?",
    "Give me an overview of all my accounts.",
    "What is my total liquid cash?",
    "Show me my current financial position.",
    "How much cash do I have available after bills?",
    "Pull the latest balances from my linked accounts.",
    "Sync my bank and credit-card data.",
    "What is my current net worth?",
    "How has my net worth changed over the last year?",
    "Show my net-worth history by month.",
    "List my most recent transactions.",
    "Find transactions from Whole Foods.",
    "Search for charges containing Uber.",
    "Show all transactions from last Friday.",
    "Which transactions are still unlocked?",
    "Show me transactions I previously locked.",
    "Find duplicate or suspicious transactions.",
    "Correct the merchant name on this transaction.",
    "Fix these five incorrectly categorized transactions.",
    "Lock the reviewed transactions.",
    "Add a cash transaction for 45 dollars at the farmers market.",
    "Delete the duplicate transaction from yesterday.",
    "Show my financial dashboard.",
    "What bills and cash movements are coming up?",
    "Give me a cash-flow summary for next month.",
    "Show all my planned transactions.",
    "Add a planned rent payment for the first of next month.",
    "Change the amount of my planned insurance payment.",
    "Cancel the planned payment for the old gym membership.",
    "When should I expect my next paycheck?",
    "Show my recent income.",
    "List all expected income sources.",
    "Add my freelance payment as expected income.",
    "Update the amount of my expected bonus.",
    "Cancel the expected income entry for that job.",
    "List my active subscriptions.",
    "Which subscriptions renewed recently?",
    "Find subscriptions that look unused.",
    "Show my savings buckets.",
    "How much is in each sinking fund?",
    "Give me an overview of my sinking funds.",
    "Move 200 dollars into my emergency-fund bucket.",
    "Delete the vacation savings bucket.",
    "How much debt do I currently have?",
    "Show my debt overview with interest rates.",
    "Compare debt snowball and debt avalanche for me.",
    "Simulate paying an extra 300 dollars per month toward debt.",
    "What will my debt payoff timeline look like?",
    "Show my investment holdings.",
    "What is my current portfolio allocation?",
    "How much is each investment worth?",
    "Calculate portfolio allocation drift.",
    "What rebalancing would bring me back to target?",
    "Which accounts have the highest fees?",
    "Run a financial health checkup.",
    "Show my credit utilization by card.",
    "What happens to my credit utilization if I pay down 1000 dollars?",
    "Analyze whether I have too much idle cash.",
    "How much interest am I losing by keeping cash in checking?",
    "Search the web for the best current price on this laptop.",
    "Fetch the product page for this item.",
    "Find more sources about this insurance policy.",
    "Look up the return policy for this retailer.",
    "What do we know about this merchant?",
    "Save this merchant for future reference.",
    "Find merchants I have not categorized yet.",
    "Search my email for the latest electric bill.",
    "Find receipts from Amazon in my inbox.",
    "Read the email thread about my refund.",
    "Open the message from my landlord.",
    "Find the confirmation email for my flight.",
    "Analyze this price drop and draft a refund request.",
    "Search my stored financial memories for my insurance deductible.",
    "What do I know about my car loan?",
    "Find world-model information about my employer.",
    "Show the dossier for this account.",
    "Record that my emergency fund target is 10000 dollars.",
    "Remove the outdated claim about my rent.",
    "Explain why this financial claim is stored.",
    "Tag these transactions as part of my home renovation.",
    "Find transactions related to my travel plans.",
    "Log that I started a new gym routine.",
    "Show my lifestyle context from last month.",
    "Index my latest financial snapshot for semantic search.",
    "Search my vector memory for information about deductibles.",
    "Run a Python calculation comparing these two budgets.",
    "Simulate my cash flow if rent rises by 10 percent.",
    "Calculate the impact of paying off this credit card.",
    "Generate a financial digest for this month.",
    "Schedule a reminder tomorrow at 9 AM to review my budget.",
    "Show my scheduled reminders.",
    "Move my reminder to Friday afternoon.",
    "Delete the reminder about calling the bank.",
    "List my active monitoring rules.",
    "Create a rule when my checking balance falls below 1000 dollars.",
    "Delete the old grocery spending monitor.",
    "Show my recent financial alerts.",
    "Acknowledge the alert about unusual spending.",
    "Run the monitoring pass now.",
    "Check the status of my online order.",
    "Find the tracking information for my package.",
    "Watch this product for a price drop.",
    "Look up my order status on the merchant site.",
]


def tool_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for domain, capabilities in CAPABILITY_REGISTRY.items():
        for capability, tools in capabilities.items():
            for name in tools:
                card_text = (
                    f"Tool {name.replace('_', ' ')}. "
                    f"Domain: {domain}. Capability: {capability}. "
                    f"Use when the user asks about {capability.lower()} "
                    f"within {domain.lower()}."
                )
                cards.append(
                    {
                        "name": name,
                        "domain": domain,
                        "capability": capability,
                        "text": card_text,
                    }
                )
    # The registry intentionally contains a few tools in more than one place.
    # Keep one result per tool for a clean router output.
    unique: dict[str, dict[str, Any]] = {}
    for card in cards:
        unique.setdefault(card["name"], card)
    return list(unique.values())


async def index_cards(cards: list[dict[str, Any]]) -> None:
    await ensure_collection(COLLECTION)
    vectors = await asyncio.gather(*(get_embedding(card["text"]) for card in cards))
    points = []
    for card, vector in zip(cards, vectors):
        points.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tool-poc:{card['name']}")),
                "vector": vector,
                "payload": {"domain": "tool_catalog", "domain_name": card["domain"], **{k: v for k, v in card.items() if k != "domain"}},
            }
        )
    await upsert_points(points, collection_name=COLLECTION)


async def search(query: str, limit: int) -> list[dict[str, Any]]:
    base_url = _get_qdrant_url()
    vector = await get_embedding(query)
    payload = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "filter": {"must": [{"key": "domain", "match": {"value": "tool_catalog"}}]},
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{base_url}/collections/{COLLECTION}/points/search", json=payload
        )
        response.raise_for_status()
    return response.json().get("result", [])


async def main(args: argparse.Namespace) -> None:
    queries = args.query or SAMPLE_QUERIES
    cards = tool_cards()
    await index_cards(cards)

    rows: list[dict[str, Any]] = []
    for query in queries:
        results = await search(query, args.limit)
        for rank, result in enumerate(results, start=1):
            item = result.get("payload") or {}
            rows.append(
                {
                    "query": query,
                    "rank": rank,
                    "score": result.get("score"),
                    "tool": item.get("name"),
                    "domain": item.get("domain_name", item.get("domain")),
                    "capability": item.get("capability"),
                    "description": item.get("text"),
                }
            )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "tool_router_results.csv"
    txt_path = args.output_dir / "tool_router_results.txt"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with txt_path.open("w", encoding="utf-8") as handle:
        handle.write(f"Collection: {COLLECTION}\nIndexed tools: {len(cards)}\n\n")
        for query in queries:
            handle.write(f"QUERY: {query}\n")
            for row in [r for r in rows if r["query"] == query]:
                handle.write(
                    f"  {row['rank']}. {row['tool']} "
                    f"(score={row['score']:.4f}, {row['domain']} / {row['capability']})\n"
                )
            handle.write("\n")

    print(json.dumps({"csv": str(csv_path), "txt": str(txt_path), "tools": len(cards), "queries": len(queries)}, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Query to rank; repeat for multiple queries")
    parser.add_argument("--limit", type=int, default=8, help="Candidates per query (default: 8)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
