"""B1 noVNC login-intake sessions (services layer, Discord-free).

Track login-intake browser sessions end to end: spawn (open) -> done / killed
/ expired.  A session holds the per-tenant container identity, the one-time
VNC password, the published host port, and (on completion) the vault_ref of
the encrypted session profile.

Everything here is pure Python + SQLite; the Docker side is an injectable
runner so tests never touch a daemon.  The real container orchestration stays
behind ENABLE_CONCIERGE_BROWSER (see src.bot.concierge_login).
"""

from __future__ import annotations

import json
import secrets
import socket
import sqlite3
import string
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.security.profile import pack_profile_dir
from src.security.vault import Vault

VNC_PASSWORD_ALPHABET = string.ascii_letters + string.digits
VNC_PASSWORD_LENGTH = 16
DEFAULT_SESSION_TTL = timedelta(minutes=30)
DEFAULT_IMAGE = "financebot-concierge-browser:latest"
CONTAINER_PREFIX = "concierge-"


class BrowserSessionError(ValueError):
    pass


class SessionNotFound(BrowserSessionError):
    pass


class SessionNotOpen(BrowserSessionError):
    pass


def generate_vnc_password(length: int = VNC_PASSWORD_LENGTH) -> str:
    """One-time password for the noVNC client; random per session."""
    chars = []
    for _ in range(length):
        chars.append(secrets.choice(VNC_PASSWORD_ALPHABET))
    return "".join(chars)


