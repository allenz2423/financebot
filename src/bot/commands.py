import src.core.state
import html
import json
import os
import re
import mimetypes
import sqlite3
import asyncio
import contextvars
import hashlib
import ipaddress
import random
import socket
import time
import math
import platform
from collections import Counter
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode
from html import unescape
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from dotenv import load_dotenv
import discord
from discord.ext import commands

# --- UNIVERSAL CLOSE BUTTON PATCH ---
class _UniversalCloseView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        self._message = None
        
    @discord.ui.button(label=" Close", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.ctx.author.id:
            try:
                if self._message:
                    await self._message.delete()
                else:
                    await interaction.message.delete()
            except:
                pass
            self.stop()

_original_send = commands.Context.send

async def _patched_send(self, content=None, **kwargs):
    # Determine if we are sending a UI element (embed or file) with NO buttons attached
    needs_view = ("embed" in kwargs and kwargs["embed"] is not None) or                  ("embeds" in kwargs and bool(kwargs["embeds"])) or                  ("file" in kwargs and kwargs["file"] is not None) or                  ("files" in kwargs and bool(kwargs["files"]))
                 
    if needs_view and "view" not in kwargs:
        view = _UniversalCloseView(self)
        kwargs["view"] = view
        msg = await _original_send(self, content, **kwargs)
        view._message = msg
        return msg
        
    return await _original_send(self, content, **kwargs)

commands.Context.send = _patched_send
# ------------------------------------

# import plaid_sync  # Lazy import to avoid circular dependency
# import sandbox_client  # Lazy import to avoid circular dependency
import base64
from io import BytesIO
from pathlib import Path
from PIL import Image

# from src.core.state import *  # Lazy import to avoid circular dependency
# from src.utils.helpers import *  # Lazy import to avoid circular dependency
# from src.services.search import *  # Lazy import to avoid circular dependency
from src.services.gmail import (
    begin_gmail_authorization,
    complete_gmail_authorization,
    disconnect_gmail,
    gmail_status,
)
from src.db.queries import *
from src.services.llm import *


# ============================================================
# Explicit deterministic Discord help
# ============================================================
@bot.command(name="help")
async def help_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    
    embed = discord.Embed(
        title=" Delilah Help",
        description="Quick reference for advisor controls, database verification, and ledger commands.",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name=" Advisor Control",
        value=(
            "`!status` — Live advisor state, elapsed time, tool calls, and phase.\n"
            "`!peek` — Spy on the advisor's current thought process and recent tool results.\n"
            "`!auditstatus` — View the live state of the audit controller and research queue.\n"
            "`!pause` — Pause the active advisor run.\n"
            "`!cancel` — Cancel the active advisor run.\n"
            "`!switch <local|cloud>` — Hot-swap between Ollama and OpenAI-compatible.\n"
            "`!model list|get|set` — Manage models (e.g., `!model set cloud:<model_name>`)."
        ),
        inline=False,
    )

    embed.add_field(
        name=" Database / Audit — Direct SQLite",
        value=(
            "`!dbstatus` — Full database health snapshot.\n"
            "`!unlocked` — Every currently unlocked transaction.\n"
            "`!locked` — Every locked/finalized transaction.\n"
            "`!override <id>` — Admin force-unlock a locked transaction.\n"
            "`!corrections [limit]` — Recent correction history with before/after details.\n"
            "`!merchantstatus` — Cross-reference ledger merchants against known merchants and aliases.\n"
            "`!researchstatus` — Unmatched merchants that require research.\n"
            "`!snapshot` — Compare live transaction identity against the original snapshot.\n"
            "`!integrity` — Deterministic integrity checks; no LLM involved.\n"
            "`!txaudit <transaction_row_id>` — Full forensic history for one transaction."
        ),
        inline=False,
    )

    embed.add_field(
        name=" Chat / Ledger Utilities",
        value=(
            "`!chart [days]` — Generate a spending pie chart for the trailing X days.\n"
            "`!export` — Download the entire transaction ledger as a .csv file.\n"
            "`!clear` — Clear current chat/context state where supported.\n"
            "`!clearchat` — Clear persisted chat history where supported.\n"
            "`!setbudget <amount>` — Set the configured budget.\n"
            "`!checknow` — Run the configured financial check now.\n"
            "`!plaidstatus` — Pings Plaid API for a status check regarding accounts.\n"
            "`!plaidsetup` — Securely input your Plaid `client_id`/`secret` (and tokens, if you already have them).\n"
            "`!plaidlink` — First-time bank connection: opens a real Plaid Link login and saves the new access token automatically.\n"
            "`!fixbank <index>` — Re-authenticate an existing but broken bank connection via a Tailscale URL.\n"
            "`!testpush [msg]` — Send a test push notification to your phone.\n"
            "`!ntfysetup` — DM yourself the secret ntfy topic and instructions.\n"
            "`!gmail connect` — Connect a Gmail account using read-only Google OAuth.\n"
            "`!gmail status` — Show the connected Gmail account.\n"
            "`!gmail disconnect` — Remove the Gmail connection.\n"
        ),
        inline=False,
    )

    embed.add_field(
        name=" Important",
        value=(
            "Database verification commands bypass Delilah and query SQLite directly. "
            "They are the source of truth for counts, locks, corrections, and transaction integrity."
        ),
        inline=False,
    )

    embed.set_footer(text="SQLite is authoritative. Model-generated summaries are not.")
    await ctx.send(embed=embed)



# ============================================================
# Gmail OAuth / Account Controls
# ============================================================

@bot.group(name="gmail", invoke_without_command=True)
async def gmail_cmd(ctx: commands.Context):
    """Deterministic Gmail account controls."""
    if ctx.invoked_subcommand is None:
        await ctx.send(
            "**Gmail controls**\n"
            "`!gmail connect` — Connect your Gmail account.\n"
            "`!gmail status` — Show Gmail connection status.\n"
            "`!gmail disconnect` — Remove the Gmail connection."
        )


@gmail_cmd.command(name="connect")
async def gmail_connect_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)

    try:
        url = begin_gmail_authorization(user_id)

        try:
            await ctx.author.send(
                "**Gmail connection**\n\n"
                "Open this link to authorize Delilah's "
                "read-only Gmail access:\n"
                f"{url}\n\n"
                "The link expires in 10 minutes and can only be used once."
            )

            await ctx.send(
                "I sent the Gmail authorization link to your Discord DMs."
            )

        except discord.Forbidden:
            await ctx.send(
                "I couldn't DM you the Gmail authorization link. "
                "Please enable Discord DMs from this server and run "
                "`!gmail connect` again."
            )

    except Exception as exc:
        await ctx.send(
            f"Failed to start Gmail authorization: "
            f"`{type(exc).__name__}: {exc}`"
        )


@gmail_cmd.command(name="status")
async def gmail_status_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)

    try:
        await ctx.send(
            gmail_status(user_id)
        )
    except Exception as exc:
        await ctx.send(
            f"Gmail status failed: `{type(exc).__name__}: {exc}`"
        )


@gmail_cmd.command(name="disconnect")
async def gmail_disconnect_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)

    try:
        await ctx.send(
            disconnect_gmail(user_id)
        )
    except Exception as exc:
        await ctx.send(
            f"Gmail disconnect failed: `{type(exc).__name__}: {exc}`"
        )


# ============================================================
# Deterministic Database Verification Commands
# ============================================================

def _open_verification_db(*, user_id: str) -> sqlite3.Connection:
    """Open an isolated read-only SQLite connection for deterministic commands.

    Verification commands must never share the advisor's global connection/cursor.
    This prevents advisor mutations or cursor reuse from interfering with direct
    SQLite truth checks.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_open_verification_db: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    db_path = Path(src.db.queries.get_db_path()).resolve()
    conn_v = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        check_same_thread=False,
        timeout=10.0,
    )
    conn_v.execute("PRAGMA busy_timeout=10000")
    return conn_v


REPORT_CONTROL_MESSAGES: dict[int, set[discord.Message]] = {}
REPORT_CONTROL_VIEWS: dict[int, set["_ReportCloseView"]] = {}


async def _close_all_control_messages(user_id: int) -> int:
    """Close/delete all tracked report/status/browser messages for a user."""
    deleted = 0
    messages = list(REPORT_CONTROL_MESSAGES.get(user_id, set()))
    REPORT_CONTROL_MESSAGES.pop(user_id, None)
    for msg in messages:
        try:
            await msg.delete()
            deleted += 1
        except Exception:
            pass

    views = list(REPORT_CONTROL_VIEWS.get(user_id, set()))
    REPORT_CONTROL_VIEWS.pop(user_id, None)
    for view in views:
        try:
            view.stop()
        except Exception:
            pass

    browser_views = list(TRANSACTION_BROWSER_VIEWS.get(user_id, set())) if "TRANSACTION_BROWSER_VIEWS" in globals() else []
    if browser_views:
        TRANSACTION_BROWSER_VIEWS.pop(user_id, None)
    for view in browser_views:
        try:
            if getattr(view, "_message", None) is not None:
                await view._message.delete()
                deleted += 1
        except Exception:
            pass
        try:
            view.stop()
        except Exception:
            pass

    status_msg = STATUS_MESSAGES.pop(str(user_id), None)
    status_task = STATUS_UPDATE_TASKS.pop(str(user_id), None)
    if status_task is not None and not status_task.done():
        status_task.cancel()
    if status_msg is not None:
        try:
            await status_msg.delete()
            deleted += 1
        except Exception:
            pass
    return deleted


class _ReportCloseView(discord.ui.View):
    """Close controls for deterministic/report embeds."""
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=300)
        self.ctx = ctx
        self._message: discord.Message | None = None
        REPORT_CONTROL_VIEWS.setdefault(ctx.author.id, set()).add(self)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                " Only the person who ran this command can use these controls.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label=" Close", style=discord.ButtonStyle.danger)
    async def close_current(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        user_id = self.ctx.author.id
        REPORT_CONTROL_MESSAGES.get(user_id, set()).discard(self._message)
        REPORT_CONTROL_VIEWS.get(user_id, set()).discard(self)
        if STATUS_MESSAGES.get(str(user_id)) is self._message:
            STATUS_MESSAGES.pop(str(user_id), None)
            task = STATUS_UPDATE_TASKS.pop(str(user_id), None)
            if task is not None and not task.done():
                task.cancel()
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label=" Close All", style=discord.ButtonStyle.danger)
    async def close_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        await _close_all_control_messages(self.ctx.author.id)

    async def on_timeout(self) -> None:
        REPORT_CONTROL_VIEWS.get(self.ctx.author.id, set()).discard(self)
        if not REPORT_CONTROL_VIEWS.get(self.ctx.author.id):
            REPORT_CONTROL_VIEWS.pop(self.ctx.author.id, None)
        try:
            if self._message is not None:
                await self._message.edit(view=None)
        except Exception:
            pass


class _ReportPageView(discord.ui.View):
    """Interactive paginator for long deterministic command reports."""
    def __init__(self, ctx: commands.Context, title: str, chunks: list[str]):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.title = title
        self.chunks = chunks or ["No data."]
        self.page = 0
        self._message: discord.Message | None = None
        REPORT_CONTROL_VIEWS.setdefault(ctx.author.id, set()).add(self)
        self._refresh_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, len(self.chunks))

    def _refresh_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.total_pages - 1
        self.page_indicator.label = f"{self.page + 1} / {self.total_pages}"

    def _embed(self) -> discord.Embed:
        title = self.title if self.total_pages == 1 else f"{self.title} · {self.page + 1}/{self.total_pages}"
        embed = discord.Embed(title=title, description=self.chunks[self.page][:4096] or "No data.", color=discord.Color.blurple())
        embed.set_footer(text="SQLite is authoritative • Deterministic verification")
        return embed

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(" Only the person who ran this command can use these controls.", ephemeral=True)
            return False
        return True

    async def _update(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._update(interaction)

    @discord.ui.button(label=" Close", style=discord.ButtonStyle.danger)
    async def close_current(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        REPORT_CONTROL_MESSAGES.get(self.ctx.author.id, set()).discard(self._message)
        REPORT_CONTROL_VIEWS.get(self.ctx.author.id, set()).discard(self)
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label=" Close All", style=discord.ButtonStyle.danger)
    async def close_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        await _close_all_control_messages(self.ctx.author.id)
        self.stop()

    async def on_timeout(self) -> None:
        REPORT_CONTROL_VIEWS.get(self.ctx.author.id, set()).discard(self)
        try:
            if self._message is not None:
                for child in self.children:
                    child.disabled = True
                await self._message.edit(view=self)
        except Exception:
            pass


async def _send_command_report(ctx: commands.Context,
    title: str,
    body: str,
    *,
    max_chars: int = 3600,user_id: str,) -> None:
    """Send a long deterministic report as ONE interactive Discord message."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_send_command_report: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    clean_title = str(title or "REPORT")
    lines = str(body or "").splitlines()
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in lines:
        line = str(line)
        extra = len(line) + (1 if current_lines else 0)
        if current_lines and current_len + extra > max_chars:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_len = 0
        current_lines.append(line)
        current_len += extra
    if current_lines or not chunks:
        chunks.append("\n".join(current_lines))
    view = _ReportPageView(ctx, clean_title, chunks)
    msg = await ctx.send(embed=view._embed(), view=view)
    view._message = msg
    REPORT_CONTROL_MESSAGES.setdefault(ctx.author.id, set()).add(msg)


