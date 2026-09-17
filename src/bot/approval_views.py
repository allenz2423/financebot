"""B2 Discord approval view for concierge write-tier actions.

Reuses the ``LoginSessionView`` owner-check + timeout + cleanup pattern from
``src.bot.concierge_login``. On ✅ the proposal is executed via the
Browserless headless browser (act_actions.perform_act + BrowserlessActuator),
then self-verified via VLM (vlm_verify.verify_act). On ✕ it is rejected +
audited. The Discord message is edited to remove buttons on completion.
"""

from __future__ import annotations

import asyncio
import base64 as b64
import json
from typing import Any, Callable, Dict, List, Optional

import discord

from src.services.concierge.act_actions import Actuator, perform_act, _act_outcome
from src.services.concierge.approval_store import ApprovalStore
from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import validate_target_url
from src.services.concierge.tenants import TenantStore
from src.services.concierge.vlm_verify import verify_act
from src.services.concierge.browser_driver import DEFAULT_TIMEOUT_MS
from src.security.vault import DEFAULT_DB_PATH as _DB, Vault


class BrowserlessActuator(Actuator):
    """Sync Actuator backed by a Browserless/Playwright WS session.

    Playwright is async; each method drives a single page via a dedicated
    event loop so the sync ``perform_act`` contract is preserved.

    SSRF hardening: the actuator carries the tenant allowlist and RE-VALIDATES
    the live page URL (allowlist + DNS-public, via ``validate_target_url``)
    after every navigation and before every interaction, including responses
    to HTTP redirects and JS-driven main-frame navigations (framenavigated).
    A page that leaves the gated scope fails closed mid-act.
    """

    def __init__(self, url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 allowed_domains: Optional[List[str]] = None):
        self._url = url
        self._timeout = timeout_ms
        self._allowed = list(allowed_domains or [])
        self._loop = asyncio.new_event_loop()
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._nav_violation = None

    def _ensure(self) -> None:
        if self._page is not None:
            return
        self._loop.run_until_complete(self._connect())

    async def _connect(self) -> None:
        from playwright.async_api import async_playwright
        self._playwright = async_playwright()
        await self._playwright.start()
        browser = await self._playwright.chromium.connect_over_websocket(
            _ws_url()
        )
        self._browser = browser
        self._context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
        )
        self._page = await self._context.new_page()
        self._nav_violation = None

        async def _guard_frame(frame) -> None:
            try:
                url = frame.url or ""
                if frame == self._page.main_frame and url.lower().startswith(
                    ("http://", "https://")
                ):
                    validate_target_url(url, self._allowed)
            except Exception as exc:  # noqa: BLE001 — record, raise at next op
                self._nav_violation = exc

        self._page.on("framenavigated", _guard_frame)

    def _assert_on_scope(self) -> None:
        """Fail closed if the live page left the gated allowlist scope."""
        if self._nav_violation is not None:
            raise self._nav_violation
        if self._page is not None:
            validate_target_url(self._page.url, self._allowed)

    def navigate(self, url: str) -> None:
        self._ensure()
        self._loop.run_until_complete(
            self._page.goto(url, wait_until="networkidle", timeout=self._timeout)
        )
        self._assert_on_scope()

    def click(self, selector: str) -> None:
        self._ensure()
        self._assert_on_scope()
        self._loop.run_until_complete(self._page.click(selector, timeout=self._timeout))

    def type_text(self, selector: str, value: str) -> None:
        self._ensure()
        self._assert_on_scope()
        self._loop.run_until_complete(self._page.fill(selector, str(value)))

    def screenshot(self) -> bytes:
        self._ensure()
        self._assert_on_scope()
        return self._loop.run_until_complete(self._page.screenshot(full_page=True))

    def get_text(self) -> str:
        self._ensure()
        self._assert_on_scope()

        async def _eval() -> str:
            try:
                body = await self._page.evaluate(
                    "() => document.body ? document.body.innerText : ''",
                )
                return str(body or "")
            except Exception:
                return ""

        return self._loop.run_until_complete(_eval())

    def element_visible(self, selector: str) -> bool:
        self._ensure()
        self._assert_on_scope()

        async def _visible() -> bool:
            try:
                el = await self._page.query_selector(selector)
                return el is not None
            except Exception:
                return False

        return self._loop.run_until_complete(_visible())

    def close(self) -> None:
        try:
            if self._page is not None:
                self._loop.run_until_complete(self._context.close())
            if self._browser is not None:
                self._loop.run_until_complete(self._browser.close())
            if self._playwright is not None:
                self._loop.run_until_complete(self._playwright.stop())
        finally:
            try:
                if not self._loop.is_running():
                    self._loop.close()
            except RuntimeError:
                pass


def _ws_url() -> str:
    from src.services.concierge.browser_driver import _ws_url as _orig
    return _orig()


