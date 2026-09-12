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
from typing import Optional, List, Dict, Any, Tuple


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

from src.core.state import *
from src.utils.helpers import *
from src.services.search import *
from src.services.gmail import (
    begin_gmail_authorization,
    complete_gmail_authorization,
    disconnect_gmail,
    gmail_status,
)
from src.db.queries import *
from src.services.llm import *
from src.db.prefs import set_user_timezone, get_user_timezone
from src.services.analyst import run_autonomous_analyst



# ============================================================
# Explicit deterministic Discord help
# ============================================================
class _HelpPageView(discord.ui.View):
    """Interactive paginator for the !help command.

    Each page is one section of the help reference.  Pages are split so
    no single embed field exceeds Discord's 1024-char limit.
    """

    def __init__(self, ctx: commands.Context, pages: list[str]):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.pages = pages or ["No help content available."]
        self.page = 0
        self._message: discord.Message | None = None
        REPORT_CONTROL_VIEWS.setdefault(ctx.author.id, set()).add(self)
        self._refresh_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, len(self.pages))

    def _refresh_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.total_pages - 1
        self.page_indicator.label = f"{self.page + 1} / {self.total_pages}"

    def _embed(self) -> discord.Embed:
        title = " Delilah Help"
        if self.total_pages > 1:
            title = f"{title} · {self.page + 1}/{self.total_pages}"
        embed = discord.Embed(
            title=title,
            description=self.pages[self.page][:4096] or "No content.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="SQLite is authoritative • Use buttons to navigate")
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

    async def on_timeout(self) -> None:
        REPORT_CONTROL_VIEWS.get(self.ctx.author.id, set()).discard(self)
        try:
            if self._message is not None:
                for child in self.children:
                    child.disabled = True
                await self._message.edit(view=self)
        except Exception:
            pass


@bot.command(name="help")
async def help_command(ctx: commands.Context, *, section: str = ""):
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
            "`!model list|get|set` — Manage models (e.g., `!model set cloud:<model_name>`).\n`!timezone` — View or set your personal timezone (e.g., `!timezone America/Chicago`).\n"
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
        name=" Chat / Ledger — Part 1",
        value=(
            "`!chart [days]` — Generate a spending pie chart for the trailing X days.\n"
            "`!export` — Download the entire transaction ledger as a .csv file.\n"
            "`!clear` — Clear current chat/context state where supported.\n"
            "`!clearchat` — Clear persisted chat history where supported.\n"
            "`!setbudget <amount>` — Set the configured budget.\n"
            "`!checknow` — Run the configured financial check now.\n"
            "`!plaidstatus` — Pings Plaid API for a status check regarding accounts."
        ),
        inline=False,
    )

    embed.add_field(
        name=" Chat / Ledger — Part 2",
        value=(
            "`!plaidsetup` — Securely input your Plaid `client_id`/`secret` (and tokens, if you already have them).\n"
            "`!plaidlink` — First-time bank connection: opens a real Plaid Link login and saves the new access token automatically.\n"
            "`!fixbank <index>` — Re-authenticate an existing but broken bank connection via a Tailscale URL.\n"
            "`!testpush [msg]` — Send a test push notification to your phone.\n"
            "`!ntfysetup` — DM yourself the secret ntfy topic and instructions.\n"
            "`!gmail connect` — Connect a Gmail account using read-only Google OAuth.\n"
            "`!gmail status` — Show the connected Gmail account.\n"
            "`!gmail disconnect` — Remove the Gmail connection."
        ),
        inline=False,
    )

    embed.add_field(
        name=" Financial Monitor",
        value=(
            "`!monitor list` — Show all monitor rules (including disabled).\n"
            "`!monitor add <kind> <name> <json-config>` — Add a rule.\n"
            "`!monitor run` — Evaluate rules now.\n"
            "`!monitor alerts [limit]` — Show recent unacked alerts.\n"
            "`!monitor ack <id>` — Acknowledge an alert.\n"
            "`!monitor on <id>` / `!monitor off <id>` — Enable/disable a rule.\n"
            "`!monitor del <id>` — Delete a rule.\n"
            "`!monitor clear` — Delete ALL rules and alerts.\n\n"
            "Rule kinds: projected_balance_low, category_spend_exceeded, "
            "income_overdue, subscription_price_changed, unusual_transaction, "
            "recurring_bill_missing, cash_flow_change, large_deposit."
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

    # Build the full help text, then paginate it so no single embed
    # field exceeds Discord's 1024-char limit.
    sections = [
        ("Advisor Control",
         "`!status` — Live advisor state, elapsed time, tool calls, and phase.\n"
         "`!peek` — Spy on the advisor's current thought process and recent tool results.\n"
         "`!auditstatus` — View the live state of the audit controller and research queue.\n"
         "`!pause` — Pause the active advisor run.\n"
         "`!cancel` — Cancel the active advisor run.\n"
         "`!switch <local|cloud>` — Hot-swap between Ollama and OpenAI-compatible.\n"
         "`!model list|get|set` — Manage models (e.g., `!model set cloud:<model_name>`).\n`!timezone` — View or set your personal timezone (e.g., `!timezone America/Chicago`).\n"),
        ("Database / Audit — Direct SQLite",
         "`!dbstatus` — Full database health snapshot.\n"
         "`!unlocked` — Every currently unlocked transaction.\n"
         "`!locked` — Every locked/finalized transaction.\n"
         "`!override <id>` — Admin force-unlock a locked transaction.\n"
         "`!corrections [limit]` — Recent correction history with before/after details.\n"
         "`!merchantstatus` — Cross-reference ledger merchants against known merchants and aliases.\n"
         "`!researchstatus` — Unmatched merchants that require research.\n"
         "`!snapshot` — Compare live transaction identity against the original snapshot.\n"
         "`!integrity` — Deterministic integrity checks; no LLM involved.\n"
         "`!txaudit <transaction_row_id>` — Full forensic history for one transaction."),
        ("Chat / Ledger",
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
         "`!gmail disconnect` — Remove the Gmail connection."),
        ("Financial Monitor",
         "`!monitor list` — Show all monitor rules (including disabled).\n"
         "`!monitor add <kind> <name> <json-config>` — Add a rule.\n"
         "`!monitor run` — Evaluate rules now.\n"
         "`!monitor alerts [limit]` — Show recent unacked alerts.\n"
         "`!monitor ack <id>` — Acknowledge an alert.\n"
         "`!monitor on <id>` / `!monitor off <id>` — Enable/disable a rule.\n"
         "`!monitor del <id>` — Delete a rule.\n"
            "`!monitor clear` — Delete ALL rules and alerts.\n\n"
         "Rule kinds: projected_balance_low, category_spend_exceeded, "
         "income_overdue, subscription_price_changed, unusual_transaction, "
         "recurring_bill_missing, cash_flow_change, large_deposit."),
        ("Important",
         "Database verification commands bypass Delilah and query SQLite directly. "
         "They are the source of truth for counts, locks, corrections, and transaction integrity."),
    ]

    # Optional section filter: `!help <name>` jumps straight to that section.
    section_filter = section.strip().lower() if section else ""
    if section_filter:
        matched = [
            (name, body) for name, body in sections
            if section_filter in name.lower()
        ]
        if not matched:
            await ctx.send(
                f" Unknown help section `{section}`. "
                f"Available: {', '.join(name for name, _ in sections)}"
            )
            return
        sections = matched

    # Split any section whose body exceeds Discord's 1024-char field limit
    # into multiple pages, keeping each under the cap.
    pages: list[str] = []
    for name, body in sections:
        if len(body) <= 1024:
            pages.append(f"**{name}**\n{body}")
        else:
            # Split on newlines, packing lines into <=1024 chunks.
            lines = body.split("\n")
            chunk_lines: list[str] = []
            chunk_len = 0
            for line in lines:
                piece = len(line) + (1 if chunk_lines else 0)
                if chunk_lines and chunk_len + piece > 1000:
                    pages.append(f"**{name}**\n" + "\n".join(chunk_lines))
                    chunk_lines = []
                    chunk_len = 0
                chunk_lines.append(line)
                chunk_len += piece
            if chunk_lines:
                pages.append(f"**{name}**\n" + "\n".join(chunk_lines))

    view = _HelpPageView(ctx, pages)
    msg = await ctx.send(embed=view._embed(), view=view)
    view._message = msg
    REPORT_CONTROL_MESSAGES.setdefault(ctx.author.id, set()).add(msg)



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
# Autonomous Receipt Reconciliation & Itemization
# ============================================================

@bot.group(name="receipts", invoke_without_command=True)
async def receipts_cmd(ctx: commands.Context):
    """Receipt reconciliation and itemized transaction breakdown."""
    if ctx.invoked_subcommand is None:
        await ctx.send(
            "**Receipt Reconciliation & Itemization**\n"
            "`!receipts scan [days]` — Reconcile recent transactions against Gmail receipts.\n"
            "`!receipts view <tx_id>` — View itemized breakdown for a transaction.\n"
            "`!receipts add <tx_id> <item_name> <price> [qty]` — Manually add an itemized line item.\n"
            "`!receipts del <item_id>` — Delete an itemized line item."
        )


@receipts_cmd.command(name="scan")
async def receipts_scan_cmd(ctx: commands.Context, days: int = 14):
    user_id = str(ctx.author.id)
    await ctx.send(" Scanning connected Gmail for matching transaction receipts...")
    try:
        from src.db.queries import reconcile_receipts_for_user
        res = reconcile_receipts_for_user(days=days, user_id=user_id)
        embed = discord.Embed(
            title="🧾 Receipt Reconciliation Results",
            color=0x3498DB,
            description=(
                f"**Scanned Transactions:** {res['scanned_transactions']}\n"
                f"**Matched Receipts:** {res['matched_transactions']}\n"
                f"**Extracted Items:** {res['items_extracted']}"
            )
        )
        for detail in res.get("details", [])[:6]:
            embed.add_field(
                name=f"Tx #{detail['transaction_id']} — {detail['merchant']} (${detail['amount']:,.2f})",
                value=f"Subject: *{detail['subject'][:60]}*\nItems parsed: **{detail['item_count']}**",
                inline=False
            )
        embed.set_footer(text="Use `!receipts view <tx_id>` to view itemized breakdown")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f" Receipt scan failed: `{type(exc).__name__}: {exc}`")


@receipts_cmd.command(name="view")
async def receipts_view_cmd(ctx: commands.Context, tx_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import get_transaction_items
        items = get_transaction_items(tx_id, user_id=user_id)
        if not items:
            await ctx.send(f" No itemized receipt data found for transaction #{tx_id}.")
            return

        total_items_price = sum(item["total_price"] for item in items)
        embed = discord.Embed(
            title=f"🧾 Itemized Breakdown — Tx #{tx_id}",
            color=0x2ECC71,
            description=f"**Total Itemized:** ${total_items_price:,.2f} ({len(items)} items)"
        )
        for item in items:
            embed.add_field(
                name=f"#{item['id']} {item['item_name']}",
                value=f"Qty: {item['quantity']} × ${item['unit_price']:,.2f} = **${item['total_price']:,.2f}** (Source: {item['source']})",
                inline=False
            )
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f" Error fetching receipt items: `{type(exc).__name__}: {exc}`")


@receipts_cmd.command(name="add")
async def receipts_add_cmd(ctx: commands.Context, tx_id: int, item_name: str, price: float, qty: float = 1.0):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import add_transaction_item
        res = add_transaction_item(
            user_id=user_id,
            transaction_row_id=tx_id,
            item_name=item_name,
            total_price=price,
            quantity=qty,
            source="manual"
        )
        await ctx.send(f" Added item #{res['id']} **{item_name}** (${price:,.2f}) to Tx #{tx_id}.")
    except Exception as exc:
        await ctx.send(f" Failed to add item: `{type(exc).__name__}: {exc}`")


@receipts_cmd.command(name="del")
async def receipts_del_cmd(ctx: commands.Context, item_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import delete_transaction_item
        if delete_transaction_item(item_id, user_id=user_id):
            await ctx.send(f" Deleted item #{item_id}.")
        else:
            await ctx.send(f" Item #{item_id} not found.")
    except Exception as exc:
        await ctx.send(f" Failed to delete item: `{type(exc).__name__}: {exc}`")


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
    def __init__(self, ctx: commands.Context, rows: list[tuple], *, locked: bool = False, title: str = "", color: Optional[discord.Color] = None, page_size: int = 25):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.rows = rows
        self.locked = locked
        self.custom_title = title
        self.custom_color = color
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
        total = len(self.rows)
        start = self.page * self.page_size
        end = min(start + self.page_size, total)
        if self.custom_title:
            embed_title = f"{self.custom_title} ({total})"
        else:
            state_word = "Unlocked" if not self.locked else "Locked"
            icon = "🔓" if not self.locked else "🔒"
            embed_title = f"{icon} {state_word} Transactions ({total})"

        embed = discord.Embed(
            title=embed_title,
            description=(
                f"Showing **{start + 1}–{end}** of **{total}** transactions.\n"
                f"Page **{self.page + 1} / {self.total_pages}**"
            ),
            color=self.custom_color or (discord.Color.orange() if not self.locked else discord.Color.green()),
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


async def _send_transaction_browser(
    ctx: commands.Context,
    rows: list[tuple],
    *,
    locked: bool = False,
    title: str = "",
    color: Optional[discord.Color] = None,
    user_id: str,
) -> None:
    if not user_id or not isinstance(user_id, str):
        raise ValueError(
            "_send_transaction_browser: forced isolation violation — user_id is required "
            "and must be a non-empty string."
        )
    if not rows:
        header = title or (f"{'Locked' if locked else 'Unlocked'} Transactions")
        embed = discord.Embed(
            title=" No Transactions Found",
            description=f"No transactions matched for **{header}**.",
            color=color or discord.Color.blurple(),
        )
        await ctx.send(embed=embed)
        return
    view = _TransactionPageView(ctx, rows, locked=locked, title=title, color=color)
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


@bot.command(name="tx", aliases=["transactions", "recenttx"])
async def tx_command(ctx: commands.Context, limit: int = 25):
    """Browse recent transactions in your ledger with interactive pagination."""
    user_id = str(ctx.author.id)
    limit = max(1, min(limit, 200))
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """
                SELECT id, date, merchant, amount, account_used, category, status
                FROM transactions
                WHERE user_id = ?
                  AND merchant NOT LIKE '%System Balance Sync%'
                ORDER BY datetime(date) DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
        await _send_transaction_browser(
            ctx, rows, locked=False, title="💳 Recent Transactions", color=discord.Color.blue(), user_id=user_id
        )
    except Exception as exc:
        await _send_error_embed(ctx, " Transaction Query Failed", exc, user_id=user_id)


@bot.command(name="searchtx", aliases=["findtx", "txsearch"])
async def searchtx_command(ctx: commands.Context, *, query: str):
    """Search transactions by merchant, category, account, or amount filter (e.g. `!searchtx Uber`, `!searchtx >100`)."""
    user_id = str(ctx.author.id)
    q = query.strip()
    if not q:
        await ctx.send("⚠️ Please provide a search term or amount filter (e.g. `!searchtx Starbucks`, `!searchtx >50`).")
        return

    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            amt_match = re.match(r'^([><]=?|=)\s*([0-9]+(?:\.[0-9]{1,2})?)$', q)
            if amt_match:
                op, val = amt_match.groups()
                val_float = float(val)
                sql = f"""
                    SELECT id, date, merchant, amount, account_used, category, status
                    FROM transactions
                    WHERE user_id = ?
                      AND merchant NOT LIKE '%System Balance Sync%'
                      AND amount {op} ?
                    ORDER BY datetime(date) DESC, id DESC
                    LIMIT 100
                """
                cur.execute(sql, (user_id, val_float))
            else:
                like = f"%{q.lower()}%"
                cur.execute(
                    """
                    SELECT id, date, merchant, amount, account_used, category, status
                    FROM transactions
                    WHERE user_id = ?
                      AND merchant NOT LIKE '%System Balance Sync%'
                      AND (
                          LOWER(COALESCE(merchant, '')) LIKE ?
                          OR LOWER(COALESCE(clean_merchant, '')) LIKE ?
                          OR LOWER(COALESCE(category, '')) LIKE ?
                          OR LOWER(COALESCE(account_used, '')) LIKE ?
                      )
                    ORDER BY datetime(date) DESC, id DESC
                    LIMIT 100
                    """,
                    (user_id, like, like, like, like),
                )
            rows = cur.fetchall()

        await _send_transaction_browser(
            ctx, rows, locked=False, title=f"🔎 Search: '{q}'", color=discord.Color.teal(), user_id=user_id
        )
    except Exception as exc:
        await _send_error_embed(ctx, " Transaction Search Failed", exc, user_id=user_id)


@bot.command(name="txview", aliases=["txdetail", "transaction"])
async def txview_command(ctx: commands.Context, tx_id: int):
    """View full details, receipt items, tags, and notes for a specific transaction."""
    user_id = str(ctx.author.id)
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """
                SELECT id, transaction_id, date, merchant, clean_merchant, category, amount,
                       override_amount, account_used, status, is_locked, context
                FROM transactions
                WHERE user_id = ? AND id = ?
                """,
                (user_id, tx_id),
            )
            row = cur.fetchone()
            if not row:
                await ctx.send(f"❌ Transaction `#{tx_id}` not found.")
                return

            (tid, plaid_id, date, merch, clean_m, cat, amt, override_amt, acc, status, locked, context) = row

            cur.execute(
                """
                SELECT id, item_name, quantity, unit_price, total_price, source
                FROM transaction_items
                WHERE user_id = ? AND transaction_row_id = ?
                ORDER BY id ASC
                """,
                (user_id, tx_id),
            )
            items = cur.fetchall()

        embed = discord.Embed(
            title=f"🧾 Transaction #{tx_id} Details",
            color=discord.Color.green() if locked else discord.Color.blue()
        )
        amt_str = f"${float(amt):,.2f}"
        if override_amt is not None and float(override_amt) != float(amt):
            amt_str += f" (Override: **${float(override_amt):,.2f}**)"

        embed.add_field(name="Merchant", value=f"**{clean_m or merch or 'Unknown'}**\n*(Raw: `{merch}`)*", inline=True)
        embed.add_field(name="Amount", value=amt_str, inline=True)
        embed.add_field(name="Date", value=str(date)[:19], inline=True)
        embed.add_field(name="Category", value=str(cat or 'Uncategorized'), inline=True)
        embed.add_field(name="Account", value=str(acc or 'Unknown'), inline=True)
        embed.add_field(name="Status", value=f"{status} {'🔒 Locked' if locked else '🔓 Unlocked'}", inline=True)

        if items:
            item_lines = [
                f"• #{it[0]} {it[1]} — {it[2]} × ${it[3]:,.2f} = **${it[4]:,.2f}**"
                for it in items
            ]
            embed.add_field(name=f"Itemized Receipt Items ({len(items)})", value="\n".join(item_lines)[:1024], inline=False)

        if context:
            embed.add_field(name="Notes & Context", value=str(context)[:1024], inline=False)

        embed.set_footer(text=f"Plaid ID: {plaid_id or 'Manual/Synthetic'}")
        await ctx.send(embed=embed)
    except Exception as exc:
        await _send_error_embed(ctx, " Transaction View Failed", exc, user_id=user_id)



# ============================================================
# Interactive Transaction Triage Center (Buttons, Select, Modal)
# ============================================================

class _TriageNoteModal(discord.ui.Modal, title="Add Context & Tag"):
    tag = discord.ui.TextInput(
        label="Behavioral Tag",
        placeholder="e.g. impulse, necessity, dining, work, travel",
        required=True,
        max_length=50
    )
    note = discord.ui.TextInput(
        label="Context Note",
        style=discord.TextStyle.paragraph,
        placeholder="Optional details or context for why this purchase happened",
        required=False,
        max_length=500
    )

    def __init__(self, triage_view: "_TransactionTriageView", tx_row_id: int):
        super().__init__()
        self.triage_view = triage_view
        self.tx_row_id = tx_row_id

    async def on_submit(self, interaction: discord.Interaction):
        from src.db.queries import tag_transaction_context
        user_id = str(interaction.user.id)
        tag_transaction_context(self.tx_row_id, self.tag.value.strip(), self.note.value.strip(), user_id=user_id)
        await interaction.response.send_message(f" Added tag `{self.tag.value.strip()}` to Tx #{self.tx_row_id}", ephemeral=True)
        await self.triage_view._update_message()


class _TransactionCategorySelect(discord.ui.Select):
    CATEGORIES = [
        "Dining Out",
        "Groceries",
        "Shopping",
        "Entertainment",
        "Utilities",
        "Transportation / Gas",
        "Healthcare / Medical",
        "Income / Paycheck",
        "Credit Card Bill Payment",
        "P2P Transfer",
        "Travel / Vacation",
        "Subscription",
        "Business / Tax Deductible",
    ]

    def __init__(self, triage_view: "_TransactionTriageView", current_category: str):
        options = [
            discord.SelectOption(
                label=cat,
                value=cat,
                default=(cat.lower() == (current_category or "").lower())
            )
            for cat in self.CATEGORIES
        ]
        super().__init__(placeholder="Choose / change category...", min_values=1, max_values=1, options=options, row=0)
        self.triage_view = triage_view

    async def callback(self, interaction: discord.Interaction):
        if not await self.triage_view._guard(interaction):
            return
        selected_category = self.values[0]
        user_id = str(interaction.user.id)
        tx = self.triage_view.current_tx
        from src.db.queries import correct_transaction
        correct_transaction(
            transaction_row_id=tx["id"],
            category=selected_category,
            reason="triage dropdown",
            user_id=user_id
        )
        tx["category"] = selected_category
        await interaction.response.send_message(f" Recategorized #{tx['id']} as **{selected_category}**", ephemeral=True)
        await self.triage_view._update_message()


class _TransactionTriageView(discord.ui.View):
    def __init__(self, ctx: commands.Context, transactions: list[dict]):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.transactions = transactions
        self.index = 0
        self._message: discord.Message | None = None
        self._rebuild_components()

    @property
    def current_tx(self) -> dict | None:
        if 0 <= self.index < len(self.transactions):
            return self.transactions[self.index]
        return None

    def _rebuild_components(self):
        self.clear_items()
        tx = self.current_tx
        if not tx:
            return
        self.add_item(_TransactionCategorySelect(self, tx.get("category", "")))

        approve_btn = discord.ui.Button(label="Approve & Lock", style=discord.ButtonStyle.success, emoji="✅", row=1)
        approve_btn.callback = self.approve_callback
        self.add_item(approve_btn)

        flag_btn = discord.ui.Button(label="Flag", style=discord.ButtonStyle.danger, emoji="🚩", row=1)
        flag_btn.callback = self.flag_callback
        self.add_item(flag_btn)

        note_btn = discord.ui.Button(label="Tag / Note", style=discord.ButtonStyle.primary, emoji="📝", row=1)
        note_btn.callback = self.note_callback
        self.add_item(note_btn)

        skip_btn = discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", row=1)
        skip_btn.callback = self.skip_callback
        self.add_item(skip_btn)

        close_btn = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, emoji="❌", row=1)
        close_btn.callback = self.close_callback
        self.add_item(close_btn)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(" Only the command author can triage these transactions.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        tx = self.current_tx
        if not tx:
            return discord.Embed(title="🎉 Triage Queue Complete!", description="All pending transactions have been triaged.", color=0x2ECC71)

        embed = discord.Embed(
            title=f"📋 Transaction Triage Queue ({self.index + 1} of {len(self.transactions)})",
            color=0x3498DB
        )
        embed.add_field(name="Merchant", value=f"**{tx.get('clean_merchant') or tx.get('merchant')}**\n`Raw: {tx.get('merchant')}`", inline=True)
        embed.add_field(name="Amount", value=f"**${tx.get('amount', 0.0):,.2f}**", inline=True)
        embed.add_field(name="Date", value=f"{tx.get('date')}", inline=True)
        embed.add_field(name="Account", value=f"{tx.get('account_used') or 'N/A'}", inline=True)
        embed.add_field(name="Category", value=f"`{tx.get('category') or 'Uncategorized'}`", inline=True)
        status_str = f"Status: `{tx.get('status')}` | Judgment: `{tx.get('judgment') or 'None'}` | Locked: `{'Yes' if tx.get('is_locked') else 'No'}`"
        embed.add_field(name="Audit Status", value=status_str, inline=True)

        from src.db.queries import get_transaction_items
        items = get_transaction_items(tx["id"], user_id=str(self.ctx.author.id))
        if items:
            item_lines = [f"- {it['item_name']} ({it['quantity']}x) — ${it['total_price']:,.2f}" for it in items[:4]]
            embed.add_field(name=f"🧾 Itemized Receipt ({len(items)} items)", value="\n".join(item_lines), inline=False)

        embed.set_footer(text="Select category from dropdown • Use buttons to lock or tag")
        return embed

    async def _update_message(self):
        self._rebuild_components()
        if self._message:
            await self._message.edit(embed=self.build_embed(), view=self)

    async def approve_callback(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        tx = self.current_tx
        user_id = str(interaction.user.id)
        from src.db.queries import lock_transaction, correct_transaction
        correct_transaction(transaction_row_id=tx["id"], status="Evaluated", reason="triage approved", user_id=user_id)
        lock_transaction(transaction_row_id=tx["id"], locked=True, user_id=user_id)
        self.index += 1
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def flag_callback(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        tx = self.current_tx
        user_id = str(interaction.user.id)
        from src.db.queries import correct_transaction
        correct_transaction(transaction_row_id=tx["id"], status="Flagged", judgment="flagged", reason="triage flagged", user_id=user_id)
        self.index += 1
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def note_callback(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        tx = self.current_tx
        modal = _TriageNoteModal(self, tx["id"])
        await interaction.response.send_modal(modal)

    async def skip_callback(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.index += 1
        self._rebuild_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def close_callback(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.stop()
        if self._message:
            await self._message.delete()


@bot.command(name="triage")
async def triage_command(ctx: commands.Context, limit: int = 50):
    """Interactive transaction review with buttons, dropdowns, and modals."""
    user_id = str(ctx.author.id)
    limit = max(1, min(limit, 200))
    try:
        with _open_verification_db(user_id=user_id) as vconn:
            cur = vconn.cursor()
            cur.execute(
                """SELECT id, date, merchant, clean_merchant, amount, account_used, category, status, judgment, is_locked
                FROM transactions
                WHERE user_id = ? AND is_locked = 0 AND merchant NOT LIKE '%System Balance Sync%'
                ORDER BY datetime(date) DESC, id DESC
                LIMIT ?""",
                (user_id, limit)
            )
            rows = cur.fetchall()

        if not rows:
            await ctx.send("🎉 All transactions are triaged and locked! No unlocked transactions found.")
            return

        tx_list = [
            {
                "id": r[0], "date": r[1], "merchant": r[2], "clean_merchant": r[3],
                "amount": float(r[4] or 0.0), "account_used": r[5], "category": r[6],
                "status": r[7], "judgment": r[8], "is_locked": r[9]
            }
            for r in rows
        ]
        view = _TransactionTriageView(ctx, tx_list)
        msg = await ctx.send(embed=view.build_embed(), view=view)
        view._message = msg
    except Exception as exc:
        await _send_error_embed(ctx, "Triage Start Failed", exc, user_id=user_id)


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
# Debt Snowball & Avalanche Payoff Engine
# ============================================================
@bot.group(name="debt", invoke_without_command=True)
async def debt_group(ctx: commands.Context):
    """Manage liabilities and calculate Snowball / Avalanche payoff strategies."""
    if ctx.invoked_subcommand is None:
        await ctx.send(
            "**Delilah Debt Payoff Engine**\n"
            "`!debt list` — View all tracked debts, APRs, and minimum payments.\n"
            "`!debt sync` — Auto-sync credit card & loan balances from Plaid.\n"
            "`!debt add <name> <balance> [apr] [min_payment]` — Track a manual debt.\n"
            "`!debt set <id> <apr|min|balance|name> <value>` — Update a debt's details.\n"
            "`!debt del <id>` — Delete a tracked debt.\n"
            "`!debt payoff [avalanche|snowball] [extra_monthly]` — Simulate debt payoff schedule."
        )


@debt_group.command(name="list")
async def debt_list_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import list_user_debts
        debts = list_user_debts(user_id=user_id, auto_sync=True)
        if not debts:
            await ctx.send(" No tracked debts found. Use `!debt sync` or `!debt add`.")
            return

        total_bal = sum(d["balance"] for d in debts)
        total_min = sum(d["min_payment"] for d in debts)

        embed = discord.Embed(
            title="💳 Tracked Liabilities & Debts",
            color=0xE74C3C,
            description=f"**Total Debt:** ${total_bal:,.2f} | **Total Min Payments:** ${total_min:,.2f}/mo"
        )
        for d in debts:
            embed.add_field(
                name=f"#{d['id']} {d['name']}",
                value=f"Balance: **${d['balance']:,.2f}**\nAPR: **{d['apr']:.2f}%** | Min: **${d['min_payment']:,.2f}/mo**",
                inline=False
            )
        embed.set_footer(text="Run `!debt payoff avalanche 200` to simulate accelerating payoff by $200/mo")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f" Error listing debts: `{type(exc).__name__}: {exc}`")


@debt_group.command(name="sync")
async def debt_sync_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import sync_debts_from_plaid
        synced = sync_debts_from_plaid(user_id=user_id)
        await ctx.send(f" Synced {synced} credit/loan accounts from Plaid into debt tracker.")
    except Exception as exc:
        await ctx.send(f" Sync failed: `{type(exc).__name__}: {exc}`")


@debt_group.command(name="add")
async def debt_add_cmd(ctx: commands.Context, name: str, balance: float, apr: float = 0.0, min_payment: float = 0.0):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import add_or_update_debt
        res = add_or_update_debt(user_id=user_id, name=name, balance=balance, apr=apr, min_payment=min_payment)
        await ctx.send(f" Added debt #{res['id']} **{name}**: ${balance:,.2f} at {apr:.2f}% APR (Min: ${res['min_payment']:,.2f}/mo).")
    except Exception as exc:
        await ctx.send(f" Failed to add debt: `{type(exc).__name__}: {exc}`")


@debt_group.command(name="set")
async def debt_set_cmd(ctx: commands.Context, debt_id: int, field: str, value: str):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import list_user_debts, add_or_update_debt
        debts = list_user_debts(user_id=user_id, auto_sync=False)
        target = next((d for d in debts if d["id"] == debt_id), None)
        if not target:
            await ctx.send(f" Debt #{debt_id} not found.")
            return

        field = field.lower().strip()
        if field in ("apr", "rate"):
            target["apr"] = float(value.replace("%", ""))
        elif field in ("min", "min_payment", "payment"):
            target["min_payment"] = float(value.replace("$", ""))
        elif field in ("balance", "bal"):
            target["balance"] = float(value.replace("$", ""))
        elif field in ("name", "title"):
            target["name"] = value.strip()
        else:
            await ctx.send(" Field must be `apr`, `min`, `balance`, or `name`.")
            return

        add_or_update_debt(
            user_id=user_id,
            name=target["name"],
            balance=target["balance"],
            apr=target["apr"],
            min_payment=target["min_payment"],
            debt_id=debt_id
        )
        await ctx.send(f" Updated debt #{debt_id} **{target['name']}**: {field} is now {value}.")
    except Exception as exc:
        await ctx.send(f" Failed to update debt: `{type(exc).__name__}: {exc}`")


@debt_group.command(name="del")
async def debt_del_cmd(ctx: commands.Context, debt_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import delete_user_debt
        if delete_user_debt(debt_id, user_id=user_id):
            await ctx.send(f" Deleted debt #{debt_id}.")
        else:
            await ctx.send(f" Debt #{debt_id} not found.")
    except Exception as exc:
        await ctx.send(f" Failed to delete debt: `{type(exc).__name__}: {exc}`")


@debt_group.command(name="payoff")
async def debt_payoff_cmd(ctx: commands.Context, strategy: str = "avalanche", extra_monthly: float = 0.0):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import calculate_debt_payoff
        strat = strategy.lower().strip()
        if strat not in ("avalanche", "snowball"):
            try:
                extra_monthly = float(strat)
                strat = "avalanche"
            except ValueError:
                strat = "avalanche"

        res = calculate_debt_payoff(strategy=strat, extra_monthly=extra_monthly, user_id=user_id)
        if res.get("status") == "empty":
            await ctx.send(" No active debts found. Use `!debt add` or `!debt sync`.")
            return

        embed = discord.Embed(
            title=f"🎯 Debt Payoff Plan — {strat.upper()}",
            color=0x2ECC71,
            description=(
                f"**Initial Balance:** ${res['total_initial_balance']:,.2f}\n"
                f"**Extra Monthly Allocation:** ${res['extra_monthly']:,.2f}\n"
                f"**Freedom Date:** **{res['debt_free_date']}** ({res['months_to_payoff']} months)\n"
                f"**Total Interest Paid:** ${res['total_interest_paid']:,.2f}\n"
                f"**Total Amount Paid:** ${res['total_paid']:,.2f}"
            )
        )

        milestones_text = []
        for idx, m in enumerate(res["milestones"], 1):
            milestones_text.append(f"**{idx}. {m['name']}**: Month {m['month']} ({m['date']}) → +${m['freed_payment']:,.2f}/mo freed")
        if milestones_text:
            embed.add_field(name="🏁 Payoff Milestones", value="\n".join(milestones_text[:8]), inline=False)

        comp = res.get("comparison", {})
        saved_interest = comp.get("interest_saved_with_avalanche", 0.0)
        saved_months = comp.get("months_saved_with_avalanche", 0)

        comp_text = (
            f"**{comp['alt_strategy'].capitalize()}**: {comp['alt_months']} months, ${comp['alt_interest']:,.2f} interest.\n"
        )
        if strat == "avalanche":
            comp_text += f"💡 Avalanche saves **${saved_interest:,.2f}** in interest and **{saved_months} months** vs Snowball."
        else:
            comp_text += f"💡 Snowball prioritizes early wins. Avalanche would save **${-saved_interest:,.2f}** in interest."
        embed.add_field(name="⚖️ Strategy Comparison", value=comp_text, inline=False)

        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f" Simulation failed: `{type(exc).__name__}: {exc}`")


# ============================================================
# Tax Category Tagging & Deduction Exporter
# ============================================================

@bot.group(name="tax", invoke_without_command=True)
async def tax_group(ctx: commands.Context):
    """Manage tax deductions and Schedule C expense reporting."""
    if ctx.invoked_subcommand is None:
        await ctx.send(
            "**Delilah Tax Deduction Tracker**\n"
            "`!tax summary [year]` — View itemized deductions and categories for the year.\n"
            "`!tax tag <tx_id> <category>` — Mark a transaction as tax-deductible (e.g., Software, Supplies, Meals 50%).\n"
            "`!tax untag <tx_id>` — Remove tax-deductible status from a transaction.\n"
            "`!tax export [year]` — Export all deductions to a clean CSV file."
        )


@tax_group.command(name="summary")
async def tax_summary_cmd(ctx: commands.Context, year: int = None):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import get_tax_deductions_summary
        res = get_tax_deductions_summary(year=year, user_id=user_id)
        if not res["categories"]:
            await ctx.send(f" No tax deductions recorded for {res['year']}. Use `!tax tag <tx_id> <category>`.")
            return

        embed = discord.Embed(
            title=f"📊 Tax Deductions Summary — {res['year']}",
            color=0xF1C40F,
            description=(
                f"**Total Deductible:** **${res['total_deductible']:,.2f}** "
                f"(Gross Spent: ${res['total_gross_spent']:,.2f})\n"
                f"**Total Transactions:** {res['total_transactions']}"
            )
        )
        for cat in res["categories"][:10]:
            embed.add_field(
                name=f"{cat['category']} ({cat['count']} items)",
                value=f"Deductible: **${cat['deductible']:,.2f}** (Gross: ${cat['gross']:,.2f})",
                inline=True
            )
        embed.set_footer(text="Run `!tax export` to generate a full CSV for your accountant")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f" Tax summary failed: `{type(exc).__name__}: {exc}`")


@tax_group.command(name="tag")
async def tax_tag_cmd(ctx: commands.Context, tx_id: int, *, category: str = "Business"):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import tag_transaction_tax
        res = tag_transaction_tax(transaction_row_id=tx_id, is_deductible=True, tax_category=category, user_id=user_id)
        if res.get("status") == "not_found":
            await ctx.send(f" Transaction #{tx_id} not found.")
            return
        await ctx.send(f" Marked Tx #{tx_id} (**{res['merchant']}** — ${res['amount']:,.2f}) as tax-deductible under `{category}`.")
    except Exception as exc:
        await ctx.send(f" Failed to tag tax deduction: `{type(exc).__name__}: {exc}`")


@tax_group.command(name="untag")
async def tax_untag_cmd(ctx: commands.Context, tx_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import tag_transaction_tax
        res = tag_transaction_tax(transaction_row_id=tx_id, is_deductible=False, user_id=user_id)
        if res.get("status") == "not_found":
            await ctx.send(f" Transaction #{tx_id} not found.")
            return
        await ctx.send(f" Removed tax-deductible status from Tx #{tx_id}.")
    except Exception as exc:
        await ctx.send(f" Failed to untag transaction: `{type(exc).__name__}: {exc}`")


@tax_group.command(name="export")
async def tax_export_cmd(ctx: commands.Context, year: int = None):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import export_tax_csv
        target_year = year or datetime.now().year
        csv_data = export_tax_csv(year=target_year, user_id=user_id)
        if not csv_data.strip():
            await ctx.send(f" No deductions to export for {target_year}.")
            return
        
        file_obj = discord.File(
            fp=BytesIO(csv_data.encode("utf-8")),
            filename=f"tax_deductions_{target_year}.csv"
        )
        await ctx.send(
            content=f"📁 Here is your tax deductions export for **{target_year}**:",
            file=file_obj
        )
    except Exception as exc:
        await ctx.send(f" Tax export failed: `{type(exc).__name__}: {exc}`")


# ============================================================
# "What-If?" Scenario & Cash Flow Runway Simulator
# ============================================================

@bot.command(name="whatif", aliases=["scenario"])
async def whatif_cmd(ctx: commands.Context, amount: float, days: int = 60, *, name: str = "Hypothetical Event"):
    """Simulate the cash flow impact and runway change of a hypothetical purchase or income."""
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import simulate_cash_flow_scenario
        events = [{
            "name": name,
            "amount": amount,
            "offset_days": 1,
            "recurring_monthly": False
        }]
        res = simulate_cash_flow_scenario(days=days, scenario_events=events, user_id=user_id)

        scen = res["scenario"]
        base = res["baseline"]
        delta = res["delta"]

        is_expense = amount < 0
        color = 0xE74C3C if scen["min_balance"] < 0 else (0xF39C12 if is_expense else 0x2ECC71)

        embed = discord.Embed(
            title=f"🔮 'What-If?' Runway Simulation ({days} Days)",
            color=color,
            description=(
                f"**Hypothetical Event:** `{name}` (**{'+' if amount >= 0 else ''}${amount:,.2f}**)\n"
                f"**Current Liquid Funds:** ${res['starting_liquid']:,.2f}\n"
                f"**Estimated Daily Burn:** ${res['daily_burn_rate']:,.2f}/day"
            )
        )

        min_status = f"${scen['min_balance']:,.2f} on {scen['min_date']}"
        if scen["min_balance"] < 0:
            min_status += f" ⚠️ **(Depleted on Day {scen['runway_days']})**"
        embed.add_field(name="Projected Lowest Balance", value=min_status, inline=False)

        embed.add_field(
            name="Baseline (Without Event)",
            value=f"Min: ${base['min_balance']:,.2f}\nFinal: ${base['final_balance']:,.2f}",
            inline=True
        )
        embed.add_field(
            name="Scenario (With Event)",
            value=f"Min: ${scen['min_balance']:,.2f}\nFinal: ${scen['final_balance']:,.2f}",
            inline=True
        )
        embed.add_field(
            name="Net Impact",
            value=f"Min Diff: **${delta['min_balance_impact']:,.2f}**\nFinal Diff: **${delta['final_balance_impact']:,.2f}**",
            inline=True
        )

        if scen["runway_days"] is not None:
            embed.add_field(
                name="🚨 Runway Alert",
                value=f"This event would cause liquid reserves to hit **$0.00** in **{scen['runway_days']} days**.",
                inline=False
            )
        else:
            embed.add_field(
                name="✅ Runway Safety",
                value=f"Liquid cash remains positive throughout the {days}-day horizon.",
                inline=False
            )

        embed.set_footer(text="Run !upcoming to view scheduled inflows and planned expenses")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f" What-If simulation failed: `{type(exc).__name__}: {exc}`")


# ============================================================
# Financial Intelligence & Leakage Commands
# ============================================================

@bot.command(name="recurringleakage", aliases=["leakage", "phantomsubs"])
async def recurring_leakage_cmd(ctx: commands.Context, lookback_days: int = 180):
    """Audit recurring payments, identify subscription inflation, and calculate monthly burn rate."""
    user_id = str(ctx.author.id)
    try:
        from src.services.intelligence import analyze_recurring_leakage
        report = analyze_recurring_leakage(user_id, lookback_days=lookback_days)
        if len(report) > 1900:
            parts = [report[i:i+1900] for i in range(0, len(report), 1900)]
            for part in parts:
                await ctx.send(f"```text\n{part}\n```")
        else:
            await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Recurring leakage audit failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="lifestylecreep", aliases=["creep"])
async def lifestyle_creep_cmd(ctx: commands.Context, days_back: int = 180):
    """Detect statistically significant upward trends in weekly discretionary spending."""
    user_id = str(ctx.author.id)
    try:
        from src.services.intelligence import calculate_lifestyle_creep
        report = calculate_lifestyle_creep(user_id, days_back=days_back)
        await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Lifestyle creep analysis failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="nextdollar")
async def next_dollar_cmd(ctx: commands.Context, amount: float):
    """Mathematically determine the highest-ROI allocation for an incoming windfall."""
    user_id = str(ctx.author.id)
    try:
        from src.services.intelligence import allocate_next_best_dollar
        report = allocate_next_best_dollar(user_id, windfall_amount=amount)
        await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Next best dollar allocation failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="bestcard", aliases=["card", "whichcard"])
async def best_card_cmd(ctx: commands.Context, *, query: str):
    """Recommend the optimal credit card from your wallet or market for a merchant or purchase category."""
    user_id = str(ctx.author.id)
    try:
        from src.services.rewards import recommend_best_card
        res = recommend_best_card(user_id=user_id, category=query, merchant=query)
        card = res.get("recommended_card") or "Default Card"
        mult = res.get("multiplier", 1.0)
        cat = res.get("category", query)
        count = res.get("wallet_cards_count", 0)

        embed = discord.Embed(
            title="💳 Optimal Card Recommendation",
            color=0x2ECC71
        )
        embed.add_field(name="Target Purchase", value=f"`{query}` (Category: **{cat.title()}**)", inline=False)
        embed.add_field(name="Recommended Card", value=f"**{card}**", inline=True)
        embed.add_field(name="Reward Multiplier", value=f"**{mult:.1f}% Cash Back / Points**", inline=True)
        if count > 0:
            embed.set_footer(text=f"Matched against {count} cards in your personal wallet")
        else:
            embed.set_footer(text="Market benchmark profile (No credit cards linked yet)")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"❌ Best card lookup failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="rewardsaudit", aliases=["walletaudit"])
async def rewards_audit_cmd(ctx: commands.Context, days_back: int = 90):
    """Quantify earned rewards vs optimal wallet swipes and uncover missed cash back."""
    user_id = str(ctx.author.id)
    try:
        from src.services.rewards import audit_wallet_rewards
        report = audit_wallet_rewards(user_id=user_id, days_back=days_back)
        if len(report) > 1900:
            parts = [report[i:i+1900] for i in range(0, len(report), 1900)]
            for part in parts:
                await ctx.send(f"```text\n{part}\n```")
        else:
            await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Rewards audit failed: `{type(exc).__name__}: {exc}`")


# ============================================================
# Zero-Based Budgeting & Runway Commands
# ============================================================

@bot.command(name="safetospend", aliases=["safe", "freedollar"])
async def safe_to_spend_cmd(ctx: commands.Context):
    """Calculate exact safe-to-spend free cash after locking funds for credit float, sinking funds, and upcoming bills."""
    user_id = str(ctx.author.id)
    try:
        from src.services.budgeting import get_safe_to_spend_metrics
        report = get_safe_to_spend_metrics(user_id)
        await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Safe-to-Spend calculation failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="paydays", aliases=["paycheck", "incomeschedule"])
async def paydays_cmd(ctx: commands.Context):
    """Predict upcoming payday dates, income cadence, and average paycheck amounts."""
    user_id = str(ctx.author.id)
    try:
        from src.services.budgeting import predict_next_paydays
        report = predict_next_paydays(user_id)
        await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Payday prediction failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="float", aliases=["creditfloat", "velocity"])
async def float_velocity_cmd(ctx: commands.Context):
    """Analyze reliance on credit card float and calculate debt velocity."""
    user_id = str(ctx.author.id)
    try:
        from src.services.budgeting import calculate_credit_float_velocity
        report = calculate_credit_float_velocity(user_id)
        await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Float velocity analysis failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="emergencyfund", aliases=["efund", "runway"])
async def emergency_fund_cmd(ctx: commands.Context):
    """Audit emergency fund reserves, bare-bones survival burn rate, and runway in months."""
    user_id = str(ctx.author.id)
    try:
        from src.services.budgeting import calculate_emergency_fund_health
        report = calculate_emergency_fund_health(user_id)
        await ctx.send(f"```text\n{report}\n```")
    except Exception as exc:
        await ctx.send(f"❌ Emergency fund health check failed: `{type(exc).__name__}: {exc}`")


# ============================================================
# Investment Portfolio & Rebalancing Commands
# ============================================================

@bot.command(name="portfolio", aliases=["holdings", "investments"])
async def portfolio_cmd(ctx: commands.Context):
    """View investment holdings, asset class breakdown, unrealized P&L, and target allocation."""
    user_id = str(ctx.author.id)
    try:
        from src.services.portfolio import format_portfolio_report
        report = format_portfolio_report(user_id=user_id)
        await ctx.send(report)
    except Exception as exc:
        await ctx.send(f"❌ Portfolio query failed: `{type(exc).__name__}: {exc}`")


@bot.command(name="setholding")
async def set_holding_cmd(ctx: commands.Context, symbol: str, shares: float, price: float = 0.0, cost_basis: float = 0.0, asset_class: str = "Equities"):
    """Set or update an investment holding (shares=0 removes it)."""
    user_id = str(ctx.author.id)
    try:
        from src.services.portfolio import set_holding
        res = set_holding(
            user_id=user_id,
            symbol=symbol,
            shares=shares,
            cost_basis=cost_basis,
            current_price=price,
            asset_class=asset_class
        )
        if res.get("action") == "deleted":
            await ctx.send(f"🗑️ Removed holding `{symbol.upper()}`.")
        else:
            await ctx.send(f"✅ Saved holding `{res['symbol']}`: {res['shares']} shares @ ${res['current_price']:.2f} ({res['asset_class']}).")
    except Exception as exc:
        await ctx.send(f"❌ Failed to set holding: `{type(exc).__name__}: {exc}`")


@bot.command(name="rebalance", aliases=["drift"])
async def rebalance_cmd(ctx: commands.Context):
    """Calculate asset allocation drift against targets and display required rebalancing trades."""
    user_id = str(ctx.author.id)
    try:
        from src.services.portfolio import get_portfolio_summary, format_portfolio_report
        data = get_portfolio_summary(user_id=user_id)
        if not data.get("targets"):
            await ctx.send("⚠️ No target allocation set. Use `!portfolio` to view holdings.")
            return
        report = format_portfolio_report(user_id=user_id)
        await ctx.send(report)
    except Exception as exc:
        await ctx.send(f"❌ Rebalancing check failed: `{type(exc).__name__}: {exc}`")






# ============================================================
# Free-Trial & Zombie Subscription Watchdog
# ============================================================

@bot.group(name="trials", aliases=["trial"], invoke_without_command=True)
async def trials_group(ctx: commands.Context):
    """Manage free trials and detect zombie subscriptions."""
    if ctx.invoked_subcommand is None:
        await ctx.send(
            "**Free-Trial & Zombie Subscription Watchdog**\n"
            "`!trials list` — View active trials and countdowns to billing.\n"
            "`!trials add <service> <end_date> [cost] [cancel_url]` — Track a free trial (end_date: YYYY-MM-DD).\n"
            "`!trials cancel <trial_id>` — Mark a trial as cancelled.\n"
            "`!trials zombies` — Identify idle/zombie recurring subscriptions."
        )


@trials_group.command(name="list")
async def trials_list_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import list_subscription_trials
        trials = list_subscription_trials(user_id=user_id, active_only=True)
        if not trials:
            await ctx.send(" No active free trials tracked. Use `!trials add` to track one.")
            return

        embed = discord.Embed(
            title="⏳ Active Free Trials & Renewal Watch",
            color=0xE67E22,
            description=f"Tracking **{len(trials)}** active trial(s)."
        )
        for t in trials:
            days_str = f"**{t['days_remaining']} days left**" if t['days_remaining'] is not None else "Date reached"
            cost_str = f"${t['projected_cost']:,.2f}/{t['billing_cycle']}" if t['projected_cost'] > 0 else "Free / Unset"
            val = f"Expires: **{t['trial_end_date']}** ({days_str})\nProjected: {cost_str}"
            if t['cancellation_url']:
                val += f"\n[Cancellation Link]({t['cancellation_url']})"
            embed.add_field(name=f"#{t['id']} {t['service_name']}", value=val, inline=False)

        embed.set_footer(text="The financial monitor will fire an alert before renewal")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f" Failed to list trials: `{type(exc).__name__}: {exc}`")


@trials_group.command(name="add")
async def trials_add_cmd(ctx: commands.Context, service_name: str, end_date: str, cost: float = 0.0, cancel_url: str = None):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import add_subscription_trial
        res = add_subscription_trial(
            user_id=user_id,
            service_name=service_name,
            trial_end_date=end_date,
            projected_cost=cost,
            cancellation_url=cancel_url
        )
        await ctx.send(f" Tracked trial #{res['id']} for **{service_name}** expiring on `{end_date}` (${cost:,.2f}).")
    except Exception as exc:
        await ctx.send(f" Failed to add trial: `{type(exc).__name__}: {exc}`")


@trials_group.command(name="cancel")
async def trials_cancel_cmd(ctx: commands.Context, trial_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import update_trial_status
        if update_trial_status(trial_id=trial_id, status="cancelled", user_id=user_id):
            await ctx.send(f" Marked trial #{trial_id} as cancelled.")
        else:
            await ctx.send(f" Trial #{trial_id} not found.")
    except Exception as exc:
        await ctx.send(f" Failed to cancel trial: `{type(exc).__name__}: {exc}`")


@trials_group.command(name="zombies")
async def trials_zombies_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import detect_zombie_subscriptions
        zombies = detect_zombie_subscriptions(user_id=user_id)
        if not zombies:
            await ctx.send(" No recurring subscriptions found.")
            return

        total_annual = sum(z["annual_cost"] for z in zombies)
        embed = discord.Embed(
            title="🧟 Subscription Watchdog & Zombie Review",
            color=0x9B59B6,
            description=f"Auditing **{len(zombies)}** recurring subscriptions (Total: **${total_annual:,.2f}/yr**)."
        )
        for z in zombies[:8]:
            embed.add_field(
                name=f"{z['merchant']} (${z['monthly_amount']:,.2f}/mo)",
                value=f"Annual cost: **${z['annual_cost']:,.2f}** | Last: {z['last_billed']}\n💡 *{z['recommendation']}*",
                inline=False
            )
        embed.set_footer(text="Ask Delilah to draft a cancellation email for any service")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"⚠️ Zombie scan failed: `{type(exc).__name__}: {exc}`")


# ============================================================
# Virtual Round-Up & Sinking Fund Envelopes
# ============================================================
@bot.group(name="sinking", aliases=["roundup", "bucket"], invoke_without_command=True)
async def sinking_group(ctx: commands.Context):
    """Manage virtual sinking fund envelopes and automated round-up savings."""
    if ctx.invoked_subcommand is None:
        await ctx.send(
            "💰 **Virtual Round-Up & Sinking Funds Envelopes**\n"
            "`!sinking list` — View all sinking funds, funding velocity, and completion dates.\n"
            "`!sinking config <on|off> <target_bucket> [multiplier=1.0] [threshold=0.0]` — Configure round-up auto-save.\n"
            "`!sinking sweep [days=30]` — Sweep spare change round-ups into the designated fund.\n"
            "`!sinking target <name> <target_amount>` — Set or adjust the target for a fund.\n"
            "`!sinking deposit <name> <amount>` — Add manual savings to an envelope."
        )


@sinking_group.command(name="list", aliases=["overview"])
async def sinking_list_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import get_sinking_funds_overview
        overview = get_sinking_funds_overview(user_id=user_id)
        if overview.get("status") == "empty":
            await ctx.send("ℹ️ No sinking funds found. Use `!sinking target <name> <target_amount>` to create one.")
            return

        target_b_name = overview['roundup_settings'].get('target_bucket_name', 'Emergency Fund')
        embed = discord.Embed(
            title="🎯 Sinking Funds & Savings Envelopes",
            color=0x2ECC71,
            description=(
                f"**Total Saved:** ${overview['total_saved']:,.2f} / ${overview['total_target']:,.2f} "
                f"(`{overview['overall_progress_pct']}%`)\n"
                f"**Round-Up Engine:** `{'Active (' + target_b_name + ')' if overview['roundup_settings']['enabled'] else 'Disabled'}`"
            )
        )
        for b in overview["buckets"]:
            proj = b["projected_completion_date"] or "Indeterminate (need more deposits)"
            vel = f"${b['monthly_velocity']:,.2f}/mo" if b['monthly_velocity'] > 0 else "No recent flow"
            embed.add_field(
                name=f"{b['milestone']} {b['name']}",
                value=(
                    f"Balance: **${b['current_amount']:,.2f}** / ${b['target_amount']:,.2f} (`{b['progress_pct']}%`)\n"
                    f"Remaining: **${b['remaining_amount']:,.2f}** | Velocity: {vel}\n"
                    f"Target Finish: `{proj}`"
                ),
                inline=False
            )
        embed.set_footer(text="Use !sinking sweep to auto-transfer spare change from recent transactions")
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"⚠️ Sinking funds query failed: `{type(exc).__name__}: {exc}`")


@sinking_group.command(name="config")
async def sinking_config_cmd(ctx: commands.Context, enabled: str, target_bucket: str, multiplier: float = 1.0, threshold: float = 0.0):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import set_roundup_settings
        is_on = enabled.lower() in ("true", "1", "on", "yes", "enable", "active")
        res = set_roundup_settings(
            user_id=user_id,
            enabled=is_on,
            target_bucket_name=target_bucket,
            multiplier=multiplier,
            whole_dollar_roundup=1.0
        )
        await ctx.send(
            f"✅ Round-up settings saved!\n"
            f"- Status: `{'Enabled' if res['enabled'] else 'Disabled'}`\n"
            f"- Target Sinking Fund: **{res['target_bucket_name']}**\n"
            f"- Multiplier: `{res['multiplier']}x`"
        )
    except Exception as exc:
        await ctx.send(f"⚠️ Failed to update round-up config: `{type(exc).__name__}: {exc}`")


@sinking_group.command(name="sweep")
async def sinking_sweep_cmd(ctx: commands.Context, days: int = 30):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import apply_transaction_roundups
        res = apply_transaction_roundups(user_id=user_id, days=days)
        if res.get("status") == "empty":
            await ctx.send("ℹ️ No eligible transactions found to round up.")
            return

        embed = discord.Embed(
            title="🧹 Spare-Change Round-Up Sweep Executed",
            color=0x1ABC9C,
            description=(
                f"Swept **${res['total_swept']:,.2f}** across **{res['roundups_count']}** transaction(s) "
                f"into **{res['target_bucket']}**!\n"
                f"New Bucket Balance: **${res['current_bucket_amount']:,.2f}** / ${res['target_amount']:,.2f} (`{res['progress_pct']}%`)"
            )
        )
        if res.get("transactions_swept"):
            sample = [f"- {t['merchant']} (${t['amount']:,.2f}) ➔ **+${t['roundup']:,.2f}**" for t in res["transactions_swept"][:5]]
            embed.add_field(name="Recent Sweeps", value="\n".join(sample), inline=False)

        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"⚠️ Sinking sweep failed: `{type(exc).__name__}: {exc}`")


@sinking_group.command(name="target")
async def sinking_target_cmd(ctx: commands.Context, name: str, target: float):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import adjust_savings_bucket
        msg = adjust_savings_bucket(name=name, target=target, user_id=user_id)
        await ctx.send(msg)
    except Exception as exc:
        await ctx.send(f"⚠️ Failed to adjust bucket target: `{type(exc).__name__}: {exc}`")


@sinking_group.command(name="deposit")
async def sinking_deposit_cmd(ctx: commands.Context, name: str, amount: float):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import adjust_savings_bucket
        from src.core.state import c, safe_commit
        msg = adjust_savings_bucket(name=name, delta=amount, user_id=user_id)
        c.execute(
            "INSERT INTO bucket_contributions (user_id, bucket_name, amount, source) VALUES (?, ?, ?, 'manual_deposit')",
            (user_id, name, amount)
        )
        safe_commit()
        await ctx.send(f"{msg}\n📥 Logged deposit of **${amount:,.2f}** to contribution history.")
    except Exception as exc:
        await ctx.send(f"⚠️ Failed to deposit into bucket: `{type(exc).__name__}: {exc}`")


# ============================================================
# LaTeX Financial Digest & Scorecard Command
# ============================================================
@bot.command(name="digest", aliases=["scorecard", "report"])
async def digest_command(ctx: commands.Context, period: str = "monthly"):
    """
    Generate an executive financial scorecard & downloadable LaTeX digest.
    Usage: !digest [weekly|monthly]
    """
    user_id = str(ctx.author.id)
    p_clean = period.lower().strip()
    if p_clean in ("weekly", "week", "7d", "7"):
        chosen_period = "weekly"
    else:
        chosen_period = "monthly"

    try:
        from src.services.report_generator import export_financial_digest
        res = export_financial_digest(
            period=chosen_period,
            user_id=user_id,
            user_name=ctx.author.display_name or "Delilah Client"
        )
        d = res["digest"]

        grade_colors = {
            "A+": 0x2ECC71, "A": 0x27AE60, "B": 0x3498DB,
            "C": 0xF39C12, "D": 0xE67E22, "F": 0xE74C3C
        }
        col = grade_colors.get(d["grade"], 0x3498DB)

        embed = discord.Embed(
            title=f"📊 {d['period_label']} Financial Health Scorecard",
            color=col,
            description=(
                f"**Overall Grade: `{d['grade']}` ({d['financial_health_score']}/100)**\n"
                f"Evaluation Period: **Last {d['days']} Days**\n\n"
                f"**Net Cash Flow:** `${d['net_cash_flow']:+,.2f}`\n"
                f"• Inflows: `${d['inflow']:,.2f}`\n"
                f"• Outflows: `${d['outflow']:,.2f}`\n"
                f"• Savings Rate: **{d['savings_rate_pct']:.1f}%**"
            )
        )

        if d.get("category_spending"):
            top_cats = d["category_spending"][:4]
            cat_str = "\n".join([f"• **{c['category']}**: ${c['amount']:,.2f} (`{c['percentage']:.0f}%`)" for c in top_cats])
            embed.add_field(name="🏷️ Top Spending Categories", value=cat_str, inline=False)

        debt = d.get("debt_summary", {})
        sf = d.get("sinking_funds_summary", {})
        embed.add_field(
            name="💳 Debt Position",
            value=f"Total: **${debt.get('total_debt', 0.0):,.2f}** ({debt.get('debt_count', 0)} accounts)",
            inline=True
        )
        embed.add_field(
            name="🎯 Sinking Funds",
            value=f"Saved: **${sf.get('total_saved', 0.0):,.2f}** / ${sf.get('total_target', 0.0):,.2f} (`{sf.get('progress_pct', 0.0)}%`)",
            inline=True
        )

        if d.get("top_transactions"):
            largest = d["top_transactions"][0]
            embed.add_field(
                name="💥 Largest Purchase",
                value=f"**{largest['merchant']}**: ${largest['amount']:,.2f} (`{largest['category']}`)",
                inline=False
            )

        embed.set_footer(text="Full LaTeX report attached below • Compile with pdflatex or import into Overleaf")

        files_to_send = []
        if res.get("pdf_path") and os.path.exists(res["pdf_path"]):
            files_to_send.append(discord.File(res["pdf_path"], filename=f"Delilah_Digest_{chosen_period}.pdf"))
        if res.get("tex_path") and os.path.exists(res["tex_path"]):
            files_to_send.append(discord.File(res["tex_path"], filename=f"Delilah_Digest_{chosen_period}.tex"))

        await ctx.send(embed=embed, files=files_to_send)
    except Exception as exc:
        await ctx.send(f"⚠️ Failed to generate financial digest: `{type(exc).__name__}: {exc}`")


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
        from src.services.llm import AUDIT_SESSION_STATE
        AUDIT_SESSION_STATE.pop(uid, None)
    except Exception:
        pass
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
async def on_command_error(ctx, error):
    """Surface command failures to the user instead of swallowing them.

    Without this, a command that raises during dispatch (bad embed, view
    error, Discord API rejection) fails silently — the user sees nothing
    and the logs show only a generic error. This makes the failure visible
    without leaking stack traces into the channel.
    """
    import traceback
    print(f" [COMMAND ERROR] {ctx.command!r} invoked by {ctx.author.id}: {type(error).__name__}: {error}", flush=True)
    traceback.print_exc()

    # Don't spam the channel on CancelledError or CommandNotFound.
    if isinstance(error, (commands.CancelledError, commands.CommandNotFound, commands.MissingPermissions, commands.BotMissingPermissions)):
        return

    try:
        await ctx.send(f" **{type(error).__name__}:** `{str(error)[:300]}`")
    except Exception:
        pass


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

    # Apply idempotent DB migrations (monitor_rules, monitor_alerts, ...).
    # Existing financial data is preserved: migrations only ADD tables/columns.
    try:
        from src.db.migrations import apply_all
        applied = apply_all(conn)
        if applied:
            print(f" [DB] Applied migrations: {', '.join(applied)}")
    except Exception as exc:
        print(f" [DB] Migration apply failed: {type(exc).__name__}: {exc}")

    # Persistent, LLM-free financial monitor.  Rules are declarative
    # predicates; when one fires an alert row is written and pushed.
    try:
        from src.services.monitor import monitor_watchdog_loop
        t_monitor = bot.loop.create_task(monitor_watchdog_loop())
        _PERSISTENT_TASKS.add(t_monitor)
        t_monitor.add_done_callback(_PERSISTENT_TASKS.discard)
        print(" [MONITOR] Persistent financial monitor started.")
    except Exception as exc:
        print(f" [MONITOR] Monitor watchdog failed to start: {type(exc).__name__}: {exc}")

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

    # ── Voice memos / audio attachments ──────────────────────────
    # Discord voice memos arrive as audio attachments. Transcribe them
    # before the advisor loop so the model treats them like typed text.
    audio_extensions = (".ogg", ".mp3", ".wav", ".m4a", ".webm", ".opus", ".aac", ".flac")
    audio_attachments = [
        att for att in message.attachments
        if (att.content_type or "").startswith("audio/")
        or att.filename.lower().endswith(audio_extensions)
    ]
    if audio_attachments:
        try:
            from src.services.stt import transcribe as stt_transcribe, STTError
            for att in audio_attachments:
                if att.size > 25 * 1024 * 1024:
                    print(f" [STT] skipping oversized audio {att.filename} ({att.size} bytes)")
                    continue
                audio_bytes = await att.read()
                try:
                    transcript = stt_transcribe(audio_bytes, filename=att.filename)
                except STTError as exc:
                    print(f" [STT] {att.filename} failed: {exc}")
                    try:
                        await message.channel.send(
                            f"  I couldn't transcribe `{att.filename}`: `{exc}`"
                        )
                    except Exception:
                        pass
                    continue
                if transcript:
                    prompt = (prompt + "\n" if prompt else "") + f"[voice memo] {transcript}"
                    print(
                        f" [STT] {att.filename} → {len(transcript)} chars: "
                        f"{transcript[:120]!r}",
                        flush=True,
                    )
        except Exception as exc:
            print(f" [STT] audio handling crashed: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()

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


__all__ = ['check_now', 'on_raw_reaction_add', '_send_transaction_browser', 'queue_worker', '_AdvisorReplyHandle', 'researchstatus_command', 'help_command', 'test_push_cmd', 'ntfy_setup_cmd', 'model_set', 'set_budget', 'model_get', 'BACKGROUND_TASKS_STARTED', 'on_message', '_ReportCloseView', '_ollama_installed_models', 'dbstatus_command', 'model_command', 'preload_advisor_model', 'locked_command', 'audit_transaction_command', 'clear_all', 'merchantstatus_command', 'wipe_memory_error', 'TRANSACTION_BROWSER_VIEWS', 'clear_chat', '_ReportPageView', 'recategorize_error', 'model_command_error', 'audit_categories', 'wipe_memory', 'check_now_error', 'audit_categories_error', 'clear_chat_error', 'corrections_command', 'integrity_command', '_send_error_embed', '_open_verification_db', '_ollama_base_url', '_ollama_warm_model', '_audit_identity_mismatches', 'model_list', '_close_all_control_messages', 'snapshot_command', '_ollama_unload_model', 'recategorize', 'clear_all_error', '_TransactionPageView', 'REPORT_CONTROL_VIEWS', 'main', '_db_identity_mismatches', 'on_ready', 'REPORT_CONTROL_MESSAGES', 'unlocked_command', '_send_thinking_placeholder', 'reclassify_all', '_send_command_report', 'reclassify_all_error', '_HelpPageView']




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



# ============================================================
# Persistent Financial Monitor
# ============================================================

@bot.group(name="monitor", invoke_without_command=True)
async def monitor_cmd(ctx: commands.Context):
    """Deterministic, LLM-free financial monitoring."""
    if ctx.invoked_subcommand is None:
        await ctx.send(
            "**Financial Monitor**\n"
            "`!monitor list` — Show active rules.\n"
            "`!monitor create <instruction>` — Create a rule from natural language.\n"
            "`!monitor add <kind> <name> <json-config>` — Add a rule manually.\n"
            "`!monitor run` — Evaluate rules now.\n"
            "`!monitor alerts [limit]` — Show recent alerts.\n"
            "`!monitor ack <id>` — Ack an alert.\n"
            "`!monitor off <id>` / `!monitor on <id>` — Disable/enable a rule.\n"
            "`!monitor del <id>` — Delete a rule.\n"
            "`!monitor clear` — Delete ALL rules and alerts.\n\n"
            "Rule kinds: projected_balance_low, category_spend_exceeded, "
            "income_overdue, subscription_price_changed, unusual_transaction, "
            "recurring_bill_missing, cash_flow_change, large_deposit."
        )


@monitor_cmd.command(name="clear")
async def monitor_clear_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.db.queries import conn
        c = conn.cursor()
        c.execute("DELETE FROM monitor_rules WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM monitor_alerts WHERE user_id = ?", (user_id,))
        conn.commit()
        await ctx.send("🧹 All monitor rules and alerts have been cleared.")
    except Exception as e:
        await ctx.send(f"❌ Error clearing monitors: {e}")

@monitor_cmd.command(name="list")
async def monitor_list_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.services.monitor import list_monitor_rules
        rules = list_monitor_rules(conn, user_id, include_disabled=True)
        if not rules:
            await ctx.send(" No monitor rules configured. Use `!monitor add`.")
            return
        lines = [f" **Monitor rules ({len(rules)})**"]
        for r in rules:
            state = "on" if r["enabled"] else "off"
            lines.append(
                f"- #{r['id']} [{state}] {r['name']} ({r['kind']}) "
                f"severity={r['severity']} cooldown={r['cooldown_hours']}h "
                f"last={r['last_fired_at'] or 'never'}"
            )
        await ctx.send("\n".join(lines))
    except Exception as exc:
        await ctx.send(f" Monitor list failed: `{type(exc).__name__}: {exc}`")


@monitor_cmd.command(name="create")
async def monitor_create_cmd(ctx: commands.Context, *, instruction: str):
    """Create a monitor rule from natural English (e.g. !monitor create alert if dining exceeds $200 this week)."""
    user_id = str(ctx.author.id)
    try:
        import json
        from src.services.monitor import compile_natural_language_rule, add_monitor_rule
        spec = compile_natural_language_rule(instruction)
        ok, msg, rid = add_monitor_rule(
            conn,
            user_id=user_id,
            name=spec["name"],
            kind=spec["kind"],
            config=spec["config"],
            severity=spec.get("severity", "warning"),
            cooldown_hours=spec.get("cooldown_hours", 24)
        )
        if ok:
            embed = discord.Embed(
                title="🛡️ Monitor Rule Created",
                color=0x2ECC71,
                description=f"Rule **#{rid}** successfully registered from natural language."
            )
            embed.add_field(name="Name", value=spec["name"], inline=True)
            embed.add_field(name="Kind", value=f"`{spec['kind']}`", inline=True)
            embed.add_field(name="Severity", value=spec["severity"].capitalize(), inline=True)
            embed.add_field(name="Validated Parameters", value=f"```json\n{json.dumps(spec['config'], indent=2)}\n```", inline=False)
            embed.set_footer(text="The monitor engine will continuously evaluate this rule against your ledger.")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Failed to register rule: {msg}")
    except Exception as exc:
        await ctx.send(f"⚠️ {exc}")


@monitor_cmd.command(name="add")
async def monitor_add_cmd(ctx: commands.Context, kind: str, name: str, config: str = "{}"):
    user_id = str(ctx.author.id)
    try:
        import json
        from src.services.monitor import add_monitor_rule
        parsed = json.loads(config or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("config must be a JSON object")
        ok, msg, rid = add_monitor_rule(conn, user_id, name, kind, parsed)
        await ctx.send(msg if ok else f" {msg}")
    except Exception as exc:
        await ctx.send(f" Monitor add failed: `{type(exc).__name__}: {exc}`")


@monitor_cmd.command(name="run")
async def monitor_run_cmd(ctx: commands.Context):
    user_id = str(ctx.author.id)
    try:
        from src.services.monitor import run_monitor_pass
        res = run_monitor_pass(conn, user_id, deliver=False)
        await ctx.send(
            f" **Monitor pass complete**\n"
            f"Rules evaluated: {res['rules_evaluated']}\n"
            f"Rules fired: {res['rules_fired']}\n"
            f"Alert IDs: {res['alert_ids'] or 'none'}"
        )
    except Exception as exc:
        await ctx.send(f" Monitor run failed: `{type(exc).__name__}: {exc}`")


@monitor_cmd.command(name="alerts")
async def monitor_alerts_cmd(ctx: commands.Context, limit: int = 20):
    user_id = str(ctx.author.id)
    try:
        from src.services.monitor import list_alerts
        alerts = list_alerts(conn, user_id, limit=limit, include_acked=False)
        if not alerts:
            await ctx.send(" No unacked monitor alerts.")
            return
        lines = [f" **Monitor alerts ({len(alerts)})**"]
        for a in alerts[:limit]:
            lines.append(
                f"- #{a['id']} [{a['severity']}] {a['title']}\n"
                f"  {a['detail']}\n"
                f"  {a['fired_at']}"
            )
        await ctx.send("\n".join(lines)[:1900])
    except Exception as exc:
        await ctx.send(f" Monitor alerts failed: `{type(exc).__name__}: {exc}`")


@monitor_cmd.command(name="ack")
async def monitor_ack_cmd(ctx: commands.Context, alert_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.services.monitor import ack_alert
        await ctx.send(ack_alert(conn, alert_id, user_id))
    except Exception as exc:
        await ctx.send(f" Monitor ack failed: `{type(exc).__name__}: {exc}`")


@monitor_cmd.command(name="on")
async def monitor_on_cmd(ctx: commands.Context, rule_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.services.monitor import toggle_monitor_rule
        await ctx.send(toggle_monitor_rule(conn, rule_id, user_id, True))
    except Exception as exc:
        await ctx.send(f" Monitor on failed: `{type(exc).__name__}: {exc}`")


@monitor_cmd.command(name="off")
async def monitor_off_cmd(ctx: commands.Context, rule_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.services.monitor import toggle_monitor_rule
        await ctx.send(toggle_monitor_rule(conn, rule_id, user_id, False))
    except Exception as exc:
        await ctx.send(f" Monitor off failed: `{type(exc).__name__}: {exc}`")


@monitor_cmd.command(name="del")
async def monitor_del_cmd(ctx: commands.Context, rule_id: int):
    user_id = str(ctx.author.id)
    try:
        from src.services.monitor import delete_monitor_rule
        await ctx.send(delete_monitor_rule(conn, rule_id, user_id))
    except Exception as exc:
        await ctx.send(f" Monitor del failed: `{type(exc).__name__}: {exc}`")


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

@bot.command(name="analyst")
async def analyst_cmd(ctx, action: str = "status"):
    """
    Manage the Epistemic Autonomous Analyst.
    Usage: !analyst run
    """
    user_id = str(ctx.author.id)
    if action == "run":
        await ctx.send("Starting Epistemic Autonomous Analyst pass...")
        result = await run_autonomous_analyst(user_id)
        await ctx.send(f"**Analyst Result**: {result}")
    else:
        await ctx.send("Usage: !analyst run")


@bot.command(name="timezone")
async def timezone_cmd(ctx, tz: str = None):
    """
    Set or view your personal timezone (e.g., America/New_York).
    """
    user_id = str(ctx.author.id)
    if not tz:
        current_tz = get_user_timezone(user_id)
        await ctx.send(f"Your current timezone is: **{current_tz}**\nTo change it, use: `!timezone America/Los_Angeles`")
        return
        
    try:
        set_user_timezone(user_id, tz)
        await ctx.send(f"✅ Your timezone has been updated to **{tz}**.")
    except Exception as e:
        await ctx.send(f"❌ Invalid timezone: `{tz}`.\n\n**Common Timezones:**\n• `America/New_York` (Eastern)\n• `America/Chicago` (Central)\n• `America/Denver` (Mountain)\n• `America/Los_Angeles` (Pacific)\n• `Europe/London` (GMT/BST)\n\nPlease use one of those formats (case-sensitive)!")
