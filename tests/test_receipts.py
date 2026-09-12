import sqlite3
import pytest
import src.db.queries as q
from src.services.receipt_parser import parse_receipt_text

TEST_USER_1 = "user-receipts-1"
TEST_USER_2 = "user-receipts-2"

@pytest.fixture(autouse=True)
def setup_receipts_db(monkeypatch):
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
            status TEXT DEFAULT 'Evaluated'
        );
        CREATE TABLE IF NOT EXISTS transaction_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            transaction_row_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity REAL DEFAULT 1.0,
            unit_price REAL,
            total_price REAL,
            category TEXT,
            source TEXT DEFAULT 'gmail_receipt',
            raw_receipt_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

    monkeypatch.setattr(q, "conn", test_conn)
    monkeypatch.setattr(q, "c", test_c)
    yield test_conn
    test_conn.close()


def test_parse_receipt_text():
    sample_text = """
    Thanks for your order!
    Order #112-9876543-2109876

    Items:
    1x Anker USB-C Cable $14.99
    2x Organic Coffee Beans 12oz $28.00
    Subtotal: $42.99
    Sales Tax: $3.87
    Grand Total: $46.86
    Payment: Visa ending in 4321
    """

    items = parse_receipt_text(sample_text)
    assert len(items) == 2

    # Check Anker Cable
    cable = next(i for i in items if "Anker" in i["item_name"])
    assert cable["quantity"] == 1.0
    assert cable["total_price"] == 14.99

    # Check Coffee Beans
    coffee = next(i for i in items if "Coffee" in i["item_name"])
    assert coffee["quantity"] == 2.0
    assert coffee["total_price"] == 28.00
    assert coffee["unit_price"] == 14.00


def test_transaction_items_crud_and_isolation():
    # User 1 adds items to tx #101
    item1 = q.add_transaction_item(
        user_id=TEST_USER_1,
        transaction_row_id=101,
        item_name="Mechanical Keyboard",
        total_price=89.99,
        quantity=1.0
    )
    assert item1["id"] is not None
    assert item1["item_name"] == "Mechanical Keyboard"

    # User 2 cannot see User 1's items
    u2_items = q.get_transaction_items(101, user_id=TEST_USER_2)
    assert len(u2_items) == 0

    # User 1 sees items
    u1_items = q.get_transaction_items(101, user_id=TEST_USER_1)
    assert len(u1_items) == 1
    assert u1_items[0]["total_price"] == 89.99

    # User 2 cannot delete User 1's item
    assert not q.delete_transaction_item(item1["id"], user_id=TEST_USER_2)
    assert len(q.get_transaction_items(101, user_id=TEST_USER_1)) == 1

    # User 1 deletes item
    assert q.delete_transaction_item(item1["id"], user_id=TEST_USER_1)
    assert len(q.get_transaction_items(101, user_id=TEST_USER_1)) == 0


def test_parse_receipt_text_varied_formats():
    from src.services.receipt_parser import parse_receipt_text

    text = """
    TRADER JOE'S #501
    ---------------------------
    • Organic Bananas - $1.99
    - Greek Yogurt : $4.50
    * Almond Milk $3.29
    1. Sourdough Bread       $4.99
    2x Avocado 4-Pack - $8.00
    Subtotal: $22.77
    Tax: $0.00
    Total: $22.77
    """
    items = parse_receipt_text(text)
    assert len(items) == 5

    names = [i["item_name"] for i in items]
    assert "Organic Bananas" in names
    assert "Greek Yogurt" in names
    assert "Almond Milk" in names
    assert "Sourdough Bread" in names
    assert "Avocado 4-Pack" in names

    avo = next(i for i in items if "Avocado" in i["item_name"])
    assert avo["quantity"] == 2.0
    assert avo["total_price"] == 8.00
    assert avo["unit_price"] == 4.00