class ActionApprovalView(discord.ui.View):
    """[✅ Approve] / [✕ Reject] for a pending concierge write-tier proposal.

    Only the user who triggered the original request (uid embedded in the
    proposal) can approve or reject. Modeled on ``LoginSessionView``.
    """

    def __init__(
        self,
        proposal_id: str,
        owner_uid: int,
        tenant: str,
        timeout: float = 600.0,
    ):
        super().__init__(timeout=timeout)
        self.proposal_id = proposal_id
        self.owner_uid = int(owner_uid)
        self.tenant = tenant

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_uid

    @discord.ui.button(label="✅ Approve & Execute", style=discord.ButtonStyle.success)
    async def _approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the person who requested this action can approve it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)

        store = ApprovalStore()
        if not store.claim(self.proposal_id):
            await interaction.followup.send(
                "⚠️ This approval request can no longer be executed (already "
                "handled, or its approval window expired).", ephemeral=True
            )
            return
        proposal = store.get(self.proposal_id)
        allowed = proposal.get("allowed_domains") or []
        audit = AuditLog(_DB)
        audit.append(
            actor=self.tenant, action="act_approved", tenant=self.tenant,
            subject=proposal["kind"], detail={"proposal_id": self.proposal_id},
        )

        vault = Vault(_DB)

        def _resolve_secret(vault_ref: str) -> str:
            payload = json.loads(
                vault.resolve(self.tenant, vault_ref, consumer_url=proposal["url"])
            )
            values = list(payload.values()) if isinstance(payload, dict) else []
            if not values:
                raise ValueError(f"vault_ref {vault_ref} holds no value")
            return str(values[0])

        actuator = BrowserlessActuator(
            proposal["url"], timeout_ms=DEFAULT_TIMEOUT_MS,
            allowed_domains=allowed,
        )
        try:
            res = perform_act(
                kind=proposal["kind"],
                args=proposal["args"],
                allowed_domains=allowed,
                tenant=self.tenant,
                actuator=actuator,
                audit=audit,
                include_screenshot=True,
                resolve_secret=_resolve_secret,
            )
            page_text = res.get("summary", "") or ""
            verification = await verify_act(
                proposal["url"], proposal["steps"], page_text=page_text,
                allowed_domains=allowed,
            )
            outcome = _act_outcome(res)
            audit.append(
                actor=self.tenant, action="act_" + outcome, tenant=self.tenant,
                subject=proposal["kind"],
                detail={
                    "proposal_id": self.proposal_id,
                    "status": res["status"],
                    "verify_verdict": verification.get("verdict"),
                    "verify_confidence": verification.get("confidence"),
                },
            )
            store.update_status(
                self.proposal_id, outcome,
                result_detail={
                    "act_result": _result_detail(res),
                    "verification": verification,
                },
            )

            status_emoji = "✅" if outcome == "executed" else "⚠️"
            outcome_word = "executed" if outcome == "executed" else "rolled back"
            rec = res.get("receipt") or {}
            receipt_line = (
                "\nReceipt: " + ", ".join(f"{k}={v}" for k, v in rec.items())
                if rec else ""
            )
            msg = (
                f"{status_emoji} **{proposal['kind']}** {outcome_word} on "
                f"{proposal['url']}\n"
                f"Verify: **{verification['verdict']}** "
                f"(conf {verification['confidence']:.2f})\n"
                f"Summary: {res.get('summary', '(no result)')[:300]}"
                f"{receipt_line}"
            )
            await interaction.followup.send(msg, ephemeral=False)
        except Exception as exc:
            audit.append(
                actor=self.tenant, action="act_error", tenant=self.tenant,
                subject=proposal["kind"],
                detail={"proposal_id": self.proposal_id, "error": str(exc)[:300]},
            )
            store.update_status(self.proposal_id, "rolled_back",
                                result_detail={"error": str(exc)[:300]})
            await interaction.followup.send(
                f"⚠️ Execution failed — act rolled back: "
                f"{type(exc).__name__}: {str(exc)[:300]}",
                ephemeral=True,
            )
        finally:
            actuator.close()
            for item in list(self.children):
                item.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="✕ Reject", style=discord.ButtonStyle.danger)
    async def _reject(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the person who requested this action can reject it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        store = ApprovalStore()
        proposal = store.get(self.proposal_id)
        if proposal and proposal["status"] == "pending":
            store.update_status(self.proposal_id, "rejected")
            audit = AuditLog(_DB)
            audit.append(
                actor=self.tenant, action="act_rejected", tenant=self.tenant,
                subject=proposal["kind"], detail={"proposal_id": self.proposal_id},
            )
            await interaction.followup.send(
                "✕ Write-tier action rejected. No execution occurred.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "This request is no longer pending.", ephemeral=True
            )
        for item in list(self.children):
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def on_timeout(self) -> None:
        """Auto-expire after the approval TTL window; buttons go gray.

        Transitions the proposal to ``expired`` server-side (not just the
        Discord view timeout) — and the terminal transition revokes any vault
        secrets vaulted for it.
        """
        try:
            store = ApprovalStore()
            proposal = store.get(self.proposal_id)
            if proposal and proposal["status"] == "pending":
                store.update_status(
                    self.proposal_id, "expired",
                    result_detail={"reason": "approval prompt timed out"},
                )
                AuditLog(_DB).append(
                    actor=self.tenant, action="act_expired", tenant=self.tenant,
                    subject=proposal["kind"],
                    detail={"proposal_id": self.proposal_id},
                )
        except Exception:
            pass
        for item in list(self.children):
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    async def on_error(self, interaction: discord.Interaction, exc: Exception) -> None:
        print(f" [CONCIERGE APPROVAL VIEW] error: {type(exc).__name__}: {exc}")
        for item in list(self.children):
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass


def _result_detail(res: Dict[str, Any]) -> Dict[str, Any]:
    """Persistable (JSON-safe) view of an act result: no raw screenshot bytes.

    The screenshot blob is kept ephemeral (DM attachment path only) and is
    excluded from the SQLite ``result_detail`` so the status transition can
    never crash the approval callback on bytes serialization.
    """
    safe = dict(res)
    safe.pop("screenshot_png", None)
    return safe


__all__ = ["ActionApprovalView", "BrowserlessActuator"]
