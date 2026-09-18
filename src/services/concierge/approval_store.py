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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.security.vault import DEFAULT_DB_PATH as _DB, Vault

# Proposals born on this branch may predate the allowed_domains column.
_TERMINAL = frozenset({"executed", "rolled_back", "rejected", "expired", "denied"})
_DEFAULT_APPROVAL_TTL = 600.0
# Auto-pilot mission lifetime: one approval grants follow-up acts on the same
# target domain until the mission lapses, so a login flow can keep going
# (healing interstitials, corrected steps) without re-approval.
_MISSION_TTL = 1800.0
# Records stored through the user-owned capture boundary (policy='persistent')
# survive proposal terminal transitions: they were captured for reuse, not
# vaulted for a single act.
_PERSISTENT_POLICIES = frozenset({"persistent"})


def db_path() -> str:
    return _DB


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for DBs created before B2 hardening."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(concierge_act_proposals)").fetchall()}
    if "approval_ttl" not in cols:
        conn.execute(
            "ALTER TABLE concierge_act_proposals ADD COLUMN approval_ttl REAL NOT NULL DEFAULT 600.0"
        )
    if "allowed_domains" not in cols:
        conn.execute(
            "ALTER TABLE concierge_act_proposals ADD COLUMN allowed_domains TEXT NOT NULL DEFAULT '[]'"
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
            approval_ttl  REAL NOT NULL DEFAULT 600.0,
            allowed_domains TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_concierge_act_tenant
        ON concierge_act_proposals(tenant, status, created_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concierge_act_missions (
            mission_id         TEXT PRIMARY KEY,
            tenant             TEXT NOT NULL,
            uid                TEXT NOT NULL,
            kind               TEXT NOT NULL,
            domain             TEXT NOT NULL,
            url                TEXT,
            origin_proposal_id TEXT,
            created_at         TEXT NOT NULL,
            expires_at         TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'active'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_concierge_act_mission_scope
        ON concierge_act_missions(tenant, kind, domain, status)
    """)
    _migrate_schema(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(concierge_act_missions)").fetchall()}
    if "state_vault_ref" not in cols:
        conn.execute("ALTER TABLE concierge_act_missions ADD COLUMN state_vault_ref TEXT")
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
        allowed_domains: Optional[List[str]] = None,
    ) -> str:
        """Insert a pending proposal; returns its proposal_id.

        ``allowed_domains`` is persisted (not re-fetched at execution time) so
        the approve callback re-validates against exactly what was gated at
        propose time, while remaining independently re-checked per execution.
        """
        proposal_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            conn.execute(
                """INSERT INTO concierge_act_proposals
                   (proposal_id, tenant, uid, kind, args, url, steps, status,
                    created_at, approval_ttl, allowed_domains)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    proposal_id, tenant, uid, kind,
                    json.dumps(args, sort_keys=True),
                    url,
                    json.dumps(steps) if steps else None,
                    now,
                    float(approval_ttl),
                    json.dumps(allowed_domains or [], sort_keys=True),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return proposal_id

    # ------------------------------------------------------------------
    # Auto-pilot missions: a single approved proposal grants follow-up acts
    # on the same target (tenant, kind, domain) until the mission lapses.
    # ------------------------------------------------------------------

    def grant_mission(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Create/refresh the active auto-pilot mission for an approved proposal.

        Called on approval: the ✅ now covers follow-up acts on the same
        target domain for ``_MISSION_TTL`` seconds, so the model can keep
        going (corrected steps, healing interstitials) without asking the
        user again. An existing active mission for the same scope is
        refreshed (window extended) rather than duplicated.
        """
        proposal = self.get(proposal_id)
        host = _host_of((proposal or {}).get("url"))
        if not proposal or not host:
            return None
        self.expire_missions()
        existing = self.active_mission(proposal["tenant"], proposal["kind"], host)
        if existing:
            return self._extend_mission(existing["mission_id"])
        mission_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=_MISSION_TTL)
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            conn.execute(
                """INSERT INTO concierge_act_missions
                   (mission_id, tenant, uid, kind, domain, url,
                    origin_proposal_id, created_at, expires_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                (
                    mission_id, proposal["tenant"], proposal["uid"],
                    proposal["kind"], host, proposal["url"],
                    proposal_id, now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.active_mission(proposal["tenant"], proposal["kind"], host)

    def _extend_mission(self, mission_id: str) -> Optional[Dict[str, Any]]:
        expires = datetime.now(timezone.utc) + timedelta(seconds=_MISSION_TTL)
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """UPDATE concierge_act_missions
                    SET expires_at = ?, status = 'active'
                    WHERE mission_id = ?""",
                (expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), mission_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM concierge_act_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def active_mission(self, tenant: str, kind: str, domain: str) -> Optional[Dict[str, Any]]:
        """The active auto-pilot mission for a scope, if any (expiry enforced)."""
        self.expire_missions()
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT * FROM concierge_act_missions
                   WHERE tenant = ? AND kind = ? AND domain = ? AND status = 'active'
                   ORDER BY created_at DESC LIMIT 1""",
                (tenant, kind, _host_of(domain) or domain),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def active_mission_for(self, proposal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Scope lookup for a freshly proposed act (dispatcher auto-pilot gate)."""
        host = _host_of(proposal.get("url"))
        if not host or not proposal.get("tenant"):
            return None
        return self.active_mission(proposal["tenant"], proposal.get("kind", ""), host)

    def active_missions_for_tenant(self, tenant: str, limit: int = 10) -> List[Dict[str, Any]]:
        """All active missions for the tenant (model-visible concierge context)."""
        self.expire_missions()
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT mission_id, kind, domain, url, origin_proposal_id,
                          state_vault_ref, created_at, expires_at
                   FROM concierge_act_missions
                   WHERE tenant = ? AND status = 'active'
                   ORDER BY created_at DESC LIMIT ?""",
                (tenant, limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def cancel_missions_for_tenant(self, tenant: str) -> int:
        """Revoke all still-active browser missions for a tenant."""
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            cur = conn.execute(
                """UPDATE concierge_act_missions
                   SET status = 'cancelled'
                   WHERE tenant = ? AND status = 'active'""",
                (tenant,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def set_mission_state_ref(self, mission_id: str, tenant: str, vault_ref: str) -> None:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            conn.execute(
                "UPDATE concierge_act_missions SET state_vault_ref = ? "
                "WHERE mission_id = ? AND tenant = ? AND status = 'active'",
                (vault_ref, mission_id, tenant),
            )
            conn.commit()
        finally:
            conn.close()

    def expire_missions(self) -> int:
        """Lapse missions past their TTL (server-side, not just view timeout)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            cur = conn.execute(
                """UPDATE concierge_act_missions
                    SET status = 'expired'
                    WHERE status = 'active' AND expires_at <= ?""",
                (now,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def recent_for_tenant(self, tenant: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Latest proposals for the tenant regardless of status (status tool).

        Powers ``concierge_act_status`` so the model can answer "is the
        login done / what happened" from real post-approval state instead of
        guessing from the approval prompt. Read-only.
        """
        self.expire_stale()
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT proposal_id, kind, url, status, created_at, approved_at,
                          executed_at, result_detail
                   FROM concierge_act_proposals
                   WHERE tenant = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (tenant, limit),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            item = {
                "proposal_id": r["proposal_id"],
                "kind": r["kind"],
                "url": r["url"],
                "status": r["status"],
                "created_at": r["created_at"],
                "approved_at": r["approved_at"],
                "executed_at": r["executed_at"],
            }
            try:
                item["result_detail"] = json.loads(r["result_detail"]) if r["result_detail"] else None
            except Exception:
                item["result_detail"] = None
            out.append(item)
        return out

    def get(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        self.expire_stale()
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
            "allowed_domains": json.loads(row["allowed_domains"] or "[]"),
        }

    def claim(self, proposal_id: str) -> bool:
        """Atomically claim a pending proposal for execution.

        Only one caller can win: the transition is a single guarded UPDATE
        (``WHERE proposal_id = ? AND status = 'pending'``), so concurrent
        double-clicks cannot double-execute. Also enforces the approval TTL
        server-side: proposals whose window lapsed transition to ``expired``
        instead of executing.
        """
        row = self.get(proposal_id)
        if not row or row["status"] != "pending":
            return False
        created = _parse_ts(row.get("created_at"))
        raw_ttl = row.get("approval_ttl")
        ttl = float(raw_ttl) if raw_ttl is not None else _DEFAULT_APPROVAL_TTL
        if datetime.now(timezone.utc) >= created + timedelta(seconds=ttl):
            self.update_status(
                proposal_id, "expired",
                result_detail={"reason": "approval window expired before approval"},
            )
            return False
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        try:
            cur = conn.execute(
                """UPDATE concierge_act_proposals
                    SET status = 'approved', approved_at = datetime('now', 'localtime')
                    WHERE proposal_id = ? AND status = 'pending'""",
                (proposal_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def expire_stale(self, now_ts: Optional[datetime] = None) -> int:
        """Transition pending proposals past their approval TTL to ``expired``.

        Server-side expiry is what makes the 120/600s windows enforceable —
        the Discord view timeout alone never catches a prompt whose message
        failed to send or whose callback never fired. Revokes any vault
        secrets vaulted for the expired proposals.
        """
        now = now_ts or datetime.now(timezone.utc)
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT proposal_id, tenant, steps, created_at, approval_ttl "
                "FROM concierge_act_proposals WHERE status = 'pending'"
            ).fetchall()
            expired = []
            for row in rows:
                created = _parse_ts(row["created_at"])
                raw_ttl = row["approval_ttl"]
                ttl = float(raw_ttl) if raw_ttl is not None else _DEFAULT_APPROVAL_TTL
                if now >= created + timedelta(seconds=ttl):
                    expired.append(row)
            for row in expired:
                conn.execute(
                    """UPDATE concierge_act_proposals
                        SET status = 'expired', approved_at = datetime('now', 'localtime')
                        WHERE proposal_id = ? AND status = 'pending'""",
                    (row["proposal_id"],),
                )
            conn.commit()
        finally:
            conn.close()
        for row in expired:
            self._revoke_refs(
                row["tenant"],
                _refs_from_steps(json.loads(row["steps"]) if row["steps"] else []),
            )
        return len(expired)

    def update_status(
        self, proposal_id: str, status: str, result_detail: Optional[Dict[str, Any]] = None
    ) -> bool:
        now_col = "approved_at" if status in ("approved", "rejected", "expired") else "executed_at"
        tenant, refs = None, []
        if status in _TERMINAL:
            row = self.get(proposal_id)
            if row:
                tenant = row.get("tenant")
                refs = _refs_from_steps(row.get("steps") or [])
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
            ok = cur.rowcount > 0
        finally:
            conn.close()
        if ok and tenant and refs:
            self._revoke_refs(tenant, refs)
        return ok

    def _revoke_refs(self, tenant: Optional[str], refs: List[str]) -> None:
        """Best-effort revoke of per-proposal vault secrets once terminal.

        User-stored credentials (policy='persistent') are skipped: they were
        captured for reuse and stay active for the next act; only secrets
        vaulted for this specific proposal are revoked.
        """
        if not tenant or not refs:
            return
        try:
            vault = Vault(self.db_path)
            for ref in refs:
                try:
                    if vault.get_record(tenant, ref).get("policy", "") in _PERSISTENT_POLICIES:
                        continue
                    vault.revoke(tenant, ref)
                except Exception:  # noqa: BLE001 — revocation is best-effort
                    pass
        except Exception:  # noqa: BLE001 — never block the status transition
            pass

    def pending_for_tenant(self, tenant: str, limit: int = 20) -> List[Dict[str, Any]]:
        self.expire_stale()
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


def _parse_ts(ts: Optional[str]) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _host_of(url: Any) -> str:
    """Bare host of a proposal URL, www-stripped, for mission scope keying."""
    if not url:
        return ""
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


def _refs_from_steps(steps: Any) -> List[str]:
    return [
        str(s["vault_ref"]) for s in (steps or [])
        if isinstance(s, dict) and s.get("vault_ref")
    ]


__all__ = ["ApprovalStore", "db_path"]
