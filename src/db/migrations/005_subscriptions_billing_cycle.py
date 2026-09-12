"""Migration 005: Subscriptions billing cycle and upcoming bill calendar schema.

Adds cadence, next_due_date, category, notes, and auto_renew to subscriptions table.
"""

from __future__ import annotations
import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        merchant        TEXT NOT NULL,
        last_amount     REAL DEFAULT 0.0,
        last_date       TEXT,
        status          TEXT DEFAULT 'Active',
        cadence         TEXT DEFAULT 'monthly',
        next_due_date   TEXT,
        category        TEXT DEFAULT 'Subscriptions',
        notes           TEXT,
        auto_renew      INTEGER DEFAULT 1,
        UNIQUE(user_id, merchant)
    )
    """)

    cursor = conn.execute("PRAGMA table_info(subscriptions)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    if "cadence" not in existing_cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN cadence TEXT DEFAULT 'monthly'")
    if "next_due_date" not in existing_cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN next_due_date TEXT")
    if "category" not in existing_cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN category TEXT DEFAULT 'Subscriptions'")
    if "notes" not in existing_cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN notes TEXT")
    if "auto_renew" not in existing_cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN auto_renew INTEGER DEFAULT 1")

    c.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions (user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_due ON subscriptions (user_id, next_due_date)")
    conn.commit()
