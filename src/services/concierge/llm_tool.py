"""B1 LLM entry for concierge read-tier actions (no approval by design).

The ONLY concierge tool exposed to the model is ``request_concierge_action``
and in B1 it accepts read kinds only (order_status / tracking / price_watch).
Reads are sandboxed + audited by construction, so there is no approval step —
but every attempt is gated: tenant enabled, tenant's domain allowlist, the
read-actions mask-only path, and an audit row either way.

Tenant identity is injected from the interaction, never model-supplied.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from src.services.concierge.act_actions import ACT_KINDS, ActActionError, _validate_steps, _act_target_url
from src.services.concierge.approval_store import ApprovalStore
from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import READ_KINDS, ReadGateError
from src.services.concierge.read_actions import (
    ReadActionError,
    perform_read_action,
    perform_read_action_async,
)
from src.services.concierge.tenants import TenantStore, TenantError


class ConciergeToolError(ValueError):
    pass


def request_concierge_action(
    tenant: str,
    kind: str,
    args: Dict[str, Any],
    tenants: TenantStore,
    audit: AuditLog,
    fetch_async: Callable[[str], Any],
    include_screenshot: bool = True,
) -> Dict[str, Any]:
    """Run one read-tier concierge action; returns a MASK-ONLY dict.

    ``fetch_async(url)`` is awaited by the caller wiring (llm.py) — this
    helper stays synchronous and returns the async fetcher untouched, letting
    the dispatch layer decide how to drive it.
    """
    if kind not in READ_KINDS:
        raise ConciergeToolError(
            "kind must be one of " + ", ".join(sorted(READ_KINDS)) + " (read-tier only)"
        )
    if not isinstance(args, dict):
        raise ConciergeToolError("args must be a dict")
    try:
        status = tenants.status(tenant)
    except TenantError:
        raise ConciergeToolError("concierge is not enabled for you")
    if not status.get("enabled"):
        raise ConciergeToolError("concierge is not enabled for you")
    allowed = status.get("allow_domains") or []
    try:
        return perform_read_action(
            kind, args, allowed, tenant,
            fetcher=lambda url: _await(fetch_async, url),
            audit=audit,
            include_screenshot=include_screenshot,
        )
    except ReadActionError as exc:
        raise ConciergeToolError(str(exc)) from exc


def _await(fn: Callable[[str], Any], url: str) -> Dict[str, Any]:
    """Bridge for sync contexts: resolves a coroutine fetcher.

    Only valid where no event loop is already running (tests, scripts); the
    bot dispatch path uses ``request_concierge_action_async`` instead.
    """
    result = fn(url)
    import asyncio
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    if not isinstance(result, dict):
        raise ReadActionError("read fetcher must return a dict")
    return result


async def request_concierge_action_async(
    tenant: str,
    kind: str,
    args: Dict[str, Any],
    tenants: TenantStore,
    audit: AuditLog,
    fetch_async: Callable[[str], Any],
    include_screenshot: bool = True,
) -> Dict[str, Any]:
    """Async variant for use inside the bot event loop (llm.py dispatch).

    Same gates as ``request_concierge_action`` but awaits the fetcher
    directly instead of bridging through a fresh event loop.
    """
    if kind not in READ_KINDS:
        raise ConciergeToolError(
            "kind must be one of " + ", ".join(sorted(READ_KINDS)) + " (read-tier only)"
        )
    if not isinstance(args, dict):
        raise ConciergeToolError("args must be a dict")
    try:
        status = tenants.status(tenant)
    except TenantError:
        raise ConciergeToolError("concierge is not enabled for you")
    if not status.get("enabled"):
        raise ConciergeToolError("concierge is not enabled for you")
    allowed = status.get("allow_domains") or []
    try:
        return await perform_read_action_async(
            kind, args, allowed, tenant,
            fetcher=fetch_async,
            audit=audit,
            include_screenshot=include_screenshot,
        )
    except ReadActionError as exc:
        raise ConciergeToolError(str(exc)) from exc


def tool_result_json(result: Dict[str, Any]) -> str:
    """Serialize a read result for model context: masks only, no screenshot."""
    safe = dict(result)
    safe.pop("screenshot_png", None)
    return json.dumps(safe, separators=(",", ":"), sort_keys=True)


def propose_concierge_act(
    tenant: str,
    kind: str,
    args: Dict[str, Any],
    tenants: TenantStore,
    audit: AuditLog,
) -> Dict[str, Any]:
    """Validate + store a write-tier act proposal awaiting human approval.

    Writes are never self-executing: the model only *proposes*, a human must
    approve via the Discord ``ActionApprovalView``. This function performs
    all pre-execution validation (tenant enabled, domain allowlisted, steps
    well-formed) and persists a pending proposal.
    """
    if kind not in ACT_KINDS:
        raise ConciergeToolError(
            "kind must be one of " + ", ".join(sorted(ACT_KINDS)) + " (write-tier only)"
        )
    if not isinstance(args, dict):
        raise ConciergeToolError("args must be a dict")
    try:
        status = tenants.status(tenant)
    except TenantError:
        raise ConciergeToolError("concierge is not enabled for you")
    if not status.get("enabled"):
        raise ConciergeToolError("concierge is not enabled for you")
    allowed = status.get("allow_domains") or []

    try:
        steps = _validate_steps(args.get("steps"))
    except ActActionError as exc:
        raise ConciergeToolError(str(exc)) from exc

    try:
        url = _act_target_url(kind, args, allowed)
    except ReadGateError as exc:
        audit.append(
            actor=tenant, action="act_refused", tenant=tenant,
            subject=kind, detail={"reason": str(exc)[:300], "kind": kind},
        )
        raise ConciergeToolError(str(exc)) from exc

    audit.append(
        actor=tenant, action="act_proposed", tenant=tenant,
        subject=kind, detail={"kind": kind, "url": url, "steps": len(steps)},
    )

    uid = tenant.split(":", 1)[1] if tenant.startswith("user:") else ""
    store = ApprovalStore()
    proposal_id = store.create(
        tenant=tenant, uid=uid, kind=kind, args=args,
        url=url, steps=steps,
    )
    return {
        "proposal_id": proposal_id,
        "kind": kind,
        "url": url,
        "steps": steps,
        "status": "pending",
        "message": (
            f"Write-tier act '{kind}' submitted for your approval at {url}. "
            f"Check Discord for the approval prompt."
        ),
    }


__all__ = [
    "request_concierge_action",
    "request_concierge_action_async",
    "propose_concierge_act",
    "tool_result_json",
    "ConciergeToolError",
]