"""Demo action functions for the concierge executor (B0 stub).

``action_fn`` contract (see state.py): receives ``resolved`` — dict of
vault_ref -> decrypted bytes — and the draft's secret-free ``payload``;
must return a summary dict. Resolved secrets exist ONLY inside the call
frame: the receipt returned below contains masks (last4), never the PAN.

This is explicitly a DEMO. A real ``order_pizza`` (B2) would drive the
pizza site through the egress sandbox; this one fabricates a receipt.
"""

from __future__ import annotations

import json
from typing import Any, Dict


def order_pizza(resolved: Dict[str, bytes], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a resolved payment record + draft payload into a masked receipt.

    Raises ValueError for missing order fields so the executor can show the
    escalate path; never returns the PAN/expiry.
    """
    if not resolved:
        raise ValueError("order_pizza requires at least one vault_ref")
    if not isinstance(payload, dict):
        raise ValueError("order_pizza payload must be a dict")
    required = ("merchant", "items", "delivery_address")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        raise ValueError("order_pizza missing payload fields: " + ", ".join(missing))

    ref, raw = next(iter(resolved.items()))
    fields = json.loads(raw.decode("utf-8"))
    pan_key = next((k for k in fields if k.startswith("card_pan|")), None)
    digits = "".join(ch for ch in str(fields.get(pan_key) or "") if ch.isdigit())
    last4 = digits[-4:] if digits else "????"

    items = payload["items"]
    if isinstance(items, list):
        items = ", ".join(str(i) for i in items)
    return {
        "order_id": "FAKE-" + ref.split("_")[1][:6].upper(),
        "status": "confirmed (demo)",
        "merchant": payload["merchant"],
        "items": items,
        "delivery_address": payload["delivery_address"],
        "amount_usd": 19.99,
        "card_last4": last4,
    }


__all__ = ["order_pizza"]