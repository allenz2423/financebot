"""Migration 007: durable, owner-scoped task runs, steps, and events.

Task/event history is append-only in this first slice. Foreign-key deletion is
restricted rather than cascading through immutable evidence; a retention or
privacy-purge policy must be designed explicitly before adding deletion APIs.
Turns and tool-call rows linked to durable steps may exceed SessionStore's
ordinary per-session retention caps so those evidence references are not
silently lost.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """Install additive task persistence without changing tool execution."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_runs (
            task_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
            parent_task_id TEXT REFERENCES task_runs(task_id) ON DELETE RESTRICT,
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'waiting_user',
                    'waiting_approval', 'verifying', 'needs_reconciliation',
                    'succeeded', 'partial', 'failed', 'cancelled')),
            lane TEXT NOT NULL DEFAULT 'interactive'
                CHECK (lane IN ('interactive', 'background')),
            enqueued_at TEXT NOT NULL,
            current_step_id TEXT,
            wait_reason TEXT,
            cancellation_requested INTEGER NOT NULL DEFAULT 0
                CHECK (cancellation_requested IN (0, 1)),
            result_ref TEXT,
            version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_runs_owner_status_queue
            ON task_runs(user_id, status, lane, enqueued_at, task_id);
        CREATE INDEX IF NOT EXISTS idx_task_runs_session
            ON task_runs(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_task_runs_parent
            ON task_runs(parent_task_id);

        CREATE TABLE IF NOT EXISTS task_steps (
            step_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES task_runs(task_id) ON DELETE RESTRICT,
            step_order INTEGER NOT NULL CHECK (step_order >= 0),
            dependency_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'ready', 'running', 'waiting_user',
                    'waiting_approval', 'verifying', 'needs_reconciliation',
                    'succeeded', 'partial', 'failed', 'cancelled', 'skipped')),
            next_action TEXT,
            retry_policy_json TEXT NOT NULL DEFAULT '{}',
            tool_call_id INTEGER REFERENCES tool_calls(id) ON DELETE RESTRICT,
            -- ReceiptStore owns this dynamically installed table; SessionStore
            -- validates owner/call identity before storing this reference.
            receipt_id TEXT,
            version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (task_id, step_order)
        );
        CREATE INDEX IF NOT EXISTS idx_task_steps_task_order
            ON task_steps(task_id, step_order);
        CREATE INDEX IF NOT EXISTS idx_task_steps_receipt
            ON task_steps(receipt_id);

        CREATE TABLE IF NOT EXISTS task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES task_runs(task_id) ON DELETE RESTRICT,
            step_id TEXT REFERENCES task_steps(step_id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_events_task
            ON task_events(task_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_task_events_step
            ON task_events(step_id, event_id);

        CREATE TRIGGER IF NOT EXISTS task_events_no_update
        BEFORE UPDATE ON task_events BEGIN
            SELECT RAISE(ABORT, 'task_events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS task_events_no_delete
        BEFORE DELETE ON task_events BEGIN
            SELECT RAISE(ABORT, 'task_events are append-only');
        END;
        """
    )
