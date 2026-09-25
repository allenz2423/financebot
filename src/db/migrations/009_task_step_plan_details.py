"""Migration 009: persist plan text on the existing durable task steps."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(task_steps)").fetchall()
    }
    if "description" not in columns:
        conn.execute("ALTER TABLE task_steps ADD COLUMN description TEXT")
    if "completion_criteria" not in columns:
        conn.execute("ALTER TABLE task_steps ADD COLUMN completion_criteria TEXT")
    if "retry_count" not in columns:
        conn.execute(
            "ALTER TABLE task_steps ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0 "
            "CHECK (retry_count >= 0)"
        )