def test_extract_receipt_total():
    from src.services.receipt_parser import extract_receipt_total

    assert extract_receipt_total("Total: $42.50\nThanks for visiting!") == 42.50
    assert extract_receipt_total("Grand Total: $109.95\nVisa") == 109.95
    assert extract_receipt_total("Amount Charged: $15.00") == 15.00
    assert extract_receipt_total("No total here") is None


def test_match_and_attach_receipt():
    from src.services.receipt_parser import match_receipt_to_transaction, parse_and_attach_receipt

    # Insert transactions for TEST_USER_1
    q.c.execute("""
        INSERT INTO transactions (id, user_id, date, merchant, clean_merchant, amount, category)
        VALUES 
        (10, 'user-receipts-1', '2026-09-05', 'TRADER JOES #10', 'Trader Joes', 22.77, 'Groceries'),
        (20, 'user-receipts-1', '2026-09-06', 'TARGET T-1234', 'Target', 85.00, 'Shopping'),
        (30, 'user-receipts-2', '2026-09-05', 'TRADER JOES #10', 'Trader Joes', 22.77, 'Groceries')
    """)
    q.safe_commit()

    receipt_text = """
    TRADER JOE'S #10
    2026-09-05
    • Organic Bananas - $2.77
    • Greek Yogurt - $20.00
    Total: $22.77
    """

    # 1. Match candidates
    candidates = match_receipt_to_transaction(receipt_text, [{"total_price": 22.77}], user_id=TEST_USER_1)
    assert len(candidates) >= 1
    assert candidates[0]["transaction"]["id"] == 10
    assert candidates[0]["score"] >= 50

    # 2. Attach with explicit ID
    res1 = parse_and_attach_receipt(receipt_text, user_id=TEST_USER_1, transaction_id=10)
    assert res1["success"] is True
    assert res1["transaction_id"] == 10
    assert res1["items_count"] == 2
    assert res1["receipt_total"] == 22.77

    # Verify items in DB
    items_in_db = q.get_transaction_items(10, user_id=TEST_USER_1)
    assert len(items_in_db) == 2

    # User 2 cannot access items for transaction 10
    u2_items = q.get_transaction_items(10, user_id=TEST_USER_2)
    assert len(u2_items) == 0

    # 3. Attach with auto-match (no transaction_id provided)
    # Target receipt
    target_receipt = """
    TARGET
    • Desk Lamp $45.00
    • USB Charger $40.00
    Order Total: $85.00
    """
    res2 = parse_and_attach_receipt(target_receipt, user_id=TEST_USER_1)
    assert res2["success"] is True
    assert res2["transaction_id"] == 20
    assert res2["items_count"] == 2

    # 4. User 1 cannot attach to User 2's transaction #30
    res3 = parse_and_attach_receipt(receipt_text, user_id=TEST_USER_1, transaction_id=30)
    assert res3["success"] is False
    assert "not found" in res3["error"]


@pytest.mark.asyncio
async def test_receipts_parse_discord_command():
    from unittest.mock import AsyncMock, MagicMock
    from src.bot.commands import receipts_parse_cmd

    # Seed transaction
    q.c.execute("""
        INSERT INTO transactions (id, user_id, date, merchant, clean_merchant, amount, category)
        VALUES (55, '9999', '2026-09-08', 'COSTCO WHSE', 'Costco', 50.00, 'Groceries')
    """)
    q.safe_commit()

    mock_ctx = MagicMock()
    mock_ctx.author.id = 9999
    mock_ctx.send = AsyncMock()

    # Call with explicit tx_id
    receipt_body = "55 1x Kirkland Nuts $20.00\n1x Olive Oil $30.00\nTotal: $50.00"
    await receipts_parse_cmd(mock_ctx, content=receipt_body)

    assert mock_ctx.send.called
    embed = mock_ctx.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "Attached 2 Items to Tx #55" in embed.title
    assert "Costco" in embed.description
    assert "$50.00" in embed.description

