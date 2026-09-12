"""Migration 003: Category budgets and monthly spending pacing schema.

Enables tracking of category-specific monthly budget limits and burn-rate pacing.
"""

from __future__ import annotations
import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS category_budgets (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        category        TEXT NOT NULL,
        monthly_limit   REAL NOT NULL,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, category)
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cat_budgets_user ON category_budgets (user_id)")

    conn.commit()
