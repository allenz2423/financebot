"""Headless end-to-end demo: capture a card -> draft -> approve -> order_pizza.

Runs entirely on a throwaway tmp-dir DB (never touches data/). It exercises
the REAL B0 path: one-time capture token, generic capture submission with
retention split (PAN/expiry vaulted, CVV discarded), draft/approve state
machine, and the executor running the demo ``order_pizza`` action_fn. Prints
a receipt and asserts the PAN/CVV appear nowhere in the output or in the DB.

Run from the repo root:
    python scripts/demo_concierge_pizza.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.security.vault import Vault
from src.services.concierge.capture_schema import validate_capture_intent
from src.services.concierge.capture_submit import process_capture_submission
from src.services.concierge.capture_tokens import CaptureTokenStore
from src.services.concierge.demo_actions import order_pizza
from src.services.concierge.state import DraftStore, execute_draft

TENANT = "user:342385739952160769"  # the owner's own tenant id, for a familiar receipt
PAN = "378282246310005"
CVV = "882"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="concierge-demo-"))
    db = tmp / "demo.db"
    vault = Vault(db_path=str(db), master_key="demo-only-master-key")
    tokens = CaptureTokenStore(str(db))
    drafts = DraftStore(str(db))

    # 1. declare the capture intent (one generic engine; CVV forced ephemeral)
    intent = validate_capture_intent({
        "label": "Pizza payment",
        "explanation": "Pay for the demo pizza order.",
        "kind": "payment",
        "consumer_scope": ["pizza.example.com"],
        "fields": [
            {"type": "card_pan", "label": "Card number"},
            {"type": "card_exp", "label": "Expiry"},
            {"type": "card_cvv", "label": "CVV", "required": True, "retention": "ephemeral"},
        ],
    }, tenant=TENANT)

    # 2. issue a one-time token and submit the form (server-side path)
    token = tokens.issue(TENANT, intent)
    sub = process_capture_submission(tokens, vault, token, TENANT, {
        "card_pan|Card number": PAN,
        "card_exp|Expiry": "12/29",
        "card_cvv|CVV": CVV,
    })
    ref = sub["stored"][0]["vault_ref"]
    assert sub["ephemeral_fields"] == ["CVV"], sub

    # 3. draft -> approve -> execute with the demo action_fn
    draft_id = drafts.create(TENANT, "payment", "order_pizza", [ref], {
        "merchant": "Pizza Corner (demo)",
        "items": ["large pepperoni", "garlic knots"],
        "delivery_address": "123 demo st, brooklyn",
    })
    drafts.approve(draft_id, TENANT, TENANT)
    out = execute_draft(drafts, vault, draft_id, TENANT, order_pizza)
    receipt = out["result"]

    # 4. print the masked receipt
    print("\n===== DEMO: order_pizza receipt =====")
    rows = (
        ("order_id", receipt["order_id"]),
        ("status", receipt["status"]),
        ("merchant", receipt["merchant"]),
        ("items", receipt["items"]),
        ("delivery", receipt["delivery_address"]),
        ("amount_usd", str(receipt["amount_usd"])),
        ("card_last4", receipt["card_last4"]),
    )
    for key, value in rows:
        print(f"  {key:<12} {value}")

    # 5. escalation path: a draft missing a required payload field
    bad_id = drafts.create(TENANT, "payment", "order_pizza", [ref], {
        "merchant": "Pizza Corner (demo)",
        "items": ["garlic knots"],
    })
    drafts.approve(bad_id, TENANT, TENANT)
    try:
        execute_draft(drafts, vault, bad_id, TENANT, order_pizza)
    except ValueError as exc:
        escalated = drafts.get(bad_id)
        assert escalated["status"] == "escalated", escalated["status"]
        print("\n===== DEMO: escalation path =====")
        print("  reason: " + escalated["result"]["reason"])

    # 6. confinement assertions: PAN/CVV nowhere in any output or stored row
    surface = " ".join([
        json.dumps(out),
        json.dumps(drafts.get(draft_id)),
        json.dumps(drafts.get(bad_id)),
        json.dumps(vault.list_records(TENANT)),
    ])
    resolved = vault.resolve(TENANT, ref)
    assert PAN not in surface and CVV not in surface
    # resolve() is the executor-only plaintext path, so the PAN legitimately
    # exists here — inside the call frame. The CVV must NOT: it was discarded
    # at submission with no promotion path.
    assert CVV not in resolved.decode()
    assert "card_cvv" not in resolved.decode()
    assert receipt["card_last4"] == PAN[-4:]
    print("\n  PAN/CVV absent from receipt, draft rows, and manifests; CVV never stored.")
    print("  Plaintext exists only inside the executor call frame — confinement holds.")
    print("  (tmp db at " + str(db) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())