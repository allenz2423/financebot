"""Draft/approve state machine + executor skeleton (B0).

Flow: draft -> approved -> executing -> confirmed, with terminal states
denied/expired/escalated.  No auto-approve in v1.  The executor refuses
untrusted data: unknown vault refs, foreign refs, revoked refs, and action
payloads that carry secret-looking values are all rejected before anything
resolves.  Approval is owner-scoped: only the draft's tenant can approve.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.security.vault import Vault

APPROVAL_TTL = timedelta(minutes=10)


class StateError(ValueError):
    pass


class DraftNotFound(StateError):
    pass


class DraftNotActionable(StateError):
    pass


class ExecutorRefused(StateError):
    pass


class DraftStore:
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
                CREATE TABLE IF NOT EXISTS concierge_drafts (
                    draft_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    vault_refs TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    executed_at TEXT,
                    result TEXT
                )
            """)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def create(
        self,
        tenant: str,
        kind: str,
        action: str,
        vault_refs: List[str],
        payload: Dict[str, Any],
    ) -> str:
        """Create a draft. ``payload`` must already be secret-free; the
        executor re-validates before anything resolves."""
        if not isinstance(payload, dict):
            raise StateError("draft payload must be a dict")
        draft_id = "d_" + __import__("secrets").token_hex(6)
        now = self._now()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO concierge_drafts
                    (draft_id, tenant, kind, action, vault_refs, payload,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """, (
                draft_id, tenant, kind, action,
                json.dumps(vault_refs, separators=(",", ":")),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now, now,
            ))
            conn.commit()
        return draft_id

    def _row(self, draft_id: str) -> sqlite3.Row:
        cur = self._conn().cursor()
        cur.execute("SELECT * FROM concierge_drafts WHERE draft_id = ?", (draft_id,))
        row = cur.fetchone()
        if not row:
            raise DraftNotFound(f"draft {draft_id!r} not found")
        return row

    def get(self, draft_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        row = self._row(draft_id)
        if tenant is not None and row["tenant"] != tenant:
            raise DraftNotFound(f"draft {draft_id!r} not found for this tenant")
        return {
            "draft_id": row["draft_id"],
            "tenant": row["tenant"],
            "kind": row["kind"],
            "action": row["action"],
            "vault_refs": json.loads(row["vault_refs"] or "[]"),
            "payload": json.loads(row["payload"] or "{}"),
            "status": row["status"],
            "created_at": row["created_at"],
            "approved_at": row["approved_at"],
            "executed_at": row["executed_at"],
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def list_for(self, tenant: str, limit: int = 20) -> List[Dict[str, Any]]:
        cur = self._conn().cursor()
        cur.execute(
            "SELECT * FROM concierge_drafts WHERE tenant = ? ORDER BY created_at DESC LIMIT ?",
            (tenant, limit),
        )
        return [self.get(r["draft_id"]) for r in cur.fetchall()]

    def _transition(self, draft_id: str, expected: str, status: str, **extra) -> None:
        now = self._now()
        sets = ["status = ?", "updated_at = ?"]
        values: List[Any] = [status, now]
        for col, val in extra.items():
            sets.append(f"{col} = ?")
            values.append(val)
        values.extend([draft_id, expected])
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE concierge_drafts SET {', '.join(sets)} "
                "WHERE draft_id = ? AND status = ?",
                values,
            )
            conn.commit()
            if cur.rowcount != 1:
                raise DraftNotActionable(
                    f"draft {draft_id!r} is not in state {expected!r} (cannot -> {status})"
                )

    def approve(self, draft_id: str, tenant: str, actor: str) -> None:
        """Owner-only approval: draft -> approved (no auto-approve in v1)."""
        row = self._row(draft_id)
        if row["tenant"] != tenant:
            raise DraftNotFound(f"draft {draft_id!r} not found for this tenant")
        if row["status"] != "draft":
            raise DraftNotActionable(f"draft {draft_id!r} is not in state 'draft'")
        self._transition(draft_id, "draft", "approved", approved_by=actor, approved_at=self._now())

    def deny(self, draft_id: str, tenant: str) -> None:
        row = self._row(draft_id)
        if row["tenant"] != tenant:
            raise DraftNotFound(f"draft {draft_id!r} not found for this tenant")
        self._transition(draft_id, row["status"], "denied") if row["status"] == "draft" \
            else self._transition(draft_id, "approved", "denied")

    def expire_stale(self, ttl: timedelta = APPROVAL_TTL) -> int:
        """draft/approved older than TTL -> expired."""
        cutoff = (datetime.now(timezone.utc) - ttl).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE concierge_drafts SET status = 'expired', updated_at = ? "
                "WHERE status IN ('draft', 'approved') AND created_at < ?",
                (self._now(), cutoff),
            )
            conn.commit()
            return cur.rowcount

    def to_executing(self, draft_id: str) -> None:
        self._transition(draft_id, "approved", "executing")

    def confirm(self, draft_id: str, result: Dict[str, Any]) -> None:
        self._transition(
            draft_id, "executing", "confirmed",
            result=json.dumps(result, sort_keys=True, separators=(",", ":")),
            executed_at=self._now(),
        )

    def escalate(self, draft_id: str, reason: str) -> None:
        self._transition(
            draft_id, "executing", "escalated",
            result=json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":")),
        )


_SECRET_PATTERNS = ("password=", "secret=", "token=", "authorization=", "api_key=", "pan=")


def _looks_like_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(p in lowered for p in _SECRET_PATTERNS) or (
        len(value) >= 24 and value != value.lower()
    )


def reject_untrusted_payload(vault: Vault, tenant: str, vault_refs: List[str], payload: Dict[str, Any]) -> None:
    """Executor pre-flight: refuse anything untrusted before resolving.

    - every vault_ref exists AND belongs to this tenant (foreign refs refused);
    - every ref is active (revoked refs refused even if they linger in context);
    - payload carries no secret-looking values (a draft is a capability ref,
      not a data carrier).
    """
    for ref in vault_refs:
        rec = vault.get_record(tenant, ref)  # raises if foreign/unknown
        if rec["kind"] not in ("secret", "payment", "session_profile"):
            raise ExecutorRefused(f"vault_ref {ref!r} has unknown kind")
    def walk(obj: Any):
        if isinstance(obj, dict):
            for k, val in obj.items():
                if _looks_like_secret(str(k)) or _looks_like_secret(val):
                    raise ExecutorRefused("draft payload carries secret-looking data (use vault_refs)")
                walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, str) and _looks_like_secret(obj):
            raise ExecutorRefused("draft payload carries secret-looking data (use vault_refs)")
    walk(payload)


def execute_draft(
    store: DraftStore,
    vault: Vault,
    draft_id: str,
    tenant: str,
    action_fn: Callable[[Dict[str, bytes], Dict[str, Any]], Dict[str, Any]],
    consumer_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Run an approved draft to completion inside a guarded scope.

    Approve -> executing, pre-flight, resolve secrets ONLY inside the
    ``action_fn`` call frame (the caller may not retain or return them),
    then executing -> confirmed.  The returned result is the action's own
    summary (receipt, status); it never contains resolved secrets.
    """
    draft = store.get(draft_id, tenant=tenant)
    if draft["status"] != "approved":
        raise DraftNotActionable(f"draft {draft_id!r} must be approved before execution")
    store.to_executing(draft_id)
    try:
        reject_untrusted_payload(vault, tenant, draft["vault_refs"], draft["payload"])
        resolved: Dict[str, bytes] = {}
        for ref in draft["vault_refs"]:
            resolved[ref] = vault.resolve(tenant, ref, consumer_url=consumer_url)
        result = action_fn(resolved, draft["payload"])
    except Exception as exc:
        store._transition(draft_id, "executing", "escalated",
                          result=json.dumps({"reason": str(exc)}, sort_keys=True))
        raise
    if not isinstance(result, dict):
        raise StateError("action_fn must return a dict")
    store.confirm(draft_id, result)
    return {"draft_id": draft_id, "status": "confirmed", "result": result}


__all__ = [
    "DraftStore",
    "StateError",
    "DraftNotFound",
    "DraftNotActionable",
    "ExecutorRefused",
    "execute_draft",
    "reject_untrusted_payload",
    "APPROVAL_TTL",
]