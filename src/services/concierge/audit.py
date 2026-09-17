"""Append-only, hash-chained audit log for the concierge layer (B0.

Every administrative action, capture, draft transition, and executor run
writes one row here.  The chain invariant: ``entry_hash = sha256(prev_hash +
canonical_payload)`` and each row's ``prev_hash`` equals the previous row's
``entry_hash``; there is no UPDATE/DELETE API and ``verify_chain`` detects
any tampering, reordering, or hole.

The B0 redaction policy is applied by CALLERS before they append (see
``src.services.concierge.redaction``): values that must never be logged are
already masks by the time they reach this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuditError(ValueError):
    pass


class AuditLog:
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
                CREATE TABLE IF NOT EXISTS audit_chain (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    tenant TEXT,
                    action TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '-',
                    detail TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @staticmethod
    def _canonical(parts: List[Any]) -> str:
        return json.dumps(parts, separators=(",", ":"), sort_keys=True)

    def append(
        self,
        actor: str,
        action: str,
        tenant: Optional[str] = None,
        subject: str = "-",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Append one audit row; returns its seq.

        ``detail`` must ALREADY be redacted by the caller (never raw secrets).
        """
        if detail is None:
            detail_json = "{}"
        elif not isinstance(detail, dict):
            raise AuditError("audit detail must be a dict")
        else:
            detail_json = json.dumps(detail, sort_keys=True, separators=(",", ":"))
        ts = self._now()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT seq, entry_hash FROM audit_chain ORDER BY seq DESC LIMIT 1")
            last = cur.fetchone()
            prev_hash = last["entry_hash"] if last else ("0" * 64)
            payload = self._canonical([ts, actor, str(tenant or ""), action, subject, detail_json])
            entry_hash = hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()
            cur.execute("""
                INSERT INTO audit_chain (prev_hash, entry_hash, ts, actor, tenant, action, subject, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (prev_hash, entry_hash, ts, actor, tenant, action, subject, detail_json))
            conn.commit()
            return cur.lastrowid

    def tail(self, limit: int = 25, tenant: Optional[str] = None) -> List[Dict[str, Any]]:
        cur = self._conn().cursor()
        if tenant is None:
            cur.execute(
                "SELECT * FROM audit_chain ORDER BY seq DESC LIMIT ?",
                (limit,),
            )
        else:
            cur.execute(
                "SELECT * FROM audit_chain WHERE tenant = ? ORDER BY seq DESC LIMIT ?",
                (tenant, limit),
            )
        return [self._row(r) for r in cur.fetchall()]

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "seq": row["seq"],
            "ts": row["ts"],
            "actor": row["actor"],
            "tenant": row["tenant"],
            "action": row["action"],
            "subject": row["subject"],
            "detail": json.loads(row["detail"] or "{}"),
        }

    def verify_chain(self) -> Dict[str, Any]:
        """Walk the chain; report ok=True or the first broken link."""
        cur = self._conn().cursor()
        cur.execute("SELECT * FROM audit_chain ORDER BY seq ASC")
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {"ok": True, "rows": 0, "broken_at": None, "reason": None}
        prev_hash = "0" * 64
        for row in rows:
            recomputed_payload = self._canonical([
                row["ts"], row["actor"], str(row["tenant"] or ""),
                row["action"], row["subject"], row["detail"],
            ])
            recomputed = hashlib.sha256(
                (prev_hash + recomputed_payload).encode("utf-8")
            ).hexdigest()
            if recomputed != row["entry_hash"] or row["prev_hash"] != prev_hash:
                return {
                    "ok": False,
                    "rows": len(rows),
                    "broken_at": row["seq"],
                    "reason": "entry hash mismatch" if recomputed != row["entry_hash"] else "prev_hash mismatch",
                }
            prev_hash = row["entry_hash"]
        return {"ok": True, "rows": len(rows), "broken_at": None, "reason": None}

    def count(self, tenant: Optional[str] = None) -> int:
        cur = self._conn().cursor()
        if tenant is None:
            cur.execute("SELECT COUNT(*) AS n FROM audit_chain")
        else:
            cur.execute("SELECT COUNT(*) AS n FROM audit_chain WHERE tenant = ?", (tenant,))
        return int(cur.fetchone()["n"])


__all__ = ["AuditLog", "AuditError"]