import pytest
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from src.bot.commands import (
    _TransactionPageView,
    tx_command,
    searchtx_command,
    txview_command,
)

def test_transaction_page_view_pagination():
    mock_ctx = MagicMock()
    mock_ctx.author.id = 99999

    sample_rows = [
        (i, f"2026-09-{i:02d}", f"Store {i}", float(i * 10), "Checking", "Food", "Pending")
        for i in range(1, 60)
    ]

    view = _TransactionPageView(mock_ctx, sample_rows, title="Test Explorer", color=discord.Color.purple(), page_size=25)
    assert view.total_pages == 3
    assert view.page == 0
    assert view.previous.disabled is True
    assert view.next.disabled is False

    embed0 = view._embed()
    assert "Test Explorer (59)" in embed0.title
    assert "Showing **1–25** of **59**" in embed0.description
    assert len(embed0.fields) == 25

    # Page 1
    view.page = 1
    view._refresh_buttons()
    assert view.previous.disabled is False
    assert view.next.disabled is False
    embed1 = view._embed()
    assert "Showing **26–50** of **59**" in embed1.description

    # Page 2 (last)
    view.page = 2
    view._refresh_buttons()
    assert view.previous.disabled is False
    assert view.next.disabled is True
    embed2 = view._embed()
    assert "Showing **51–59** of **59**" in embed2.description
    assert len(embed2.fields) == 9

@pytest.mark.asyncio
async def test_tx_command_flow(tmp_path):
    db_file = tmp_path / "finances.db"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            date TEXT,
            merchant TEXT,
            amount REAL,
            account_used TEXT,
            category TEXT,
            status TEXT
        )
    """)
    cur.execute("""
        INSERT INTO transactions (user_id, date, merchant, amount, account_used, category, status)
        VALUES ('123', '2026-09-10', 'Coffee Shop', 4.75, 'Sapphire', 'Dining', 'Settled')
    """)
    cur.execute("""
        INSERT INTO transactions (user_id, date, merchant, amount, account_used, category, status)
        VALUES ('123', '2026-09-11', 'System Balance Sync Auto', 0.0, 'Sapphire', 'Transfer', 'Settled')
    """)
    conn.commit()
    conn.close()

    mock_ctx = MagicMock()
    mock_ctx.author.id = 123
    mock_ctx.send = AsyncMock()

    with patch("src.bot.commands._open_verification_db") as mock_open:
        mock_open.return_value = sqlite3.connect(db_file)
        await tx_command(mock_ctx, limit=10)

        assert mock_ctx.send.called
        call_kwargs = mock_ctx.send.call_args.kwargs
        view = call_kwargs.get("view")
        embed = call_kwargs.get("embed")
        assert view is not None
        assert len(view.rows) == 1
        assert "Coffee Shop" in str(embed.fields[0].name)

@pytest.mark.asyncio
async def test_searchtx_command_text_and_filter(tmp_path):
    db_file = tmp_path / "finances.db"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            date TEXT,
            merchant TEXT,
            clean_merchant TEXT,
            amount REAL,
            account_used TEXT,
            category TEXT,
            status TEXT
        )
    """)
    cur.execute("""
        INSERT INTO transactions (user_id, date, merchant, clean_merchant, amount, account_used, category, status)
        VALUES 
        ('123', '2026-09-01', 'UBER *TRIP 123', 'Uber', 25.50, 'Amex Gold', 'Travel', 'Settled'),
        ('123', '2026-09-02', 'APPLE STORE #10', 'Apple', 1299.00, 'Apple Card', 'Electronics', 'Settled'),
        ('123', '2026-09-03', 'WHOLEFDS MKT', 'Whole Foods', 64.20, 'Amex Gold', 'Groceries', 'Settled')
    """)
    conn.commit()
    conn.close()

    mock_ctx = MagicMock()
    mock_ctx.author.id = 123
    mock_ctx.send = AsyncMock()

    with patch("src.bot.commands._open_verification_db") as mock_open:
        mock_open.return_value = sqlite3.connect(db_file)

        # 1. Text search for 'uber'
        await searchtx_command(mock_ctx, query="uber")
        view1 = mock_ctx.send.call_args.kwargs.get("view")
        assert len(view1.rows) == 1
        assert view1.rows[0][2] == 'UBER *TRIP 123'

        # 2. Amount comparison '>100'
        await searchtx_command(mock_ctx, query=">100")
        view2 = mock_ctx.send.call_args.kwargs.get("view")
        assert len(view2.rows) == 1
        assert view2.rows[0][2] == 'APPLE STORE #10'

        # 3. Amount comparison '<50'
        await searchtx_command(mock_ctx, query="<50")
        view3 = mock_ctx.send.call_args.kwargs.get("view")
        assert len(view3.rows) == 1
        assert view3.rows[0][2] == 'UBER *TRIP 123'

@pytest.mark.asyncio
async def test_txview_command_details(tmp_path):
    db_file = tmp_path / "finances.db"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            transaction_id TEXT,
            date TEXT,
            merchant TEXT,
            clean_merchant TEXT,
            category TEXT,
            amount REAL,
            override_amount REAL,
            account_used TEXT,
            status TEXT,
            is_locked INTEGER,
            context TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE transaction_items (
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            transaction_row_id INTEGER,
            item_name TEXT,
            quantity REAL,
            unit_price REAL,
            total_price REAL,
            source TEXT
        )
    """)
    cur.execute("""
        INSERT INTO transactions (id, user_id, transaction_id, date, merchant, clean_merchant, category, amount, override_amount, account_used, status, is_locked, context)
        VALUES (42, '123', 'plaid_abc', '2026-09-05', 'COSTCO WHSE #12', 'Costco', 'Wholesale', 150.00, 145.00, 'Costco Visa', 'Settled', 1, 'Split with roommate')
    """)
    cur.execute("""
        INSERT INTO transaction_items (id, user_id, transaction_row_id, item_name, quantity, unit_price, total_price, source)
        VALUES (1, '123', 42, 'Kirkland Olive Oil', 2, 12.50, 25.00, 'manual_receipt')
    """)
    conn.commit()
    conn.close()

    mock_ctx = MagicMock()
    mock_ctx.author.id = 123
    mock_ctx.send = AsyncMock()

    with patch("src.bot.commands._open_verification_db") as mock_open:
        mock_open.return_value = sqlite3.connect(db_file)

        # Non-existent
        await txview_command(mock_ctx, tx_id=999)
        assert "not found" in mock_ctx.send.call_args.args[0]

        # Existing
        await txview_command(mock_ctx, tx_id=42)
        embed = mock_ctx.send.call_args.kwargs.get("embed")
        assert embed is not None
        assert "Transaction #42 Details" in embed.title
        field_dict = {f.name: f.value for f in embed.fields}
        assert "Costco" in field_dict["Merchant"]
        assert "$150.00" in field_dict["Amount"]
        assert "Override: **$145.00**" in field_dict["Amount"]
        assert "Kirkland Olive Oil" in field_dict["Itemized Receipt Items (1)"]
        assert "Split with roommate" in field_dict["Notes & Context"]
