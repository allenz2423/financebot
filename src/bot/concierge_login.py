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

import os
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
    return out.stdout.strip()


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
            self.remover(sess["container_name"])
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
            self.store.kill(self.session_id, actor)
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
    port = SESSIONS.next_vnc_port()
    sess = SESSIONS.create(tenant, domain + " login", domain, port)
    argv = build_docker_run(
        sess,
        image=os.getenv(IMAGE_ENV, "").strip() or "financebot-concierge-browser:latest",
        accel=os.getenv(ACCEL_ENV, "cpu").strip().lower(),
        bind_host=os.getenv(BIND_ENV, "127.0.0.1").strip(),
        drm_device=os.getenv(DEVICE_ENV, "").strip(),
        drm_gid=os.getenv(GID_ENV, "").strip(),
    )
    try:
        docker_spawn(argv)
    except Exception as exc:
        SESSIONS.kill(sess["session_id"], tenant)
        _audit(actor, "login_error", tenant, error=str(exc)[:300])
        await ctx.send("⚠️ Couldn't start the concierge browser: " + str(exc)[:300])
        return
    _audit(actor, "login_spawn", tenant, domain=domain,
           session_id=sess["session_id"], container=sess["container_name"])
    link = "http://" + os.getenv(BIND_ENV, "127.0.0.1").strip() + ":" + str(port) \
        + "/vnc.html?autoconnect=1&password=" + sess["vnc_password"]
    view = LoginSessionView(sess["session_id"], ctx.author.id, sess)
    msg = (
        "🌐 **Login intake for " + domain + "**\n"
        "A disposable browser is up. Log in on the merchant's own site; your "
        "passwords never touch Discord.\n\n"
        "🔗 <" + link + ">\n"
        "(The noVNC port binds to localhost only — remote solving needs an SSH tunnel.)\n\n"
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
    "browser_enabled",
]