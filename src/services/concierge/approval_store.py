"""B2 approval store: pending write-tier action proposals awaiting human sign-off.

SQLite-backed, mirrors the concierge AuditLog DB (DEFAULT_DB_PATH). A proposal
is created when the LLM requests a write-tier act (send_email / schedule_event
/ fill_form); it transitions pending → approved/rejected/executed. Each
proposal carries the validated kind + args + allowed_domains + tenant so the
approval callback can execute without trusting model-supplied identity.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.security.vault import DEFAULT_DB_PATH as _DB


def db_path() -> str:
    return _DB


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for DBs created before approval_ttl existed."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(concierge_act_proposals)").fetchall()}
    if "approval_ttl" not in cols:
        conn.execute(
            "ALTER TABLE concierge_act_proposals ADD COLUMN approval_ttl REAL NOT NULL DEFAULT 600.0"
        )
        conn.commit()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concierge_act_proposals (
            proposal_id  TEXT PRIMARY KEY,
            tenant       TEXT NOT NULL,
            uid          TEXT NOT NULL,
            kind         TEXT NOT NULL,
            args         TEXT NOT NULL,
            url          TEXT,
            steps        TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL,
            approved_at  TEXT,
            executed_at  TEXT,
            result_detail TEXT,
            approval_ttl  REAL NOT NULL DEFAULT 600.0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_concierge_act_tenant
        ON concierge_act_proposals(tenant, status, created_at)
    """)
    _migrate_schema(conn)
    conn.commit()


def _ensure_schema() -> None:
    conn = _connect()
    try:
        _init_schema(conn)
    finally:
        conn.close()


_ensure_schema()
_local = threading.local()


class ApprovalStore:
    """Thin SQLite wrapper for concierge write-tier approval proposals."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _DB
        self._ensure()

    def _ensure(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            _init_schema(conn)
        finally:
            conn.close()

    def create(
        self,
        tenant: str,
        uid: str,
        kind: str,
        args: Dict[str, Any],
        url: Optional[str] = None,
        steps: Optional[List[Dict[str, Any]]] = None,
        approval_ttl: float = 600.0,
    ) -> str:
        """Insert a pending proposal; returns its proposal_id."""
        proposal_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            conn.execute(
                """INSERT INTO concierge_act_proposals
                   (proposal_id, tenant, uid, kind, args, url, steps, status, created_at, approval_ttl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    proposal_id, tenant, uid, kind,
                    json.dumps(args, sort_keys=True),
                    url,
                    json.dumps(steps) if steps else None,
                    now,
                    float(approval_ttl),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return proposal_id

    def get(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM concierge_act_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "proposal_id": row["proposal_id"],
            "tenant": row["tenant"],
            "uid": row["uid"],
            "kind": row["kind"],
            "args": json.loads(row["args"] or "{}"),
            "url": row["url"],
            "steps": json.loads(row["steps"]) if row["steps"] else [],
            "status": row["status"],
            "created_at": row["created_at"],
            "approved_at": row["approved_at"],
            "executed_at": row["executed_at"],
            "result_detail": json.loads(row["result_detail"]) if row["result_detail"] else None,
            "approval_ttl": float(row["approval_ttl"]) if row["approval_ttl"] is not None else 600.0,
        }

    def update_status(
        self, proposal_id: str, status: str, result_detail: Optional[Dict[str, Any]] = None
    ) -> bool:
        now_col = "approved_at" if status in ("approved", "rejected") else "executed_at"
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            cur = conn.execute(
                f"""UPDATE concierge_act_proposals
                    SET status = ?, {now_col} = datetime('now', 'localtime'),
                        result_detail = ?
                    WHERE proposal_id = ?""",
                (status, json.dumps(result_detail) if result_detail else None, proposal_id),
            )
            conn.commit()
            conn.close()
            return cur.rowcount > 0
        finally:
            conn.close()

    def pending_for_tenant(self, tenant: str, limit: int = 20) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT proposal_id, kind, status, created_at
                   FROM concierge_act_proposals
                   WHERE tenant = ? AND status = 'pending'
                   ORDER BY created_at DESC LIMIT ?""",
                (tenant, limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]


__all__ = ["ApprovalStore", "db_path"]
