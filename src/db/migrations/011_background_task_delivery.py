"""Make background completion-notice claims unique per durable task."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_task_background_notice_claimed_once
           ON task_events(task_id)
           WHERE event_type='task.background_completion_notice_claimed'"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_task_background_notice_started_once
           ON task_events(task_id)
           WHERE event_type='task.background_completion_notice_started'"""
    )
