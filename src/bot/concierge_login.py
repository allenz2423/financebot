"""B1 noVNC login intake — Discord layer (flag-gated, safe by default).

``!concierge login <domain>`` (tenant self-service): spawns a disposable
concierge-browser container with a fresh tmpfs profile + one-time VNC
password, posts the noVNC link with a [✅ Done] / [✕ Cancel] owner-checked
view, and on [✅ Done] packs the container's profile into the vault as an
encrypted ``session_profile`` (mount-only).  On [✕ Cancel] the session is
killed and the container removed; nothing is stored.

The real Docker path runs only when ENABLE_CONCIERGE_BROWSER=1; with the
flag off the flow refuses with an audit row (safe-by-default in the public
repo).  Spawn/fetch/remove are injectable so tests never touch a daemon.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import discord

from src.core.state import bot
from src.security.vault import DEFAULT_DB_PATH as _DB
from src.security.vault import Vault
from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import domain_matches
from src.services.concierge.browser_sessions import (
    BrowserSessionError,
    BrowserSessionStore,
    build_docker_run,
    complete_login_session,
)
from src.services.concierge.tenants import TenantStore

AUDIT = AuditLog(_DB)
SESSIONS = BrowserSessionStore(_DB)
TENANTS = TenantStore(_DB)
VAULT = Vault(_DB)

ENABLE_ENV = "ENABLE_CONCIERGE_BROWSER"
ACCEL_ENV = "CONCIERGE_ACCEL"
IMAGE_ENV = "CONCIERGE_BROWSER_IMAGE"
DEVICE_ENV = "CONCIERGE_DRM_DEVICE"
GID_ENV = "CONCIERGE_DRM_GID"
BIND_ENV = "CONCIERGE_VNC_BIND"
PUBLIC_BASE_ENV = "CONCIERGE_VNC_PUBLIC_BASE"
NETWORK_ENV = "CONCIERGE_DOCKER_NETWORK"
INTERNAL_NETWORK_ENV = "CONCIERGE_DOCKER_INTERNAL_NETWORK"

_SAFE_DOMAIN_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-*")


def browser_enabled() -> bool:
    return os.getenv(ENABLE_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


def _valid_domain(domain: str) -> bool:
    d = str(domain or "").strip().lower()
    if not d:
        return False
    if len(d) > 253:
        return False
    if ".." in d:
        return False
    if "/" in d or ":" in d or "@" in d:
        return False
    if d.startswith(".") or d.endswith("."):
        return False
    for ch in d:
        if ch not in _SAFE_DOMAIN_CHARS:
            return False
    return "." in d


def _audit(actor: str, action: str, tenant: str, **detail) -> None:
    AUDIT.append(actor=actor, action=action, tenant=tenant,
                 subject="login-intake", detail=detail)


def docker_spawn(argv: List[str]) -> str:
    """Run a docker-run argv (built by build_docker_run) and return the id."""
    out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        msg = (out.stderr or out.stdout or "").strip()[:300]
        raise BrowserSessionError("docker spawn failed: " + msg)
    container_id = out.stdout.strip()
    internal_network = os.getenv(INTERNAL_NETWORK_ENV, "").strip()
    if internal_network:
        # Start on the egress network so it supplies the default route, then
        # attach the private network so this API can resolve and proxy noVNC.
        # Docker run accepts only one --network, so attach the second network
        # immediately after creation. If that fails, remove the container
        # rather than handing the user a browser that can only display blank
        # pages.
        name_idx = argv.index("--name") + 1
        container_name = argv[name_idx]
        connect = subprocess.run(
            ["docker", "network", "connect", internal_network, container_name],
            capture_output=True, text=True, timeout=60,
        )
        if connect.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, text=True, timeout=60,
            )
            msg = (connect.stderr or connect.stdout or "").strip()[:300]
            raise BrowserSessionError("docker private-network attach failed: " + msg)
    return container_id


def docker_fetch_profile(container_name: str, dest_name: str) -> str:
    """docker cp the container's tmpfs profile into a host scratch dir."""
    dest = Path(tempfile.gettempdir()) / "concierge-login" / dest_name
    dest.mkdir(parents=True, exist_ok=True)
    argv = ["docker", "cp", container_name + ":/tmp/profile/.", str(dest)]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        msg = (out.stderr or out.stdout or "").strip()[:300]
        raise BrowserSessionError("docker cp failed: " + msg)
    return str(dest)


