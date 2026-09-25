"""Migration 006: bounded durable conversational session storage.

The session store is deliberately separate from the legacy ``chat_history``
table.  It gives new callers an explicit user/channel/thread scope and a
stable idempotency key without changing the running bot's existing history
contract.  All DDL is additive and safe to run repeatedly.

FTS5 is a derived index: if the SQLite build does not provide FTS5, the
canonical tables still install successfully and the store uses a parameterized
LIKE fallback.
"""

from __future__ import annotations

import sqlite3


def _create_fts(conn: sqlite3.Connection) -> None:
    """Best-effort FTS5 setup; canonical session data must not depend on it."""
    try:
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )"""
        )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF content ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )
        # A newly-created external-content index is empty.  REBUILD is safe on
        # repeat and also repairs an index left behind without its triggers.
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    except sqlite3.OperationalError as exc:
        if "fts5" not in str(exc).lower() and "virtual table" not in str(exc).lower():
            raise


def apply(conn: sqlite3.Connection) -> None:
    """Create the durable session schema.  The operation is idempotent."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT NOT NULL,
            session_key     TEXT NOT NULL,
            channel_id      TEXT NOT NULL DEFAULT '',
            thread_id       TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'completed', 'failed', 'cancelled', 'archived')),
            metadata_json   TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            last_activity_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (user_id, session_key, channel_id, thread_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user_scope
            ON sessions(user_id, channel_id, thread_id, updated_at DESC);

        CREATE TABLE IF NOT EXISTS turns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            turn_key        TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            sequence_no     INTEGER NOT NULL,
            status          TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
            metadata_json   TEXT NOT NULL DEFAULT '{}',
            error_text      TEXT,
            started_at      TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at    TEXT,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (session_id, turn_key),
            UNIQUE (session_id, idempotency_key),
            UNIQUE (session_id, sequence_no)
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session_sequence
            ON turns(session_id, sequence_no DESC);

        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            turn_id         INTEGER REFERENCES turns(id) ON DELETE CASCADE,
            message_key     TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            content_type    TEXT NOT NULL DEFAULT 'text',
            metadata_json   TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (session_id, message_key)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session_created
            ON messages(session_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_turn
            ON messages(turn_id, id);

        CREATE TABLE IF NOT EXISTS tool_calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            turn_id         INTEGER REFERENCES turns(id) ON DELETE CASCADE,
            call_key        TEXT NOT NULL,
            tool_name       TEXT NOT NULL,
            arguments_json  TEXT NOT NULL DEFAULT '{}',
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
            result_json     TEXT,
            error_text      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at    TEXT,
            UNIQUE (session_id, call_key)
        );

        CREATE INDEX IF NOT EXISTS idx_tool_calls_session_created
            ON tool_calls(session_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_turn
            ON tool_calls(turn_id, id);
        """
    )
    _create_fts(conn)
