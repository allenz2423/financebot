"""Migration 010: persist coarse turn-controller phases on task runs."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
    }
    if "phase" not in columns:
        conn.execute(
            "ALTER TABLE task_runs ADD COLUMN phase TEXT NOT NULL DEFAULT 'received' "
            "CHECK (phase IN ('received', 'planning', 'executing', 'awaiting_child', "
            "'awaiting_user', 'verifying', 'delivering', 'completed', 'partial', 'failed'))"
        )
    if "phase_next_action" not in columns:
        conn.execute("ALTER TABLE task_runs ADD COLUMN phase_next_action TEXT")
    # Existing records predate the phase column. Backfill from their already
    # durable lifecycle state so restart/resume does not treat a running task
    # as a brand-new received turn.
    conn.execute(
        """UPDATE task_runs SET phase=CASE status
               WHEN 'running' THEN 'executing'
               WHEN 'waiting_user' THEN 'awaiting_user'
               WHEN 'waiting_approval' THEN 'awaiting_user'
               WHEN 'verifying' THEN 'verifying'
               WHEN 'needs_reconciliation' THEN 'partial'
               WHEN 'succeeded' THEN 'completed'
               WHEN 'partial' THEN 'partial'
               WHEN 'failed' THEN 'failed'
               WHEN 'cancelled' THEN 'failed'
               ELSE 'planning'
           END
           WHERE phase='received'"""
    )
