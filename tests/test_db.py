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