def docker_remove_container(container_name: str) -> None:
    argv = ["docker", "rm", "-f", container_name]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        msg = (out.stderr or out.stdout or "").strip()[:300]
        raise BrowserSessionError("docker rm failed: " + msg)


def docker_published_host_ports() -> set:
    """Host TCP ports published by running containers (best-effort).

    The bot runs *inside* a container, so an OS bind probe of ``127.0.0.1``
    checks the container's own loopback, not the host's: a sibling concierge
    container holding a published host port is invisible to
    ``next_vnc_port``. Ask Docker directly so a fresh spawn never reuses a
    port that an orphaned container still holds.
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Ports}}"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001 — no daemon/access: fall back to the OS probe
        return set()
    if out.returncode != 0:
        return set()
    ports: set = set()
    for match in re.findall(r":(\d+)->", out.stdout or ""):
        try:
            ports.add(int(match))
        except ValueError:
            pass
    return ports


def reap_expired_sessions(
    store: Optional[BrowserSessionStore] = None,
    remover: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """Reclaim disposable containers for dead or superseded login sessions.

    Idempotent and best-effort: a container already gone (``--rm``) is not an
    error.  Runs periodically from ``concierge_browser_reaper_loop`` so an
    abandoned session cannot leak a headed Chromium and its published noVNC
    port forever — previously only an explicit ✕ Cancel removed a container.
    """
    store = store or SESSIONS
    remover = remover or docker_remove_container
    removed: List[str] = []
    for sess in store.retirable():
        try:
            remover(sess["container_name"])
        except Exception as exc:  # noqa: BLE001 — the container may already be gone
            print(f" [CONCIERGE REAPER] remove {sess['container_name']} skipped: {exc}")
        store.retire(sess["session_id"])
        removed.append(sess["container_name"])
    return removed


async def concierge_browser_reaper_loop(interval_seconds: int = 600) -> None:
    """Long-lived task: periodically reclaim stale concierge login containers."""
    if not browser_enabled():
        print(" [CONCIERGE REAPER] concierge browser disabled; loop not started.")
        return
    print(f" [CONCIERGE REAPER] loop starting (interval={interval_seconds}s).")
    while True:
        try:
            removed = await asyncio.to_thread(reap_expired_sessions)
            if removed:
                print(f" [CONCIERGE REAPER] reclaimed {len(removed)} container(s): {removed}")
        except Exception as exc:  # noqa: BLE001
            print(f" [CONCIERGE REAPER] iteration failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(interval_seconds)


class LoginSessionView(discord.ui.View):
    """Owner-checked [✅ Done] / [✕ Cancel] buttons for one login session."""

    def __init__(
        self,
        session_id: str,
        owner_uid: int,
        session: Dict[str, Any],
        store: Optional[BrowserSessionStore] = None,
        vault: Optional[Vault] = None,
        fetcher: Optional[Callable[[str, str], str]] = None,
        remover: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(timeout=600)
        self.session_id = session_id
        self.owner_uid = int(owner_uid)
        self.session = session
        self.store = store or SESSIONS
        self.vault = vault or VAULT
        self.fetcher = fetcher or docker_fetch_profile
        self.remover = remover or docker_remove_container

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_uid

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success)
    async def _done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the person who started this login can finish it.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        actor = "user:" + str(interaction.user.id)
        scratch: Optional[str] = None
        try:
            sess = self.store.get(self.session_id, tenant=actor)
            scratch = self.fetcher(sess["container_name"], "profile-" + sess["session_id"])
            rec = complete_login_session(
                self.store, self.vault, self.session_id, actor, self.fetcher,
                consumer_scope=[sess["domain"]],
            )
            # Keep the headed Chromium alive: approved agentic missions attach
            # to this exact process, which is also what the user's VNC shows.
            # Cancellation still removes the container.
            _audit(actor, "login_done", actor, vault_ref=rec["vault_ref"],
                   domain=sess["domain"])
            await interaction.followup.send(
                "✅ Profile saved. Session **" + rec["label"]
                + "** stored (mount-only, scope: " + ", ".join(rec["consumer_scope"])
                + "). Logged-in reads are now possible on that domain.",
                ephemeral=True,
            )
        except Exception as exc:
            _audit(actor, "login_error", actor, error=str(exc)[:300])
            try:
                await interaction.followup.send(
                    "⚠️ Couldn't finalize the login session: " + str(exc)[:300],
                    ephemeral=True,
                )
            except Exception:
                pass
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

    @discord.ui.button(label="✕ Cancel", style=discord.ButtonStyle.danger)
    async def _cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the person who started this login can cancel it.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        actor = "user:" + str(interaction.user.id)
        try:
            sess = self.store.get(self.session_id, tenant=actor)
            container = sess["container_name"]
            # Remove the disposable container even if the row already moved
            # past 'open' (e.g. expired by the TTL). Previously kill() raised
            # and the remover never ran, leaking the container and its
            # published port permanently.
            try:
                self.store.kill(self.session_id, actor)
            except BrowserSessionError as kill_exc:
                _audit(actor, "login_kill_noop", actor, domain=sess["domain"],
                       reason=str(kill_exc)[:200])
            self.remover(container)
            _audit(actor, "login_killed", actor, domain=sess["domain"])
            await interaction.followup.send(
                "✕ Login session cancelled; the disposable browser is gone.",
                ephemeral=True,
            )
        except Exception as exc:
            _audit(actor, "login_error", actor, error=str(exc)[:300])
            try:
                await interaction.followup.send(
                    "⚠️ Couldn't cancel the login session: " + str(exc)[:300],
                    ephemeral=True,
                )
            except Exception:
                pass
        finally:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass


async def handle_login(ctx, raw_domain: str) -> None:
    """``!concierge login <domain>`` — spawn a noVNC login-intake session."""
    tenant = "user:" + str(ctx.author.id)
    actor = tenant
    domain = str(raw_domain or "").strip().lower()
    lstrip = domain
    while lstrip.startswith("*."):
        lstrip = lstrip[2:]
    if not _valid_domain(domain):
        _audit(actor, "login_refused", tenant, reason="invalid domain")
        await ctx.send("Usage: `!concierge login <domain>` with a plain hostname.")
        return
    if not TENANTS.is_enabled(tenant):
        _audit(actor, "login_refused", tenant, reason="tenant not enabled")
        await ctx.send("Concierge is not enabled for you yet (an admin must run `!concierge enable`).")
        return
    allowed = TENANTS.status(tenant)["allow_domains"]
    ok = False
    for a in allowed:
        if domain_matches(a, lstrip):
            ok = True
            break
    if not ok:
        _audit(actor, "login_refused", tenant, reason="domain not allowlisted")
        await ctx.send(
            "Domain `" + domain + "` is not on your concierge allowlist "
            "(an admin must run `!concierge allow @you " + domain + "` first)."
        )
        return
    if not browser_enabled():
        _audit(actor, "login_refused", tenant, reason="ENABLE_CONCIERGE_BROWSER off")
        await ctx.send(
            "The concierge browser is disabled. Set `" + ENABLE_ENV + "=1` to "
            "allow login-intake sessions."
        )
        return
    SESSIONS.expire_stale()

    # The headed browser is the user's visible session and is also the session
    # agentic missions attach to. Never create a second VNC for the same
    # tenant/domain while one is still available.
    existing = SESSIONS.latest_ready_for_domain(tenant, lstrip)
    if existing:
        public_base = os.getenv(PUBLIC_BASE_ENV, "").strip().rstrip("/")
        if public_base:
            link = public_base + "/concierge/vnc/" + existing["session_id"] \
                + "/vnc.html?autoconnect=1&password=" + existing["vnc_password"]
        else:
            link = "http://" + os.getenv(BIND_ENV, "127.0.0.1").strip() + ":" \
                + str(existing["vnc_port"]) + "/vnc.html?autoconnect=1&password=" \
                + existing["vnc_password"]
        _audit(actor, "login_reuse", tenant, domain=domain,
               session_id=existing["session_id"])
        await ctx.send(
            "🌐 **Existing concierge browser reused for " + domain + "**\n"
            "I did not start another VNC session.\n\n🔗 <" + link + ">"
        )
        return

    # Reclaim dead/superseded containers *now* before allocating a port. An
    # expired session's Chromium keeps its published host port until the
    # container is actually removed, and the 10-minute reaper loop may not have
    # run since the last restart.
    try:
        reap_expired_sessions()
    except Exception as exc:  # noqa: BLE001 — best-effort; allocation below is still guarded
        print(f" [CONCIERGE LOGIN] pre-spawn reclaim skipped: {exc}")

    # Ports held by sibling containers are invisible to the in-process OS
    # probe (we run inside a container), so exclude Docker's published ports
    # and retry with the next one if a spawn still collides.
    occupied = docker_published_host_ports()
    sess = None
    port = 0
    last_exc: Optional[Exception] = None
    for _attempt in range(3):
        port = SESSIONS.next_vnc_port(exclude=occupied)
        sess = SESSIONS.create(tenant, domain + " login", domain, port)
        argv = build_docker_run(
            sess,
            image=os.getenv(IMAGE_ENV, "").strip() or "financebot-concierge-browser:latest",
            accel=os.getenv(ACCEL_ENV, "cpu").strip().lower(),
            bind_host=os.getenv(BIND_ENV, "127.0.0.1").strip(),
            drm_device=os.getenv(DEVICE_ENV, "").strip(),
            drm_gid=os.getenv(GID_ENV, "").strip(),
            docker_network=os.getenv(NETWORK_ENV, "").strip(),
        )
        try:
            docker_spawn(argv)
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — clash: try the next port
            last_exc = exc
            try:
                SESSIONS.kill(sess["session_id"], tenant)
            except Exception:
                pass
            occupied.add(port)
            sess = None
    if sess is None:
        _audit(actor, "login_error", tenant, error=str(last_exc)[:300])
        await ctx.send("⚠️ Couldn't start the concierge browser: " + str(last_exc)[:300])
        return
    _audit(actor, "login_spawn", tenant, domain=domain,
           session_id=sess["session_id"], container=sess["container_name"])
    public_base = os.getenv(PUBLIC_BASE_ENV, "").strip().rstrip("/")
    if public_base:
        link = public_base + "/concierge/vnc/" + sess["session_id"] \
            + "/vnc.html?autoconnect=1&password=" + sess["vnc_password"]
    else:
        link = "http://" + os.getenv(BIND_ENV, "127.0.0.1").strip() + ":" + str(port) \
            + "/vnc.html?autoconnect=1&password=" + sess["vnc_password"]
    view = LoginSessionView(sess["session_id"], ctx.author.id, sess)
    access_note = (
        "(This session is protected by its one-time VNC password and expires automatically.)"
        if public_base else
        "(The noVNC port binds to localhost only — remote solving needs an SSH tunnel.)"
    )
    msg = (
        "🌐 **Login intake for " + domain + "**\n"
        "A disposable browser is up. Log in on the merchant's own site; your "
        "passwords never touch Discord.\n\n"
        "🔗 <" + link + ">\n"
        + access_note + "\n\n"
        "Click **✅ Done** when you're logged in, or **✕ Cancel** to abort."
    )
    try:
        await ctx.author.send(msg, view=view)
    except Exception:
        await ctx.send(msg, view=view)


__all__ = [
    "LoginSessionView",
    "handle_login",
    "docker_spawn",
    "docker_fetch_profile",
    "docker_remove_container",
    "reap_expired_sessions",
    "concierge_browser_reaper_loop",
    "browser_enabled",
]
