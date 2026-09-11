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
