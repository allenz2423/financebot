"""Concierge tenant records + admin configuration (B0.

Hard multitenancy rules (2.8): tenant identity is interaction-derived and
never model-supplied; per-tenant partitions (``tenant`` column everywhere);
the admin set comes from config ONLY (``CONCIERGE_ADMINS``), never from
Discord roles or chat.  Least-privilege enable: default tier ``read``,
default spend cap $0.  Every admin action is audited by the caller.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

TIERS = frozenset({"read", "write", "spend"})


class TenantError(ValueError):
    pass


class NotAdminError(TenantError):
    pass


def admins_from_env() -> List[str]:
    raw = os.getenv("CONCIERGE_ADMINS", "").strip()
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def is_admin(user_id: str, admins: Optional[List[str]] = None) -> bool:
    admins = admins_from_env() if admins is None else admins
    return str(user_id) in admins


class TenantStore:
    def __init__(self, db_path: str, admins: Optional[List[str]] = None):
        self.db_path = str(db_path)
        self.admins = list(admins) if admins is not None else admins_from_env()
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
                CREATE TABLE IF NOT EXISTS concierge_tenants (
                    tenant TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    tier TEXT NOT NULL DEFAULT 'read',
                    spend_cap REAL NOT NULL DEFAULT 0.0,
                    allow_domains TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _require_admin(self, actor: str) -> None:
        if not is_admin(actor, self.admins):
            raise NotAdminError(f"{actor} is not a concierge admin")

    def enable(self, actor: str, tenant: str, tier: str = "read", spend_cap: float = 0.0) -> Dict[str, Any]:
        """Least-privilege enable: read tier, $0 cap unless explicitly raised."""
        self._require_admin(actor)
        if tier not in TIERS:
            raise TenantError(f"tier must be one of {sorted(TIERS)}")
        if spend_cap < 0:
            raise TenantError("spend_cap must be >= 0")
        now = self._now()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO concierge_tenants (tenant, enabled, tier, spend_cap, created_at, updated_at)
                VALUES (?, 1, ?, ?, ?, ?)
                ON CONFLICT(tenant) DO UPDATE SET
                    enabled = 1, tier = excluded.tier, spend_cap = excluded.spend_cap,
                    updated_at = excluded.updated_at
            """, (tenant, tier, float(spend_cap), now, now))
            conn.commit()
        return self.status(tenant)

    def disable(self, actor: str, tenant: str) -> Dict[str, Any]:
        self._require_admin(actor)
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE concierge_tenants SET enabled = 0, updated_at = ? WHERE tenant = ?",
                (self._now(), tenant),
            )
            conn.commit()
        return self.status(tenant)

    def set_tier(self, actor: str, tenant: str, tier: str) -> Dict[str, Any]:
        self._require_admin(actor)
        if tier not in TIERS:
            raise TenantError(f"tier must be one of {sorted(TIERS)}")
        with self._conn() as conn:
            conn.execute(
                "UPDATE concierge_tenants SET tier = ?, updated_at = ? WHERE tenant = ?",
                (tier, self._now(), tenant),
            )
            conn.commit()
        return self.status(tenant)

    def set_spend_cap(self, actor: str, tenant: str, cap: float) -> Dict[str, Any]:
        self._require_admin(actor)
        if cap < 0:
            raise TenantError("spend_cap must be >= 0")
        with self._conn() as conn:
            conn.execute(
                "UPDATE concierge_tenants SET spend_cap = ?, updated_at = ? WHERE tenant = ?",
                (float(cap), self._now(), tenant),
            )
            conn.commit()
        return self.status(tenant)

    def allow_domain(self, actor: str, tenant: str, domain: str) -> Dict[str, Any]:
        self._require_admin(actor)
        domain = str(domain or "").strip().lower()
        if not domain or ".." in domain:
            raise TenantError("invalid domain")
        row = self._row(tenant)
        domains = set(json.loads(row["allow_domains"] or "[]"))
        domains.add(domain)
        with self._conn() as conn:
            conn.execute(
                "UPDATE concierge_tenants SET allow_domains = ?, updated_at = ? WHERE tenant = ?",
                (json.dumps(sorted(domains), separators=(",", ":")), self._now(), tenant),
            )
            conn.commit()
        return self.status(tenant)

    def _row(self, tenant: str) -> sqlite3.Row:
        cur = self._conn().cursor()
        cur.execute("SELECT * FROM concierge_tenants WHERE tenant = ?", (tenant,))
        row = cur.fetchone()
        if not row:
            raise TenantError(f"tenant {tenant!r} is not enabled for concierge")
        return row

    def status(self, tenant: str) -> Dict[str, Any]:
        try:
            row = self._row(tenant)
        except TenantError:
            return {"tenant": tenant, "enabled": False}
        return {
            "tenant": row["tenant"],
            "enabled": bool(row["enabled"]),
            "tier": row["tier"],
            "spend_cap": row["spend_cap"],
            "allow_domains": json.loads(row["allow_domains"] or "[]"),
        }

    def is_enabled(self, tenant: str) -> bool:
        try:
            return bool(self._row(tenant)["enabled"])
        except TenantError:
            return False

    def list_tenants(self) -> List[Dict[str, Any]]:
        cur = self._conn().cursor()
        cur.execute("SELECT tenant FROM concierge_tenants ORDER BY created_at DESC")
        return [self.status(r["tenant"]) for r in cur.fetchall()]


__all__ = [
    "TenantStore",
    "TenantError",
    "NotAdminError",
    "is_admin",
    "admins_from_env",
    "TIERS",
]