"""
Delilah Financial OS - Merchant Normalization & Canonicalization Engine.

Cleans messy transaction statement descriptors, resolves aliases against
the known merchant registry, and canonicalizes ledger merchants.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, Any, List, Optional, Tuple

import discord

from src.core.state import DB_PATH
from src.db.migrations import apply_all


# Known US state abbreviations for trailing location stripping
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"
}


def clean_raw_merchant_descriptor(raw: str) -> str:
    """
    Algorithmic cleaning for raw statement descriptors.
    Strips POS noise, payment aggregator prefixes, store numbers, phone numbers,
    URLs, and trailing city/state suffixes.
    """
    if not raw or not isinstance(raw, str):
        return ""

    text = raw.strip()

    # 1. Strip payment processor and aggregator prefixes
    text = re.sub(
        r"^(?:AplPay\s*|TST\s*\*\s*|SQ\s*\*\s*|SQUARE\s*\*\s*|UEP\s*\*\s*|PAYPAL\s*\*\s*|PAYPAL\s+TO\s+|SP\s*\*\s*|STRIPE\s*\*\s*|VENMO\s*\*\s*|GOOGLE\s*\*\s*)",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # 2. Strip card transaction & POS authorization noise
    text = re.sub(r"^(?:POS\s+DEBIT|CHECKCARD|PURCHASE\s+AUTHORIZED\s+ON\s+\d{2}/\d{2}|RECURRING\s+PAYMENT|ONLINE\s+PAYMENT|DEBIT\s+PURCHASE\s+-?\s*)\s*", "", text, flags=re.IGNORECASE)

    # 3. Strip web URLs and phone numbers
    text = re.sub(r"\b(?:https?://|www\.)\S+\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\.com|\.net|\.org|\.io|\.co)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\d{3}[-.\s]??\d{3}[-.\s]??\d{4}|\(\d{3}\)\s*\d{3}[-.\s]??\d{4}|8\d{2}[-.\s]??\d{3}[-.\s]??\d{4})\b", "", text)

    # 4. Common Amazon descriptor cleaning
    if re.search(r"\b(?:AMZN\s+MKTP|AMZN\s+DIGITAL|AMAZON\s+MKTPLACE|AMAZON\s+PAYMENTS)\b", text, re.IGNORECASE):
        text = "Amazon"
    elif re.search(r"\b(?:UBER\s*\*?\s*EATS)\b", text, re.IGNORECASE):
        text = "Uber Eats"
    elif re.search(r"\b(?:UBER\s*\*?\s*TRIP)\b", text, re.IGNORECASE):
        text = "Uber"
    elif re.search(r"\b(?:LYFT\s*\*?\s*RIDE)\b", text, re.IGNORECASE):
        text = "Lyft"

    # 5. Strip store / terminal / location numbers and trailing location tails
    text = re.sub(r"\b(?:STORE|LOC|LOCATION|UNIT|SUITE|STE)\s*#?\s*\d+.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"#\s*\d+.*$", "", text)
    text = re.sub(r"\b\d{5,}.*$", "", text)  # standalone long reference numbers (e.g. 5754234890)

    # 6. Strip trailing US state / zip suffixes (e.g. "SAN FRANCISCO CA 94103" or "SEATTLE WA")
    # Match trailing city + state abbreviation
    words = text.split()
    if len(words) >= 2:
        last_word = words[-1].upper().rstrip(".,")
        if last_word in _US_STATES:
            words.pop()
            if words and words[-1].isdigit() and len(words[-1]) == 5:
                words.pop()
            text = " ".join(words)
        elif len(words) >= 3 and words[-1].isdigit() and len(words[-1]) == 5:
            if words[-2].upper().rstrip(".,") in _US_STATES:
                words.pop()
                words.pop()
                text = " ".join(words)

    # 7. Clean punctuation and excess whitespace
    text = re.sub(r"[*_\-–—]+$", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()

    # 8. Proper casing: capitalize words unless it's already mixed/clean
    if text.isupper() or text.islower():
        text = text.title()

    # Always clean apostrophes (e.g. Joe'S -> Joe's)
    text = re.sub(r"([A-Za-z])'S\b", r"\1's", text)

    # Fix common acronyms/brands
    text = re.sub(r"\bCvs\b", "CVS", text)
    text = re.sub(r"\bAtm\b", "ATM", text)
    text = re.sub(r"\bBpb\b", "BP", text)
    text = re.sub(r"\bUsps\b", "USPS", text)
    text = re.sub(r"\bUps\b", "UPS", text)
    text = re.sub(r"\bAmc\b", "AMC", text)
    text = re.sub(r"\bTrader Joe'?S\b", "Trader Joe's", text, flags=re.IGNORECASE)
    text = re.sub(r"\bMcdonald'?S\b", "McDonald's", text, flags=re.IGNORECASE)

    return text or raw.strip()


def resolve_canonical_merchant(
    raw: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Resolves a raw statement descriptor against known_merchants and merchant_aliases.
    Falls back to algorithmic cleaning if no registry entry matches.
    """
    if not raw or not isinstance(raw, str):
        return {
            "raw_merchant": "",
            "canonical_name": "",
            "category": None,
            "confidence": "none",
            "matched_by": "none",
        }

    raw_key = re.sub(r"\s+", " ", raw.strip().lower())
    cleaned = clean_raw_merchant_descriptor(raw)
    clean_key = re.sub(r"\s+", " ", cleaned.strip().lower())

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        c = db_conn.cursor()

        # 1. Exact match on known_merchants.merchant_key
        c.execute("""
            SELECT canonical_name, category, confidence
            FROM known_merchants
            WHERE merchant_key = ?
            LIMIT 1
        """, (raw_key,))
        row = c.fetchone()
        if row:
            return {
                "raw_merchant": raw,
                "canonical_name": row[0],
                "category": row[1],
                "confidence": row[2] or "high",
                "matched_by": "registry_exact",
            }

        # 2. Match on merchant_aliases.alias_key
        c.execute("""
            SELECT km.canonical_name, km.category, km.confidence
            FROM merchant_aliases ma
            JOIN known_merchants km ON km.id = ma.known_merchant_id
            WHERE ma.alias_key = ?
            LIMIT 1
        """, (raw_key,))
        row = c.fetchone()
        if row:
            return {
                "raw_merchant": raw,
                "canonical_name": row[0],
                "category": row[1],
                "confidence": row[2] or "high",
                "matched_by": "alias_exact",
            }

        # 3. Match using cleaned descriptor key on known_merchants
        if clean_key != raw_key:
            c.execute("""
                SELECT canonical_name, category, confidence
                FROM known_merchants
                WHERE merchant_key = ?
                LIMIT 1
            """, (clean_key,))
            row = c.fetchone()
            if row:
                return {
                    "raw_merchant": raw,
                    "canonical_name": row[0],
                    "category": row[1],
                    "confidence": "high",
                    "matched_by": "registry_cleaned",
                }

            # Match using cleaned key on merchant_aliases
            c.execute("""
                SELECT km.canonical_name, km.category, km.confidence
                FROM merchant_aliases ma
                JOIN known_merchants km ON km.id = ma.known_merchant_id
                WHERE ma.alias_key = ?
                LIMIT 1
            """, (clean_key,))
            row = c.fetchone()
            if row:
                return {
                    "raw_merchant": raw,
                    "canonical_name": row[0],
                    "category": row[1],
                    "confidence": "high",
                    "matched_by": "alias_cleaned",
                }

        # 4. Fallback to algorithmic cleaned descriptor
        return {
            "raw_merchant": raw,
            "canonical_name": cleaned,
            "category": None,
            "confidence": "medium" if cleaned != raw else "low",
            "matched_by": "heuristic_cleaner",
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def add_merchant_alias(
    *,
    alias: str,
    canonical_name: str,
    category: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Registers an alias mapping from a raw statement descriptor to a canonical merchant.
    Ensures both known_merchants and merchant_aliases entries are created/updated.
    """
    alias_str = str(alias or "").strip()
    canon_str = str(canonical_name or "").strip()
    if not alias_str:
        raise ValueError("Alias cannot be empty")
    if not canon_str:
        raise ValueError("Canonical merchant name cannot be empty")

    alias_key = re.sub(r"\s+", " ", alias_str.lower())
    canon_key = re.sub(r"\s+", " ", canon_str.lower())

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        apply_all(db_conn)
        c = db_conn.cursor()

        # 1. Ensure known_merchants has canonical record
        c.execute("SELECT id FROM known_merchants WHERE merchant_key = ?", (canon_key,))
        km_row = c.fetchone()
        now_str = "datetime('now')"
        if not km_row:
            c.execute("""
                INSERT INTO known_merchants (merchant_key, canonical_name, category, confidence, source, updated_at)
                VALUES (?, ?, ?, 'high', 'user_alias', datetime('now'))
            """, (canon_key, canon_str, category))
            km_id = c.lastrowid
        else:
            km_id = km_row[0]
            if category:
                c.execute("""
                    UPDATE known_merchants
                    SET category = COALESCE(?, category), updated_at = datetime('now')
                    WHERE id = ?
                """, (category, km_id))

        # 2. Add or update alias
        c.execute("""
            INSERT INTO merchant_aliases (known_merchant_id, alias_key, notes, source, updated_at)
            VALUES (?, ?, ?, 'user', datetime('now'))
            ON CONFLICT(alias_key) DO UPDATE SET
                known_merchant_id = excluded.known_merchant_id,
                updated_at = datetime('now')
        """, (km_id, alias_key, f"Mapped to {canon_str}"))
        db_conn.commit()

        return {
            "status": "success",
            "alias": alias_str,
            "alias_key": alias_key,
            "canonical_name": canon_str,
            "category": category,
            "known_merchant_id": km_id,
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def canonicalize_user_transactions(
    *,
    user_id: str,
    dry_run: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Scans all transactions for user_id and updates clean_merchant using
    the canonical merchant resolver.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("canonicalize_user_transactions: forced isolation violation — user_id is required")

    def _execute(db_conn: sqlite3.Connection) -> Dict[str, Any]:
        apply_all(db_conn)
        c = db_conn.cursor()

        c.execute("""
            SELECT id, merchant, clean_merchant
            FROM transactions
            WHERE user_id = ?
            ORDER BY id ASC
        """, (user_id,))
        rows = c.fetchall()

        total_inspected = len(rows)
        updated_count = 0
        modifications: List[Dict[str, Any]] = []

        for r in rows:
            tx_id = r[0]
            raw_merchant = r[1] or ""
            current_clean = r[2]

            resolved = resolve_canonical_merchant(raw_merchant, conn=db_conn)
            canon = resolved["canonical_name"]

            if canon and canon != current_clean:
                updated_count += 1
                if len(modifications) < 25:
                    modifications.append({
                        "id": tx_id,
                        "raw": raw_merchant,
                        "old_clean": current_clean,
                        "new_clean": canon,
                        "matched_by": resolved["matched_by"],
                    })

                if not dry_run:
                    c.execute("""
                        UPDATE transactions
                        SET clean_merchant = ?
                        WHERE id = ? AND user_id = ?
                    """, (canon, tx_id, user_id))

        if not dry_run:
            db_conn.commit()

        return {
            "user_id": user_id,
            "total_transactions": total_inspected,
            "canonicalized_count": updated_count,
            "dry_run": dry_run,
            "sample_modifications": modifications,
        }

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def suggest_merchant_aliases(
    *,
    user_id: str,
    limit: int = 15,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """
    Finds unique unmapped raw merchant strings in user transactions and proposes
    algorithmic clean canonical names.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("suggest_merchant_aliases: forced isolation violation — user_id is required")

    def _execute(db_conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        apply_all(db_conn)
        c = db_conn.cursor()

        c.execute("""
            SELECT t.merchant, COUNT(*) as cnt, SUM(t.amount) as total_amt
            FROM transactions t
            LEFT JOIN known_merchants km ON LOWER(TRIM(t.merchant)) = LOWER(TRIM(km.merchant_key))
            LEFT JOIN merchant_aliases ma ON ma.alias_key = LOWER(TRIM(t.merchant))
            WHERE t.user_id = ?
              AND km.id IS NULL
              AND ma.id IS NULL
              AND t.merchant IS NOT NULL
              AND t.merchant != ''
            GROUP BY t.merchant
            ORDER BY cnt DESC, total_amt DESC
            LIMIT ?
        """, (user_id, max(1, min(limit, 50))))

        rows = c.fetchall()
        suggestions = []
        for r in rows:
            raw_m, cnt, total_amt = r[0], r[1], float(r[2] or 0.0)
            cleaned = clean_raw_merchant_descriptor(raw_m)
            suggestions.append({
                "raw_merchant": raw_m,
                "suggested_canonical": cleaned,
                "transaction_count": cnt,
                "total_volume": round(total_amt, 2),
            })
        return suggestions

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def format_merchants_overview_embed(
    stats: Dict[str, Any],
    suggestions: List[Dict[str, Any]],
) -> discord.Embed:
    """Format merchant cleanup overview into a Discord embed."""
    embed = discord.Embed(
        title="🏷️ Merchant Normalization & Ledger Consistency",
        color=discord.Color.blue(),
        description=(
            f"**Total Transactions:** {stats.get('total_transactions', 0):,}\n"
            f"**Transactions Standardized:** {stats.get('canonicalized_count', 0):,}\n"
            f"**Mode:** `{'Dry Run (Preview)' if stats.get('dry_run') else 'Applied to Database'}`"
        )
    )

    mods = stats.get("sample_modifications", [])
    if mods:
        sample_lines = [
            f"• `{m['raw'][:25]}` ➔ **{m['new_clean']}** ({m['matched_by']})"
            for m in mods[:8]
        ]
        embed.add_field(name="✨ Sample Canonicalizations", value="\n".join(sample_lines), inline=False)

    if suggestions:
        sug_lines = [
            f"• `{s['raw_merchant'][:25]}` ➔ **{s['suggested_canonical']}** ({s['transaction_count']} txs, ${s['total_volume']:,.2f})"
            for s in suggestions[:8]
        ]
        embed.add_field(name="💡 Suggested Merchant Aliases", value="\n".join(sug_lines), inline=False)

    embed.set_footer(text="Use !cleanmerchants to batch apply · !addalias <raw> -> <canonical> to map")
    return embed
