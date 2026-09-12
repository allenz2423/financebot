"""
Receipt parsing and reconciliation service for Delilah.
Extracts itemized line items from receipts (plain text / HTML from Gmail)
and links them to corresponding ledger transactions.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


def parse_receipt_text(text: str) -> List[Dict[str, Any]]:
    """
    Deterministically extract line items (name, quantity, price) from receipt text.
    Handles Amazon, Apple, Uber/Lyft, grocery, and general itemized receipt layouts.
    """
    if not text:
        return []

    items: List[Dict[str, Any]] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Pattern 1: "1x Item Name $12.34" or "2 x Item Name ... $10.00"
    p1 = re.compile(
        r'^(?:(?P<qty>\d+)\s*[xX]\s+)?(?P<name>[A-Za-z0-9\s\-_.,\'()&/]+?)\s+[\$£€]?(?P<price>\d+\.\d{2})$'
    )

    # Pattern 2: "Item Name ... Qty: 1 ... $12.34"
    p2 = re.compile(
        r'^(?P<name>[A-Za-z0-9\s\-_.,\'()&/]+?)\s+(?:Qty|Quantity):\s*(?P<qty>\d+)\s+.*?[\$£€]?(?P<price>\d+\.\d{2})'
    )

    # Pattern 3: Amazon "Ordered: Item Name" followed by or containing "$12.34"
    p3 = re.compile(
        r'^(?:Item|Ordered|Product):\s*(?P<name>[A-Za-z0-9\s\-_.,\'()&/]+?)\s*(?:[-–|]\s*)?[\$£€]?(?P<price>\d+\.\d{2})'
    )

    # Pattern 4: Table-like "Description       $12.34"
    p4 = re.compile(
        r'^(?P<name>[A-Za-z0-9\s\-_.,\'()&/]{3,60})\s{2,}[\$£€]?(?P<price>\d+\.\d{2})$'
    )

    # Pattern 5: Separator-style "Item Name - $12.34" or "Item Name : $12.34"
    p5 = re.compile(
        r'^(?:(?P<qty>\d+)\s*[xX]\s+)?(?P<name>[A-Za-z0-9\s\-_.,\'()&/]+?)\s*[-–—:]\s*[\$£€]?(?P<price>\d+\.\d{2})$'
    )

    # Skip lines that are summary lines rather than individual items
    skip_keywords = {
        "subtotal", "total", "tax", "sales tax", "tip", "delivery fee",
        "service fee", "balance due", "amount charged", "order total",
        "visa", "mastercard", "amex", "payment method", "discount",
        "promotion", "shipping", "estimated tax", "grand total", "net amount"
    }

    seen_names = set()

    for line in lines:
        cleaned_line = line.strip()
        # Strip leading bullets, dashes, or numbered lists (e.g. "1. ", "• ", "- ", "* ")
        cleaned_line = re.sub(r'^(?:[•\-*]|\d+[\.)])\s*', '', cleaned_line).strip()
        lowered = cleaned_line.lower()

        # Ignore obvious non-item or header/footer lines
        if any(lowered.startswith(kw) or lowered == kw for kw in skip_keywords):
            continue

        match = None
        for pat in (p1, p2, p3, p4, p5):
            m = pat.match(cleaned_line)
            if m:
                match = m
                break

        if match:
            groups = match.groupdict()
            name = re.sub(r'[\s\-–—:.]+$', '', groups.get("name", "")).strip()
            # Avoid single digit or junk merchant header captures
            if not name or len(name) < 2 or name.lower() in skip_keywords:
                continue

            try:
                price = float(groups.get("price", 0.0))
            except ValueError:
                continue

            qty_str = groups.get("qty")
            qty = float(qty_str) if qty_str else 1.0

            # Deduplicate exact line repeats
            item_key = (name.lower(), price)
            if item_key in seen_names:
                continue
            seen_names.add(item_key)

            items.append({
                "item_name": name,
                "quantity": qty,
                "unit_price": round(price / qty, 2) if qty > 0 else price,
                "total_price": price,
            })

    return items


def extract_receipt_total(text: str) -> Optional[float]:
    """Extract total amount from receipt text if present."""
    if not text:
        return None
    patterns = [
        re.compile(r'(?:grand\s+total|order\s+total|amount\s+charged|amount\s+paid|total\s+due|balance\s+due|total)\s*[:=-]?\s*[\$£€]?\s*(\d+\.\d{2})', re.IGNORECASE),
        re.compile(r'[\$£€]\s*(\d+\.\d{2})\s*(?:total|paid|charged)', re.IGNORECASE)
    ]
    for line in reversed(text.splitlines()):
        cleaned = line.strip()
        for pat in patterns:
            m = pat.search(cleaned)
            if m:
                try:
                    val = float(m.group(1))
                    if val > 0:
                        return val
                except ValueError:
                    continue
    return None


def match_receipt_to_transaction(
    text: str,
    items: List[Dict[str, Any]],
    *,
    user_id: str,
    days: int = 45,
) -> List[Dict[str, Any]]:
    """
    Search recent transactions in user's ledger to find candidate transactions
    matching the receipt by total amount, item sum, merchant name, or date.
    Returns ranked candidates with confidence scores.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("match_receipt_to_transaction: forced isolation violation — user_id is required")

    import src.db.queries as q

    declared_total = extract_receipt_total(text)
    items_sum = round(sum(it["total_price"] for it in items), 2) if items else None

    q.c.execute(
        """SELECT id, date, merchant, clean_merchant, amount, category
        FROM transactions
        WHERE user_id = ? AND amount > 0
          AND merchant NOT LIKE '%System Balance Sync%'
        ORDER BY datetime(date) DESC, id DESC
        LIMIT 100""",
        (user_id,)
    )
    rows = q.c.fetchall()

    text_lower = text.lower()
    candidates = []

    for r in rows:
        tx_id = r[0]
        tx_date = str(r[1] or "")[:10]
        merch = str(r[2] or "")
        clean_m = str(r[3] or "")
        amt = float(r[4] or 0.0)
        cat = str(r[5] or "")

        score = 0
        reasons = []

        # 1. Amount matching
        if declared_total is not None and abs(amt - declared_total) < 0.01:
            score += 55
            reasons.append(f"Exact total match (${declared_total:,.2f})")
        elif items_sum is not None and abs(amt - items_sum) < 0.01:
            score += 50
            reasons.append(f"Exact item sum match (${items_sum:,.2f})")
        elif declared_total is not None and abs(amt - declared_total) <= 0.05:
            score += 40
            reasons.append("Close total match (+/- 5 cents)")
        elif items_sum is not None and abs(amt - items_sum) <= 0.05:
            score += 35
            reasons.append("Close item sum match (+/- 5 cents)")
        elif declared_total is not None and declared_total > 0 and abs(amt - declared_total) / declared_total < 0.05:
            score += 20
            reasons.append("Within 5% of total")

        # 2. Merchant matching
        merch_tokens = [t.lower() for t in re.split(r'[^a-zA-Z0-9]+', clean_m or merch) if len(t) > 2]
        matched_tokens = [t for t in merch_tokens if t in text_lower]
        if matched_tokens:
            score += 25
            reasons.append(f"Merchant match: '{', '.join(matched_tokens)}'")

        # 3. Date proximity/mention
        if tx_date and (tx_date in text or tx_date[5:] in text):
            score += 15
            reasons.append(f"Date match ({tx_date})")

        if score >= 20:
            candidates.append({
                "score": score,
                "reasons": reasons,
                "transaction": {
                    "id": tx_id,
                    "date": tx_date,
                    "merchant": clean_m or merch,
                    "amount": amt,
                    "category": cat,
                }
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def parse_and_attach_receipt(
    text: str,
    *,
    user_id: str,
    transaction_id: Optional[int] = None,
    source: str = "manual_paste",
) -> Dict[str, Any]:
    """
    Parse receipt text into itemized line items and attach them to a ledger transaction.
    If transaction_id is provided, validates and attaches to that transaction.
    If omitted, attempts to find the best candidate transaction.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("parse_and_attach_receipt: forced isolation violation — user_id is required")

    import src.db.queries as q

    items = parse_receipt_text(text)
    receipt_total = extract_receipt_total(text)
    items_sum = round(sum(it["total_price"] for it in items), 2) if items else 0.0

    if not items:
        return {
            "success": False,
            "error": "No valid itemized line items found in receipt text. Ensure items have names and prices (e.g. '1x Milk $3.50').",
            "items": [],
            "transaction_id": None,
        }

    target_tx = None
    if transaction_id is not None:
        q.c.execute(
            "SELECT id, date, merchant, clean_merchant, amount, category FROM transactions WHERE id = ? AND user_id = ?",
            (transaction_id, user_id)
        )
        row = q.c.fetchone()
        if not row:
            return {
                "success": False,
                "error": f"Transaction #{transaction_id} not found in your ledger.",
                "items": items,
                "transaction_id": transaction_id,
            }
        target_tx = {
            "id": row[0],
            "date": row[1],
            "merchant": row[3] or row[2],
            "amount": float(row[4]),
            "category": row[5],
        }
    else:
        candidates = match_receipt_to_transaction(text, items, user_id=user_id)
        if not candidates:
            return {
                "success": False,
                "error": "Could not automatically match receipt to any transaction in your ledger. Please supply transaction ID with `!receipts parse <tx_id> <receipt_text>`.",
                "items": items,
                "candidates": [],
            }
        top = candidates[0]
        if top["score"] >= 45:
            target_tx = top["transaction"]
            transaction_id = target_tx["id"]
        else:
            return {
                "success": False,
                "error": "Ambiguous match: multiple possible transactions found. Please specify transaction ID explicitly.",
                "items": items,
                "candidates": [c["transaction"] for c in candidates[:5]],
            }

    inserted_items = []
    for it in items:
        res = q.add_transaction_item(
            user_id=user_id,
            transaction_row_id=transaction_id,
            item_name=it["item_name"],
            total_price=it["total_price"],
            quantity=it.get("quantity", 1.0),
            unit_price=it.get("unit_price"),
            source=source,
        )
        inserted_items.append(res)

    return {
        "success": True,
        "transaction_id": transaction_id,
        "transaction": target_tx,
        "items_count": len(inserted_items),
        "items": inserted_items,
        "receipt_total": receipt_total,
        "items_sum": items_sum,
    }



def find_matching_gmail_receipt(
    transaction: Dict[str, Any],
    *,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Search connected Gmail account for an email matching the transaction amount and date.
    Returns the parsed email details and itemized list if a match is found.
    """
    from src.services.gmail import search_gmail, read_gmail_message

    merchant = transaction.get("clean_merchant") or transaction.get("merchant") or ""
    amount = float(transaction.get("amount", 0.0))
    tx_date_str = transaction.get("date", "")[:10]  # YYYY-MM-DD

    if amount <= 0:
        return None

    # Sanitize merchant name for search
    search_merchant = re.sub(r'[^a-zA-Z0-9]', ' ', merchant).split()
    lead_merchant = search_merchant[0] if search_merchant else ""

    # Search queries to try in order of specificity
    queries = [
        f"{lead_merchant} {amount:.2f}",
        f"{amount:.2f} receipt",
        f"{amount:.2f}",
    ]

    target_date = None
    if tx_date_str:
        try:
            target_date = datetime.strptime(tx_date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    for q in queries:
        try:
            res = search_gmail(query=q, max_results=5, user_id=user_id)
            messages = res.get("messages", [])
        except Exception:
            continue

        for msg_summary in messages:
            msg_id = msg_summary.get("id")
            if not msg_id:
                continue

            try:
                full_msg = read_gmail_message(message_id=msg_id, user_id=user_id)
            except Exception:
                continue

            body = full_msg.get("body", "")
            # Verify amount is in the email body or snippet
            amount_str = f"{amount:.2f}"
            if amount_str not in body and amount_str not in full_msg.get("snippet", ""):
                continue

            # Check date proximity if available (within 4 days)
            msg_date_str = full_msg.get("date", "")
            if target_date and msg_date_str:
                try:
                    # e.g. "2026-09-08 14:32:00" or ISO
                    m_date = datetime.fromisoformat(msg_date_str[:10]).date()
                    if abs((m_date - target_date).days) > 4:
                        continue
                except Exception:
                    pass

            # Extract line items
            parsed_items = parse_receipt_text(body)

            return {
                "message_id": msg_id,
                "subject": full_msg.get("subject", ""),
                "sender": full_msg.get("sender", ""),
                "date": full_msg.get("date", ""),
                "items": parsed_items,
                "raw_snippet": full_msg.get("snippet", ""),
            }

    return None