class BrowserSessionStore:
    """SQLite registry of login-intake sessions (one row per spawned container)."""

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
                CREATE TABLE IF NOT EXISTS concierge_browser_sessions (
                    session_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    label TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    container_name TEXT NOT NULL,
                    vnc_password TEXT NOT NULL,
                    vnc_port INTEGER NOT NULL,
                    vault_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def next_vnc_port(
        self,
        base: int = 6081,
        check_host: Optional[bool] = None,
        exclude: Optional[set] = None,
    ) -> int:
        """First base+offset port free in the registry, the OS, and ``exclude``.

        The registry can outlive a container (daemon restart, manual removal,
        or an earlier failed spawn), so a database-only check is insufficient.

        ``exclude`` lets the caller pass host ports it knows are taken by
        something this process cannot see — e.g. the published ports of sibling
        Docker containers. The bot itself runs inside a container, so the OS
        bind probe below only sees the container's own loopback and would
        happily reuse a host port a concierge container still holds.
        """
        # Production uses the default range and must account for orphaned
        # Docker containers. Custom bases are retained as registry-only for
        # deterministic callers/tests that reserve ports abstractly.
        if check_host is None:
            check_host = base == 6081
        exclude = exclude or set()
        offset = 0
        while offset < 200:
            port = base + offset
            cur = self._conn().cursor()
            cur.execute(
                "SELECT 1 FROM concierge_browser_sessions WHERE vnc_port = ? "
                "AND status IN ('open', 'done')",
                (port,),
            )
            if cur.fetchone() is not None:
                offset += 1
                continue
            if port in exclude:
                offset += 1
                continue
            if not check_host:
                return port
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                # Some test sandboxes and restricted runtimes disallow socket
                # creation entirely.  The registry reservation is still
                # authoritative there; production runtimes continue to use
                # the OS probe when socket access is available.
                return port
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            except OSError:
                offset += 1
                continue
            finally:
                probe.close()
            return port
        raise BrowserSessionError("no free VNC port in range")

    def create(
        self,
        tenant: str,
        label: str,
        domain: str,
        vnc_port: int,
        vnc_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a login-intake session for one disposable container."""
        session_id = "ls_" + secrets.token_hex(6)
        password = vnc_password or generate_vnc_password()
        container_name = CONTAINER_PREFIX + session_id
        now = self._now()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO concierge_browser_sessions
                    (session_id, tenant, label, domain, status, container_name,
                     vnc_password, vnc_port, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """, (
                session_id, tenant, label, domain, container_name,
                password, int(vnc_port), now, now,
            ))
            conn.commit()
        return self.get(session_id, tenant=tenant)

    def _row(self, session_id: str) -> sqlite3.Row:
        cur = self._conn().cursor()
        cur.execute(
            "SELECT * FROM concierge_browser_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            raise SessionNotFound(f"browser session {session_id!r} not found")
        return row

    def get(self, session_id: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        row = self._row(session_id)
        if tenant is not None and row["tenant"] != tenant:
            raise SessionNotFound(f"browser session {session_id!r} not found for this tenant")
        return {
            "session_id": row["session_id"],
            "tenant": row["tenant"],
            "label": row["label"],
            "domain": row["domain"],
            "status": row["status"],
            "container_name": row["container_name"],
            "vnc_password": row["vnc_password"],
            "vnc_port": row["vnc_port"],
            "vault_ref": row["vault_ref"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def finish(self, session_id: str, tenant: str, vault_ref: str) -> Dict[str, Any]:
        """open -> done, recording the stored session-profile vault_ref."""
        row = self._row(session_id)
        if row["tenant"] != tenant:
            raise SessionNotFound(f"browser session {session_id!r} not found for this tenant")
        if row["status"] != "open":
            raise SessionNotOpen(f"browser session {session_id!r} is {row['status']}")
        with self._conn() as conn:
            conn.execute(
                "UPDATE concierge_browser_sessions "
                "SET status = 'done', vault_ref = ?, updated_at = ? "
                "WHERE session_id = ?",
                (vault_ref, self._now(), session_id),
            )
            conn.commit()
        return self.get(session_id, tenant=tenant)

    def kill(self, session_id: str, tenant: str) -> Dict[str, Any]:
        """open -> killed (cancelled without storing a profile)."""
        row = self._row(session_id)
        if row["tenant"] != tenant:
            raise SessionNotFound(f"browser session {session_id!r} not found for this tenant")
        if row["status"] != "open":
            raise SessionNotOpen(f"browser session {session_id!r} is {row['status']}")
        with self._conn() as conn:
            conn.execute(
                "UPDATE concierge_browser_sessions SET status = 'killed', updated_at = ? "
                "WHERE session_id = ?",
                (self._now(), session_id),
            )
            conn.commit()
        return self.get(session_id, tenant=tenant)

    def expire_stale(self, ttl: timedelta = DEFAULT_SESSION_TTL) -> int:
        """open sessions older than TTL -> expired."""
        cutoff = (datetime.now(timezone.utc) - ttl).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE concierge_browser_sessions SET status = 'expired', updated_at = ? "
                "WHERE status = 'open' AND created_at < ?",
                (self._now(), cutoff),
            )
            conn.commit()
            return cur.rowcount

    def list_for(self, tenant: str, limit: int = 20) -> List[Dict[str, Any]]:
        cur = self._conn().cursor()
        cur.execute(
            "SELECT * FROM concierge_browser_sessions WHERE tenant = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (tenant, limit),
        )
        return [self.get(r["session_id"]) for r in cur.fetchall()]

    def latest_ready_for_domain(self, tenant: str, domain: str) -> Optional[Dict[str, Any]]:
        """Return the newest completed headed browser for this tenant/domain.

        The active login browser is the browser the user can see in VNC. It is
        eligible both before and after the Done button so an approved action
        cannot silently jump to a second headless browser.
        """
        host = str(domain or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        cur = self._conn().cursor()
        # Sessions are stored under the raw host the user typed, which may keep
        # a "www." prefix, while every caller looks up the stripped host. Match
        # both forms so a www. login is still found and reused (and the agentic
        # mission attaches to the live authenticated browser instead of opening
        # a fresh headless one).
        cur.execute(
            "SELECT session_id FROM concierge_browser_sessions "
            "WHERE tenant = ? AND (domain = ? OR domain = ?) "
            "AND status IN ('open', 'done') "
            "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, "
            "updated_at DESC LIMIT 1",
            (tenant, host, "www." + host),
        )
        row = cur.fetchone()
        return self.get(row["session_id"], tenant=tenant) if row else None

    def retirable(self) -> List[Dict[str, Any]]:
        """Sessions whose disposable container should be reclaimed.

        Terminal sessions (expired / killed) are always retirable, and so is
        any open/done session that is not the newest ready browser for its
        (tenant, domain): only the newest login per tenant/domain can still be
        attached to, so an older headed Chromium (and its published noVNC
        port) is dead weight.  Previously nothing removed them — the login
        view only removed a container on an explicit Cancel — so an abandoned
        or superseded session leaked a Chromium per attempt, forever.

        The newest ready browser is NEVER retired, even when old: it is the
        one a mission can still attach to and the user may still be using, so
        ageing it out here would yank a live VNC session out from under them.
        A session only becomes reclaimable once it is superseded by a newer
        login for the same (tenant, domain), or is already terminal.
        """
        dead: Dict[str, Dict[str, Any]] = {}
        cur = self._conn().cursor()
        cur.execute(
            "SELECT session_id FROM concierge_browser_sessions "
            "WHERE status IN ('expired', 'killed')"
        )
        for row in cur.fetchall():
            dead[row["session_id"]] = self.get(row["session_id"])

        cur.execute(
            "SELECT session_id, tenant, domain FROM concierge_browser_sessions "
            "WHERE status IN ('open', 'done') ORDER BY updated_at DESC"
        )
        seen = set()
        for row in cur.fetchall():
            host = str(row["domain"] or "").lower()
            if host.startswith("www."):
                host = host[4:]
            key = (row["tenant"], host)
            if key in seen:
                dead[row["session_id"]] = self.get(row["session_id"])
            else:
                seen.add(key)
        return list(dead.values())

    def retire(self, session_id: str) -> None:
        """Mark a reclaimed session terminal so the reaper never re-sweeps it.

        ``'reaped'`` is deliberately distinct from ``'expired'``: the reaper
        selects ``expired``/``killed`` rows, so a row must leave those states
        once its container is gone, or every sweep would retry a container
        that no longer exists. ``'reaped'`` is excluded from the ready-browser
        lookups too, so it can never be attached to again.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE concierge_browser_sessions SET status = 'reaped', updated_at = ? "
                "WHERE session_id = ?",
                (self._now(), session_id),
            )
            conn.commit()


def build_docker_run(
    session: Dict[str, Any],
    image: str = DEFAULT_IMAGE,
    accel: str = "cpu",
    profile_tmpfs: str = "/tmp/profile",
    bind_host: str = "127.0.0.1",
    drm_device: str = "",
    drm_gid: str = "",
    docker_network: str = "",
    extra_env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """docker-run argv for one disposable login-intake container.

    ``accel`` follows the 2.6 knob: ``igpu`` attaches the host DRM node
    (required: ``drm_device`` + ``drm_gid``), ``cpu`` passes software-GL
    flags.  The profile lives on a tmpfs that dies with the container; the
    published noVNC port binds to ``bind_host`` (127.0.0.1 by default).
    """
    argv = [
        "docker", "run", "--rm", "-d",
        "--name", session["container_name"],
        "-e", "VNC_PASSWORD=" + session["vnc_password"],
        "-e", "PROFILE_DIR=" + profile_tmpfs,
        "-e", "CONCIERGE_ACCEL=" + accel,
        "-p", bind_host + ":" + str(session["vnc_port"]) + ":6080",
        "--tmpfs", profile_tmpfs + ":size=256m",
    ]
    if docker_network:
        argv.extend(["--network", docker_network])
    if accel == "igpu":
        if not drm_device or not drm_gid:
            raise BrowserSessionError(
                "CONCIERGE_ACCEL=igpu requires CONCIERGE_DRM_DEVICE and CONCIERGE_DRM_GID"
            )
        argv.append("--device")
        argv.append(drm_device + ":" + drm_device)
        argv.append("--group-add")
        argv.append(drm_gid)
    for key, value in (extra_env or {}).items():
        argv.append("-e")
        argv.append(key + "=" + str(value))
    argv.append(image)
    if session.get("domain"):
        argv.append(session["domain"])
    return argv


def complete_login_session(
    store: BrowserSessionStore,
    vault: Vault,
    session_id: str,
    tenant: str,
    fetch_profile_dir: Callable[[str, str], str],
    consumer_scope: List[str],
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """Finish an open session: pull the profile out, pack it, vault it.

    ``fetch_profile_dir(container_name, dest_dir)`` copies the container's
    profile into a caller-owned directory (real path: ``docker cp`` while the
    container is still alive; tests: a fixture).  Returns a MASK-ONLY
    completion record; the raw profile never leaves this call frame and the
    caller owns cleanup of ``dest_dir``.
    """
    sess = store.get(session_id, tenant=tenant)
    if sess["status"] != "open":
        raise SessionNotOpen(f"browser session {session_id!r} is {sess['status']}")
    profile_dir = fetch_profile_dir(sess["container_name"], "profile-" + sess["session_id"])
    blob = pack_profile_dir(profile_dir)
    rec = vault.store_session_profile(
        tenant, label or sess["label"], blob, consumer_scope,
        # a logged-in cookie profile is meant to be reused across acts, not
        # revoked when one proposal referencing it reaches a terminal state.
        policy="persistent",
    )
    store.finish(session_id, tenant, rec["vault_ref"])
    return {
        "vault_ref": rec["vault_ref"],
        "kind": rec["kind"],
        "delivery_mode": rec["delivery_mode"],
        "label": rec["label"],
        "consumer_scope": rec["consumer_scope"],
        "status": "stored",
    }


__all__ = [
    "BrowserSessionStore",
    "BrowserSessionError",
    "SessionNotFound",
    "SessionNotOpen",
    "generate_vnc_password",
    "build_docker_run",
    "complete_login_session",
    "DEFAULT_IMAGE",
    "CONTAINER_PREFIX",
]
