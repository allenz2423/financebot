import pytest
import sqlite3
from io import BytesIO
from PIL import Image

from src.services.charts import (
    render_category_spending_chart,
    render_cash_flow_bar_chart,
    render_net_worth_trend_chart,
    generate_spending_chart_for_user,
    generate_cash_flow_chart_for_user,
    generate_net_worth_chart_for_user,
)


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE transactions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            account_id TEXT,
            amount REAL,
            date TEXT,
            name TEXT,
            merchant TEXT,
            category TEXT,
            status TEXT
        )
    """)
    c.execute("""
        CREATE TABLE financial_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            captured_at TEXT,
            net_worth REAL,
            liquid_cash REAL,
            total_debt REAL,
            investments REAL
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def test_render_category_spending_chart_valid_png():
    items = [
        ("Groceries", 450.25),
        ("Dining Out", 310.50),
        ("Utilities", 180.00),
    ]
    png_bytes = render_category_spending_chart(items, title="Test Breakdown", period_label="Last 30 Days")
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")

    # Verify Pillow can open it as a valid image
    img = Image.open(BytesIO(png_bytes))
    assert img.format == "PNG"
    assert img.width == 750


def test_render_category_spending_chart_empty():
    png_bytes = render_category_spending_chart([], title="Empty Chart")
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_cash_flow_bar_chart_valid_png():
    daily_data = [
        {"date": "2026-09-01", "inflow": 2500.0, "outflow": 120.0},
        {"date": "2026-09-02", "inflow": 0.0, "outflow": 45.50},
        {"date": "2026-09-03", "inflow": 50.0, "outflow": 300.0},
    ]
    png_bytes = render_cash_flow_bar_chart(daily_data, title="Cash Flow", period_label="Sep 1 - Sep 3")
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")

    img = Image.open(BytesIO(png_bytes))
    assert img.format == "PNG"
    assert img.width == 800


def test_render_cash_flow_bar_chart_empty():
    png_bytes = render_cash_flow_bar_chart([], title="Empty Cash Flow")
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_net_worth_trend_chart_valid_png():
    snapshots = [
        ("2026-06-01", 50000.0),
        ("2026-07-01", 52300.0),
        ("2026-08-01", 51800.0),
        ("2026-09-01", 54500.0),
    ]
    png_bytes = render_net_worth_trend_chart(snapshots, title="Net Worth Growth")
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")

    img = Image.open(BytesIO(png_bytes))
    assert img.format == "PNG"
    assert img.width == 800


def test_render_net_worth_trend_chart_empty():
    png_bytes = render_net_worth_trend_chart([], title="Empty Net Worth")
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_spending_chart_for_user_isolation_and_query(test_db):
    user_a = "user_101"
    user_b = "user_102"

    c = test_db.cursor()
    # User A transactions
    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('t1', ?, 85.00, date('now', '-5 days'), 'Target', 'Target', 'Groceries', 'Evaluated'),
               ('t2', ?, 45.00, date('now', '-2 days'), 'Shell', 'Shell', 'Gas', 'Evaluated')
    """, (user_a, user_a))

    # User B transactions (should be strictly isolated)
    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('t3', ?, 999.00, date('now', '-1 days'), 'Rolex', 'Rolex', 'Luxury', 'Evaluated')
    """, (user_b,))
    test_db.commit()

    # Query User A
    png_a = generate_spending_chart_for_user(user_id=user_a, days=30, conn=test_db)
    assert png_a.startswith(b"\x89PNG\r\n\x1a\n")

    # Missing user_id isolation check
    with pytest.raises(ValueError, match="forced isolation violation"):
        generate_spending_chart_for_user(user_id="", days=30, conn=test_db)


def test_generate_cash_flow_chart_for_user(test_db):
    user_id = "user_202"
    c = test_db.cursor()
    c.execute("""
        INSERT INTO transactions (id, user_id, amount, date, name, merchant, category, status)
        VALUES ('t10', ?, -2000.0, date('now', '-3 days'), 'Payroll', 'Employer', 'Income', 'Evaluated'),
               ('t11', ?, 150.0, date('now', '-2 days'), 'Costco', 'Costco', 'Groceries', 'Evaluated')
    """, (user_id, user_id))
    test_db.commit()

    png_bytes = generate_cash_flow_chart_for_user(user_id=user_id, days=14, conn=test_db)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(ValueError, match="forced isolation violation"):
        generate_cash_flow_chart_for_user(user_id="", days=14, conn=test_db)


def test_generate_net_worth_chart_for_user(test_db):
    user_id = "user_303"
    c = test_db.cursor()
    c.execute("""
        INSERT INTO financial_snapshots (user_id, captured_at, net_worth, liquid_cash, total_debt, investments)
        VALUES (?, datetime('now', '-10 days'), 42000.0, 10000.0, 2000.0, 34000.0),
               (?, datetime('now', '-1 days'), 43500.0, 10500.0, 1500.0, 34500.0)
    """, (user_id, user_id))
    test_db.commit()

    png_bytes = generate_net_worth_chart_for_user(user_id=user_id, days=30, conn=test_db)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(ValueError, match="forced isolation violation"):
        generate_net_worth_chart_for_user(user_id="", days=30, conn=test_db)
