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
from typing import Any, Iterator, Mapping, Sequence

from src.db.migrations import apply_all


class SessionStoreError(RuntimeError):
    """Base error for the durable session store."""


class SessionNotFound(SessionStoreError):
    """Raised when a scoped session does not exist."""


class IdempotencyConflict(SessionStoreError):
    """Raised when an idempotency key is reused for different immutable data."""


class InvalidLifecycleTransition(SessionStoreError):
    """Raised when a terminal turn/tool call is changed to another state."""


class TaskNotFound(SessionStoreError):
    """A task or task step is absent from the requested owner scope."""


class ConcurrentTaskUpdate(SessionStoreError):
    """A task or step changed after the caller's expected version was read."""


_FTS_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TURN_TERMINAL = {"completed", "failed", "cancelled"}
_TOOL_TERMINAL = {"succeeded", "failed", "cancelled"}
_VALID_ROLES = {"system", "user", "assistant", "tool", "developer"}
_TASK_STATUSES = {
    "queued", "running", "waiting_user", "waiting_approval", "verifying",
    "needs_reconciliation", "succeeded", "partial", "failed", "cancelled",
}
_TASK_STEP_STATUSES = _TASK_STATUSES | {"pending", "ready", "skipped"}
_TASK_LANES = {"interactive", "background"}
_ALLOWED_TASK_EVENT_KEYS = {
    "status", "lane", "from", "to", "version", "expectedversion",
    "steporder", "dependencyids", "nextaction", "reasoncode", "waitreason",
    "questionid", "question", "allowedanswers", "expiresat", "approvalid",
    "inputonly",
    "actionfingerprint", "toolcallid", "receiptid", "resultref", "evidenceref",
    "sourcemessageid", "steeredstepid", "correction", "type", "format",
    "enum", "required",
    "toolname", "operation", "risk", "summary", "approvalexpiresat",
    "approvalstatus", "approvedby", "decidedat",
}
_UNSET = object()


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


