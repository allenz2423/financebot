"""Concierge credential vault (B0). Encrypted at rest, mask-only reads,
tenant-partitioned. Master key: CONCIERGE_MASTER_KEY env or generated and
persisted next to the DB (mode 0600), mirroring the SECRET_KEY precedent.

Confinement invariant (2.10c): no public API returns plaintext except the
single executor-only ``resolve`` path; everything else returns masks.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.security import crypto

# kind -> delivery_mode. SCHEMA-ASSIGNED, never selected by the model.
KIND_DELIVERY_MODE = {
    "secret": "resolve_plaintext",
    "payment": "resolve_plaintext",  # + PCI rules enforced outside this module
    "session_profile": "mount_only",
}
KINDS = frozenset(KIND_DELIVERY_MODE)

FIELD_TYPES = frozenset({
    "text", "password", "otp", "card_pan", "card_exp",
    "card_cvv", "captcha", "token", "url",
})
FORCED_EPHEMERAL = frozenset({"card_cvv", "captcha"})
PAYMENT_ONLY_FIELDS = frozenset({"card_pan", "card_exp", "card_cvv"})
SECRET_FIELD_TYPES = frozenset({
    "password", "otp", "card_pan", "card_exp", "card_cvv", "captcha", "token",
})

DEFAULT_DB_PATH = os.getenv("CONCIERGE_DB_PATH", "data/concierge.db")
DEFAULT_MASTER_KEY_ENV = "CONCIERGE_MASTER_KEY"
MASTER_KEY_FILE = "concierge_master.key"
BULLET = "\u2022"  # mask bullet, kept out of source text on purpose

TENANT_KEY = "tenant"


class VaultError(ValueError):
    pass


class VaultRecordNotFound(VaultError):
    pass


class VaultScopeError(VaultError):
    pass


class VaultRevokedError(VaultError):
    pass


def bootstrap_master_key(db_path: str = DEFAULT_DB_PATH) -> str:
    """env or generate-and-persist (0600) a durable vault master key."""
    env_key = os.getenv(DEFAULT_MASTER_KEY_ENV, "").strip()
    if env_key:
        return env_key
    db_dir = Path(db_path).resolve().parent
    db_dir.mkdir(parents=True, exist_ok=True)
    key_file = db_dir / MASTER_KEY_FILE
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    key_file.write_text(key + "\n", encoding="utf-8")
    os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    print(f" [VAULT] master key generated at {key_file} (env {DEFAULT_MASTER_KEY_ENV} overrides)")
    return key


def mask_display(field_type: str, value: Any) -> str:
    """Mask-only display string for a single captured field."""
    text = str(value or "").strip()
    if not text:
        return ""
    if field_type == "card_pan":
        digits = re.sub(r"\D", "", text)
        return "".join([BULLET] * 4) + " " + digits[-4:]
    if field_type == "card_exp":
        return "".join([BULLET] * 2) + "/" + "".join([BULLET] * 2)
    if field_type == "token":
        return "tok_" + "".join([BULLET] * 4)
    if field_type in SECRET_FIELD_TYPES:
        return "".join([BULLET] * 4)
    return text[:120]


def _validate_domain(domain: str) -> None:
    d = str(domain or "").strip().lower().lstrip(".")
    ok = bool(d) and len(d) <= 253 and ".." not in d
    if ok:
        ok = bool(re.match(
            r"^[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?(\.[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?)*$",
            d,
        ))
    if not ok:
        raise VaultError(f"invalid consumer scope domain {domain!r}")


def _host_of(url: str) -> Optional[str]:
    host = urlparse(url).hostname
    return host.lower() if host else None


def _domain_matches(scope: str, host: Optional[str]) -> bool:
    """Exact host or suffixed subdomain: amazon.com covers www.amazon.com."""
    if not host:
        return False
    s = scope.strip().lower().lstrip(".*")
    if s.startswith("*."):
        s = s[2:]
    return host == s or host.endswith("." + s)


class Vault:
    """Credential store: encrypted at rest, mask-only reads, tenant-partitioned."""

    def __init__(self, db_path: Optional[str] = None, master_key: Optional[str] = None):
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        self.master_key = master_key or bootstrap_master_key(self.db_path)
        self.key = crypto.derive_key(self.master_key)
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
                CREATE TABLE IF NOT EXISTS vault_records (
                    record_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    delivery_mode TEXT NOT NULL,
                    label TEXT NOT NULL,
                    display TEXT NOT NULL,
                    consumer_scope TEXT NOT NULL,
                    policy TEXT NOT NULL DEFAULT '',
                    ciphertext TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vault_records_tenant ON vault_records(tenant)"
            )
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _field_payload(self, kind: str, fields: Dict[str, str]) -> Dict[str, str]:
        """Validate captured fields (unknown types REJECTED, never coerced;
        forced-ephemeral never stored; card types only under kind=payment)."""
        if not fields:
            raise VaultError("vault: capture intent requires at least one field")
        if len(fields) > 12:
            raise VaultError("vault: too many fields")
        normalized: Dict[str, str] = {}
        for key, value in fields.items():
            if "|" not in key:
                raise VaultError(f"vault: field key {key!r} must be '<type>|<label>'")
            ftype, _label = key.split("|", 1)
            if ftype not in FIELD_TYPES:
                raise VaultError(f"vault: unknown field type {ftype!r} (rejected, never coerced)")
            if ftype in FORCED_EPHEMERAL:
                raise VaultError(f"vault: field type {ftype!r} can never be vaulted")
            if ftype in PAYMENT_ONLY_FIELDS and kind != "payment":
                raise VaultError(f"vault: field type {ftype!r} requires kind=payment")
            normalized[key] = str(value)
        return normalized

    def store_fields(
        self,
        tenant: str,
        kind: str,
        label: str,
        fields: Dict[str, str],
        consumer_scope: List[str],
        policy: str = "",
    ) -> Dict[str, Any]:
        """Encrypt captured values into one vault record; returns masks only.

        ``fields`` keys are "<field_type>|<field_label>"; values arrive from
        the capture POST handler only, never from model context.
        """
        if kind not in KINDS:
            raise VaultError(f"vault: unknown kind {kind!r}")
        if not consumer_scope:
            raise VaultError("vault: consumer_scope is required and immutable at capture")
        for scope in consumer_scope:
            _validate_domain(scope)

        payload = self._field_payload(kind, fields)
        envelope = crypto.encrypt_str(
            self.key,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        record_id = f"{kind[0]}_{secrets.token_hex(6)}"
        display_parts = [
            mask_display(key.split("|", 1)[0], value) for key, value in payload.items()
        ]
        display = ", ".join(p for p in display_parts if p) or label
        scope_json = json.dumps([s.lower() for s in consumer_scope], sort_keys=True)

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO vault_records
                    (record_id, tenant, kind, delivery_mode, label, display,
                     consumer_scope, policy, ciphertext, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """, (
                record_id, tenant, kind, KIND_DELIVERY_MODE[kind], label, display,
                scope_json, policy, envelope, self._now(), self._now(),
            ))
            conn.commit()
        return {
            "vault_ref": record_id,
            "kind": kind,
            "delivery_mode": KIND_DELIVERY_MODE[kind],
            "label": label,
            "display": display,
            "consumer_scope": [s.lower() for s in consumer_scope],
            "policy": policy,
        }

    def store_session_profile(
        self,
        tenant: str,
        label: str,
        profile_bytes: bytes,
        consumer_scope: List[str],
        policy: str = "",
    ) -> Dict[str, Any]:
        """Store an encrypted session profile. mount_only: no plaintext path."""
        if not consumer_scope:
            raise VaultError("vault: consumer_scope is required")
        for scope in consumer_scope:
            _validate_domain(scope)
        record_id = f"profile_{secrets.token_hex(6)}"
        envelope = crypto.encrypt_bytes(self.key, profile_bytes)
        scope_json = json.dumps([s.lower() for s in consumer_scope], sort_keys=True)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO vault_records
                    (record_id, tenant, kind, delivery_mode, label, display,
                     consumer_scope, policy, ciphertext, status, created_at, updated_at)
                VALUES (?, ?, 'session_profile', 'mount_only', ?, ?, ?, ?, ?, 'active', ?, ?)
            """, (
                record_id, tenant, label, label[:60],
                scope_json, policy, envelope, self._now(), self._now(),
            ))
            conn.commit()
        return {
            "vault_ref": record_id,
            "kind": "session_profile",
            "delivery_mode": "mount_only",
            "label": label,
            "display": label[:60],
            "consumer_scope": [s.lower() for s in consumer_scope],
        }

    def _row_or_raise(self, tenant: str, vault_ref: str) -> sqlite3.Row:
        cur = self._conn().cursor()
        cur.execute(
            "SELECT * FROM vault_records WHERE record_id = ? AND tenant = ?",
            (vault_ref, tenant),
        )
        row = cur.fetchone()
        if not row:
            raise VaultRecordNotFound(f"vault_ref {vault_ref!r} not found for this tenant")
        return row

    @staticmethod
    def _mask(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "vault_ref": row["record_id"],
            "kind": row["kind"],
            "delivery_mode": row["delivery_mode"],
            "label": row["label"],
            "display": row["display"],
            "consumer_scope": json.loads(row["consumer_scope"] or "[]"),
            "policy": row["policy"],
        }

    def get_record(self, tenant: str, vault_ref: str) -> Dict[str, Any]:
        """Mask-only view of one record; no value field can be present."""
        return self._mask(self._row_or_raise(tenant, vault_ref))

    def list_records(self, tenant: str) -> List[Dict[str, Any]]:
        """Mask-only list of the tenant's active records."""
        cur = self._conn().cursor()
        cur.execute(
            "SELECT * FROM vault_records WHERE tenant = ? AND status = 'active' "
            "ORDER BY created_at DESC",
            (tenant,),
        )
        return [self._mask(r) for r in cur.fetchall()]

    def build_manifest(self, tenant: str) -> Dict[str, Any]:
        """Live-derived per-turn mask-only manifest for tool context."""
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for rec in self.list_records(tenant):
            by_kind.setdefault(rec["kind"], []).append({
                "vault_ref": rec["vault_ref"],
                "kind": rec["kind"],
                "display": rec["display"],
                "scope": rec["consumer_scope"],
                "policy": rec["policy"],
            })
        manifest: Dict[str, Any] = {}
        for kind in ("payment", "secret", "session_profile"):
            if by_kind.get(kind):
                manifest[kind + "s"] = by_kind[kind]
        return manifest

    def revoke(self, tenant: str, vault_ref: str) -> None:
        """Revoke a record. Manifests drop it next turn; the executor refuses
        it immediately even if it lingers in model context."""
        self.set_status(tenant, vault_ref, "revoked")

    def set_status(self, tenant: str, vault_ref: str, status: str) -> None:
        if status not in ("active", "revoked", "disabled"):
            raise VaultError(f"bad status {status!r}")
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE vault_records SET status = ?, updated_at = ? "
                "WHERE record_id = ? AND tenant = ?",
                (status, self._now(), vault_ref, tenant),
            )
            if cur.rowcount == 0:
                raise VaultRecordNotFound(f"vault_ref {vault_ref!r} not found for this tenant")
            conn.commit()

    def resolve(
        self,
        tenant: str,
        vault_ref: str,
        consumer_url: Optional[str] = None,
        allow_revoked: bool = False,
    ) -> bytes:
        """THE single plaintext-returning path, executor-only, fully guarded:
        tenant-bound, active-only, scope-checked when a consumer URL is given."""
        row = self._row_or_raise(tenant, vault_ref)
        if not allow_revoked and row["status"] != "active":
            raise VaultRevokedError(f"vault_ref {vault_ref!r} is {row['status']}")
        if KIND_DELIVERY_MODE.get(row["kind"]) != "resolve_plaintext":
            raise VaultError(
                f"vault_ref {vault_ref!r} is {row['kind']} (mount-only: no plaintext path)"
            )
        if consumer_url:
            host = _host_of(consumer_url)
            scopes = json.loads(row["consumer_scope"] or "[]")
            if not any(_domain_matches(s, host) for s in scopes):
                raise VaultScopeError(f"vault_ref {vault_ref!r} scope does not cover {host!r}")
        envelope = row["ciphertext"]
        if not envelope:
            raise VaultError(
                f"vault_ref {vault_ref!r} has no resolvable payload (session profiles mount only)"
            )
        return crypto.decrypt_bytes(self.key, envelope)

    def materialize_profile(self, tenant: str, vault_ref: str, dest_dir: str) -> str:
        """Mount-only decrypt path for session profiles (executor/browser only).

        ``resolve()`` keeps refusing mount_only;THIS is the designed mount path:
        decrypts to ``dest_dir`` (caller-owned tmpfs) and returns the unpacked profile
        dir, ready to hand to a headed browser.  The caller MUST destroy the
        dir after the session dies;the encrypted blob at rest never changes.
        """
        row = self._row_or_raise(tenant, vault_ref)
        if row["kind"] != "session_profile":
            raise VaultError(
                f"vault_ref {vault_ref!r} is {row['kind']} (only session profiles mount)"
            )
        if row["status"] != "active":
            raise VaultRevokedError(f"vault_ref {vault_ref!r} is {row['status']}")
        envelope = row["ciphertext"]
        if not envelope:
            raise VaultError(f"vault_ref {vault_ref!r} has no mountable payload")
        raw = crypto.decrypt_bytes(self.key, envelope)
        from src.security.profile import unpack_profile
        return str(unpack_profile(raw, dest_dir))


__all__ = [
    "Vault",
    "VaultError",
    "VaultRecordNotFound",
    "VaultScopeError",
    "VaultRevokedError",
    "bootstrap_master_key",
    "mask_display",
    "KIND_DELIVERY_MODE",
    "KINDS",
    "FIELD_TYPES",
    "FORCED_EPHEMERAL",
    "PAYMENT_ONLY_FIELDS",
    "SECRET_FIELD_TYPES",
    "DEFAULT_DB_PATH",
    "TENANT_KEY",
]