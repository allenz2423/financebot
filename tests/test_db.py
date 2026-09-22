import sqlite3
import pytest
from src.core.state import DB_PATH, conn

def test_db_connection():
    assert isinstance(conn, sqlite3.Connection)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1

def test_db_transactions_table():
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
    row = cur.fetchone()
    assert row is not None


def test_plaid_accounts_ddl_has_single_user_id_column():
    """The plaid_accounts DDL must create exactly one user_id column.

    Regression: src/core/state.py shipped a duplicate `user_id TEXT,` line.
    SQLite rejects duplicate column names, so a fresh database boot crashed
    before table creation completed.
    """
    import re
    from pathlib import Path

    src = Path(__file__).parent.parent / "src/core/state.py"
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS plaid_accounts \(.*?\)",
        src.read_text(),
        re.S,
    )
    assert m is not None
    ddl = m.group(0)
    assert ddl.count("user_id TEXT") == 1

    fresh = sqlite3.connect(":memory:")
    fresh.execute(ddl)  # duplicate column name raises OperationalError
    cols = [r[1] for r in fresh.execute("PRAGMA table_info(plaid_accounts)")]
    assert cols.count("user_id") == 1
