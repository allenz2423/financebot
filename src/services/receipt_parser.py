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
        lowered = cleaned_line.lower()

        # Ignore obvious non-item or header/footer lines
        if any(lowered.startswith(kw) or lowered == kw for kw in skip_keywords):
            continue

        match = None
        for pat in (p1, p2, p3, p4):
            m = pat.match(cleaned_line)
            if m:
                match = m
                break

        if match:
            groups = match.groupdict()
            name = groups.get("name", "").strip()
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
