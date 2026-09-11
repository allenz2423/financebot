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