def _task_event_payload(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Keep task events to task metadata and evidence references, not execution data."""
    payload = {} if value is None else value
    if not isinstance(payload, Mapping):
        raise TypeError("task event payload must be a mapping")

    def inspect(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = re.sub(r"[^a-z]", "", str(key).casefold())
                if normalized not in _ALLOWED_TASK_EVENT_KEYS:
                    raise ValueError(
                        f"unsupported task event payload field {key!r}"
                    )
                inspect(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                inspect(nested)

    inspect(payload)
    return payload


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
        for column in (
            "metadata_json", "arguments_json", "result_json",
            "dependency_ids_json", "retry_policy_json", "payload_json",
        ):
            if column in item:
                default = (
                    None if column == "result_json"
                    else [] if column == "dependency_ids_json"
                    else {}
                )
                item[column.removesuffix("_json")] = _decode(item.pop(column), default)
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

    def mark_turn_recall_excluded(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist that this turn contains data excluded from later recall."""
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        key = _required(turn_id, "turn_id")
        now = _now()
        with self._write() as conn:
            session_pk = self._resolve_session_id(conn, scope)
            row = conn.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_key=?",
                (session_pk, key),
            ).fetchone()
            if row is None:
                raise SessionNotFound("turn is not present in the requested scope")
            metadata = _decode(row["metadata_json"], {})
            metadata["session_recall_excluded"] = True
            conn.execute(
                "UPDATE turns SET metadata_json=?, updated_at=? WHERE id=?",
                (_json(metadata, default={}), now, row["id"]),
            )
            return self._row(conn.execute(
                "SELECT * FROM turns WHERE id=?", (row["id"],)
            ).fetchone())  # type: ignore[return-value]

    def list_turns(
        self,
        user_id: str,
        session_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        bounded = max(1, min(int(limit), self.max_turns_per_session))
        with self._lock:
            session_pk = self._resolve_session_id(self.connection, scope)
            rows = self.connection.execute(
                """SELECT * FROM turns WHERE session_id=?
                   ORDER BY sequence_no DESC LIMIT ?""",
                (session_pk, bounded),
            ).fetchall()
            return [self._row(row) for row in reversed(rows)]  # type: ignore[misc]

    def tool_call_turn_keys_for_retained_messages(
        self,
        user_id: str,
        session_id: str,
        *,
        tool_names: Sequence[str],
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> tuple[list[str], bool]:
        """Return matching call turn keys relevant to retained messages.

        The boolean reports a matching call with no turn link. Its associated
        message cannot be identified safely, so callers should fail closed.
        """
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        names = tuple(dict.fromkeys(_required(name, "tool_name") for name in tool_names))
        if not names:
            return [], False
        placeholders = ",".join("?" for _ in names)
        with self._lock:
            session_pk = self._resolve_session_id(self.connection, scope)
            unlinked = self.connection.execute(
                f"""SELECT 1 FROM tool_calls
                    WHERE session_id=? AND turn_id IS NULL
                      AND tool_name IN ({placeholders}) LIMIT 1""",
                (session_pk, *names),
            ).fetchone() is not None
            rows = self.connection.execute(
                f"""SELECT DISTINCT t.turn_key, t.sequence_no
                    FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id
                    WHERE tc.session_id=? AND tc.tool_name IN ({placeholders})
                      AND EXISTS (
                          SELECT 1 FROM messages m
                          WHERE m.session_id=tc.session_id AND m.turn_id=tc.turn_id
                      )
                    ORDER BY t.sequence_no DESC LIMIT ?""",
                (session_pk, *names, self.max_messages_per_session),
            ).fetchall()
            return [str(row["turn_key"]) for row in rows], unlinked

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

    def get_message(
        self,
        user_id: str,
        session_id: str,
        message_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        with self._lock:
            session_pk = self._resolve_session_id(self.connection, scope)
            row = self.connection.execute(
                "SELECT * FROM messages WHERE session_id=? AND message_key=?",
                (session_pk, _required(message_id, "message_id")),
            ).fetchone()
            if row is None:
                raise SessionNotFound("message is not present in the requested owner scope")
            return self._row(row)  # type: ignore[return-value]

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

    @staticmethod
    def _insert_task_event(
        conn: sqlite3.Connection,
        *,
        task_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        step_id: str | None = None,
    ) -> int:
        cursor = conn.execute(
            """INSERT INTO task_events(task_id, step_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                task_id,
                step_id,
                _required(event_type, "event_type"),
                _json(_task_event_payload(payload), default={}),
                _now(),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _validate_task_status(status: str, *, step: bool = False) -> str:
        value = _required(status, "status").casefold()
        allowed = _TASK_STEP_STATUSES if step else _TASK_STATUSES
        if value not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return value

    def create_task_run(
        self,
        user_id: str,
        session_id: str,
        objective: str,
        *,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        status: str = "queued",
        lane: str = "interactive",
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(user_id, session_id, channel_id, thread_id)
        task_key = _required(task_id or f"task_{uuid.uuid4().hex}", "task_id")
        goal = _required(objective, "objective")
        task_status = self._validate_task_status(status)
        task_lane = _required(lane, "lane").casefold()
        if task_lane not in _TASK_LANES:
            raise ValueError(f"lane must be one of {sorted(_TASK_LANES)}")
        parent_key = _scope_part(parent_task_id) or None
        now = _now()
        with self._write() as conn:
            session_pk = int(self._ensure_session(conn, scope, None)["id"])
            if parent_key is not None:
                parent = conn.execute(
                    "SELECT task_id FROM task_runs WHERE task_id=? AND user_id=?",
                    (parent_key, scope[0]),
                ).fetchone()
                if parent is None:
                    raise TaskNotFound("parent task is not present in the requested owner scope")
            conn.execute(
                """INSERT INTO task_runs(
                       task_id, user_id, session_id, parent_task_id, objective,
                       status, lane, enqueued_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_key, scope[0], session_pk, parent_key, goal,
                    task_status, task_lane, now, now, now,
                ),
            )
            self._insert_task_event(
                conn, task_id=task_key, event_type="task.created",
                payload={"status": task_status, "lane": task_lane},
            )
            return self._row(
                conn.execute("SELECT * FROM task_runs WHERE task_id=?", (task_key,)).fetchone()
            )  # type: ignore[return-value]

    def _task_pk(self, conn: sqlite3.Connection, user_id: str, task_id: str) -> sqlite3.Row:
        row = conn.execute(
            """SELECT t.*, s.session_key, s.channel_id, s.thread_id
               FROM task_runs t JOIN sessions s ON s.id=t.session_id
               WHERE t.task_id=? AND t.user_id=? AND s.user_id=?""",
            (_required(task_id, "task_id"), _required(user_id, "user_id"), _required(user_id, "user_id")),
        ).fetchone()
        if row is None:
            raise TaskNotFound("task is not present in the requested owner scope")
        return row

    def _validate_task_links(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        *,
        tool_call_id: int | None,
        receipt_id: str | None,
    ) -> tuple[int | None, str | None]:
        call_pk = None if tool_call_id is None else int(tool_call_id)
        receipt_key = _scope_part(receipt_id) or None
        call_key = None
        if call_pk is not None:
            call = conn.execute(
                """SELECT tc.id, tc.call_key FROM tool_calls tc JOIN sessions s ON s.id=tc.session_id
                   WHERE tc.id=? AND s.user_id=?""",
                (call_pk, _required(user_id, "user_id")),
            ).fetchone()
            if call is None:
                raise TaskNotFound("tool call is not present in the requested owner scope")
            call_key = str(call["call_key"])
        if receipt_key is not None:
            if not self._has_table("tool_receipts"):
                raise TaskNotFound("tool receipt is not present in the requested owner scope")
            receipt = conn.execute(
                "SELECT receipt_id, call_id FROM tool_receipts WHERE receipt_id=? AND user_id=?",
                (receipt_key, _required(user_id, "user_id")),
            ).fetchone()
            if receipt is None:
                raise TaskNotFound("tool receipt is not present in the requested owner scope")
            if call_key is not None and str(receipt["call_id"]) != call_key:
                raise ValueError("tool-call and receipt links must refer to the same call ID")
        return call_pk, receipt_key

    def add_task_step(
        self,
        user_id: str,
        task_id: str,
        step_order: int,
        *,
        step_id: str | None = None,
        dependency_ids: list[str] | tuple[str, ...] = (),
        status: str = "pending",
        next_action: str | None = None,
        retry_policy: Mapping[str, Any] | None = None,
        tool_call_id: int | None = None,
        receipt_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        step_key = _required(step_id or f"step_{uuid.uuid4().hex}", "step_id")
        order = int(step_order)
        if order < 0:
            raise ValueError("step_order must be non-negative")
        step_status = self._validate_task_status(status, step=True)
        dependencies = [_required(item, "dependency_id") for item in dependency_ids]
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("dependency_ids must be unique")
        call_pk, receipt_key = self._validate_task_links(
            self.connection, owner, tool_call_id=tool_call_id, receipt_id=receipt_id
        )
        now = _now()
        with self._write() as conn:
            self._task_pk(conn, owner, task_key)
            for dependency in dependencies:
                prior = conn.execute(
                    "SELECT step_order FROM task_steps WHERE task_id=? AND step_id=?",
                    (task_key, dependency),
                ).fetchone()
                if prior is None or int(prior[0]) >= order:
                    raise ValueError("each dependency must be an existing earlier step in this task")
            conn.execute(
                """INSERT INTO task_steps(
                       step_id, task_id, step_order, dependency_ids_json, status,
                       next_action, retry_policy_json, tool_call_id, receipt_id,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    step_key, task_key, order, _json(dependencies, default=[]),
                    step_status, next_action, _json(retry_policy, default={}),
                    call_pk, receipt_key, now, now,
                ),
            )
            conn.execute(
                "UPDATE task_runs SET version=version+1, updated_at=? WHERE task_id=? AND user_id=?",
                (now, task_key, owner),
            )
            self._insert_task_event(
                conn, task_id=task_key, step_id=step_key, event_type="step.created",
                payload={"step_order": order, "status": step_status, "dependency_ids": dependencies},
            )
            return self._row(
                conn.execute("SELECT * FROM task_steps WHERE step_id=?", (step_key,)).fetchone()
            )  # type: ignore[return-value]

    def start_task_step(
        self,
        user_id: str,
        task_id: str,
        step_order: int,
        *,
        expected_task_status: str,
        expected_task_version: int,
        tool_call_id: int,
        receipt_id: str,
        next_action: str,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a running step and claim its task before dispatch."""
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        order = int(step_order)
        if order < 0:
            raise ValueError("step_order must be non-negative")
        before = self._validate_task_status(expected_task_status)
        if before not in {"queued", "running"}:
            raise ValueError("only queued or running tasks may claim a dispatch step")
        version = int(expected_task_version)
        if version < 0:
            raise ValueError("expected_task_version must be non-negative")
        step_key = _required(step_id or f"step_{uuid.uuid4().hex}", "step_id")
        action = _required(next_action, "next_action")
        now = _now()
        with self._write() as conn:
            task = self._task_pk(conn, owner, task_key)
            next_order = int(conn.execute(
                "SELECT COALESCE(MAX(step_order), -1) + 1 FROM task_steps WHERE task_id=?",
                (task_key,),
            ).fetchone()[0])
            if order != next_order:
                raise ValueError("dispatch step order must be the next contiguous task step")
            if task["status"] != before or int(task["version"]) != version:
                raise ConcurrentTaskUpdate("task status/version changed before dispatch claim")
            call_pk, receipt_key = self._validate_task_links(
                conn, owner, tool_call_id=tool_call_id, receipt_id=receipt_id
            )
            assert call_pk is not None and receipt_key is not None
            step = conn.execute(
                """INSERT INTO task_steps(
                       step_id, task_id, step_order, dependency_ids_json, status,
                       next_action, tool_call_id, receipt_id, created_at, updated_at
                   ) VALUES (?, ?, ?, '[]', 'running', ?, ?, ?, ?, ?)""",
                (step_key, task_key, order, action, call_pk, receipt_key, now, now),
            )
            cursor = conn.execute(
                """UPDATE task_runs SET status='running', current_step_id=?,
                       version=version+1, updated_at=?
                   WHERE task_id=? AND user_id=? AND status=? AND version=?""",
                (step_key, now, task_key, owner, before, version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentTaskUpdate("task status/version changed before dispatch claim")
            self._insert_task_event(
                conn, task_id=task_key, step_id=step_key,
                event_type="step.dispatch_claimed",
                payload={
                    "step_order": order, "status": "running", "next_action": action,
                    "tool_call_id": call_pk, "receipt_id": receipt_key,
                    "from": before, "to": "running", "version": version + 1,
                    "expected_version": version,
                },
            )
            return {
                "task": self._row(conn.execute(
                    "SELECT * FROM task_runs WHERE task_id=?", (task_key,)
                ).fetchone()),
                "step": self._row(conn.execute(
                    "SELECT * FROM task_steps WHERE step_id=?", (step_key,)
                ).fetchone()),
            }

    def claim_task_step(
        self,
        user_id: str,
        task_id: str,
        step_id: str,
        *,
        expected_task_status: str,
        expected_task_version: int,
        expected_step_version: int,
        receipt_id: str,
    ) -> dict[str, Any]:
        """Atomically link a prepared receipt and move a pending step to running."""
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        step_key = _required(step_id, "step_id")
        before = self._validate_task_status(expected_task_status)
        if before not in {"queued", "running"}:
            raise ValueError("only queued or running tasks may claim a dispatch step")
        task_version = int(expected_task_version)
        now = _now()
        with self._write() as conn:
            task = self._task_pk(conn, owner, task_key)
            step = conn.execute(
                "SELECT * FROM task_steps WHERE task_id=? AND step_id=?",
                (task_key, step_key),
            ).fetchone()
            if step is None:
                raise TaskNotFound("task step is not present in the requested owner scope")
            if task["status"] != before or int(task["version"]) != task_version:
                raise ConcurrentTaskUpdate("task status/version changed before step claim")
            if step["status"] != "pending":
                raise ConcurrentTaskUpdate("only a pending step can be claimed")
            _call_pk, receipt_key = self._validate_task_links(
                conn, owner, tool_call_id=int(step["tool_call_id"]), receipt_id=receipt_id
            )
            assert receipt_key is not None
            step_cursor = conn.execute(
                """UPDATE task_steps SET status='running', receipt_id=?, version=version+1, updated_at=?
                   WHERE task_id=? AND step_id=? AND status='pending' AND version=?""",
                (receipt_key, now, task_key, step_key, int(expected_step_version)),
            )
            if step_cursor.rowcount != 1:
                raise ConcurrentTaskUpdate("task step changed before claim")
            task_cursor = conn.execute(
                """UPDATE task_runs SET status='running', current_step_id=?, version=version+1, updated_at=?
                   WHERE task_id=? AND user_id=? AND status=? AND version=?""",
                (step_key, now, task_key, owner, before, task_version),
            )
            if task_cursor.rowcount != 1:
                raise ConcurrentTaskUpdate("task changed before step claim")
            self._insert_task_event(
                conn, task_id=task_key, step_id=step_key,
                event_type="step.dispatch_claimed",
                payload={"status": "running", "from": "pending", "to": "running",
                         "receipt_id": receipt_key, "tool_call_id": int(step["tool_call_id"])},
            )
            return {
                "task": self._row(conn.execute(
                    "SELECT * FROM task_runs WHERE task_id=?", (task_key,)
                ).fetchone()),
                "step": self._row(conn.execute(
                    "SELECT * FROM task_steps WHERE step_id=?", (step_key,)
                ).fetchone()),
            }

    def transition_task_run(
        self,
        user_id: str,
        task_id: str,
        *,
        expected_status: str,
        expected_version: int,
        new_status: str,
        wait_reason: Any = _UNSET,
        current_step_id: Any = _UNSET,
        cancellation_requested: Any = _UNSET,
        result_ref: Any = _UNSET,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        before = self._validate_task_status(expected_status)
        after = self._validate_task_status(new_status)
        version = int(expected_version)
        if version < 0:
            raise ValueError("expected_version must be non-negative")
        updates: dict[str, Any] = {}
        if wait_reason is not _UNSET:
            updates["wait_reason"] = None if wait_reason is None else str(wait_reason)
        if current_step_id is not _UNSET:
            updates["current_step_id"] = None if current_step_id is None else _required(current_step_id, "current_step_id")
        if cancellation_requested is not _UNSET:
            updates["cancellation_requested"] = int(bool(cancellation_requested))
        if result_ref is not _UNSET:
            updates["result_ref"] = None if result_ref is None else _required(result_ref, "result_ref")
        now = _now()
        with self._write() as conn:
            self._task_pk(conn, owner, task_key)
            if updates.get("current_step_id") is not None:
                step = conn.execute(
                    "SELECT 1 FROM task_steps WHERE task_id=? AND step_id=?",
                    (task_key, updates["current_step_id"]),
                ).fetchone()
                if step is None:
                    raise TaskNotFound("current step is not part of this task")
            assignments = ["status=?", "version=version+1", "updated_at=?"]
            values: list[Any] = [after, now]
            for column, value in updates.items():
                assignments.append(f"{column}=?")
                values.append(value)
            values.extend([task_key, owner, before, version])
            cursor = conn.execute(
                f"UPDATE task_runs SET {', '.join(assignments)} "
                "WHERE task_id=? AND user_id=? AND status=? AND version=?",
                values,
            )
            if cursor.rowcount != 1:
                raise ConcurrentTaskUpdate("task status/version changed before transition")
            self._insert_task_event(
                conn, task_id=task_key, event_type="task.transitioned",
                payload={"from": before, "to": after, "expected_version": version, "version": version + 1},
            )
            if event_type:
                self._insert_task_event(
                    conn, task_id=task_key, event_type=event_type,
                    payload=event_payload,
                )
            return self._row(
                conn.execute("SELECT * FROM task_runs WHERE task_id=?", (task_key,)).fetchone()
            )  # type: ignore[return-value]

    def transition_task_step(
        self,
        user_id: str,
        task_id: str,
        step_id: str,
        *,
        expected_status: str,
        expected_version: int,
        new_status: str,
        next_action: Any = _UNSET,
        retry_policy: Any = _UNSET,
        tool_call_id: Any = _UNSET,
        receipt_id: Any = _UNSET,
    ) -> dict[str, Any]:
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        step_key = _required(step_id, "step_id")
        before = self._validate_task_status(expected_status, step=True)
        after = self._validate_task_status(new_status, step=True)
        version = int(expected_version)
        if version < 0:
            raise ValueError("expected_version must be non-negative")
        updates: dict[str, Any] = {}
        if next_action is not _UNSET:
            updates["next_action"] = None if next_action is None else str(next_action)
        if retry_policy is not _UNSET:
            updates["retry_policy_json"] = _json(retry_policy, default={})
        if tool_call_id is not _UNSET or receipt_id is not _UNSET:
            existing = self.get_task_step(owner, task_key, step_key)
            selected_call = existing["tool_call_id"] if tool_call_id is _UNSET else tool_call_id
            selected_receipt = existing["receipt_id"] if receipt_id is _UNSET else receipt_id
            call_pk, receipt_key = self._validate_task_links(
                self.connection, owner, tool_call_id=selected_call, receipt_id=selected_receipt
            )
            updates["tool_call_id"] = call_pk
            updates["receipt_id"] = receipt_key
        now = _now()
        with self._write() as conn:
            self._task_pk(conn, owner, task_key)
            assignments = ["status=?", "version=version+1", "updated_at=?"]
            values: list[Any] = [after, now]
            for column, value in updates.items():
                assignments.append(f"{column}=?")
                values.append(value)
            values.extend([step_key, task_key, before, version])
            cursor = conn.execute(
                f"UPDATE task_steps SET {', '.join(assignments)} "
                "WHERE step_id=? AND task_id=? AND status=? AND version=?",
                values,
            )
            if cursor.rowcount != 1:
                raise ConcurrentTaskUpdate("task-step status/version changed before transition")
            conn.execute(
                "UPDATE task_runs SET version=version+1, updated_at=? WHERE task_id=? AND user_id=?",
                (now, task_key, owner),
            )
            self._insert_task_event(
                conn, task_id=task_key, step_id=step_key, event_type="step.transitioned",
                payload={"from": before, "to": after, "expected_version": version, "version": version + 1},
            )
            return self._row(
                conn.execute("SELECT * FROM task_steps WHERE step_id=?", (step_key,)).fetchone()
            )  # type: ignore[return-value]

    def transition_task_step_and_run(
        self,
        user_id: str,
        task_id: str,
        step_id: str,
        *,
        expected_step_status: str,
        expected_step_version: int,
        new_step_status: str,
        expected_task_status: str,
        expected_task_version: int,
        new_task_status: str,
        next_action: str | None = None,
        wait_reason: str | None = None,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """CAS a step and its parent task in one SQLite transaction."""
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        step_key = _required(step_id, "step_id")
        old_step = self._validate_task_status(expected_step_status, step=True)
        new_step = self._validate_task_status(new_step_status, step=True)
        old_task = self._validate_task_status(expected_task_status)
        new_task = self._validate_task_status(new_task_status)
        step_version = int(expected_step_version)
        task_version = int(expected_task_version)
        now = _now()
        with self._write() as conn:
            self._task_pk(conn, owner, task_key)
            step_cursor = conn.execute(
                """UPDATE task_steps SET status=?, next_action=?, version=version+1, updated_at=?
                   WHERE step_id=? AND task_id=? AND status=? AND version=?""",
                (new_step, next_action, now, step_key, task_key, old_step, step_version),
            )
            if step_cursor.rowcount != 1:
                raise ConcurrentTaskUpdate("task-step status/version changed before atomic transition")
            task_cursor = conn.execute(
                """UPDATE task_runs SET status=?, wait_reason=?, version=version+1, updated_at=?
                   WHERE task_id=? AND user_id=? AND status=? AND version=?""",
                (new_task, wait_reason, now, task_key, owner, old_task, task_version),
            )
            if task_cursor.rowcount != 1:
                raise ConcurrentTaskUpdate("task status/version changed before atomic transition")
            self._insert_task_event(
                conn, task_id=task_key, step_id=step_key,
                event_type="step.transitioned",
                payload={"from": old_step, "to": new_step,
                         "expected_version": step_version, "version": step_version + 1},
            )
            self._insert_task_event(
                conn, task_id=task_key, event_type="task.transitioned",
                payload={"from": old_task, "to": new_task,
                         "expected_version": task_version, "version": task_version + 1},
            )
            if event_type:
                self._insert_task_event(
                    conn, task_id=task_key, step_id=step_key,
                    event_type=event_type, payload=event_payload,
                )
            return {
                "task": self._row(conn.execute(
                    "SELECT * FROM task_runs WHERE task_id=?", (task_key,)
                ).fetchone()),
                "step": self._row(conn.execute(
                    "SELECT * FROM task_steps WHERE step_id=?", (step_key,)
                ).fetchone()),
            }

    def append_task_event(
        self,
        user_id: str,
        task_id: str,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        step_key = _scope_part(step_id) or None
        with self._write() as conn:
            self._task_pk(conn, owner, task_key)
            if step_key is not None and conn.execute(
                "SELECT 1 FROM task_steps WHERE task_id=? AND step_id=?",
                (task_key, step_key),
            ).fetchone() is None:
                raise TaskNotFound("task step is not present in the requested owner scope")
            event_id = self._insert_task_event(
                conn, task_id=task_key, step_id=step_key,
                event_type=event_type, payload=payload,
            )
            return self._row(
                conn.execute("SELECT * FROM task_events WHERE event_id=?", (event_id,)).fetchone()
            )  # type: ignore[return-value]

    def get_task_step(self, user_id: str, task_id: str, step_id: str) -> dict[str, Any]:
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        step_key = _required(step_id, "step_id")
        with self._lock:
            self._task_pk(self.connection, owner, task_key)
            row = self.connection.execute(
                "SELECT * FROM task_steps WHERE task_id=? AND step_id=?",
                (task_key, step_key),
            ).fetchone()
            if row is None:
                raise TaskNotFound("task step is not present in the requested owner scope")
            return self._row(row)  # type: ignore[return-value]

    def list_task_steps(
        self, user_id: str, task_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return bounded step metadata without loading task events or all steps."""
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        bounded_limit = max(1, min(int(limit), 20))
        with self._lock:
            self._task_pk(self.connection, owner, task_key)
            rows = self.connection.execute(
                """SELECT step_id, status, substr(next_action, 1, 201) AS next_action
                   FROM task_steps WHERE task_id=?
                   ORDER BY step_order, step_id LIMIT ?""",
                (task_key, bounded_limit),
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[misc]

    def has_confirmed_task_call(
        self, user_id: str, task_id: str, *, tool_name: str, arguments: Any
    ) -> bool:
        """Detect an exact action already confirmed within this task."""
        owner = _required(user_id, "user_id")
        task_key = _required(task_id, "task_id")
        args_json = _json(arguments, default={})
        with self._lock:
            self._task_pk(self.connection, owner, task_key)
            if not self._has_table("tool_receipts"):
                return False
            row = self.connection.execute(
                """SELECT 1 FROM task_steps ts
                   JOIN tool_calls tc ON tc.id=ts.tool_call_id
                   JOIN tool_receipts tr ON tr.receipt_id=ts.receipt_id
                   WHERE ts.task_id=? AND ts.status='succeeded'
                     AND tc.tool_name=? AND tc.arguments_json=?
                     AND tr.user_id=? AND tr.status='confirmed'
                   LIMIT 1""",
                (task_key, _required(tool_name, "tool_name"), args_json, owner),
            ).fetchone()
            return row is not None

    def get_tool_call(self, user_id: str, tool_call_id: int) -> dict[str, Any]:
        owner = _required(user_id, "user_id")
        with self._lock:
            row = self.connection.execute(
                """SELECT tc.* FROM tool_calls tc JOIN sessions s ON s.id=tc.session_id
                   WHERE tc.id=? AND s.user_id=?""",
                (int(tool_call_id), owner),
            ).fetchone()
            if row is None:
                raise TaskNotFound("tool call is not present in the requested owner scope")
            return self._row(row)  # type: ignore[return-value]

    def task_requires_monitor_readback(self, user_id: str, task_id: str) -> bool:
        """A confirmed monitor insert is incomplete until owner-scoped read-back."""
        owner = _required(user_id, "user_id")
        with self._lock:
            self._task_pk(self.connection, owner, task_id)
            if not self._has_table("tool_receipts"):
                return False
            creates = self.connection.execute(
                """SELECT ts.step_order, tc.tool_name, tc.arguments_json, tr.result_summary
                   FROM task_steps ts
                   JOIN tool_calls tc ON tc.id=ts.tool_call_id
                   JOIN tool_receipts tr ON tr.receipt_id=ts.receipt_id
                   WHERE ts.task_id=? AND ts.status='succeeded'
                     AND tc.tool_name IN ('monitor_create_natural_rule','monitor_add_rule')
                     AND tr.user_id=? AND tr.status='confirmed'
                   ORDER BY ts.step_order""",
                (task_id, owner),
            ).fetchall()
            if not creates:
                return False
            reads = self.connection.execute(
                """SELECT ts.step_order, tr.result_summary FROM task_steps ts
                   JOIN tool_calls tc ON tc.id=ts.tool_call_id
                   JOIN tool_receipts tr ON tr.receipt_id=ts.receipt_id
                   WHERE ts.task_id=? AND ts.status='succeeded'
                     AND tc.tool_name='monitor_list_rules'
                     AND tr.user_id=? AND tr.status='confirmed'
                   ORDER BY ts.step_order""",
                (task_id, owner),
            ).fetchall()
            for created in creates:
                match = re.search(r"#(\d+)", str(created["result_summary"] or ""))
                if match is None:
                    return True
                rule_id = match.group(1)
                if not self._has_table("monitor_rules"):
                    return True
                rule = self.connection.execute(
                    """SELECT name, kind, config FROM monitor_rules
                       WHERE id=? AND user_id=?""",
                    (int(rule_id), owner),
                ).fetchone()
                if rule is None:
                    return True
                try:
                    normalized_config = json.dumps(
                        json.loads(rule["config"] or "{}"),
                        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                    )
                except (TypeError, ValueError):
                    return True
                expected_prefix = f"- #{rule_id} "
                expected_identity = f"] {rule['name']} ({rule['kind']})"
                expected_config = f"config={normalized_config}"
                if not any(
                    int(read["step_order"]) > int(created["step_order"])
                    and any(
                        line.startswith(expected_prefix)
                        and expected_identity in line
                        and expected_config in line
                        for line in str(read["result_summary"] or "").splitlines()
                    )
                    for read in reads
                ):
                    return True
            return False

    def get_task(
        self,
        user_id: str,
        task_id: str,
        *,
        include_steps: bool = True,
        include_events: bool = True,
    ) -> dict[str, Any]:
        owner = _required(user_id, "user_id")
        with self._lock:
            row = self._task_pk(self.connection, owner, task_id)
            result = self._row(row)  # type: ignore[assignment]
            if include_steps:
                result["steps"] = [
                    self._row(item)
                    for item in self.connection.execute(
                        "SELECT * FROM task_steps WHERE task_id=? ORDER BY step_order, step_id",
                        (result["task_id"],),
                    ).fetchall()
                ]
            if include_events:
                result["events"] = [
                    self._row(item)
                    for item in self.connection.execute(
                        "SELECT * FROM task_events WHERE task_id=? ORDER BY event_id",
                        (result["task_id"],),
                    ).fetchall()
                ]
            return result

    def list_tasks(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        statuses: list[str] | tuple[str, ...] | None = None,
        lane: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        owner = _required(user_id, "user_id")
        limit = max(1, min(int(limit), 500))
        clauses = ["t.user_id=?"]
        params: list[Any] = [owner]
        if session_id is not None:
            clauses.append("s.session_key=?")
            params.append(_required(session_id, "session_id"))
        if statuses is not None:
            normalized = [self._validate_task_status(item) for item in statuses]
            if not normalized:
                return []
            clauses.append(f"t.status IN ({','.join('?' for _ in normalized)})")
            params.extend(normalized)
        if lane is not None:
            selected_lane = _required(lane, "lane").casefold()
            if selected_lane not in _TASK_LANES:
                raise ValueError(f"lane must be one of {sorted(_TASK_LANES)}")
            clauses.append("t.lane=?")
            params.append(selected_lane)
        params.append(limit)
        ordering = "t.updated_at DESC, t.task_id" if session_id is not None else "t.enqueued_at, t.task_id"
        with self._lock:
            rows = self.connection.execute(
                f"SELECT t.* FROM task_runs t JOIN sessions s ON s.id=t.session_id "
                f"WHERE {' AND '.join(clauses)} "
                f"AND s.user_id=t.user_id ORDER BY {ordering} LIMIT ?",
                params,
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[misc]

    def list_task_summaries(
        self,
        user_id: str,
        *,
        session_id: str,
        channel_id: str | None,
        thread_id: str | None,
        statuses: list[str] | tuple[str, ...] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Read bounded task fields for model-facing progress summaries."""
        owner = _required(user_id, "user_id")
        session = _required(session_id, "session_id")
        bounded_limit = max(1, min(int(limit), 20))
        clauses = [
            "t.user_id=?", "s.session_key=?", "s.channel_id=?",
            "s.thread_id=?", "s.user_id=t.user_id",
        ]
        params: list[Any] = [
            owner, session, _scope_part(channel_id), _scope_part(thread_id),
        ]
        if statuses is not None:
            normalized = [self._validate_task_status(item) for item in statuses]
            if not normalized:
                return []
            clauses.append(f"t.status IN ({','.join('?' for _ in normalized)})")
            params.extend(normalized)
        params.append(bounded_limit)
        with self._lock:
            rows = self.connection.execute(
                f"""SELECT t.task_id, substr(t.objective, 1, 501) AS objective,
                           t.status, t.lane, t.updated_at,
                           substr(t.wait_reason, 1, 201) AS wait_reason
                    FROM task_runs t JOIN sessions s ON s.id=t.session_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY t.updated_at DESC, t.task_id LIMIT ?""",
                params,
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[misc]

    def _prune(self, conn: sqlite3.Connection, session_pk: int) -> None:
        # Retention is normally bounded. Keep any turn whose tool-call row is
        # referenced by a durable task step: migration 006 cascades from turn
        # to tool call, and deleting that evidence would violate task history.
        # Such referenced turns may exceed the ordinary retention cap.
        conn.execute(
            """DELETE FROM turns WHERE session_id=? AND status IN ('completed','failed','cancelled')
               AND id NOT IN (SELECT id FROM turns WHERE session_id=? ORDER BY id DESC LIMIT ?)
               AND id NOT IN (
                   SELECT tc.turn_id FROM tool_calls tc
                   JOIN task_steps ts ON ts.tool_call_id=tc.id
                   WHERE tc.session_id=? AND tc.turn_id IS NOT NULL
               )""",
            (session_pk, session_pk, self.max_turns_per_session, session_pk),
        )
        conn.execute(
            """DELETE FROM messages WHERE session_id=? AND id NOT IN
               (SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?)""",
            (session_pk, session_pk, self.max_messages_per_session),
        )
        # Task steps are durable references to execution evidence. Preserve a
        # linked tool-call row even if that makes the per-session cap a soft
        # bound; deleting it would silently erase the task's evidence link.
        conn.execute(
            """DELETE FROM tool_calls WHERE session_id=? AND id NOT IN
               (SELECT id FROM tool_calls WHERE session_id=? ORDER BY id DESC LIMIT ?)
               AND id NOT IN (
                   SELECT tool_call_id FROM task_steps WHERE tool_call_id IS NOT NULL
               )""",
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
            where = "m.session_id=?"
            if before_id is not None:
                where += " AND m.id < ?"
                params.append(int(before_id))
            params.append(limit)
            rows = self.connection.execute(
                f"""SELECT m.*, t.turn_key FROM messages m
                    LEFT JOIN turns t ON t.id=m.turn_id
                    WHERE {where}
                    ORDER BY m.id DESC LIMIT ?""",
                params,
            ).fetchall()
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
                        f"""SELECT m.*, t.turn_key, s.user_id, s.session_key, s.channel_id, s.thread_id
                            FROM messages_fts f JOIN messages m ON m.id=f.rowid
                            LEFT JOIN turns t ON t.id=m.turn_id
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
                f"""SELECT m.*, t.turn_key, s.user_id, s.session_key, s.channel_id, s.thread_id
                    FROM messages m JOIN sessions s ON s.id=m.session_id
                    LEFT JOIN turns t ON t.id=m.turn_id
                    WHERE {' AND '.join(where)} AND m.content LIKE ? ESCAPE '\\'
                    ORDER BY m.id DESC LIMIT ?""",
                [*params, f"%{literal}%", limit],
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[misc]


__all__ = [
    "ConcurrentTaskUpdate", "IdempotencyConflict", "InvalidLifecycleTransition",
    "SessionNotFound", "SessionStore", "SessionStoreError", "TaskNotFound",
]
