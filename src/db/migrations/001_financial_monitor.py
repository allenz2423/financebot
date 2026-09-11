"""Migration 001: Persistent financial monitor schema.

Adds tables that let Delilah run scheduled, event-driven financial
monitoring *without* the LLM.  A monitor rule is a small deterministic
predicate over the ledger.  When the predicate fires, an alert row is
written and optionally pushed to Discord / ntfy.

This migration is backwards compatible:
  * All tables use CREATE TABLE IF NOT EXISTS.
  * All new columns use ALTER TABLE ... ADD COLUMN with
    "duplicate column name" ignored.
  * No existing table is dropped, truncated, or rewritten.
  * Existing rows are never touched.

The migration is idempotent and safe to run repeatedly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """Add a column if it does not already exist."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def apply(conn: sqlite3.Connection) -> None:
    """Apply migration 001. Idempotent."""
    c = conn.cursor()

    # ---------------------------------------------------------------
    # Monitor rules
    #
    # A rule is a declarative predicate.  `kind` selects the evaluator;
    # `config` is a JSON object holding the parameters.  Rules are
    # owned by a user_id and can be enabled/disabled without deletion.
    # ---------------------------------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS monitor_rules (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        name            TEXT NOT NULL,
        kind            TEXT NOT NULL,
        config          TEXT NOT NULL DEFAULT '{}',
        enabled         INTEGER DEFAULT 1,
        severity        TEXT DEFAULT 'info',
        cooldown_hours  INTEGER DEFAULT 24,
        last_fired_at   TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_rules_user ON monitor_rules(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_rules_kind ON monitor_rules(kind)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_rules_enabled ON monitor_rules(enabled)")

    # ---------------------------------------------------------------
    # Monitor alerts
    #
    # Every time a rule fires, one row is inserted here.  Alerts are
    # never deleted by the monitor engine; they are pruned only by an
    # explicit admin command, so the audit trail is durable.
    # ---------------------------------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS monitor_alerts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        rule_id         INTEGER NOT NULL,
        rule_kind       TEXT NOT NULL,
        severity        TEXT NOT NULL,
        title           TEXT NOT NULL,
        detail          TEXT NOT NULL,
        fired_at        TEXT NOT NULL DEFAULT (datetime('now')),
        delivered_discord INTEGER DEFAULT 0,
        delivered_ntfy   INTEGER DEFAULT 0,
        acked           INTEGER DEFAULT 0,
        acked_at        TEXT,
        FOREIGN KEY (rule_id) REFERENCES monitor_rules(id) ON DELETE CASCADE
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_alerts_user ON monitor_alerts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_alerts_rule ON monitor_alerts(rule_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_alerts_fired ON monitor_alerts(fired_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_alerts_acked ON monitor_alerts(acked)")

    # ---------------------------------------------------------------
    # Monitor run log
    #
    # One row per evaluation pass so the engine is observable even
    # when no rules fire.
    # ---------------------------------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS monitor_run_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        started_at      TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at     TEXT,
        rules_evaluated INTEGER DEFAULT 0,
        rules_fired     INTEGER DEFAULT 0,
        error           TEXT
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_run_log_user ON monitor_run_log(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_monitor_run_log_started ON monitor_run_log(started_at)")

    conn.commit()


__all__ = ["apply"]