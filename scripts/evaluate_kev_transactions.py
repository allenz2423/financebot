#!/usr/bin/env python3
"""Evaluate KEV transaction predictions against human-confirmed corrections.

Examples:
  python scripts/evaluate_kev_transactions.py --db data/finances.db --export-training /tmp/kev-train.jsonl
  python scripts/evaluate_kev_transactions.py --db data/finances.db --user 342... --run-model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.kev_transactions import (
    KevTransactionPolicy,
    classification_metrics,
    classify_transaction_batch,
    ensure_kev_transaction_schema,
    export_training_jsonl,
    load_heldout_labels,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/finances.db"))
    parser.add_argument("--user")
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument("--export-training")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_kev_transaction_schema(conn)
    if args.export_training:
        count = export_training_jsonl(conn, args.export_training, user_id=args.user, holdout="train")
        print(json.dumps({"exported_training_examples": count, "path": args.export_training}))

    labels = load_heldout_labels(conn, user_id=args.user, holdout="test")
    print(json.dumps({"heldout_labels": len(labels), "run_model": args.run_model}))
    if not args.run_model or not labels:
        return 0

    placeholders = ",".join("?" for _ in labels)
    rows = conn.execute(
        f"""SELECT id, user_id, transaction_id, merchant, clean_merchant, amount,
                  date, account_used FROM transactions WHERE id IN ({placeholders})""",
        [int(label["transaction_row_id"]) for label in labels],
    ).fetchall()
    items = [
        {"id": row[0], "user_id": row[1], "transaction_id": row[2], "merchant": row[3],
         "clean_merchant": row[4], "amount": row[5], "date": row[6], "account_used": row[7]}
        for row in rows
    ]
    result = asyncio.run(classify_transaction_batch(items, conn=conn, user_id=args.user, policy=KevTransactionPolicy(), persist=True))
    label_map = {entry["transaction_row_id"]: entry["category"] for entry in labels}
    print(json.dumps({"batch_id": result.batch_id, **classification_metrics(result.predictions, label_map)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
