"""Migration 004: Sinking funds goal milestones and target deadline schema.

Adds target_date, category, and timestamps to savings_buckets to support
deadline-aware savings schedules, required monthly deposits, and deficit tracking.
"""

from __future__ import annotations
import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS savings_buckets (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        name            TEXT NOT NULL,
        target_amount   REAL NOT NULL DEFAULT 0.0,
        current_amount  REAL NOT NULL DEFAULT 0.0,
        category        TEXT DEFAULT 'General',
        target_date     TEXT,
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(user_id, name)
    )
    """)

    # Ensure columns exist if table was previously created with older schema
    cursor = conn.execute("PRAGMA table_info(savings_buckets)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "target_date" not in existing_cols:
        conn.execute("ALTER TABLE savings_buckets ADD COLUMN target_date TEXT")
    if "category" not in existing_cols:
        conn.execute("ALTER TABLE savings_buckets ADD COLUMN category TEXT DEFAULT 'General'")
    if "created_at" not in existing_cols:
        conn.execute("ALTER TABLE savings_buckets ADD COLUMN created_at TEXT")
    if "updated_at" not in existing_cols:
        conn.execute("ALTER TABLE savings_buckets ADD COLUMN updated_at TEXT")

    # Ensure bucket_contributions exists
    c.execute("""
    CREATE TABLE IF NOT EXISTS bucket_contributions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        bucket_name     TEXT NOT NULL,
        amount          REAL NOT NULL,
        source          TEXT DEFAULT 'manual',
        transaction_id  INTEGER,
        created_at      TEXT DEFAULT (datetime('now'))
    )
    """)

    c.execute("CREATE INDEX IF NOT EXISTS idx_savings_buckets_user ON savings_buckets (user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bucket_contrib_user ON bucket_contributions (user_id, bucket_name)")

    conn.commit()
