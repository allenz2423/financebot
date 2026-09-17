"""One-time capture tokens (B0).

Tokens are single-use, tenant-bound, intent-bound (the stored validated intent
is authoritative and returned on redeem), expire after ~10 minutes, and are
stored hashed at rest.  The plaintext token is returned exactly once, at
issue, to build the capture URL.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

TOKEN_TTL = timedelta(minutes=10)


class CaptureTokenError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


class CaptureTokenStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._local = threading.local()
        Path(self.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capture_tokens (
                    token_hash TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, tenant: str, intent: Dict[str, Any]) -> str:
        """Store hashed token + intent; return plaintext token once."""
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + TOKEN_TTL
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO capture_tokens (token_hash, tenant, intent_json, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                self._hash(token),
                tenant,
                json.dumps(intent, sort_keys=True, separators=(",", ":")),
                expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                _now(),
            ))
            conn.commit()
        return token

    def peek(self, token: str, tenant: str) -> Dict[str, Any]:
        """Validate WITHOUT consuming (GET page render): tenant-bound, unused,
        not expired; returns the stored intent (the POST still consumes once)."""
        if not token:
            raise CaptureTokenError("missing capture token")
        h = self._hash(token)
        cur = self._conn().cursor()
        cur.execute("SELECT * FROM capture_tokens WHERE token_hash = ?", (h,))
        row = cur.fetchone()
        if not row:
            raise CaptureTokenError("capture token invalid")
        if row["tenant"] != tenant:
            raise CaptureTokenError("capture token does not belong to this tenant")
        if row["used_at"] is not None:
            raise CaptureTokenError("capture token already used")
        if _parse_ts(row["expires_at"]) < datetime.now(timezone.utc):
            raise CaptureTokenError("capture token expired")
        return json.loads(row["intent_json"])

    def redeem(self, token: str, tenant: str) -> Dict[str, Any]:
        """Consume the token exactly once; return the stored validated intent.

        Non-retryable errors: unknown, wrong tenant, already used, expired.
        """
        if not token:
            raise CaptureTokenError("missing capture token")
        h = self._hash(token)
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM capture_tokens WHERE token_hash = ?", (h,))
            row = cur.fetchone()
            if not row:
                raise CaptureTokenError("capture token invalid")
            if row["tenant"] != tenant:
                raise CaptureTokenError("capture token does not belong to this tenant")
            if row["used_at"] is not None:
                raise CaptureTokenError("capture token already used")
            if _parse_ts(row["expires_at"]) < datetime.now(timezone.utc):
                raise CaptureTokenError("capture token expired")
            cur.execute(
                "UPDATE capture_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (_now(), h),
            )
            conn.commit()
            if cur.rowcount != 1:
                raise CaptureTokenError("capture token already used")
        return json.loads(row["intent_json"])

    def purge_expired(self) -> int:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM capture_tokens WHERE expires_at < ? AND used_at IS NULL",
                (_now(),),
            )
            conn.commit()
            return cur.rowcount


__all__ = ["CaptureTokenStore", "CaptureTokenError", "TOKEN_TTL", "_parse_ts", "_now"]