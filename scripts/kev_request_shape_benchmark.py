#!/usr/bin/env python3
"""Measure how KEV latency scales with question and choice count.

This intentionally bypasses Delilah's transaction service so request-shape
costs are separated from database, merchant, and application behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.transaction_taxonomy import CATEGORY_DESCRIPTIONS, TRANSACTION_CATEGORIES


def request_shape(name: str, state: dict[str, object]) -> dict[str, object]:
    if name == "A_binary":
        return {"is_food": {"type": "noul", "instructions": "Is this transaction primarily for food or groceries?"}}
    if name == "B_broad_8":
        choices = {
            "income": "Income or inflow",
            "housing": "Housing or shelter",
            "food": "Food or groceries",
            "transport": "Transportation",
            "health": "Healthcare or medical",
            "shopping": "Shopping or merchandise",
            "services": "Bills, subscriptions, or services",
            "transfer": "Transfer or other financial movement",
        }
        return {"category": {"type": "choice", "instructions": "Which broad category best fits this transaction?", "criteria": choices}}
    if name == "C_detailed_30":
        choices = {path: CATEGORY_DESCRIPTIONS[path] for path in TRANSACTION_CATEGORIES[:30]}
        return {"category": {"type": "choice", "instructions": "Which detailed category best fits this transaction?", "criteria": choices}}
    if name == "D_six_binary":
        return {
            "recurring": {"type": "noul", "instructions": "Is this transaction likely recurring?"},
            "subscription": {"type": "noul", "instructions": "Is this transaction likely a subscription?"},
            "transfer": {"type": "noul", "instructions": "Is this a transfer rather than ordinary spending?"},
            "enrichment": {"type": "noul", "instructions": "Would external merchant enrichment help?"},
            "review": {"type": "noul", "instructions": "Should this be reviewed by a human?"},
            "food": {"type": "noul", "instructions": "Is this primarily food or groceries?"},
        }
    if name == "E_full_taxonomy":
        return {
            "category": {
                "type": "choice",
                "instructions": "Choose the single most specific hierarchical category for this transaction.",
                "criteria": CATEGORY_DESCRIPTIONS,
            },
            **request_shape("D_six_binary", state),
        }
    raise ValueError(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8009/v1/systemone")
    parser.add_argument("--model", default="kev-latest")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    state = {
        "merchant": "DISCORD 4156454065",
        "normalized_merchant": "DISCORD",
        "amount": 12.99,
        "date": "2026-09-22",
        "account": "checking",
    }
    output: list[dict[str, object]] = []
    with httpx.Client(timeout=args.timeout) as client:
        for name in ("A_binary", "B_broad_8", "C_detailed_30", "D_six_binary", "E_full_taxonomy"):
            questions = request_shape(name, state)
            payload = {"state": state, "model": args.model, "questions": questions}
            started = time.perf_counter()
            row: dict[str, object] = {
                "shape": name,
                "question_count": len(questions),
                "choice_count": sum(len(q.get("criteria", {})) for q in questions.values() if isinstance(q, dict)),
            }
            try:
                response = client.post(args.url, json=payload)
                response.raise_for_status()
                data = response.json()
                row.update({
                    "status": "ok",
                    "http_status": response.status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "server_latency_ms": data.get("latency_ms"),
                    "input_tokens": (data.get("usage") or {}).get("input_tokens"),
                    "output_tokens": (data.get("usage") or {}).get("output_tokens"),
                    "answer_count": len(data.get("answers") or {}),
                })
            except Exception as exc:
                row.update({
                    "status": "error",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            print(json.dumps(row), flush=True)
            output.append(row)
    print(json.dumps({"results": output}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
