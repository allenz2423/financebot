import os
import asyncio
import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import plaid_sync


def test_format_net_cash():
    assert plaid_sync._format_net_cash(150.50) == "+$150.50"
    assert plaid_sync._format_net_cash(-45.20) == "-$45.20"
    assert plaid_sync._format_net_cash(0.0) == "+$0.00"


def test_compute_checking_position():
    sample_accounts = [
        {
            "account_id": "acc_1",
            "name": "Main Checking",
            "type": "depository",
            "subtype": "checking",
            "balances": {"available": 1500.0, "current": 1600.0}
        },
        {
            "account_id": "acc_2",
            "name": "Savings",
            "type": "depository",
            "subtype": "savings",
            "balances": {"available": 5000.0, "current": 5000.0}
        },
        {
            "account_id": "acc_3",
            "name": "Sapphire Reserve",
            "type": "credit",
            "subtype": "credit card",
            "balances": {"current": 450.0, "limit": 10000.0}
        }
    ]

    pos = plaid_sync._compute_checking_position(sample_accounts)
    assert pos["checking_balance"] == 1500.0
    assert pos["total_credit_debt"] == 450.0
    assert pos["net_cash"] == 1050.0
    assert len(pos["credit_cards"]) == 1
    assert pos["credit_cards"][0]["name"] == "Sapphire Reserve"


def test_cursor_table_and_resets_cursors(tmp_path):
    db_file = tmp_path / "test_plaid.db"
    conn = sqlite3.connect(str(db_file))

    # 1. Initialize tables
    plaid_sync._ensure_cursor_table(conn)

    # 2. Set cursors for two users
    plaid_sync._set_cursor(conn, "user_A", "TOKEN_1", "cursor_val_1")
    plaid_sync._set_cursor(conn, "user_B", "TOKEN_2", "cursor_val_2")

    assert plaid_sync._get_cursor(conn, "user_A", "TOKEN_1") == "cursor_val_1"
    assert plaid_sync._get_cursor(conn, "user_B", "TOKEN_2") == "cursor_val_2"

    # 3. Reset user_A cursor only
    plaid_sync.resets_cursors(conn, user_id="user_A")
    assert plaid_sync._get_cursor(conn, "user_A", "TOKEN_1") is None
    assert plaid_sync._get_cursor(conn, "user_B", "TOKEN_2") == "cursor_val_2"

    # 4. Reset globally
    plaid_sync.reset_cursors(conn)
    assert plaid_sync._get_cursor(conn, "user_B", "TOKEN_2") is None
    conn.close()


def test_discover_polling_targets(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Primary DB
    primary_db = data_dir / "finances.db"
    conn = sqlite3.connect(str(primary_db))
    cur = conn.cursor()
    cur.execute("CREATE TABLE user_info (user_id TEXT, key TEXT, value TEXT)")
    cur.execute("INSERT INTO user_info VALUES ('user_primary_1', 'plaid_access_tokens', 'token_123')")
    cur.execute("CREATE TABLE plaid_accounts (user_id TEXT)")
    cur.execute("INSERT INTO plaid_accounts VALUES ('user_primary_2')")
    cur.execute("CREATE TABLE transactions (user_id TEXT)")
    conn.commit()
    conn.close()

    # Partitioned DBs
    part1 = data_dir / "finances_partitioned_user_99.db"
    conn_p = sqlite3.connect(str(part1))
    conn_p.close()

    targets = plaid_sync.discover_polling_targets(
        base_dir=str(data_dir),
        default_db=str(primary_db),
    )

    expected_pairs = {
        (str(primary_db), "user_primary_1"),
        (str(primary_db), "user_primary_2"),
        (str(part1), "partitioned_user_99"),
    }
    assert set(targets) == expected_pairs


def test_transaction_removal_and_modification(tmp_path):
    db_file = tmp_path / "test_ops.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE transactions (
            transaction_id TEXT,
            pending_transaction_id TEXT,
            date TEXT,
            merchant TEXT,
            amount REAL,
            override_amount REAL,
            status TEXT
        )
    """)
    cur.execute("""
        INSERT INTO transactions (transaction_id, date, merchant, amount, status)
        VALUES ('tx_100', '2026-09-01', 'Old Merchant', 25.0, 'Evaluated')
    """)
    conn.commit()

    # Test modification
    asyncio.run(plaid_sync._handle_modified_transaction(
        conn,
        asyncio.Queue(),
        {"transaction_id": "tx_100", "date": "2026-09-02", "name": "New Merchant", "amount": 30.0, "pending": False},
        user_id="user_1",
    ))
    cur.execute("SELECT merchant, amount, date FROM transactions WHERE transaction_id = 'tx_100'")
    row = cur.fetchone()
    assert row[0] == "New Merchant"
    assert row[1] == 30.0
    assert row[2] == "2026-09-02"

    # Test removal
    plaid_sync._handle_removed_transaction(conn, {"transaction_id": "tx_100"})
    cur.execute("SELECT status FROM transactions WHERE transaction_id = 'tx_100'")
    assert cur.fetchone()[0] == "Removed"
    conn.close()