async def _send_error_embed(ctx: commands.Context, title: str, exc: Exception, *, user_id: str) -> None:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_send_error_embed: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    embed = discord.Embed(
        title=title,
        description=f"`{type(exc).__name__}: {exc}`",
        color=discord.Color.red(),
    )
    embed.set_footer(text="SQLite verification command")
    view = _ReportCloseView(ctx)
    msg = await ctx.send(embed=embed, view=view)
    view._message = msg
    REPORT_CONTROL_MESSAGES.setdefault(ctx.author.id, set()).add(msg)


def _db_identity_mismatches(cur, user_id: str) -> list[tuple]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_db_identity_mismatches: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    cur.execute(
        """
        SELECT
            t.id,
            o.merchant,
            t.merchant,
            o.amount,
            t.amount,
            o.account_used,
            t.account_used,
            o.date,
            t.date,
            o.transaction_id,
            t.transaction_id
        FROM transactions_original o
        LEFT JOIN transactions t ON t.id = o.id
        WHERE o.user_id = ? AND (t.id IS NULL
           OR o.merchant IS NOT t.merchant
           OR o.amount IS NOT t.amount
           OR o.account_used IS NOT t.account_used
           OR o.date IS NOT t.date
           OR o.transaction_id IS NOT t.transaction_id)
        ORDER BY o.id
        """
    )
    return cur.fetchall()


def _audit_identity_mismatches(cur, user_id: str) -> list[tuple]:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_audit_identity_mismatches: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    cur.execute(
        """
        SELECT
            tc.id,
            tc.transaction_row_id,
            json_extract(tc.before_state, '$.merchant'),
            json_extract(tc.after_state, '$.merchant'),
            json_extract(tc.before_state, '$.amount'),
            json_extract(tc.after_state, '$.amount'),
            json_extract(tc.before_state, '$.account_used'),
            json_extract(tc.after_state, '$.account_used'),
            json_extract(tc.before_state, '$.date'),
            json_extract(tc.after_state, '$.date'),
            json_extract(tc.before_state, '$.transaction_id'),
            json_extract(tc.after_state, '$.transaction_id')
        FROM transaction_correction_log tc
        WHERE tc.user_id = ? AND (json_extract(tc.before_state, '$.merchant')
              IS NOT json_extract(tc.after_state, '$.merchant')
           OR json_extract(tc.before_state, '$.amount')
              IS NOT json_extract(tc.after_state, '$.amount')
           OR json_extract(tc.before_state, '$.account_used')
              IS NOT json_extract(tc.after_state, '$.account_used')
           OR json_extract(tc.before_state, '$.date')
              IS NOT json_extract(tc.after_state, '$.date')
           OR json_extract(tc.before_state, '$.transaction_id')
              IS NOT json_extract(tc.after_state, '$.transaction_id'))
        ORDER BY tc.id
        """
    )
    return cur.fetchall()


