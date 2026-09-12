"""Migration 002: Portfolio holdings and asset allocation targets schema.

Enables tracking of investment holdings (shares, cost basis, market price, asset class)
and target asset allocation drift analysis.
"""

from __future__ import annotations
import sqlite3

def apply(conn: sqlite3.Connection) -> None:
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS investment_holdings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        shares          REAL NOT NULL,
        cost_basis      REAL DEFAULT 0.0,
        current_price   REAL DEFAULT 0.0,
        asset_class     TEXT DEFAULT 'Equities',
        updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, symbol)
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_holdings_user ON investment_holdings (user_id)")

    c.execute("""
    CREATE TABLE IF NOT EXISTS portfolio_targets (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        target_pct      REAL NOT NULL,
        updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, symbol)
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_targets_user ON portfolio_targets (user_id)")

    conn.commit()
