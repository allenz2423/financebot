"""Bounded durable conversational session storage for Delilah.

This module is intentionally independent of ``src.core.state``.  Callers can
inject the bot's SQLite connection or provide a path, which keeps tests and
future workers from importing Discord/global state just to persist a turn.

The write path follows the useful Hermes Agent patterns found in
``hermes_state.py`` and ``hermes_state_search.py``: acquire the SQLite write
lock up front with ``BEGIN IMMEDIATE``, use explicit scope predicates, make
derived FTS state fail-open to canonical data, cap input before FTS parsing,
and escape LIKE wildcards in the fallback.  Financial mutations remain out of
scope: this store records conversation/tool activity and never authorizes or
executes a tool.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from src.db.migrations import apply_all


class SessionStoreError(RuntimeError):
    """Base error for the durable session store."""


class SessionNotFound(SessionStoreError):
    """Raised when a scoped session does not exist."""


class IdempotencyConflict(SessionStoreError):
    """Raised when an idempotency key is reused for different immutable data."""


class InvalidLifecycleTransition(SessionStoreError):
    """Raised when a terminal turn/tool call is changed to another state."""


_FTS_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TURN_TERMINAL = {"completed", "failed", "cancelled"}
_TOOL_TERMINAL = {"succeeded", "failed", "cancelled"}
_VALID_ROLES = {"system", "user", "assistant", "tool", "developer"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _required(value: Any, name: str) -> str:
    value = str(value).strip() if value is not None else ""
    if not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _scope_part(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _json(value: Any, *, default: Any) -> str:
    if value is None:
        value = default
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata/tool payload must be JSON serializable") from exc


def _decode(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SessionStore:
    """A thread-safe, bounded session/turn/message/tool-call repository.

    ``max_messages_per_session``, ``max_turns_per_session`` and
    ``max_tool_calls_per_session`` are retention bounds, not query limits.  A
    prune runs after each successful append, retaining the newest rows by
    SQLite insertion id.  Defaults are deliberately finite so a long-lived
    bot cannot grow one conversation without limit.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] | sqlite3.Connection | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        max_messages_per_session: int = 1000,
        max_turns_per_session: int = 250,
        max_tool_calls_per_session: int = 1000,
    ) -> None:
        if isinstance(db_path, sqlite3.Connection):
            if connection is not None:
                raise ValueError("provide either db_path or connection, not both")
            connection = db_path
            db_path = None
        if connection is not None and db_path is not None:
            raise ValueError("provide either db_path or connection, not both")
        for name, value in (
            ("max_messages_per_session", max_messages_per_session),
            ("max_turns_per_session", max_turns_per_session),
            ("max_tool_calls_per_session", max_tool_calls_per_session),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

        self.max_messages_per_session = max_messages_per_session
        self.max_turns_per_session = max_turns_per_session
        self.max_tool_calls_per_session = max_tool_calls_per_session
        self._lock = threading.RLock()
        self._owns_connection = connection is None
        if connection is None:
            selected_path = db_path or os.getenv("DELILAH_DB_PATH") or "data/finances.db"
            path = Path(selected_path)
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(path), check_same_thread=False, timeout=10.0, isolation_level=None
            )
            self.db_path = str(path)
        else:
            self.db_path = None
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        if self._owns_connection and self.db_path != ":memory:":
            # WAL is a safe default for the bot's concurrent readers/workers;
            # failure leaves SQLite's normal journal mode available.
            try:
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=NORMAL")
                self.connection.execute("PRAGMA journal_size_limit=67108864")
            except sqlite3.OperationalError:
                pass
        apply_all(self.connection)
        self.connection.commit()
        self._fts_enabled = self._has_table("messages_fts")

    def close(self) -> None:
        if self._owns_connection and self.connection is not None:
            self.connection.close()
            self.connection = None  # type: ignore[assignment]

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _has_table(self, name: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE (type='table' OR type='view') AND name=?", (name,)
        ).fetchone()
        return row is not None

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Serialize the local writer and acquire SQLite's writer lock up front."""
        with self._lock:
            conn = self.connection
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _scope(user_id: Any, session_id: Any, channel_id: Any, thread_id: Any) -> tuple[str, str, str, str]:
        return (
            _required(user_id, "user_id"),
            _required(session_id, "session_id"),
            _scope_part(channel_id),
            _scope_part(thread_id),
        )

    def _session_row(self, conn: sqlite3.Connection, scope: tuple[str, str, str, str]) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM sessions
               WHERE user_id=? AND session_key=? AND channel_id=? AND thread_id=?""", scope
        ).fetchone()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for column in ("metadata_json", "arguments_json", "result_json"):
            if column in item:
                item[column.removesuffix("_json")] = _decode(item.pop(column), {} if column != "result_json" else None)
        return item

    def _ensure_session(
        self, conn: sqlite3.Connection, scope: tuple[str, str, str, str], metadata: Mapping[str, Any] | None
    ) -> sqlite3.Row:
        now = _now()
        conn.execute(
            """INSERT INTO sessions(user_id, session_key, channel_id, thread_id, metadata_json,
                                     created_at, updated_at, last_activity_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, session_key, channel_id, thread_id) DO UPDATE SET
                   updated_at=excluded.updated_at, last_activity_at=excluded.last_activity_at""",
            (*scope, _json(metadata, default={}), now, now, now),
        )
        return self._session_row(conn, scope)  # type: ignore[return-value]

    def get_or_create_session(
        self, user_id: str, session_id: str, *, channel_id: str | None = None,
        thread_id: str | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        with self._write() as conn:
            return self._row(self._ensure_session(conn, scope, metadata))  # type: ignore[return-value]

    def get_session(
        self, user_id: str, session_id: str, *, channel_id: str | None = None, thread_id: str | None = None
    ) -> dict[str, Any] | None:
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        with self._lock:
            return self._row(self._session_row(self.connection, scope))

    def _resolve_session_id(self, conn: sqlite3.Connection, scope: tuple[str, str, str, str]) -> int:
        row = self._session_row(conn, scope)
        if row is None:
            raise SessionNotFound(f"session is not present in the requested user/channel/thread scope")
        return int(row["id"])

    def begin_turn(
        self, user_id: str, session_id: str, *, turn_id: str | None = None,
        idempotency_key: str | None = None, channel_id: str | None = None,
        thread_id: str | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        turn_key = _required(turn_id or str(uuid.uuid4()), "turn_id")
        idem = _required(idempotency_key or turn_key, "idempotency_key")
        with self._write() as conn:
            session_pk = int(self._ensure_session(conn, scope, None)["id"])
            existing = conn.execute(
                "SELECT * FROM turns WHERE session_id=? AND (turn_key=? OR idempotency_key=?)",
                (session_pk, turn_key, idem),
            ).fetchone()
            if existing is not None:
                if existing["turn_key"] != turn_key and existing["idempotency_key"] != idem:
                    raise IdempotencyConflict("turn idempotency key is already used by another turn")
                return self._row(existing)  # type: ignore[return-value]
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM turns WHERE session_id=?", (session_pk,)
            ).fetchone()[0]
            now = _now()
            try:
                conn.execute(
                    """INSERT INTO turns(session_id, turn_key, idempotency_key, sequence_no, metadata_json,
                                         started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (session_pk, turn_key, idem, sequence, _json(metadata, default={}), now, now),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM turns WHERE session_id=? AND (turn_key=? OR idempotency_key=?)",
                    (session_pk, turn_key, idem),
                ).fetchone()
                if existing is None:
                    raise
                return self._row(existing)  # type: ignore[return-value]
            conn.execute("UPDATE sessions SET updated_at=?, last_activity_at=? WHERE id=?", (now, now, session_pk))
            self._prune(conn, session_pk)
            return self._row(conn.execute("SELECT * FROM turns WHERE session_id=? AND turn_key=?", (session_pk, turn_key)).fetchone())  # type: ignore[return-value]

    def finish_turn(
        self, user_id: str, session_id: str, turn_id: str, *, status: str = "completed",
        error: str | None = None, channel_id: str | None = None, thread_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in _TURN_TERMINAL:
            raise ValueError(f"status must be one of {sorted(_TURN_TERMINAL)}")
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        turn_key = _required(turn_id, "turn_id")
        with self._write() as conn:
            session_pk = self._resolve_session_id(conn, scope)
            row = conn.execute("SELECT * FROM turns WHERE session_id=? AND turn_key=?", (session_pk, turn_key)).fetchone()
            if row is None:
                raise SessionNotFound("turn is not present in the requested scope")
            if row["status"] in _TURN_TERMINAL and row["status"] != status:
                raise InvalidLifecycleTransition(f"turn is already {row['status']}")
            now = _now()
            conn.execute(
                "UPDATE turns SET status=?, error_text=?, completed_at=?, updated_at=? WHERE id=?",
                (status, error, row["completed_at"] or now, now, row["id"]),
            )
            conn.execute("UPDATE sessions SET updated_at=?, last_activity_at=? WHERE id=?", (now, now, session_pk))
            return self._row(conn.execute("SELECT * FROM turns WHERE id=?", (row["id"],)).fetchone())  # type: ignore[return-value]

    complete_turn = finish_turn

    def add_message(
        self, user_id: str, session_id: str, *, role: str, content: str, message_id: str | None = None,
        turn_id: str | None = None, content_type: str = "text", metadata: Mapping[str, Any] | None = None,
        channel_id: str | None = None, thread_id: str | None = None,
    ) -> dict[str, Any]:
        role = _required(role, "role").lower()
        if role not in _VALID_ROLES:
            raise ValueError(f"unsupported message role: {role}")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        key = _required(message_id or str(uuid.uuid4()), "message_id")
        with self._write() as conn:
            session_pk = int(self._ensure_session(conn, scope, None)["id"])
            turn_pk = None
            if turn_id is not None:
                turn = conn.execute("SELECT id FROM turns WHERE session_id=? AND turn_key=?", (session_pk, _required(turn_id, "turn_id"))).fetchone()
                if turn is None:
                    raise SessionNotFound("turn is not present in the requested scope")
                turn_pk = turn["id"]
            existing = conn.execute("SELECT * FROM messages WHERE session_id=? AND message_key=?", (session_pk, key)).fetchone()
            if existing is not None:
                if existing["role"] != role or existing["content"] != content:
                    raise IdempotencyConflict("message idempotency key is already used for different content")
                return self._row(existing)  # type: ignore[return-value]
            try:
                conn.execute(
                    """INSERT INTO messages(session_id, turn_id, message_key, role, content, content_type,
                                             metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (session_pk, turn_pk, key, role, content, _required(content_type, "content_type"), _json(metadata, default={})),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute("SELECT * FROM messages WHERE session_id=? AND message_key=?", (session_pk, key)).fetchone()
                if existing is None:
                    raise
                if existing["role"] != role or existing["content"] != content:
                    raise IdempotencyConflict("message idempotency key is already used for different content")
                return self._row(existing)  # type: ignore[return-value]
            now = _now()
            conn.execute("UPDATE sessions SET updated_at=?, last_activity_at=? WHERE id=?", (now, now, session_pk))
            self._prune(conn, session_pk)
            return self._row(conn.execute("SELECT * FROM messages WHERE session_id=? AND message_key=?", (session_pk, key)).fetchone())  # type: ignore[return-value]

    def record_tool_call(
        self, user_id: str, session_id: str, *, tool_name: str, arguments: Any = None,
        call_id: str | None = None, turn_id: str | None = None, status: str = "pending",
        result: Any = None, error: str | None = None, channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"pending", "running", *(_TOOL_TERMINAL)}:
            raise ValueError("invalid tool-call status")
        name = _required(tool_name, "tool_name")
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        key = _required(call_id or str(uuid.uuid4()), "call_id")
        args_json = _json(arguments, default={})
        with self._write() as conn:
            session_pk = int(self._ensure_session(conn, scope, None)["id"])
            turn_pk = None
            if turn_id is not None:
                turn = conn.execute("SELECT id FROM turns WHERE session_id=? AND turn_key=?", (session_pk, _required(turn_id, "turn_id"))).fetchone()
                if turn is None:
                    raise SessionNotFound("turn is not present in the requested scope")
                turn_pk = turn["id"]
            existing = conn.execute("SELECT * FROM tool_calls WHERE session_id=? AND call_key=?", (session_pk, key)).fetchone()
            if existing is not None:
                if existing["tool_name"] != name or existing["arguments_json"] != args_json:
                    raise IdempotencyConflict("tool-call idempotency key is already used for different arguments")
                return self._row(existing)  # type: ignore[return-value]
            now = _now()
            completed = now if status in _TOOL_TERMINAL else None
            try:
                conn.execute(
                    """INSERT INTO tool_calls(session_id, turn_id, call_key, tool_name, arguments_json,
                                               status, result_json, error_text, created_at, updated_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_pk, turn_pk, key, name, args_json, status, _json(result, default=None) if result is not None else None, error, now, now, completed),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute("SELECT * FROM tool_calls WHERE session_id=? AND call_key=?", (session_pk, key)).fetchone()
                if existing is None:
                    raise
                if existing["tool_name"] != name or existing["arguments_json"] != args_json:
                    raise IdempotencyConflict("tool-call idempotency key is already used for different arguments")
                return self._row(existing)  # type: ignore[return-value]
            conn.execute("UPDATE sessions SET updated_at=?, last_activity_at=? WHERE id=?", (now, now, session_pk))
            self._prune(conn, session_pk)
            return self._row(conn.execute("SELECT * FROM tool_calls WHERE session_id=? AND call_key=?", (session_pk, key)).fetchone())  # type: ignore[return-value]

    def finish_tool_call(
        self, user_id: str, session_id: str, call_id: str, *, status: str = "succeeded", result: Any = None,
        error: str | None = None, channel_id: str | None = None, thread_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in _TOOL_TERMINAL:
            raise ValueError(f"status must be one of {sorted(_TOOL_TERMINAL)}")
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        key = _required(call_id, "call_id")
        with self._write() as conn:
            session_pk = self._resolve_session_id(conn, scope)
            row = conn.execute("SELECT * FROM tool_calls WHERE session_id=? AND call_key=?", (session_pk, key)).fetchone()
            if row is None:
                raise SessionNotFound("tool call is not present in the requested scope")
            if row["status"] in _TOOL_TERMINAL and row["status"] != status:
                raise InvalidLifecycleTransition(f"tool call is already {row['status']}")
            now = _now()
            result_json = _json(result, default=None) if result is not None else row["result_json"]
            conn.execute(
                "UPDATE tool_calls SET status=?, result_json=?, error_text=?, completed_at=?, updated_at=? WHERE id=?",
                (status, result_json, error, row["completed_at"] or now, now, row["id"]),
            )
            conn.execute("UPDATE sessions SET updated_at=?, last_activity_at=? WHERE id=?", (now, now, session_pk))
            return self._row(conn.execute("SELECT * FROM tool_calls WHERE id=?", (row["id"],)).fetchone())  # type: ignore[return-value]

    complete_tool_call = finish_tool_call

    def _prune(self, conn: sqlite3.Connection, session_pk: int) -> None:
        # Retention is explicit and bounded.  Foreign-key cascades remove
        # dependent messages/tool calls when old turns leave the window.
        conn.execute(
            """DELETE FROM turns WHERE session_id=? AND status IN ('completed','failed','cancelled')
               AND id NOT IN (SELECT id FROM turns WHERE session_id=? ORDER BY id DESC LIMIT ?)""",
            (session_pk, session_pk, self.max_turns_per_session),
        )
        conn.execute(
            """DELETE FROM messages WHERE session_id=? AND id NOT IN
               (SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?)""",
            (session_pk, session_pk, self.max_messages_per_session),
        )
        conn.execute(
            """DELETE FROM tool_calls WHERE session_id=? AND id NOT IN
               (SELECT id FROM tool_calls WHERE session_id=? ORDER BY id DESC LIMIT ?)""",
            (session_pk, session_pk, self.max_tool_calls_per_session),
        )

    def list_messages(
        self, user_id: str, session_id: str, *, channel_id: str | None = None, thread_id: str | None = None,
        limit: int = 100, before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        limit = max(1, min(int(limit), self.max_messages_per_session))
        with self._lock:
            session_pk = self._resolve_session_id(self.connection, scope)
            params: list[Any] = [session_pk]
            where = "session_id=?"
            if before_id is not None:
                where += " AND id < ?"
                params.append(int(before_id))
            params.append(limit)
            rows = self.connection.execute(f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?", params).fetchall()
            return [self._row(row) for row in reversed(rows)]  # type: ignore[misc]

    @staticmethod
    def _safe_fts_query(query: str) -> str:
        # Hermes quotes user terms before MATCH.  This stricter variant treats
        # every token literally, so operators, column selectors and wildcards
        # cannot alter the query grammar.
        query = str(query or "")[:512]
        tokens = _FTS_TOKEN_RE.findall(query)
        return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:64])

    def search_messages(
        self, user_id: str, query: str, *, session_id: str | None = None,
        channel_id: str | None = None, thread_id: str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        user = _required(user_id, "user_id")
        limit = max(1, min(int(limit), self.max_messages_per_session))
        fts_query = self._safe_fts_query(query)
        if not fts_query:
            return []
        where = ["s.user_id=?"]
        params: list[Any] = [user]
        if session_id is not None:
            where.append("s.session_key=?")
            params.append(_required(session_id, "session_id"))
        if channel_id is not None:
            where.append("s.channel_id=?")
            params.append(_scope_part(channel_id))
        if thread_id is not None:
            where.append("s.thread_id=?")
            params.append(_scope_part(thread_id))
        with self._lock:
            if self._fts_enabled:
                try:
                    rows = self.connection.execute(
                        f"""SELECT m.*, s.user_id, s.session_key, s.channel_id, s.thread_id
                            FROM messages_fts f JOIN messages m ON m.id=f.rowid
                            JOIN sessions s ON s.id=m.session_id
                            WHERE f.messages_fts MATCH ? AND {' AND '.join(where)}
                            ORDER BY m.id DESC LIMIT ?""",
                        [fts_query, *params, limit],
                    ).fetchall()
                    return [self._row(row) for row in rows]  # type: ignore[misc]
                except sqlite3.DatabaseError:
                    # Derived state may be unavailable/corrupt; canonical
                    # rows remain authoritative and safe to search.
                    self._fts_enabled = False
            literal = _escape_like(str(query or "")[:512])
            rows = self.connection.execute(
                f"""SELECT m.*, s.user_id, s.session_key, s.channel_id, s.thread_id
                    FROM messages m JOIN sessions s ON s.id=m.session_id
                    WHERE {' AND '.join(where)} AND m.content LIKE ? ESCAPE '\\'
                    ORDER BY m.id DESC LIMIT ?""",
                [*params, f"%{literal}%", limit],
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[misc]


__all__ = [
    "IdempotencyConflict", "InvalidLifecycleTransition", "SessionNotFound", "SessionStore", "SessionStoreError",
]
