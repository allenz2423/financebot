import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.merchants import (
    clean_raw_merchant_descriptor,
    resolve_canonical_merchant,
    add_merchant_alias,
    canonicalize_user_transactions,
    suggest_merchant_aliases,
    format_merchants_overview_embed,
)
from src.db.migrations import apply_all


@pytest.fixture
def merchants_db():
    conn = sqlite3.connect(":memory:")
    apply_all(conn)
    # Ensure transactions table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            transaction_id TEXT,
            date TEXT NOT NULL,
            merchant TEXT NOT NULL,
            clean_merchant TEXT,
            category TEXT,
            amount REAL NOT NULL,
            account_used TEXT,
            status TEXT DEFAULT 'Evaluated',
            is_locked INTEGER DEFAULT 0
        )
    """)
    # Ensure known_merchants and merchant_aliases exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS known_merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_key TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL,
            category TEXT,
            confidence TEXT NOT NULL DEFAULT 'high',
            notes TEXT,
            evidence_url TEXT,
            source TEXT NOT NULL DEFAULT 'curated',
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merchant_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            known_merchant_id INTEGER NOT NULL,
            alias_key TEXT NOT NULL UNIQUE,
            notes TEXT,
            source TEXT NOT NULL DEFAULT 'curated',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (known_merchant_id) REFERENCES known_merchants(id)
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def test_clean_raw_merchant_descriptor():
    # 1. Payment aggregator prefixes
    assert clean_raw_merchant_descriptor("TST* SWEETGREEN") == "Sweetgreen"
    assert clean_raw_merchant_descriptor("SQ *BLUE BOTTLE COFFEE") == "Blue Bottle Coffee"
    assert clean_raw_merchant_descriptor("PAYPAL *STEAM GAMES") == "Steam Games"
    assert clean_raw_merchant_descriptor("AplPay WHOLE FOODS") == "Whole Foods"

    # 2. Amazon variants
    assert clean_raw_merchant_descriptor("AMZN MKTP US*2B8J81") == "Amazon"
    assert clean_raw_merchant_descriptor("AMZN DIGITAL*9K12") == "Amazon"

    # 3. POS authorization noise & card prefixes
    assert clean_raw_merchant_descriptor("PURCHASE AUTHORIZED ON 09/12 UBER *TRIP 1234") == "Uber"
    assert clean_raw_merchant_descriptor("POS DEBIT CHEVRON 0092384") == "Chevron"

    # 4. Location noise, store numbers, phone numbers
    assert clean_raw_merchant_descriptor("TARGET STORE #1042 SEATTLE WA") == "Target"
    assert clean_raw_merchant_descriptor("TRADER JOE'S #543 SAN FRANCISCO CA 94103") == "Trader Joe's"
    assert clean_raw_merchant_descriptor("MCDONALD'S 800-244-6227") == "McDonald's"

    # 5. Empty / whitespace cases
    assert clean_raw_merchant_descriptor("") == ""
    assert clean_raw_merchant_descriptor(None) == ""


def test_resolve_canonical_merchant_registry_and_alias(merchants_db):
    # Seed known_merchants
    merchants_db.execute("""
        INSERT INTO known_merchants (merchant_key, canonical_name, category, updated_at)
        VALUES ('starbucks', 'Starbucks Coffee', 'Coffee Shop', datetime('now'))
    """)
    km_id = merchants_db.execute("SELECT id FROM known_merchants WHERE merchant_key = 'starbucks'").fetchone()[0]

    # Seed alias
    merchants_db.execute("""
        INSERT INTO merchant_aliases (known_merchant_id, alias_key, notes, updated_at)
        VALUES (?, 'sbux store 1234', 'Starbucks alias', datetime('now'))
    """, (km_id,))
    merchants_db.commit()

    # Exact registry match
    res1 = resolve_canonical_merchant("STARBUCKS", conn=merchants_db)
    assert res1["canonical_name"] == "Starbucks Coffee"
    assert res1["matched_by"] == "registry_exact"
    assert res1["category"] == "Coffee Shop"

    # Alias match
    res2 = resolve_canonical_merchant("SBUX STORE 1234", conn=merchants_db)
    assert res2["canonical_name"] == "Starbucks Coffee"
    assert res2["matched_by"] == "alias_exact"

    # Cleaned match (raw has aggregator prefix)
    res3 = resolve_canonical_merchant("SQ *STARBUCKS", conn=merchants_db)
    assert res3["canonical_name"] == "Starbucks Coffee"
    assert res3["matched_by"] == "registry_cleaned"

    # Heuristic fallback (not in DB)
    res4 = resolve_canonical_merchant("TST* JOE'S PIZZA #4", conn=merchants_db)
    assert res4["canonical_name"] == "Joe's Pizza"
    assert res4["matched_by"] == "heuristic_cleaner"


def test_add_merchant_alias_and_validation(merchants_db):
    res = add_merchant_alias(
        alias="TST* SWEETGREEN NOMAD",
        canonical_name="Sweetgreen",
        category="Dining Out",
        conn=merchants_db,
    )
    assert res["status"] == "success"
    assert res["canonical_name"] == "Sweetgreen"
    assert res["category"] == "Dining Out"

    # Verify resolution uses the newly added alias
    resolved = resolve_canonical_merchant("TST* SWEETGREEN NOMAD", conn=merchants_db)
    assert resolved["canonical_name"] == "Sweetgreen"
    assert resolved["matched_by"] == "alias_exact"

    # Validation errors
    with pytest.raises(ValueError, match="Alias cannot be empty"):
        add_merchant_alias(alias="", canonical_name="Target", conn=merchants_db)
    with pytest.raises(ValueError, match="Canonical merchant name cannot be empty"):
        add_merchant_alias(alias="Target #123", canonical_name="", conn=merchants_db)


def test_canonicalize_user_transactions_and_isolation(merchants_db):
    user_a = "user_canon_a"
    user_b = "user_canon_b"

    # Insert transactions for user A
    merchants_db.execute("""
        INSERT INTO transactions (user_id, date, merchant, clean_merchant, amount)
        VALUES (?, '2026-09-01', 'AMZN MKTP US*123', NULL, 45.0)
    """, (user_a,))
    merchants_db.execute("""
        INSERT INTO transactions (user_id, date, merchant, clean_merchant, amount)
        VALUES (?, '2026-09-02', 'SQ *BLUE BOTTLE', 'SQ *BLUE BOTTLE', 6.50)
    """, (user_a,))

    # Insert transaction for user B
    merchants_db.execute("""
        INSERT INTO transactions (user_id, date, merchant, clean_merchant, amount)
        VALUES (?, '2026-09-03', 'AMZN MKTP US*999', NULL, 120.0)
    """, (user_b,))
    merchants_db.commit()

    # Dry run for user A
    preview = canonicalize_user_transactions(user_id=user_a, dry_run=True, conn=merchants_db)
    assert preview["canonicalized_count"] == 2
    assert preview["dry_run"] is True

    # Check DB was not modified in dry run
    r1 = merchants_db.execute("SELECT clean_merchant FROM transactions WHERE user_id = ? AND date = '2026-09-01'", (user_a,)).fetchone()
    assert r1[0] is None

    # Apply canonicalization for user A
    applied = canonicalize_user_transactions(user_id=user_a, dry_run=False, conn=merchants_db)
    assert applied["canonicalized_count"] == 2
    assert applied["dry_run"] is False

    # Check user A records were updated
    r_a1 = merchants_db.execute("SELECT clean_merchant FROM transactions WHERE user_id = ? AND date = '2026-09-01'", (user_a,)).fetchone()
    assert r_a1[0] == "Amazon"
    r_a2 = merchants_db.execute("SELECT clean_merchant FROM transactions WHERE user_id = ? AND date = '2026-09-02'", (user_a,)).fetchone()
    assert r_a2[0] == "Blue Bottle"

    # Ensure user B's transaction was completely isolated and untouched
    r_b = merchants_db.execute("SELECT clean_merchant FROM transactions WHERE user_id = ? AND date = '2026-09-03'", (user_b,)).fetchone()
    assert r_b[0] is None


def test_suggest_merchant_aliases(merchants_db):
    user_id = "user_sug"
    merchants_db.execute("INSERT INTO transactions (user_id, date, merchant, amount) VALUES (?, '2026-09-01', 'TST* JOE PIZZA', 20.0)", (user_id,))
    merchants_db.execute("INSERT INTO transactions (user_id, date, merchant, amount) VALUES (?, '2026-09-02', 'TST* JOE PIZZA', 25.0)", (user_id,))
    merchants_db.execute("INSERT INTO transactions (user_id, date, merchant, amount) VALUES (?, '2026-09-03', 'CHEVRON 09482', 50.0)", (user_id,))
    merchants_db.commit()

    suggestions = suggest_merchant_aliases(user_id=user_id, limit=5, conn=merchants_db)
    assert len(suggestions) == 2
    # Top suggestion should be 'TST* JOE PIZZA' (count 2)
    assert suggestions[0]["raw_merchant"] == "TST* JOE PIZZA"
    assert suggestions[0]["suggested_canonical"] == "Joe Pizza"
    assert suggestions[0]["transaction_count"] == 2
    assert suggestions[0]["total_volume"] == 45.0

    embed = format_merchants_overview_embed({"total_transactions": 3, "canonicalized_count": 3, "dry_run": True}, suggestions)
    assert "Merchant Normalization" in embed.title
    assert any("Suggested Merchant Aliases" in f.name for f in embed.fields)


@pytest.mark.asyncio
async def test_merchants_discord_commands(merchants_db, monkeypatch):
    import src.services.merchants as m_mod
    from src.bot.commands import merchants_group, merchants_clean_subcmd, merchants_alias_subcmd

    # Seed transaction
    merchants_db.execute("""
        INSERT INTO transactions (user_id, date, merchant, clean_merchant, amount)
        VALUES ('ctx_merch_user', '2026-09-01', 'TST* SWEETGREEN', NULL, 15.50)
    """)
    merchants_db.commit()

    orig_canon = m_mod.canonicalize_user_transactions
    orig_sug = m_mod.suggest_merchant_aliases
    orig_alias = m_mod.add_merchant_alias

    monkeypatch.setattr(m_mod, "canonicalize_user_transactions", lambda user_id, dry_run=False, conn=None: orig_canon(user_id=user_id, dry_run=dry_run, conn=merchants_db))
    monkeypatch.setattr(m_mod, "suggest_merchant_aliases", lambda user_id, limit=15, conn=None: orig_sug(user_id=user_id, limit=limit, conn=merchants_db))
    monkeypatch.setattr(m_mod, "add_merchant_alias", lambda alias, canonical_name, category=None, conn=None: orig_alias(alias=alias, canonical_name=canonical_name, category=category, conn=merchants_db))

    ctx = MagicMock()
    ctx.author.id = "ctx_merch_user"
    ctx.send = AsyncMock()
    ctx.invoked_subcommand = None

    # Test merchants overview command
    await merchants_group(ctx)
    ctx.send.assert_called_once()
    embed = ctx.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "Merchant Normalization" in embed.title

    # Test clean apply command
    ctx.send.reset_mock()
    await merchants_clean_subcmd(ctx, mode="apply")
    ctx.send.assert_called_once()
    embed_clean = ctx.send.call_args.kwargs.get("embed")
    assert "Applied to Database" in embed_clean.description

    # Test alias command
    ctx.send.reset_mock()
    await merchants_alias_subcmd(ctx, mapping="TST* CHIPOTLE 123 -> Chipotle [Fast Casual]")
    ctx.send.assert_called_once()
    assert "Chipotle" in ctx.send.call_args[0][0]
