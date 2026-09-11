import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = "data/finances.db"

_thread_local = threading.local()

@contextmanager
def get_db():
    """Thread-safe connection manager for SQLite."""
    if not hasattr(_thread_local, "conn"):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _thread_local.conn = conn
    
    conn = _thread_local.conn
    # We yield the connection, and the caller is responsible for creating a cursor if needed
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