@bot.command(name="dbstatus")
async def dbstatus_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    
    loading_embed = discord.Embed(
        title=" Checking Database Status",
        description="Reading the database directly from SQLite…",
        color=discord.Color.blurple(),
    )
    loading = await ctx.send(embed=loading_embed)
    try:
        user_id = str(ctx.author.id)
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()

            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(is_locked=0),0), COALESCE(SUM(is_locked=1),0) FROM transactions"
            )
            total, unlocked, locked = cur.fetchone()

            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE is_locked=0 AND merchant NOT LIKE '%System Balance Sync%'"
            )
            unlocked_non_sync = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND is_locked=1 AND merchant NOT LIKE '%System Balance Sync%'", (user_id,))
            locked_non_sync = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM transaction_correction_log")
            corrections = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM transaction_correction_log WHERE date(occurred_at)=date('now','localtime')")
            corrections_today = cur.fetchone()[0]

            cur.execute("SELECT occurred_at, transaction_row_id FROM transaction_correction_log ORDER BY id DESC LIMIT 1")
            last_corr = cur.fetchone()

            cur.execute("SELECT COUNT(*) FROM known_merchants")
            known_merchants_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM merchant_aliases")
            alias_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(DISTINCT merchant) FROM transactions WHERE user_id = ?", (user_id,))
            distinct_merchants = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(DISTINCT t.merchant)
                FROM transactions t
                LEFT JOIN known_merchants km ON LOWER(TRIM(t.merchant)) = LOWER(TRIM(km.merchant_key))
                LEFT JOIN merchant_aliases ma ON ma.alias_key = LOWER(TRIM(t.merchant))
                WHERE t.user_id = ? AND km.id IS NULL AND ma.id IS NULL
                """
            )
            unknown_exact = cur.fetchone()[0]

            user_id = str(ctx.author.id)
            identity = _db_identity_mismatches(cur, user_id=user_id)
            audit_identity = _audit_identity_mismatches(cur, user_id=user_id)

            cur.execute("SELECT COUNT(*) FROM transactions_original")
            original_count = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM transaction_correction_log tc LEFT JOIN transactions t ON t.id=tc.transaction_row_id WHERE t.id IS NULL"
            )
            orphan_corrections = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND status='Pending'", (user_id,))
            pending = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND status='Evaluated'", (user_id,))
            evaluated = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND status='Ignored'", (user_id,))
            ignored = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM transactions WHERE is_locked NOT IN (0,1) OR is_locked IS NULL")
            invalid_lock = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(transaction_id) - COUNT(DISTINCT transaction_id) FROM transactions WHERE transaction_id IS NOT NULL"
            )
            duplicate_txids = cur.fetchone()[0]

            cur.execute("SELECT value FROM transaction_snapshot_metadata WHERE key='created_at' LIMIT 1")
            snapshot_created = cur.fetchone()

            last_text = f"#{last_corr[1]} @ {last_corr[0]}" if last_corr else "none"
            status = (
                " HEALTHY"
                if not identity and not audit_identity and orphan_corrections == 0 and invalid_lock == 0 and duplicate_txids == 0
                else " ATTENTION"
            )

            body = (
                "════════ DATABASE STATUS ════════\n"
                f"Database: {src.db.queries.get_db_path()}\n"
                f"Snapshot baseline: {original_count} rows\n"
                f"Snapshot created: {snapshot_created[0] if snapshot_created else 'unknown'}\n\n"
                "TRANSACTIONS\n"
                f"  Total: {total}\n"
                f"  Unlocked: {unlocked}\n"
                f"  Unlocked excluding system sync: {unlocked_non_sync}\n"
                f"  Locked: {locked}\n"
                f"  Locked excluding system sync: {locked_non_sync}\n"
                f"  Evaluated: {evaluated}\n"
                f"  Pending: {pending}\n"
                f"  Ignored: {ignored}\n\n"
                "CORRECTION HISTORY\n"
                f"  Total corrections: {corrections}\n"
                f"  Corrections today: {corrections_today}\n"
                f"  Last correction: {last_text}\n"
                f"  Orphaned logs: {orphan_corrections}\n\n"
                "MERCHANT REGISTRY\n"
                f"  Known merchants: {known_merchants_count}\n"
                f"  Aliases: {alias_count}\n"
                f"  Distinct ledger merchant strings: {distinct_merchants}\n"
                f"  Unmatched exact/alias strings: {unknown_exact}\n\n"
                "INTEGRITY\n"
                f"  Original-vs-current identity mismatches: {len(identity)}\n"
                f"  Correction-log identity mismatches: {len(audit_identity)}\n"
                f"  Duplicate transaction IDs: {max(0, duplicate_txids)}\n"
                f"  Invalid lock states: {invalid_lock}\n"
                f"  STATUS: {status}\n"
                "═══════════════════════════════"
            )

        try:
            await loading.delete()
        except Exception:
            pass
        await _send_command_report(ctx, " DB STATUS", body, user_id=user_id)
    except Exception as exc:
        try:
            await loading.delete()
        except Exception:
            pass
        await _send_error_embed(ctx, " DB Status Check Failed", exc, user_id=user_id)


TRANSACTION_BROWSER_VIEWS: dict[int, set["_TransactionPageView"]] = {}


class _TransactionPageView(discord.ui.View):
    def __init__(self, ctx: commands.Context, rows: list[tuple], *, locked: bool, page_size: int = 25):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.rows = rows
        self.locked = locked
        self.page_size = page_size
        self.page = 0
        self._message: discord.Message | None = None
        TRANSACTION_BROWSER_VIEWS.setdefault(self.ctx.author.id, set()).add(self)
        self._refresh_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(len(self.rows) / self.page_size))

    def _refresh_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.total_pages - 1
        self.page_indicator.label = f"{self.page + 1} / {self.total_pages}"

    def _embed(self) -> discord.Embed:
        state_word = "Unlocked" if not self.locked else "Locked"
        icon = "" if not self.locked else ""
        total = len(self.rows)
        start = self.page * self.page_size
        end = min(start + self.page_size, total)
        embed = discord.Embed(
            title=f"{icon} {state_word} Transactions",
            description=(
                f"Showing **{start + 1}–{end}** of **{total}** transactions.\n"
                f"Page **{self.page + 1} / {self.total_pages}**"
            ),
            color=discord.Color.orange() if not self.locked else discord.Color.green(),
        )
        page_rows = self.rows[start:end]
        if not page_rows:
            embed.add_field(name="Transactions", value="No transactions on this page.", inline=False)
        else:
            for rid, date, merchant, amount, account, category, status in page_rows:
                date_text = str(date or "—")[:19]
                merchant_text = str(merchant or "—")[:80]
                account_text = str(account or "—")[:40]
                category_text = str(category or "—")[:50]
                status_text = str(status or "—")[:24]
                amount_text = f"${float(amount or 0):,.2f}"
                resolved = _resolve_known_merchant(merchant)
                registry = "KNOWN" if resolved else "UNKNOWN"
                if resolved and resolved.get("canonical_name"):
                    registry += f" → {resolved['canonical_name']}"
                embed.add_field(
                    name=f"#{rid} · {merchant_text}"[:256],
                    value=(
                        f"**Date:** {date_text}\n"
                        f"**Amount:** {amount_text} · **Account:** {account_text}\n"
                        f"**Category:** {category_text} · **Status:** {status_text}\n"
                        f"**Registry:** {registry}"
                    )[:1024],
                    inline=True,
                )
        embed.set_footer(text="SQLite is authoritative • Use the buttons to browse")
        return embed

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                " Only the person who ran this command can use these controls.",
                ephemeral=True,
            )
            return False
        return True

    async def _update(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if self.page > 0:
            self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if self.page < self.total_pages - 1:
            self.page += 1
        await self._update(interaction)

    @discord.ui.button(label=" Close", style=discord.ButtonStyle.danger)
    async def close_current(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.edit_message(view=None, embed=self._embed())
        self.stop()
        TRANSACTION_BROWSER_VIEWS.get(self.ctx.author.id, set()).discard(self)

    @discord.ui.button(label=" Close All", style=discord.ButtonStyle.danger)
    async def close_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        views = list(TRANSACTION_BROWSER_VIEWS.get(self.ctx.author.id, set()))
        deleted = 0
        for view in views:
            try:
                if view._message is not None:
                    await view._message.delete()
                    deleted += 1
            except Exception:
                pass
            view.stop()
        TRANSACTION_BROWSER_VIEWS.pop(self.ctx.author.id, None)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        TRANSACTION_BROWSER_VIEWS.get(self.ctx.author.id, set()).discard(self)
        if not TRANSACTION_BROWSER_VIEWS.get(self.ctx.author.id):
            TRANSACTION_BROWSER_VIEWS.pop(self.ctx.author.id, None)
        try:
            if self._message is not None:
                await self._message.edit(view=self)
        except Exception:
            pass


async def _send_transaction_browser(ctx: commands.Context,
    rows: list[tuple],
    *,
    locked: bool,user_id: str,) -> None:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_send_transaction_browser: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not rows:
        state_word = "locked" if locked else "unlocked"
        embed = discord.Embed(
            title=f" No {state_word.title()} Transactions",
            description=f"No {state_word} transactions found.",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)
        return
    view = _TransactionPageView(ctx, rows, locked=locked)
    view._message = await ctx.send(embed=view._embed(), view=view)


@bot.command(name="unlocked")
async def unlocked_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """
                SELECT id, date, merchant, amount, account_used, category, status
                FROM transactions
                WHERE user_id = ?
                  AND is_locked=0
                  AND merchant NOT LIKE '%System Balance Sync%'
                ORDER BY datetime(date) DESC, id DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        await _send_transaction_browser(ctx, rows, locked=False, user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Unlocked Check Failed", exc, user_id=user_id)


@bot.command(name="locked")
async def locked_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """
                SELECT id, date, merchant, amount, account_used, category, status
                FROM transactions
                WHERE user_id = ?
                  AND is_locked=1
                  AND merchant NOT LIKE '%System Balance Sync%'
                ORDER BY datetime(date) DESC, id DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        await _send_transaction_browser(ctx, rows, locked=True, user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Locked Check Failed", exc, user_id=user_id)


@bot.command(name="corrections")
async def corrections_command(ctx: commands.Context, limit: int = 25):
    user_id = str(ctx.author.id)
    limit = max(1, min(int(limit), 100))
    try:
        user_id = str(ctx.author.id)
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """
                SELECT tc.id,
                       tc.transaction_row_id,
                       tc.occurred_at,
                       tc.source,
                       tc.reason,
                       tc.changes,
                       tc.before_state,
                       tc.after_state
                FROM transaction_correction_log tc
                INNER JOIN transactions t
                    ON t.id = tc.transaction_row_id
                WHERE t.user_id = ?
                ORDER BY tc.id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
        lines = [f"Recent corrections: {len(rows)}", ""]
        for cid, rid, occurred, source, reason, changes, before_state, after_state in rows:
            lines.append(f"#{cid} | tx #{rid} | {occurred} | {source or 'unknown'}")
            lines.append(f"Reason: {reason or '—'}")
            lines.append(f"Changes: {changes or '—'}")
            lines.append(f"Before: {before_state or '—'}")
            lines.append(f"After:  {after_state or '—'}")
            lines.append("---")
        await _send_command_report(ctx, " CORRECTION AUDIT", "\n".join(lines), user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Correction Check Failed", exc, user_id=user_id)


@bot.command(name="merchantstatus")
async def merchantstatus_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """
                SELECT t.merchant, COUNT(*) AS tx_count,
                       CASE WHEN km.id IS NULL AND ma.id IS NULL THEN 'UNKNOWN' ELSE 'KNOWN' END AS status,
                       COALESCE(km.canonical_name, km2.canonical_name) AS canonical,
                       COALESCE(km.category, km2.category) AS category,
                       COALESCE(km.confidence, km2.confidence) AS confidence
                FROM transactions t
                LEFT JOIN known_merchants km ON LOWER(TRIM(t.merchant)) = LOWER(TRIM(km.merchant_key))
                LEFT JOIN merchant_aliases ma ON ma.alias_key = LOWER(TRIM(t.merchant))
                LEFT JOIN known_merchants km2 ON km2.id = ma.known_merchant_id
            WHERE t.user_id = ?
                GROUP BY
                    t.merchant,
                    CASE WHEN km.id IS NULL AND ma.id IS NULL THEN 'UNKNOWN' ELSE 'KNOWN' END,
                    COALESCE(km.canonical_name, km2.canonical_name),
                    COALESCE(km.category, km2.category),
                    COALESCE(km.confidence, km2.confidence)
                ORDER BY status, tx_count DESC, t.merchant
                """,
                (user_id,)
            )
            ledger_rows = cur.fetchall()
            cur.execute("SELECT id, merchant_key, canonical_name, category, confidence, source, updated_at FROM known_merchants ORDER BY merchant_key")
            registry_rows = cur.fetchall()
        lines = [f"Ledger merchant strings: {len(ledger_rows)}", "", "LEDGER CROSS-REFERENCE"]
        for merchant, count, status, canonical, category, confidence in ledger_rows:
            lines.append(f"{status:<7} | {count:>3} tx | {merchant} | → {canonical or 'NO REGISTRY MATCH'} | {category or '—'} | {confidence or '—'}")
        lines.append("")
        lines.append("KNOWN MERCHANT REGISTRY")
        for rid, key, canonical, category, confidence, source, updated in registry_rows:
            lines.append(f"#{rid} | {key} → {canonical} | {category or '—'} | {confidence} | {source} | {updated}")
        await _send_command_report(ctx, " MERCHANT STATUS", "\n".join(lines), user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Merchant Status Check Failed", exc, user_id=user_id)


@bot.command(name="researchstatus")
async def researchstatus_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """
                SELECT t.merchant, COUNT(*) AS tx_count
                FROM transactions t
                LEFT JOIN known_merchants km ON LOWER(TRIM(t.merchant)) = LOWER(TRIM(km.merchant_key))
                LEFT JOIN merchant_aliases ma ON ma.alias_key = LOWER(TRIM(t.merchant))
                WHERE t.user_id = ? AND km.id IS NULL AND ma.id IS NULL
                GROUP BY t.merchant ORDER BY tx_count DESC, t.merchant
                """
            )
            unknowns = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM known_merchants")
            known = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM merchant_aliases")
            alias_count = cur.fetchone()[0]
        lines = [
            f"Known merchants: {known}",
            f"Aliases: {alias_count}",
            f"Currently unmatched merchant strings: {len(unknowns)}",
            "",
            "RESEARCH CANDIDATES — these are not established in the registry and must be researched before correction:",
        ]
        for merchant, count in unknowns:
            lines.append(f"{count:>3} tx | {merchant}")
        await _send_command_report(ctx, " RESEARCH STATUS", "\n".join(lines), user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Research Status Check Failed", exc, user_id=user_id)


@bot.command(name="snapshot")
async def snapshot_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            snapshot_mismatches = _db_identity_mismatches(cur, user_id=user_id)
            cur.execute("SELECT COUNT(*) FROM transactions_original")
            original_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM transactions")
            current_count = cur.fetchone()[0]
            lines = [
            f"Original snapshot rows: {original_count}",
            f"Current transaction rows: {current_count}",
            f"Identity mismatches: {len(mismatches)}",
            ]
            if mismatches:
                lines.append("")
                lines.append("MISMATCHES")
                for row in mismatches:
                    rid, om, cm, oa, ca, oacct, cacct, od, cd, otid, ctid = row
                    lines.append(
                        f"#{rid}: merchant {om!r} → {cm!r}; amount {oa} → {ca}; account {oacct!r} → {cacct!r}; date {od!r} → {cd!r}; txid {otid!r} → {ctid!r}"
                    )
            else:
                lines.append(" Merchant / amount / account / date / transaction_id all match the original snapshot.")
        await _send_command_report(ctx, " ORIGINAL SNAPSHOT CHECK", "\n".join(lines), user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Snapshot Check Failed", exc, user_id=user_id)


@bot.command(name="integrity")
async def integrity_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            snapshot_mismatches = _db_identity_mismatches(cur, user_id=user_id)
            audit_mismatches = _audit_identity_mismatches(cur, user_id=user_id)
            cur.execute("SELECT COUNT(*) FROM transaction_correction_log tc LEFT JOIN transactions t ON t.id=tc.transaction_row_id WHERE t.id IS NULL")
            orphan_logs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM transactions WHERE is_locked NOT IN (0,1) OR is_locked IS NULL")
            invalid_locks = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) - COUNT(DISTINCT transaction_id) FROM transactions WHERE transaction_id IS NOT NULL")
            duplicate_txids = cur.fetchone()[0]
            status = " PASS" if not snapshot_mismatches and not audit_mismatches and orphan_logs == 0 and invalid_locks == 0 and duplicate_txids == 0 else " FAIL"
            lines = [
                f"Overall: {status}",
                "",
                f"Current-vs-original identity drift: {len(snapshot_mismatches)}",
                f"Correction-log identity drift: {len(audit_mismatches)}",
                f"Orphaned correction records: {orphan_logs}",
                f"Invalid lock states: {invalid_locks}",
                f"Duplicate transaction IDs: {max(0, duplicate_txids)}",
                "",
                "Identity fields checked: merchant, amount, account_used, date, transaction_id.",
            ]
            if snapshot_mismatches:
                lines.append("\nSnapshot violations:")
                for row in snapshot_mismatches[:50]:
                    lines.append(
                        f"  #{row[0]}: {row[1]!r} → {row[2]!r} / amount {row[3]} → {row[4]} / account {row[5]!r} → {row[6]!r}"
                    )
            if audit_mismatches:
                lines.append("\nAudit violations:")
                for row in audit_mismatches[:50]:
                    lines.append(
                        f"  correction #{row[0]} tx #{row[1]}: merchant {row[2]!r} → {row[3]!r}; amount {row[4]} → {row[5]}; account {row[6]!r} → {row[7]!r}"
                    )
        await _send_command_report(ctx, " DATABASE INTEGRITY", "\n".join(lines), user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Integrity Check Failed", exc, user_id=user_id)


@bot.command(name="txaudit")
async def audit_transaction_command(ctx: commands.Context, transaction_row_id: str = ""):
    try:
        rid = int(transaction_row_id)
    except ValueError:
        embed = discord.Embed(
            title=" Transaction Audit Usage",
            description="Use `!txaudit <transaction_row_id>`.",
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)
        return

    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute("SELECT * FROM transactions WHERE id=? AND user_id=?", (rid, user_id))

            current = cur.fetchone()
            if not current:
                embed = discord.Embed(
                    title=" Transaction Not Found",
                    description=f"Transaction `#{rid}` does not exist.",
                    color=discord.Color.red(),
                )
                await ctx.send(embed=embed)
                return
            cur.execute("PRAGMA table_info(transactions)")
            cols = [row[1] for row in cur.fetchall()]
            current_map = dict(zip(cols, current))
            cur.execute("SELECT * FROM transactions_original WHERE id=?", (rid,))
            original = cur.fetchone()
            original_map = dict(zip(cols, original)) if original else {}
            cur.execute(
                """
                SELECT id, occurred_at, action, source, reason, changes, before_state, after_state
                FROM transaction_correction_log WHERE transaction_row_id=? ORDER BY id
                """,
                (rid,),
            )
            history = cur.fetchall()

            key = _merchant_key(current_map.get("merchant"))
            resolved = None
            if key:
                cur.execute(
                    """
                    SELECT id, canonical_name, category, confidence, notes, evidence_url, source, updated_at
                    FROM known_merchants WHERE merchant_key=? LIMIT 1
                    """,
                    (key,),
                )
                row = cur.fetchone()
                if row:
                    resolved = {
                        "canonical_name": row[1], "category": row[2], "confidence": row[3], "matched_by": "exact"
                    }
                else:
                    cur.execute(
                        """
                        SELECT km.id, km.canonical_name, km.category, km.confidence
                        FROM merchant_aliases ma
                        JOIN known_merchants km ON km.id=ma.known_merchant_id
                        WHERE ma.alias_key=? LIMIT 1
                        """,
                        (key,),
                    )
                    row = cur.fetchone()
                    if row:
                        resolved = {
                            "canonical_name": row[1], "category": row[2], "confidence": row[3], "matched_by": "alias"
                        }

        lines = [
            f"Transaction #{rid}",
            f"Transaction ID: {current_map.get('transaction_id')}",
            "",
            "ORIGINAL SNAPSHOT",
            f"  Date: {original_map.get('date') if original_map else 'missing'}",
            f"  Merchant: {original_map.get('merchant') if original_map else 'missing'}",
            f"  Amount: {original_map.get('amount') if original_map else 'missing'}",
            f"  Account: {original_map.get('account_used') if original_map else 'missing'}",
            f"  Category: {original_map.get('category') if original_map else 'missing'}",
            "",
            "CURRENT",
            f"  Date: {current_map.get('date')}",
            f"  Merchant: {current_map.get('merchant')}",
            f"  Clean merchant: {current_map.get('clean_merchant')}",
            f"  Amount: {current_map.get('amount')}",
            f"  Account: {current_map.get('account_used')}",
            f"  Category: {current_map.get('category')}",
            f"  Status: {current_map.get('status')}",
            f"  Locked: {current_map.get('is_locked')}",
            f"  Judgment: {current_map.get('judgment') or '—'}",
            "",
            "MERCHANT REGISTRY",
            f"  Match: {resolved['matched_by'] if resolved else 'UNKNOWN'}",
            f"  Canonical: {resolved['canonical_name'] if resolved else '—'}",
            f"  Registry category: {resolved['category'] if resolved else '—'}",
            f"  Confidence: {resolved['confidence'] if resolved else '—'}",
            "",
            f"CORRECTION HISTORY ({len(history)} records)",
        ]
        for cid, occurred, action, source, reason, changes, before, after in history:
            lines.extend([
                f"  #{cid} @ {occurred} [{action}/{source or 'unknown'}]",
                f"    Reason: {reason or '—'}",
                f"    Changes: {changes or '—'}",
                f"    Before: {before or '—'}",
                f"    After:  {after or '—'}",
            ])
        await _send_command_report(ctx, " TRANSACTION AUDIT", "\n".join(lines), user_id=user_id)
    except Exception as exc:
        await _send_error_embed(ctx, " Transaction Audit Failed", exc, user_id=user_id)


async def queue_worker():
    while True:
        uid, row = await tx_queue.get()
        src.core.state.CURRENT_USER_ID.set(uid)
        try:
            # Automatic transaction classification is disabled.
            # Pending Plaid transactions are handled explicitly by Delilah
            # when the user asks to review pending transactions.
            print(
                f" [TX QUEUE] Ignoring automatic classification for user {uid}; "
                "pending transactions remain in SQLite."
            )
        except Exception as e:
            print(f" Worker exception: {e}")
        finally:
            tx_queue.task_done()
        await asyncio.sleep(1.0)

# ============================================================
# Live Advisor Model Switching
# ============================================================
def _ollama_base_url() -> str:
    return (
        OLLAMA_URL[: -len("/api/chat")]
        if OLLAMA_URL.endswith("/api/chat")
        else OLLAMA_URL.rstrip("/")
    )

async def _ollama_installed_models() -> list[str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{_ollama_base_url()}/api/tags")
        response.raise_for_status()
        data = response.json()
        return [str(m.get("name")) for m in data.get("models", []) if m.get("name")]

async def _ollama_unload_model(model_name: str) -> None:
    if not model_name:
        return
    payload = {"model": model_name, "prompt": "", "stream": False, "keep_alive": 0}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{_ollama_base_url()}/api/generate", json=payload)
        response.raise_for_status()

async def _ollama_warm_model(model_name: str) -> None:
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "think": False,
        "keep_alive": MODEL_KEEP_ALIVE,
        "options": {"num_ctx": ADVISOR_NUM_CTX},
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(OLLAMA_URL, json=payload)
        response.raise_for_status()

@bot.group(name="model", invoke_without_command=True)
@commands.has_permissions(administrator=True)
async def model_command(ctx: commands.Context):
    user_id = str(ctx.author.id)
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    active = os.getenv("OPENAI_MODEL", "") if provider == "openai" else src.core.state.ADVISOR_MODEL
    await ctx.send(
        f" Current provider: **{provider.upper()}**\n Current model: `{active}`\n\n"
        f"**Usage:**\n"
        f"`!switch local` or `!switch cloud`\n"
        f"`!model list`\n"
        f"`!model set cloud:<model_name>`\n"
        f"`!model set local:<model_name>`"
    )

@bot.command(name="switch")
@commands.has_permissions(administrator=True)
async def switch_provider(ctx: commands.Context, provider: str = ""):
    provider = provider.strip().lower()
    if provider not in ["local", "cloud", "ollama", "openai"]:
        await ctx.send(" Usage: `!switch local` or `!switch cloud`")
        return
    
    target = "ollama" if provider in ["local", "ollama"] else "openai"
    os.environ["LLM_PROVIDER"] = target
    
    if target == "ollama":
        current_model = src.core.state.ADVISOR_MODEL
        await ctx.send(f"🖥️ Switched to **Local (Ollama)**.\nModel: `{current_model}`")
        try:
            await _ollama_warm_model(current_model)
        except Exception:
            pass
    else:
        cloud_model = os.getenv("OPENAI_MODEL", "")
        await ctx.send(f"☁️ Switched to **Cloud (OpenAI-compatible)**.\nModel: `{cloud_model}`")

@switch_provider.error
async def switch_provider_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions.")

@model_command.command(name="get")
@commands.has_permissions(administrator=True)
async def model_get(ctx: commands.Context):
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider == "openai":
        model = os.getenv("OPENAI_MODEL", "")
        await ctx.send(f"☁️ **Current advisor model (Cloud):** `{model}`")
    else:
        await ctx.send(f"🖥️ **Current advisor model (Local):** `{src.core.state.ADVISOR_MODEL}`")

@model_command.command(name="list")
@commands.has_permissions(administrator=True)
async def model_list(ctx: commands.Context):
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    try:
        models = await _ollama_installed_models()
    except Exception as e:
        models = []
        
    lines = [f" **Current Provider:** {provider.upper()}"]
    
    lines.append("\n☁️ **Cloud Models (OpenAI-compatible)**")
    d_model = os.getenv("OPENAI_MODEL", "")
    lines.append(f"• `{d_model}`" + (" ← **current**" if provider == "openai" else ""))
    
    lines.append("\n🖥️ **Local Models (Ollama)**")
    if not models:
        lines.append(" *(Could not reach Ollama or no models installed)*")
    for name in models:
        marker = " ← **current**" if (name == src.core.state.ADVISOR_MODEL and provider == "ollama") else ""
        lines.append(f"• `{name}`{marker}")
        
    await ctx.send("\n".join(lines))

@model_command.command(name="set")
@commands.has_permissions(administrator=True)
async def model_set(ctx: commands.Context, *, model_name: str = ""):
    new_model = model_name.strip()
    if not new_model:
        await ctx.send("Usage: `!model set cloud:<model>` or `!model set local:<model>`")
        return
        
    provider_prefix = "local"
    if new_model.startswith("cloud:"):
        provider_prefix = "cloud"
        new_model = new_model.split("cloud:", 1)[1]
    elif new_model.startswith("local:"):
        provider_prefix = "local"
        new_model = new_model.split("local:", 1)[1]
    elif new_model.startswith("ollama:"):
        provider_prefix = "local"
        new_model = new_model.split("ollama:", 1)[1]
        
    if provider_prefix == "cloud":
        os.environ["OPENAI_MODEL"] = new_model
        os.environ["LLM_PROVIDER"] = "openai"
        await ctx.send(f"☁️ **Switched to Cloud.** Model set to `{new_model}`.")
        return

    if new_model == src.core.state.ADVISOR_MODEL and os.getenv("LLM_PROVIDER") == "ollama":
        await ctx.send(f" `{new_model}` is already the active local model and provider is set to local.")
        return
        
    try:
        models = await _ollama_installed_models()
    except Exception as e:
        await ctx.send(f" Could not query Ollama: `{type(e).__name__}: {e}`")
        return
        
    if new_model not in models:
        await ctx.send(f" Model `{new_model}` is not installed.\nUse `!model list` to see installed models.")
        return
        
    old_model = src.core.state.ADVISOR_MODEL
    os.environ["LLM_PROVIDER"] = "ollama"
    await ctx.send(f"🖥️ **Switching local advisor model**\n`{old_model}` → `{new_model}`\n Releasing old model...")
    
    try:
        await _ollama_unload_model(old_model)
        await ctx.send(f" Loading `{new_model}` with context `{ADVISOR_NUM_CTX:,}`...")
        await _ollama_warm_model(new_model)
        src.core.state.ADVISOR_MODEL = new_model
        await ctx.send(f" **Advisor model switched.** Now using `{src.core.state.ADVISOR_MODEL}` (Local).")
    except Exception as e:
        src.core.state.ADVISOR_MODEL = old_model
        try:
            await _ollama_warm_model(old_model)
            restored = " Previous model restored."
        except Exception as restore_error:
            restored = f"  Previous model could not be restored: {type(restore_error).__name__}: {restore_error}"
        await ctx.send(f" **Model switch failed:** `{type(e).__name__}: {e}`\nStill using `{old_model}`.{restored}")

@model_command.error
async def model_command_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions to manage models.")
    else:
        await ctx.send(f" Model command error: {error}")


@bot.command(name="clearchat")
@commands.has_permissions(administrator=True)
async def clear_chat(ctx: commands.Context, mode: str = ""):
    user_id = str(ctx.author.id)
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send(" Clearchat restricted to designated financial channel.")
        return
    also_purge = mode.lower().strip() == "purge"
    warning = " **This will permanently delete all chat history (conversation memory only — financial data is NOT affected)."
    if also_purge:
        warning += " This will ALSO purge this channel's messages."
    warning += "**\nType `CONFIRM` within 30 seconds to proceed."
    await ctx.send(warning)

    def check(m: discord.Message) -> bool:
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

    try:
        reply = await bot.wait_for("message", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(" No confirmation — clearchat cancelled.")
        return
    if reply.content.strip() != "CONFIRM":
        await ctx.send(" Clearchat cancelled.")
        return
    await ctx.send(" **Confirmed. Clearing chat history.**")
    try:
        c.execute("DELETE FROM chat_history WHERE user_id = ?", (str(ctx.author.id),))
        conn.commit()
    except Exception as e:
        await ctx.send(f" Failed to clear chat_history: {e}")
        return
    SESSION_HISTORY.clear()
    AUDIT_SESSION_STATE.clear()
    if not also_purge:
        await ctx.send(" **Chat history cleared.** Financial data untouched.")
        return
    try:
        deleted = await ctx.channel.purge(limit=1000)
        confirm = await ctx.channel.send(
            f" **Chat history cleared and {len(deleted)} message(s) purged.**"
        )
        await asyncio.sleep(5)
        await confirm.delete()
    except Exception as e:
        await ctx.send(f" Chat history cleared, but purge failed: {e}")

@clear_chat.error
async def clear_chat_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions to run `!clearchat`.")
    else:
        await ctx.send(f" Command error: {error}")

@bot.command(name="clear")
@commands.has_permissions(administrator=True)
async def clear_all(ctx: commands.Context):
    user_id = str(ctx.author.id)
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send(" Clear command restricted to designated financial channel.")
        return
    await ctx.send(
        " **This will permanently delete every transaction, income record, savings bucket, "
        "subscription, and chat history entry, purge this channel's messages, and reset the "
        "Plaid sync cursor.**\nType `CONFIRM` within 30 seconds to proceed."
    )

    def check(m: discord.Message) -> bool:
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

    try:
        reply = await bot.wait_for("message", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(" No confirmation — reset cancelled.")
        return
    if reply.content.strip() != "CONFIRM":
        await ctx.send(" Reset cancelled.")
        return
    await ctx.send(" **Confirmed. Clearing database and channel history.**")
    try:
        uid = str(ctx.author.id)
        c.execute("DELETE FROM transaction_correction_log WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM transaction_context WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM transactions WHERE user_id = ?", (uid,))
        c.execute("INSERT INTO transactions SELECT * FROM transactions_original WHERE user_id = ?", (uid,))
        c.execute("UPDATE transactions SET is_locked = 0 WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM cash_inflows WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM savings_buckets WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM subscriptions WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM budget_settings WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM chat_history WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM nudge_log WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM pending_corrections WHERE requester_user_id = ?", (uid,))
        c.execute("DELETE FROM delilah_memories WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM planned_transactions WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM lifestyle_context_log WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM balance_snapshots WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM financial_snapshots WHERE user_id = ?", (uid,))
        c.execute("INSERT INTO budget_settings (user_id, weekly_discretionary_limit, impulse_threshold) VALUES (?, 150.00, 50.00)", (uid,))
        conn.commit()
        plaid_sync.resets_cursors(conn)
    except Exception as e:
        await ctx.send(f" Failed to reset database: {e}")
        return
    try:
        deleted = await ctx.channel.purge(limit=1000)
        confirm = await ctx.channel.send(
            f" **System Reset Complete.** Purged {len(deleted)} messages, cleared `{src.db.queries.get_db_path()}`, reset Plaid cursor."
        )
        await asyncio.sleep(5)
        await confirm.delete()
    except Exception as e:
        await ctx.send(f" Database cleared, but message purge failed: {e}")

@clear_all.error
async def clear_all_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions to run `!clear`.")
    else:
        await ctx.send(f" Command error: {error}")

# ============================================================
# Recategorize Command
# ============================================================
@bot.command(name="recategorize")
@commands.has_permissions()
async def recategorize(
    ctx: commands.Context, transaction_id: str, *, new_category: str
):
    user_id = str(ctx.author.id)
    c.execute(
        "SELECT id, clean_merchant, merchant, category FROM transactions WHERE user_id = ? AND transaction_id = ?",
        (user_id, transaction_id),
    )
    row = c.fetchone()
    if not row:
        embed = discord.Embed(
            title="🔎 Transaction Not Found",
            description=(
                f"No transaction matching `{transaction_id}` was found "
                "in your account."
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"User {user_id}")
        await ctx.send(embed=embed)
        return
    row_id, clean_merchant, merchant, old_category = row
    c.execute(
        "UPDATE transactions SET category = ? WHERE id = ? AND user_id = ?",
        (new_category, row_id, user_id),
    )
    conn.commit()
    label = clean_merchant or merchant

    embed = discord.Embed(
        title="✏️ Transaction Recategorized",
        description=f"**{label}** has been recategorized.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Previous Category",
        value=f"`{old_category}`",
        inline=True,
    )
    embed.add_field(
        name="New Category",
        value=f"`{new_category}`",
        inline=True,
    )
    embed.add_field(
        name="Transaction",
        value=f"`{transaction_id}`",
        inline=False,
    )
    embed.set_footer(text=f"User {user_id}")

    await ctx.send(embed=embed)

@recategorize.error
async def recategorize_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions.")
    else:
        await ctx.send(f" Command error: {error}")

@bot.command(name="override")
@commands.has_permissions()
async def override_lock(ctx: commands.Context, *args):
    """Force unlock one or more transactions so the LLM can edit them."""
    user_id = str(ctx.author.id)
    if not args:
        await ctx.send(" Usage: `!override <id1> [id2] ...`")
        return

    ids = []
    for a in args:
        try:
            ids.append(int(a))
        except ValueError:
            pass
            
    if not ids:
        await ctx.send(" No valid integer IDs provided.")
        return

    unlocked = []
    not_found = []
    already_unlocked = []

    for rid in ids:
        c.execute("SELECT clean_merchant, merchant, amount, is_locked FROM transactions WHERE user_id = ? AND id = ?", (user_id, rid))
        row = c.fetchone()
        if not row:
            not_found.append(rid)
            continue
            
        clean_merchant, merchant, amount, is_locked = row
        
        if not is_locked:
            already_unlocked.append(rid)
            continue
            
        c.execute("UPDATE transactions SET is_locked = 0 WHERE user_id = ? AND id = ?", (user_id, rid))
        unlocked.append(rid)
        
    conn.commit()
    
    msg = []
    if unlocked:
        unlocked_str = ", ".join(["#" + str(x) for x in unlocked])
        msg.append(f" **OVERRIDE:** Successfully unlocked {len(unlocked)} transaction(s): {unlocked_str}")
    if already_unlocked:
        already_str = ", ".join(["#" + str(x) for x in already_unlocked])
        msg.append(f"ℹ Already unlocked: {already_str}")
    if not_found:
        not_found_str = ", ".join(["#" + str(x) for x in not_found])
        msg.append(f" Not found: {not_found_str}")
        
    await ctx.send("".join(msg))

@override_lock.error
async def override_lock_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions.")
    else:
        await ctx.send(f" Command error: {error}")

@bot.command(name="setbudget")
@commands.has_permissions()
async def set_budget(
    ctx: commands.Context, weekly_limit: float = None, impulse_threshold: float = None
):
    user_id = str(ctx.author.id)
    if weekly_limit is None:
        weekly_limit, impulse_threshold = get_budget_settings(user_id=user_id)
        await ctx.send(
            f" Current — weekly limit: ${weekly_limit:.2f}, impulse threshold: ${impulse_threshold:.2f}"
        )
        return
    current_limit, current_threshold = get_budget_settings(user_id=user_id)
    new_threshold = (
        impulse_threshold if impulse_threshold is not None else current_threshold
    )
    c.execute(
        "UPDATE budget_settings SET weekly_discretionary_limit = ?, impulse_threshold = ? WHERE user_id = ?",
        (weekly_limit, new_threshold, user_id),
    )
    conn.commit()
    await ctx.send(
        f" Budget updated — weekly limit: ${weekly_limit:.2f}, impulse threshold: ${new_threshold:.2f}"
    )

@bot.command(name="checknow")
@commands.has_permissions()
async def check_now(ctx: commands.Context):
    user_id = str(ctx.author.id)
    await ctx.send(" Pulling live data from Plaid now...")
    result = await pull_live_financial_data()
    await ctx.send(result)

@check_now.error
async def check_now_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions.")
    else:
        await ctx.send(f" Command error: {error}")

# ============================================================
# Self-Audit Command
# ============================================================
@bot.command(name="audit")
@commands.has_permissions()
async def audit_categories(ctx: commands.Context, days: int = 30):
    user_id = str(ctx.author.id)
    days = max(1, min(int(days), 3650))
    c.execute(
        """
        SELECT id, clean_merchant, merchant, category, amount, date
        FROM transactions WHERE status = 'Evaluated'
        AND merchant NOT LIKE '%System Balance Sync%'
        AND date >= datetime('now', ?)
        AND user_id = ?
        ORDER BY date DESC
        """,
        (f"-{days} days", user_id,),
    )
    rows = c.fetchall()
    if not rows:
        await ctx.send(f"ℹ Nothing evaluated in the last {days} day(s) to audit.")
        return
    await ctx.send(f" Re-reviewing {len(rows)} categorized transaction(s)...")

    batch_items = [
        {
            "tx_id": r[0],
            "clean_merchant": r[1] or r[2],
            "amount": r[4],
            "account_used": "",
            "txn_date": r[5],
        }
        for r in rows
    ]

    web_results = []
    for start_idx in range(0, len(batch_items), 20):
        chunk = batch_items[start_idx : start_idx + 20]
        chunk_results = await asyncio.gather(
            *[search_searxng(item["clean_merchant"]) for item in chunk],
            return_exceptions=True,
        )
        web_results.extend(chunk_results)

    for item, wr in zip(batch_items, web_results):
        item["web_context"] = _search_payload_to_web_context(wr)

    # Classify in bounded chunks so the JSON output never gets truncated.
    fresh_results: dict[str, tuple[str, str]] = {}
    for start_idx in range(0, len(batch_items), 15):
        chunk = batch_items[start_idx : start_idx + 15]
        fresh_results.update(await classify_transaction_batch(chunk))

    def normalize(cat: str) -> str:
        return re.sub(r"[^\w\s]", "", cat or "").strip().lower()

    flagged = 0
    flagged_details = []
    for r in rows:
        tx_id, clean_merchant, merchant, old_category, amount, date = r
        fresh = fresh_results.get(str(tx_id))
        if not fresh:
            continue
        new_category, explanation = fresh
        if normalize(new_category) == normalize(old_category):
            continue
        flagged += 1
        label = clean_merchant or merchant
        flagged_details.append(
            f"{label} (${amount:.2f}, {date}): `{old_category}` → `{new_category}` — {explanation}"
        )
        msg = await ctx.send(
            f" **Possible miscategorization** — {label} (${amount:.2f}, {date})\n"
            f"Currently: `{old_category}` → Fresh look suggests: `{new_category}`\n"
            f" {explanation}\nReact  to apply,  to leave as-is."
        )
        await msg.add_reaction("")
        await msg.add_reaction("")
        c.execute(
            "INSERT INTO pending_corrections (transaction_row_id, old_category, proposed_category, reasoning, discord_message_id, requester_user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                tx_id,
                old_category,
                new_category,
                explanation,
                msg.id,
                str(ctx.author.id),
            ),
        )
        conn.commit()

    if flagged == 0:
        await ctx.send(" Audit complete — nothing looked off.")
    else:
        await ctx.send(f" Flagged {flagged} for your review above  (react /).")

    uid = str(ctx.author.id)
    if flagged == 0:
        audit_summary = f"[Internal note — !audit]: Re-reviewed {len(rows)} transactions from last {days} days. Nothing looked off."
    else:
        details_text = "; ".join(flagged_details)
        audit_summary = f"[Internal note — !audit]: Re-reviewed {len(rows)} transactions. {flagged} flagged: {details_text}"

    if uid not in SESSION_HISTORY:
        SESSION_HISTORY[uid] = []
    SESSION_HISTORY[uid].append({"role": "assistant", "content": audit_summary})
    SESSION_HISTORY[uid] = SESSION_HISTORY[uid][-SESSION_HISTORY_MAX_TURNS:]

    try:
        save_chat_turn(uid, "assistant", audit_summary)
    except Exception as e:
        print(f" [audit] Failed to persist audit summary: {e}")

@audit_categories.error
async def audit_categories_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions.")
    else:
        await ctx.send(f" Command error: {error}")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    user_id = str(payload.user_id)
    if payload.user_id == bot.user.id:
        return
    src.core.state.CURRENT_USER_ID.set(str(payload.user_id))
    if str(payload.emoji) not in ("", ""):
        return
    c.execute(
        "SELECT id, transaction_row_id, proposed_category, requester_user_id FROM pending_corrections WHERE discord_message_id = ? AND status = 'awaiting'",
        (payload.message_id,),
    )
    row = c.fetchone()
    if not row:
        return
    correction_id, tx_row_id, proposed_category, requester_user_id = row
    if requester_user_id and str(payload.user_id) != str(requester_user_id):
        return
    channel = bot.get_channel(payload.channel_id)
    if str(payload.emoji) == "":
        c.execute(
            "UPDATE transactions SET category = ? WHERE id = ? AND user_id = ?",
            (proposed_category, tx_row_id, str(requester_user_id or payload.user_id)),
        )
        c.execute(
            "UPDATE pending_corrections SET status = 'applied' WHERE id = ?",
            (correction_id,),
        )
        conn.commit()
        if channel:
            await channel.send(f" Updated — category set to `{proposed_category}`.")
    else:
        c.execute(
            "UPDATE pending_corrections SET status = 'rejected' WHERE id = ?",
            (correction_id,),
        )
        conn.commit()
        if channel:
            await channel.send(" Left as-is.")


import uuid
from fastapi.responses import HTMLResponse

PLAID_UPDATE_SESSIONS = {}
PLAID_LINK_SESSIONS = {}  # session_id -> {"link_token": ..., "discord_user_id": ..., "client_id": ..., "secret": ...}

def _build_public_url(path: str) -> str:
    """Build a Plaid callback URL from .env config instead of guessing the hostname.

    BOT_PUBLIC_URL   — required base, e.g. "http://alma" or "https://mydomain.example"
    BOT_PUBLIC_PORT  — port to append (default 8000), ignored if PORT_OFF=1
    PORT_OFF         — set to 1 to omit the port entirely (e.g. behind a reverse proxy)
    """
    base = os.getenv("BOT_PUBLIC_URL", "").strip().rstrip("/")
    if not base:
        # Fallback to the old Tailscale-hostname behavior if not configured.
        base = f"http://{platform.node()}"

    port_off = os.getenv("PORT_OFF", "0").strip() == "1"
    if port_off:
        return f"{base}{path}"

    port = os.getenv("BOT_PUBLIC_PORT", "8000").strip()
    return f"{base}:{port}{path}"


class _PlaidExchangeBody(BaseModel):
    public_token: str

@app.get("/linkbank/{session_id}")
async def serve_plaid_link(session_id: str):
    session = PLAID_LINK_SESSIONS.get(session_id)
    if not session:
        return HTMLResponse("<h2>Invalid or expired session.</h2>", status_code=404)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Connect Your Bank</title>
        <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
        <style>
            body {{ font-family: sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; background: #f4f4f5; text-align: center; }}
            button {{ padding: 15px 30px; font-size: 18px; background: #000; color: #fff; border: none; border-radius: 8px; cursor: pointer; }}
            button:hover {{ background: #333; }}
            #status {{ margin-top: 20px; font-size: 15px; color: #555; }}
        </style>
    </head>
    <body>
        <h2>Connect a New Bank Account</h2>
        <p>Click below to log in to your bank via Plaid.</p>
        <button id="link-btn">Connect Bank</button>
        <div id="status"></div>
        <script>
        var handler = Plaid.create({{
            token: '{session["link_token"]}',
            onSuccess: function(public_token, metadata) {{
                document.getElementById('status').innerText = 'Finishing setup...';
                fetch('/linkbank/{session_id}/exchange', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ public_token: public_token }})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.ok) {{
                        document.body.innerHTML = "<h2> Bank connected!</h2><p>You can close this window and return to Discord.</p>";
                    }} else {{
                        document.getElementById('status').innerText = 'Error: ' + data.error;
                    }}
                }})
                .catch(err => {{
                    document.getElementById('status').innerText = 'Error: ' + err;
                }});
            }},
            onExit: function(err, metadata) {{
                if (err != null) {{ document.getElementById('status').innerText = 'Error: ' + err.error_message; }}
            }}
        }});
        document.getElementById('link-btn').onclick = function() {{ handler.open(); }};
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)



# ============================================================
# Gmail OAuth Callback
# ============================================================

@app.get("/gmail/callback")
async def gmail_oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
):
    """
    Google OAuth callback.

    The OAuth state identifies the Discord user server-side.
    The authorization code is never persisted.
    """

    if error:
        return HTMLResponse(
            f"""
            <!doctype html>
            <html>
            <head>
                <title>Gmail Authorization</title>
                <meta charset="utf-8">
            </head>
            <body>
                <h2>Gmail authorization cancelled</h2>
                <p>Google returned: {html.escape(str(error))}</p>
                <p>You can close this window and return to Discord.</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    if not code or not state:
        return HTMLResponse(
            """
            <!doctype html>
            <html>
            <head>
                <title>Gmail Authorization</title>
                <meta charset="utf-8">
            </head>
            <body>
                <h2>Gmail authorization failed</h2>
                <p>The OAuth callback was missing required parameters.</p>
                <p>You can close this window and return to Discord.</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    result = complete_gmail_authorization(
        code=code,
        state=state,
    )

    if not result.get("ok"):
        return HTMLResponse(
            f"""
            <!doctype html>
            <html>
            <head>
                <title>Gmail Authorization</title>
                <meta charset="utf-8">
            </head>
            <body>
                <h2>Gmail authorization failed</h2>
                <p>{html.escape(str(result.get("error") or "Unknown error."))}</p>
                <p>You can close this window and return to Discord.</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    email_address = html.escape(
        str(result.get("email") or "")
    )

    return HTMLResponse(
        f"""
        <!doctype html>
        <html>
        <head>
            <title>Gmail Connected</title>
            <meta charset="utf-8">
        </head>
        <body>
            <h2>Gmail connected successfully</h2>
            <p>Connected account: <strong>{email_address}</strong></p>
            <p>You can close this window and return to Discord.</p>
        </body>
        </html>
        """
    )


@app.get("/gmail/success")
async def gmail_success():
    return HTMLResponse(
        """
        <!doctype html>
        <html>
        <head>
            <title>Gmail Connected</title>
            <meta charset="utf-8">
        </head>
        <body>
            <h2>Gmail connected</h2>
            <p>You can close this window and return to Discord.</p>
        </body>
        </html>
        """
    )


@app.post("/linkbank/{session_id}/exchange")
async def exchange_plaid_link(session_id: str, body: _PlaidExchangeBody):
    session = PLAID_LINK_SESSIONS.get(session_id)
    if not session:
        return {"ok": False, "error": "Session expired or invalid."}

    env = os.getenv("PLAID_ENV", "production")
    url = f"https://{env}.plaid.com/item/public_token/exchange"
    payload = {
        "client_id": session["client_id"],
        "secret": session["secret"],
        "public_token": body.public_token,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=30.0)

    if resp.status_code != 200:
        return {"ok": False, "error": resp.text}

    new_token = resp.json().get("access_token")
    if not new_token:
        return {"ok": False, "error": "No access_token returned by Plaid."}

    uid = str(session["discord_user_id"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    from src.db.queries import conn as _conn
    _c = _conn.cursor()
    _c.execute("SELECT value FROM user_info WHERE user_id = ? AND key = 'plaid_access_tokens'", (uid,))
    row = _c.fetchone()
    existing = [t.strip() for t in (row[0] if row else "").split(",") if t.strip()]
    if new_token not in existing:
        existing.append(new_token)
    merged = ",".join(existing)

    _c.execute(
        """INSERT INTO user_info (user_id, key, value, observed_at, created_at, updated_at)
        VALUES (?, 'plaid_access_tokens', ?, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (uid, merged, now, now, now),
    )
    _conn.commit()

    PLAID_LINK_SESSIONS.pop(session_id, None)

    try:
        discord_user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
        if discord_user:
            asyncio.create_task(discord_user.send(
                f" **Bank connected!** New access token saved (now tracking {len(existing)} account token(s) total)."
            ))
    except Exception as exc:
        print(f" [PLAIDLINK] Could not DM user {uid}: {exc}")

    return {"ok": True}


@app.get("/fixbank/{session_id}")
async def serve_plaid_fix(session_id: str):
    link_token = PLAID_UPDATE_SESSIONS.get(session_id)
    if not link_token:
        return HTMLResponse("<h2>Invalid or expired session.</h2>", status_code=404)
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Fix Bank Login</title>
        <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
        <style>
            body {{ font-family: sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; background: #f4f4f5; }}
            button {{ padding: 15px 30px; font-size: 18px; background: #000; color: #fff; border: none; border-radius: 8px; cursor: pointer; }}
            button:hover {{ background: #333; }}
        </style>
    </head>
    <body>
        <h2>Bank Connection Needs Update</h2>
        <p>Click the button below to re-authenticate with your bank.</p>
        <button id="link-btn">Update Bank Login</button>
        <script>
        var handler = Plaid.create({{
            token: '{link_token}',
            onSuccess: function(public_token, metadata) {{
                document.body.innerHTML = "<h2> Success! The connection has been restored.</h2><p>You can close this window and return to Discord.</p>";
            }},
            onExit: function(err, metadata) {{
                if (err != null) {{ alert("Error: " + err.error_message); }}
            }}
        }});
        document.getElementById('link-btn').onclick = function() {{ handler.open(); }};
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

@bot.command(name="plaidlink")
@commands.has_permissions()
async def plaidlink_cmd(ctx: commands.Context):
    """First-time Plaid Link flow — connects a brand-new bank account and saves the resulting access token."""
    user_id = str(ctx.author.id)
    client_id = get_plaid_credential(ctx.author.id, "plaid_client_id")
    secret = get_plaid_credential(ctx.author.id, "plaid_secret")
    if not client_id or not secret:
        await ctx.send(" No Plaid `client_id`/`secret` on file. Run `!plaidsetup` first and enter those two fields (access tokens can stay blank).")
        return

    env = os.getenv("PLAID_ENV", "production")
    url = f"https://{env}.plaid.com/link/token/create"
    payload = {
        "client_id": client_id,
        "secret": secret,
        "client_name": "Delilah CFO",
        "country_codes": ["US"],
        "language": "en",
        "user": {"client_user_id": str(ctx.author.id)},
        "products": ["transactions"],
    }

    msg = await ctx.send(" Generating secure Plaid Link session...")

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=30.0)
        if resp.status_code != 200:
            await msg.edit(content=f" Plaid API Error: `{resp.text}`")
            return
        link_token = resp.json().get("link_token")

    if not link_token:
        await msg.edit(content=" Plaid did not return a link_token.")
        return

    session_id = str(uuid.uuid4())
    PLAID_LINK_SESSIONS[session_id] = {
        "link_token": link_token,
        "discord_user_id": ctx.author.id,
        "client_id": client_id,
        "secret": secret,
    }

    link_url = _build_public_url(f"/linkbank/{session_id}")
    await msg.edit(content=(
        f" **Bank connection link ready!**\n\n"
        f"Click this link to log in to your bank:\n"
        f" {link_url}\n\n"
        f"Once you finish, I'll DM you a confirmation, and the new token is saved automatically."
    ))

@plaidlink_cmd.error
async def plaidlink_cmd_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions.")
    else:
        await ctx.send(f" Command error: {error}")

@bot.command(name="fixbank")
@commands.has_permissions()
async def fixbank_cmd(ctx: commands.Context, index: int = None):
    user_id = str(ctx.author.id)
    
    tokens = [t.strip() for t in (get_plaid_credential(ctx.author.id, "plaid_access_tokens") or "").split(",") if t.strip()]
    if not tokens:
        await ctx.send(" No Plaid access tokens on file. Run `!plaidlink` to connect a bank for the first time, or `!plaidsetup` if you already have tokens to paste in.")
        return
    if index is None or index < 0 or index >= len(tokens):
        await ctx.send(f" Invalid token index. You have {len(tokens)} tokens (0 to {len(tokens)-1}).")
        return
        
    target_token = tokens[index]
    # Hardcoded to production since you indicated you use the production environment
    env = os.getenv("PLAID_ENV", "production") 
    url = f"https://{env}.plaid.com/link/token/create"
    
    payload = {
        "client_id": get_plaid_credential(ctx.author.id, "plaid_client_id"),
        "secret": get_plaid_credential(ctx.author.id, "plaid_secret"),
        "client_name": "Delilah CFO",
        "country_codes": ["US"],
        "language": "en",
        "user": {"client_user_id": "delilah-admin"},
        "access_token": target_token
    }
    
    msg = await ctx.send(" Generating secure Plaid Update Link...")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            await msg.edit(content=f" Plaid API Error: `{resp.text}`")
            return
            
        link_token = resp.json()['link_token']
        
    session_id = str(uuid.uuid4())
    PLAID_UPDATE_SESSIONS[session_id] = link_token
    
    fix_url = _build_public_url(f"/fixbank/{session_id}")
    await msg.edit(content=(
        f" **Bank update link ready!**\n\n"
        f"Click this link to re-authenticate:\n"
        f" {fix_url}\n\n"
        f"Once finished, run `!checknow`."
    ))


async def plaid_health_monitor():
    await asyncio.sleep(30) # Wait for bot to fully boot
    while True:
        try:
            tokens = [t.strip() for t in os.getenv("PLAID_ACCESS_TOKENS", "").split(",") if t.strip()]
            if not tokens:
                await asyncio.sleep(86400)
                continue
            
            env = os.getenv("PLAID_ENV", "production")
            url_item = f"https://{env}.plaid.com/item/get"
            client_id = os.getenv("PLAID_CLIENT_ID")
            secret = os.getenv("PLAID_SECRET")
            
            broken = []
            async with httpx.AsyncClient() as client:
                for i, token in enumerate(tokens):
                    resp = await client.post(url_item, json={"client_id": client_id, "secret": secret, "access_token": token})
                    if resp.status_code != 200:
                        err_data = resp.json()
                        if err_data.get("error_code") == "ITEM_LOGIN_REQUIRED":
                            broken.append(i)
                    else:
                        data = resp.json()
                        if data.get("item", {}).get("error"):
                            broken.append(i)
            
            if broken:
                channel = bot.get_user(int(src.core.state.CURRENT_USER_ID.get())) or bot.get_channel(DISCORD_CHANNEL_ID)
                if channel:
                    idxs = ", ".join(str(idx) for idx in broken)
                    await channel.send(
                        f" **Plaid Connection Alert!**\n"
                        f"Bank connection(s) `[{idxs}]` have disconnected (MFA required or password changed).\n"
                        f"Run `!fixbank <number>` to restore the connection."
                    )
        except Exception as e:
            print(f" Plaid health monitor error: {e}")
        
        # Check every 12 hours
        await asyncio.sleep(43200)

@bot.command(name="plaidstatus")
@commands.has_permissions()
async def plaidstatus_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    
    tokens = [t.strip() for t in (get_plaid_credential(ctx.author.id, "plaid_access_tokens") or "").split(",") if t.strip()]
    if not tokens:
        await ctx.send(" No Plaid access tokens on file. Run `!plaidlink` to connect a bank for the first time, or `!plaidsetup` if you already have tokens to paste in.")
        return
        
    env = os.getenv("PLAID_ENV", "production")
    url_item = f"https://{env}.plaid.com/item/get"
    url_inst = f"https://{env}.plaid.com/institutions/get_by_id"
    
    client_id = get_plaid_credential(ctx.author.id, "plaid_client_id")
    secret = get_plaid_credential(ctx.author.id, "plaid_secret")
    
    msg = await ctx.send(" Checking Plaid connection statuses...")
    embed = discord.Embed(title=" Plaid Connection Status", color=discord.Color.blue())
    
    async with httpx.AsyncClient() as client:
        for i, token in enumerate(tokens):
            payload = {"client_id": client_id, "secret": secret, "access_token": token}
            resp = await client.post(url_item, json=payload)
            
            if resp.status_code == 200:
                data = resp.json()
                item = data.get("item", {})
                inst_id = item.get("institution_id")
                error = item.get("error")
                
                bank_name = "Unknown Bank"
                if inst_id:
                    inst_resp = await client.post(url_inst, json={
                        "client_id": client_id, "secret": secret, 
                        "institution_id": inst_id, "country_codes": ["US"]
                    })
                    if inst_resp.status_code == 200:
                        bank_name = inst_resp.json().get("institution", {}).get("name", bank_name)
                        
                if error:
                    err_code = error.get("error_code", "UNKNOWN")
                    err_msg = error.get("error_message", "")
                    val = f" **BROKEN** (`{err_code}`)\n{err_msg}\n*Run `!fixbank {i}` to repair.*"
                else:
                    val = " **HEALTHY**\nConnection is active and syncing."
                    
                embed.add_field(name=f"[{i}] {bank_name}", value=val, inline=False)
            else:
                try:
                    err_data = resp.json()
                    err_code = err_data.get("error_code", f"HTTP {resp.status_code}")
                    if err_code == "ITEM_LOGIN_REQUIRED":
                        val = f" **LOGIN REQUIRED**\nBank credentials changed or MFA needed.\n*Run `!fixbank {i}` to repair.*"
                    else:
                        val = f" **ERROR** (`{err_code}`)\n{err_data.get('error_message', '')}"
                except Exception:
                    val = f" **HTTP {resp.status_code}**\nFailed to verify."
                    
                embed.add_field(name=f"[{i}] Connection", value=val, inline=False)
                
    view = _UniversalCloseView(ctx)
    await msg.edit(content="", embed=embed, view=view)
    view._message = msg

# ============================================================
# Discord Ready
# ============================================================


BACKGROUND_TASKS_STARTED = False
_PERSISTENT_TASKS = set()

async def preload_advisor_model():
    print(f" Warming up {src.core.state.ADVISOR_MODEL} into VRAM...")
    payload = {
        "model": src.core.state.ADVISOR_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "think": False,
        "keep_alive": "24h",
        "options": {"num_ctx": ADVISOR_NUM_CTX},
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            await client.post(OLLAMA_URL, json=payload)
            print(f" {src.core.state.ADVISOR_MODEL} warm-up completed.")
    except Exception as e:
        print(f" Failed to warm up model: {e}")

@bot.command(name="reclassifyall")
@commands.has_permissions()
async def reclassify_all(ctx: commands.Context, target: str = "failed"):
    user_id = str(ctx.author.id)
    target = target.lower().strip()
    if target == "all":
        c.execute(
            "SELECT id, message_id, merchant, amount, account_used, net_cash, date FROM transactions WHERE user_id = ?",
            (user_id,)
        )
    else:
        c.execute(
            """SELECT id, message_id, merchant, amount, account_used, net_cash, date
            FROM transactions WHERE user_id = ? AND (category LIKE '%Uncategorized%' OR judgment LIKE '%Failed%' OR status = 'Pending')""",
            (user_id,)
        )
    rows = c.fetchall()
    if not rows:
        await ctx.send("ℹ No transactions matched the reclassification criteria.")
        return
    tx_ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(tx_ids))
    c.execute(
        f"""UPDATE transactions
            SET status = 'Pending'
            WHERE user_id = ?
              AND id IN ({placeholders})""",
        [user_id, *tx_ids],
    )
    conn.commit()
    for row in rows:
        await tx_queue.put((user_id, row))

    embed = discord.Embed(
        title="🔄 Reclassification Queued",
        description=(
            f"Queued **{len(rows)}** transaction(s) for re-classification."
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Scope",
        value="Only your transactions",
        inline=True,
    )
    embed.add_field(
        name="Target",
        value=f"`{target}`",
        inline=True,
    )
    embed.set_footer(text=f"User {user_id}")
    await ctx.send(embed=embed)

@reclassify_all.error
async def reclassify_all_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" You need administrator permissions.")
    else:
        await ctx.send(f" Command error: {error}")

@bot.command(name="wipememory")
async def wipe_memory(ctx: commands.Context):
    user_id = str(ctx.author.id)
    uid = user_id
    SESSION_HISTORY[uid] = []
    try:
        c.execute("DELETE FROM chat_history WHERE user_id = ?", (uid,))
        conn.commit()
    except Exception as e:
        embed = discord.Embed(
            title="❌ Memory Clear Failed",
            description=(
                "Your in-memory context was cleared, but the persisted "
                "conversation history could not be removed."
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Error",
            value=f"`{str(e)[:900]}`",
            inline=False,
        )
        embed.set_footer(text=f"User {user_id}")
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title="🧹 Memory Wiped",
        description=(
            "Your Delilah conversation memory has been cleared.\n\n"
            "Your **financial data was not changed**."
        ),
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Scope",
        value="Your account only",
        inline=True,
    )
    embed.set_footer(text=f"User {user_id}")
    await ctx.send(embed=embed)
async def wipe_memory_error(ctx: commands.Context, error: commands.CommandError):
    user_id = str(ctx.author.id)
    await ctx.send(f" Command error: {error}")

@bot.event
async def on_ready():
    global BACKGROUND_TASKS_STARTED
    print("==========================================")
    print("Delilah Financial OS Online")
    print(f"Conversational Advisor:  {src.core.state.ADVISOR_MODEL}")
    print(f"Listening Channel: {DISCORD_CHANNEL_ID}")
    print(f"Build: {DELILAH_BUILD}")
    print("Advisor controls: !cancel / !status")
    print("==========================================")

    load_history_on_boot(limit=100)

    if BACKGROUND_TASKS_STARTED:
        return
    BACKGROUND_TASKS_STARTED = True
    t1 = bot.loop.create_task(queue_worker())
    _PERSISTENT_TASKS.add(t1)
    t1.add_done_callback(_PERSISTENT_TASKS.discard)
    # Ensure Plaid-managed tables (plaid_cursors, plaid_accounts, balance_snapshots,
    # financial_snapshots) exist regardless of whether the autosync loop runs —
    # !clear, !checknow, etc. all assume this schema is already in place.
    plaid_sync._ensure_cursor_table(conn)

    if os.getenv("ENABLE_PLAID_AUTOSYNC", "true").lower() not in ("0", "false", "no"):
        t2 = bot.loop.create_task(plaid_sync.plaid_polling_loop(conn, tx_queue, bot, DISCORD_CHANNEL_ID))
        _PERSISTENT_TASKS.add(t2)
        t2.add_done_callback(_PERSISTENT_TASKS.discard)
        print(" [PLAID] Auto-sync polling loop started.")
    else:
        print("  [PLAID] Auto-sync polling loop disabled via ENABLE_PLAID_AUTOSYNC. Use !checknow or your own scheduler.")
    t3 = bot.loop.create_task(reminder_watchdog_loop())
    _PERSISTENT_TASKS.add(t3)
    t3.add_done_callback(_PERSISTENT_TASKS.discard)
    c.execute("""SELECT id, message_id, merchant, amount, account_used, net_cash, date
        FROM transactions WHERE status = 'Pending' ORDER BY id ASC""")
    backlog = c.fetchall()
    if backlog:
        print(
            f" Found {len(backlog)} pending transaction(s). "
            "Automatic classification is disabled; leaving them pending."
        )
    await preload_advisor_model()

class _AdvisorReplyHandle:
    """Adapter that lets the advisor run even if Discord placeholder delivery stalls."""

    __slots__ = ("channel", "placeholder", "source_message_id")

    def __init__(self, channel, source_message_id: int | None = None):
        self.channel = channel
        self.placeholder = None
        self.source_message_id = source_message_id

    async def delete(self):
        placeholder = self.placeholder
        if placeholder is None:
            return
        try:
            await placeholder.delete()
        except Exception as exc:
            print(f" [THINKING DELETE FAILED] {type(exc).__name__}: {exc}")

    async def set_placeholder(self, placeholder):
        self.placeholder = placeholder


async def _send_thinking_placeholder(message: discord.Message, handle: _AdvisorReplyHandle):
    """Best-effort placeholder. It is never allowed to block advisor execution."""
    user_id = str(message.author.id)
    try:
        placeholder = await asyncio.wait_for(
            message.reply(" *Thinking...*"), timeout=8.0
        )
        await handle.set_placeholder(placeholder)
        print(f" [MESSAGE] Thinking placeholder sent id={placeholder.id}")
    except asyncio.TimeoutError:
        print(" [MESSAGE] Thinking placeholder timed out after 8s; advisor continues.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f" [MESSAGE] Thinking placeholder failed: {type(exc).__name__}: {exc}")


@bot.event
async def on_message(message: discord.Message):
    user_id = str(message.author.id)
    src.core.state.CURRENT_USER_ID.set(str(message.author.id))
    if message.author.id == bot.user.id:
        return
    src.core.state.CURRENT_USER_ID.set(str(message.author.id))

    message_id = getattr(message, "id", None)
    attachment_count = len(message.attachments or [])
    print(
        f" [MESSAGE] received id={message_id} author={message.author.id} "
        f"content_chars={len(message.content or '')} attachments={attachment_count}",
        flush=True,
    )

    try:
        await bot.process_commands(message)
    except Exception as exc:
        print(f" [MESSAGE] process_commands failed: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return

    if message.content.startswith(bot.command_prefix):
        return
    if message.webhook_id or message.author.bot or message.content.startswith("asrt"):
        return

    try:
        images, pdf_texts = await asyncio.wait_for(
            _collect_message_images(message),
            timeout=60.0,
        )

        # Web-hosted images in the user's message (e.g. Zipline URLs) must be
        # explicitly downloaded and passed through the same base64/JPEG pipeline
        # as Discord attachments. Merely downloading an image in the Python
        # sandbox does NOT make it visible to the Ollama vision model.
        web_image_urls = _extract_image_urls(message.content or "")
        if web_image_urls and len(images) < MAX_IMAGES_PER_MESSAGE:
            remaining = MAX_IMAGES_PER_MESSAGE - len(images)

            for image_url in web_image_urls[:remaining]:
                encoded = await _prepare_image_url(image_url)
                if encoded:
                    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()

                    # Avoid sending the same image twice if it was also attached
                    # directly to the Discord message.
                    existing_digests = {
                        hashlib.sha256(existing.encode("ascii")).hexdigest()
                        for existing in images
                    }

                    if digest in existing_digests:
                        print(
                            f" [MESSAGE] duplicate web image skipped "
                            f"url={image_url!r}",
                            flush=True,
                        )
                    else:
                        images.append(encoded)
                        print(
                            f" [MESSAGE] web image added to vision pipeline "
                            f"url={image_url!r}",
                            flush=True,
                        )

        print(
            f" [MESSAGE] image collection complete id={message_id} "
            f"images={len(images)} pdf_texts={len(pdf_texts)} "
            f"web_image_urls={len(web_image_urls)}",
            flush=True,
        )
    except asyncio.TimeoutError:
        print(f" [MESSAGE] image/PDF collection timed out id={message_id}")
        try:
            await asyncio.wait_for(
                message.channel.send(" I couldn't finish reading the attachment in time. Please resend the image."),
                timeout=8.0,
            )
        except Exception as send_exc:
            print(f" [MESSAGE] timeout response failed: {type(send_exc).__name__}: {send_exc}")
        return
    except Exception as exc:
        print(
            f" [MESSAGE] image/PDF collection crashed id={message_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        import traceback
        traceback.print_exc()
        try:
            await asyncio.wait_for(
                message.channel.send(content=f" Attachment processing failed: `{type(exc).__name__}`"),
                timeout=8.0,
            )
        except Exception as send_exc:
            print(f" [MESSAGE] attachment error response failed: {type(send_exc).__name__}: {send_exc}")
        return

    prompt = message.content.strip()
    if pdf_texts:
        prompt = (prompt + "\n" if prompt else "") + "\n".join(pdf_texts)

    text_attachments = []
    text_extensions = (
        '.txt', '.py', '.csv', '.json', '.md', '.log', '.yml', 
        '.yaml', '.xml', '.ini', '.sh', '.js', '.html', '.css'
    )
    for att in message.attachments:
        mime = att.content_type or ""
        fname = att.filename.lower()
        if att.size > 2_000_000:
            continue
        if mime.startswith('text/') or mime.startswith('application/json') or fname.endswith(text_extensions):
            try:
                file_bytes = await att.read()
                file_text = file_bytes.decode('utf-8')
                text_attachments.append(f"--- START OF ATTACHMENT {att.filename} ---\n{file_text}\n--- END OF ATTACHMENT {att.filename} ---")
            except UnicodeDecodeError:
                pass
            except Exception as e:
                print(f" [MESSAGE] Could not read {att.filename} as text: {e}")
                
    if text_attachments:
        prompt = (prompt + "\n\n" if prompt else "") + "\n\n".join(text_attachments)

    if not prompt and not images:
        print(f"ℹ [MESSAGE] nothing to process id={message_id}")
        return

    uid = str(message.author.id)
    handle = _AdvisorReplyHandle(message.channel, source_message_id=message_id)
    placeholder_task = None

    print(
        f" [MESSAGE] entering advisor registration id={message_id} uid={uid} "
        f"images={len(images)} prompt_chars={len(prompt)}"
    )

    try:
        async with ADVISOR_TASK_REGISTRATION_LOCK:
            print(f" [MESSAGE] advisor registration lock acquired id={message_id}")
            existing = ACTIVE_ADVISOR_TASKS.get(uid)
            if existing is not None and not existing.done():
                print(f" [MESSAGE] existing advisor task uid={uid}")
                try:
                    await asyncio.wait_for(
                        message.channel.send(
                            " **I'm already working on your previous request.** "
                            "Use `!status` to check progress or `!cancel` to stop it."
                        ),
                        timeout=8.0,
                    )
                except Exception as exc:
                    print(f" [MESSAGE] busy response failed: {type(exc).__name__}: {exc}")
                return

            # Register/create the advisor BEFORE any Discord reply API call.
            # A stalled message.reply() must never prevent the actual request from running.
            task = asyncio.create_task(
                chat_with_delilah(
                    prompt, message.author.id, handle, image_b64_list=images
                ),
                name=f"advisor:{message.author.id}:{message_id}",
            )
            ACTIVE_ADVISOR_TASKS[uid] = task
            await _set_advisor_status(
                uid,
                phase="starting",
                started_at=time.monotonic(),
                cancelled=False,
                cancel_requested=False,
                attempts=0,
                tool_calls=0,
                output_chars=0,
                last_tool=None,
                pending_tools=[],
            )
            print(
                f" [MESSAGE] advisor task registered id={message_id} "
                f"task={task.get_name()} images={len(images)}"
            )

        # Best-effort Discord UX, completely independent from advisor execution.
        placeholder_task = asyncio.create_task(
            _send_thinking_placeholder(message, handle),
            name=f"thinking:{message.author.id}:{message_id}",
        )

        print(f" [MESSAGE] awaiting advisor task id={message_id}")
        await task
        print(f" [MESSAGE] advisor task finished id={message_id}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(
            f" [MESSAGE] advisor task crashed id={message_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        import traceback
        traceback.print_exc()
        try:
            await asyncio.wait_for(
                message.channel.send(content=f" Something went wrong: `{type(exc).__name__}: {exc}`"),
                timeout=8.0,
            )
        except Exception as render_err:
            print(f" [MESSAGE] crash response failed id={message_id}: {type(render_err).__name__}: {render_err}")
    finally:
        if placeholder_task is not None and not placeholder_task.done():
            placeholder_task.cancel()
            try:
                await placeholder_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

import uvicorn

async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())


__all__ = ['check_now', 'on_raw_reaction_add', '_send_transaction_browser', 'queue_worker', '_AdvisorReplyHandle', 'researchstatus_command', 'help_command', 'test_push_cmd', 'ntfy_setup_cmd', 'model_set', 'set_budget', 'model_get', 'BACKGROUND_TASKS_STARTED', 'on_message', '_ReportCloseView', '_ollama_installed_models', 'dbstatus_command', 'model_command', 'preload_advisor_model', 'locked_command', 'audit_transaction_command', 'clear_all', 'merchantstatus_command', 'wipe_memory_error', 'TRANSACTION_BROWSER_VIEWS', 'clear_chat', '_ReportPageView', 'recategorize_error', 'model_command_error', 'audit_categories', 'wipe_memory', 'check_now_error', 'audit_categories_error', 'clear_chat_error', 'corrections_command', 'integrity_command', '_send_error_embed', '_open_verification_db', '_ollama_base_url', '_ollama_warm_model', '_audit_identity_mismatches', 'model_list', '_close_all_control_messages', 'snapshot_command', '_ollama_unload_model', 'recategorize', 'clear_all_error', '_TransactionPageView', 'REPORT_CONTROL_VIEWS', 'main', '_db_identity_mismatches', 'on_ready', 'REPORT_CONTROL_MESSAGES', 'unlocked_command', '_send_thinking_placeholder', 'reclassify_all', '_send_command_report', 'reclassify_all_error']




@bot.command(name="ntfysetup")
async def ntfy_setup_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    
    from src.db.queries import conn
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS user_info (user_id TEXT, key TEXT, value TEXT, observed_at TEXT, created_at TEXT, updated_at TEXT, UNIQUE(user_id, key))")
    c.execute("SELECT value FROM user_info WHERE user_id = ? AND key = 'ntfy_topic'", (user_id,))
    row = c.fetchone()
    
    if row and row[0]:
        topic = row[0]
    else:
        import secrets
        from datetime import datetime
        topic = f"delilah_{user_id}_{secrets.token_hex(6)}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO user_info (user_id, key, value, observed_at, created_at, updated_at) VALUES (?, 'ntfy_topic', ?, ?, ?, ?)", (user_id, topic, now, now, now))
        conn.commit()
    
    instructions = (
        " **Delilah Push Notification Setup**\n\n"
        "1. Download the free **ntfy** app (iOS/Android).\n"
        "2. Open the app and tap **+** to subscribe to a topic.\n"
        "3. Enter your secret topic exactly as shown below:\n"
        "```text\n"
        f"{topic}\n"
        "```\n"
        "4. Tap **Subscribe**.\n"
        "5. Test it by running `!testpush` in our main channel!"
    )
    
    try:
        await ctx.author.send(instructions)
        await ctx.send(" Sent you a DM with your secret ntfy setup instructions!")
    except discord.Forbidden:
        await ctx.send(" I tried to DM you the secret, but your Discord DMs are closed. Please open them and try again.")

@bot.command(name="testpush")
async def test_push_cmd(ctx: commands.Context, *, message: str = "This is a test alert from Delilah!"):
    user_id = str(ctx.author.id)
    msg = await ctx.send(" Sending test push notification to your phone...")
    try:
        await send_push_alert(message, title="Delilah Test", priority="high", user_id=user_id)
        await msg.edit(content=" **Push notification dispatched!** Check your phone.")
    except Exception as e:
        await msg.edit(content=f" Failed to send push notification: {e}")

@bot.command(name="export")
async def export_csv(ctx: commands.Context):
    import csv
    from io import StringIO
    import discord
    try:
        user_id = str(ctx.author.id)
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            user_id = str(ctx.author.id)
            cur.execute("SELECT date, merchant, amount, category, account_used, status, judgment FROM transactions WHERE user_id = ? ORDER BY date DESC", (user_id,))
            rows = cur.fetchall()
            
        f = StringIO()
        writer = csv.writer(f)
        writer.writerow(["Date", "Merchant", "Amount", "Category", "Account", "Status", "Judgment"])
        writer.writerows(rows)
        f.seek(0)
        
        file = discord.File(fp=f, filename="transactions.csv")
        await ctx.send(" **Here is your transaction export:**", file=file)
    except Exception as e:
        await ctx.send(f" Export failed: {e}")


@bot.command(name="chart")
async def chart_spending(ctx: commands.Context, days: int = 30):
    user_id = str(ctx.author.id)
    import matplotlib.pyplot as plt
    from io import BytesIO
    import discord
    try:
        user_id = str(ctx.author.id)
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute('''
                SELECT COALESCE(category, 'Uncategorized'), SUM(amount) 
                FROM transactions 
                WHERE user_id = ? AND status='Evaluated' AND amount > 0 AND merchant NOT LIKE '%System Balance Sync%' 
                AND date >= date('now', ?)
                GROUP BY COALESCE(category, 'Uncategorized')
                ORDER BY SUM(amount) DESC LIMIT 10
            ''', (f'-{days} days',))
            rows = cur.fetchall()
            
        if not rows:
            await ctx.send(f" No evaluated spending found in the last {days} days to chart.")
            return
            
        labels = [r[0] for r in rows]
        sizes = [r[1] for r in rows]
        
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=140, 
               colors=plt.cm.tab20.colors, textprops={'fontsize': 10, 'color': 'white'})
        ax.set_title(f"Spending Breakdown (Last {days} Days)", fontsize=16, pad=20, color='white')
        
        buf = BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', transparent=True)
        buf.seek(0)
        plt.close(fig)
        
        file = discord.File(fp=buf, filename="spending_chart.png")
        await ctx.send(f" **Here is your spending breakdown for the last {days} days:**", file=file)
    except Exception as e:
        await ctx.send(f" Chart generation failed: {e}")

class PlaidSetupModal(discord.ui.Modal, title="Plaid Credentials"):
    client_id = discord.ui.TextInput(label="PLAID_CLIENT_ID", required=False)
    secret = discord.ui.TextInput(label="PLAID_SECRET", required=False)
    tokens = discord.ui.TextInput(label="Access tokens (optional, leave blank)", style=discord.TextStyle.paragraph, required=False, placeholder="Only fill this in if you already have tokens. Otherwise use !plaidlink instead.")

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        src.core.state.CURRENT_USER_ID.set(uid)
        from src.db.queries import conn
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.client_id.value.strip():
            c.execute("INSERT OR REPLACE INTO user_info (user_id, key, value, observed_at, created_at, updated_at) VALUES (?, 'plaid_client_id', ?, ?, ?, ?)", (uid, self.client_id.value.strip(), now, now, now))
        if self.secret.value.strip():
            c.execute("INSERT OR REPLACE INTO user_info (user_id, key, value, observed_at, created_at, updated_at) VALUES (?, 'plaid_secret', ?, ?, ?, ?)", (uid, self.secret.value.strip(), now, now, now))
        if self.tokens.value.strip():
            c.execute("INSERT OR REPLACE INTO user_info (user_id, key, value, observed_at, created_at, updated_at) VALUES (?, 'plaid_access_tokens', ?, ?, ?, ?)", (uid, self.tokens.value.strip(), now, now, now))
        conn.commit()
        await interaction.response.send_message(" Plaid credentials saved securely!", ephemeral=True)

class PlaidSetupView(discord.ui.View):
    @discord.ui.button(label="Setup Plaid", style=discord.ButtonStyle.primary)
    async def setup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlaidSetupModal())

@bot.command(name="plaidsetup")
async def plaidsetup_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    await ctx.send(
        "Click the button below to enter your Plaid `client_id` and `secret`.\n"
        "Leave the access tokens field blank — after submitting, run `!plaidlink` to connect a bank for the first time "
        "(that field only matters if you're pasting in tokens you already have from elsewhere).",
        view=PlaidSetupView(),
    )
