import sqlite3
import pytest
import src.db.queries as q

TEST_USER_1 = "user-tax-1"
TEST_USER_2 = "user-tax-2"

@pytest.fixture(autouse=True)
def setup_tax_db(monkeypatch):
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_c = test_conn.cursor()

    test_c.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            transaction_id TEXT UNIQUE,
            date TEXT,
            merchant TEXT,
            clean_merchant TEXT,
            category TEXT,
            amount REAL,
            tax_deductible INTEGER DEFAULT 0,
            tax_category TEXT,
            status TEXT DEFAULT 'Evaluated'
        );
    """)

    monkeypatch.setattr(q, "conn", test_conn)
    monkeypatch.setattr(q, "c", test_c)
    yield test_conn
    test_conn.close()


def test_tax_tagging_and_isolation():
    # Insert test transactions
    q.c.execute("""
        INSERT INTO transactions (id, user_id, date, merchant, amount)
        VALUES (201, ?, '2026-04-10', 'GitHub Pro', 20.00),
               (202, ?, '2026-05-15', 'Client Lunch', 100.00)
    """, (TEST_USER_1, TEST_USER_1))
    q.conn.commit()

    # User 1 tags tx 201 as Software
    res1 = q.tag_transaction_tax(201, is_deductible=True, tax_category="Software", user_id=TEST_USER_1)
    assert res1["status"] == "success"
    assert res1["tax_deductible"] is True
    assert res1["tax_category"] == "Software"

    # User 2 cannot tag User 1's transaction
    res2 = q.tag_transaction_tax(201, is_deductible=True, tax_category="Hacked", user_id=TEST_USER_2)
    assert res2["status"] == "not_found"

    # User 1 tags tx 202 as Meals
    q.tag_transaction_tax(202, is_deductible=True, tax_category="Business Meals (50%)", user_id=TEST_USER_1)

    # Check summary
    summary = q.get_tax_deductions_summary(year=2026, user_id=TEST_USER_1)
    assert summary["status"] == "success"
    assert summary["total_transactions"] == 2
    assert summary["total_gross_spent"] == 120.00
    # 20 (software 100%) + 50 (meals 50% of 100) = 70 deductible
    assert summary["total_deductible"] == 70.00

    # User 2 summary must be empty
    u2_summary = q.get_tax_deductions_summary(year=2026, user_id=TEST_USER_2)
    assert u2_summary["total_transactions"] == 0

    # Test CSV export
    csv_str = q.export_tax_csv(year=2026, user_id=TEST_USER_1)
    assert "Transaction ID,Date,Merchant,Tax Category,Gross Amount,Deductible Amount" in csv_str
    assert "GitHub Pro,Software,20.00,20.00" in csv_str
    assert "Client Lunch,Business Meals (50%),100.00,50.00" in csv_str


def test_match_transaction_deduction():
    from src.services.tax_deductions import match_transaction_deduction

    # Software & Cloud
    match1 = match_transaction_deduction("AWS Cloud Services")
    assert match1 is not None
    assert match1[0].category == "Software & Cloud Infrastructure"
    assert match1[0].deductible_percentage == 1.0

    # Business Meals (50%)
    match2 = match_transaction_deduction("Client Lunch", category="Dining Out")
    assert match2 is not None
    assert match2[0].category == "Business Meals (50%)"
    assert match2[0].deductible_percentage == 0.5

    # Telecom (75%)
    match3 = match_transaction_deduction("Verizon Wireless")
    assert match3 is not None
    assert match3[0].category == "Telecommunications & Internet"
    assert match3[0].deductible_percentage == 0.75

    # Non-deductible personal expense
    assert match_transaction_deduction("AMC Theatres", category="Movies") is None


def test_scan_and_discover_deductions_preview_and_apply(setup_tax_db):
    conn = setup_tax_db
    from src.services.tax_deductions import (
        scan_and_discover_deductions,
        format_tax_discovery_embed,
        format_tax_discovery_report,
    )

    user_a = "tax_user_a"
    user_b = "tax_user_b"

    # Insert candidate transactions for User A
    conn.execute("""
        INSERT INTO transactions (user_id, date, merchant, amount, category)
        VALUES (?, '2026-06-15', 'GitHub Pro', 20.0, 'Subscriptions'),
               (?, '2026-07-20', 'AWS EMEA', 100.0, 'Software'),
               (?, '2026-08-05', 'Client Dinner', 200.0, 'Dining Out'),
               (?, '2026-09-01', 'Target', 50.0, 'General Merchandise')
    """, (user_a, user_a, user_a, user_a))

    # Insert candidate for User B to test isolation
    conn.execute("""
        INSERT INTO transactions (user_id, date, merchant, amount, category)
        VALUES (?, '2026-06-15', 'GitHub Enterprise', 500.0, 'Subscriptions')
    """, (user_b,))
    conn.commit()

    # 1. Preview mode (auto_apply=False)
    preview = scan_and_discover_deductions(user_id=user_a, year=2026, auto_apply=False, conn=conn)
    assert preview["candidates_found_count"] == 3  # GitHub, AWS, Client Dinner (Target is not matched)
    assert preview["auto_applied"] is False
    assert preview["applied_count"] == 0

    # Gross: 20 + 100 + 200 = 320
    # Deductible: 20 + 100 + (200 * 0.5) = 220
    assert preview["total_potential_deductible"] == 220.0
    # Estimated tax savings at 30%: 220 * 0.30 = 66.0
    assert preview["estimated_tax_savings"] == 66.0

    # Ensure DB was not modified in preview
    cur = conn.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND tax_deductible = 1", (user_a,))
    assert cur.fetchone()[0] == 0

    # 2. Apply mode (auto_apply=True)
    applied = scan_and_discover_deductions(user_id=user_a, year=2026, auto_apply=True, conn=conn)
    assert applied["auto_applied"] is True
    assert applied["applied_count"] == 3

    # Check DB was updated
    cur2 = conn.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND tax_deductible = 1", (user_a,))
    assert cur2.fetchone()[0] == 3

    # Ensure user B was not affected
    cur_b = conn.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND tax_deductible = 1", (user_b,))
    assert cur_b.fetchone()[0] == 0

    # 3. Test embed and report
    embed = format_tax_discovery_embed(applied)
    assert "Tax Deduction Discovery" in embed.title
    assert "$220.00" in embed.description
    assert "$66.00" in embed.description

    report = format_tax_discovery_report(applied)
    assert "AUTOMATED TAX DEDUCTION AUDIT" in report
    assert "GitHub Pro" in report


@pytest.mark.asyncio
async def test_tax_scan_discord_command(setup_tax_db, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    import src.services.tax_deductions as td_mod
    from src.bot.commands import tax_scan_subcmd

    conn = setup_tax_db
    conn.execute("""
        INSERT INTO transactions (user_id, date, merchant, amount, category)
        VALUES ('ctx_tax_user', '2026-05-01', 'DigitalOcean', 40.0, 'Cloud')
    """)
    conn.commit()

    orig_scan = td_mod.scan_and_discover_deductions
    monkeypatch.setattr(
        td_mod,
        "scan_and_discover_deductions",
        lambda user_id, year=None, auto_apply=False, marginal_tax_rate=0.30, conn=None: orig_scan(
            user_id=user_id, year=year, auto_apply=auto_apply, marginal_tax_rate=marginal_tax_rate, conn=conn or setup_tax_db
        )
    )

    ctx = MagicMock()
    ctx.author.id = "ctx_tax_user"
    ctx.send = AsyncMock()

    await tax_scan_subcmd(ctx, year=2026, mode="preview")
    ctx.send.assert_called_once()
    embed = ctx.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "$40.00" in embed.description
