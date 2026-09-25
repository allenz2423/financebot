"""Make the proactive reconciliation notice a once-per-task event."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_task_reconciliation_surfaced_once
           ON task_events(task_id) WHERE event_type='task.reconciliation_surfaced'"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_task_reconciliation_notice_claimed_once
           ON task_events(task_id) WHERE event_type='task.reconciliation_notice_claimed'"""
    )
