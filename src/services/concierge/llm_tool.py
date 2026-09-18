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

from src.security.vault import Vault, DEFAULT_DB_PATH as _DB
from src.services.concierge.act_actions import (
    ACT_KINDS,
    ActActionError,
    _validate_steps,
    _validate_step_navigations,
    _act_target_url,
    _secret_field_type,
    _step_vault_refs,
)
from src.services.concierge.approval_store import ApprovalStore, _host_of
from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import READ_KINDS, ReadGateError
from src.services.concierge.capture_tokens import CaptureTokenStore
from src.services.concierge.credential_capture import (
    CaptureRefused,
    build_credential_capture,
)
from src.services.concierge.read_actions import (
    ReadActionError,
    perform_read_action,
    perform_read_action_async,
)
from src.services.concierge.tenants import TenantStore, TenantError

# Approval prompt lifetime (seconds): spend-tier (financial) acts are high-risk.
_DEFAULT_APPROVAL_TTL = 600.0
_HIGH_RISK_APPROVAL_TTL = 120.0


def _approval_ttl(status: Dict[str, Any]) -> float:
    """Window a write-tier approval prompt stays live before auto-rejecting.

    High-risk acts (spend-tier tenants, who may move money) get a shorter
    window so a stale approval can't be executed unattended; standard write
    acts keep the default 10-minute window. Read kinds never reach here.
    """
    if status.get("tier") == "spend":
        return _HIGH_RISK_APPROVAL_TTL
    return _DEFAULT_APPROVAL_TTL


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


def request_credential_capture(
    tenant: str,
    args: Dict[str, Any],
    tenants: TenantStore,
    audit: AuditLog,
    tokens: Optional[CaptureTokenStore] = None,
) -> Dict[str, Any]:
    """Issue a credential-capture link on the model's behalf (self-service).

    The user gets a one-time URL to store login credentials for the
    requested domain; raw values never reach the model or Discord. ``tenant``
    is interaction-derived (never model-supplied). Same gates as the
    ``!concierge creds`` command: tenant enabled + domain allowlisted.
    Every attempt is audited (capture_issued / capture_refused) carrying
    domain, never the token.
    """
    if not isinstance(args, dict):
        raise ConciergeToolError("args must be a dict")
    token_store = tokens or CaptureTokenStore(_DB)
    try:
        res = build_credential_capture(
            tenant,
            args.get("domain"),
            tenants=tenants,
            tokens=token_store,
            fields=args.get("fields"),
        )
    except CaptureRefused as exc:
        audit.append(
            actor=tenant, action="capture_refused", tenant=tenant,
            detail={"reason": str(exc)[:200]},
        )
        raise ConciergeToolError(str(exc)) from exc
    audit.append(
        actor=tenant, action="capture_issued", tenant=tenant,
        detail={"domain": res["domain"]},
    )
    return res


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


def _protect_secret_steps(
    steps: List[Dict[str, Any]],
    tenant: str,
    domain: str,
    vault: "Vault",
) -> List[Dict[str, Any]]:
    """Vault-inject secret-bearing type values before anything persists.

    Matches the tool-schema contract ("secret-bearing type values are masked
    before storage and only injected from the vault at execution"): the value
    is replaced by a masked placeholder + a tenant-scoped ``vault_ref``; the
    plaintext exists only as an encrypted vault ciphertext, decrypted once at
    execution inside the approval callback. Values that can never be
    persisted (cvv/captcha — FORCED_EPHEMERAL) refuse the proposal: CVV
    re-entry at approval is a design item, not shipped yet.
    """
    protected: List[Dict[str, Any]] = []
    for i, step in enumerate(steps):
        ftype = _secret_field_type(step)
        if ftype is None:
            protected.append(dict(step))
            continue
        sel = str(step.get("sel") or step.get("key") or f"step[{i}]")
        if ftype == "ephemeral":
            raise ConciergeToolError(
                f"{sel} takes a value that can never be stored (cvv/captcha) — "
                "CVV re-entry at approval is not supported yet; drop that step"
            )
        vault_kind = "payment" if ftype in ("card_pan", "card_exp") else "secret"
        rec = vault.store_fields(
            tenant=tenant,
            kind=vault_kind,
            label=f"concierge act step {sel}",
            fields={f"{ftype}|value": str(step.get("value") or "")},
            consumer_scope=[domain],
        )
        s = dict(step)
        s["value"] = "\u2022" * 4
        s["vault_ref"] = rec["vault_ref"]
        s["vault_kind"] = vault_kind
        protected.append(s)
    return protected


def concierge_act_status(
    tenant: str,
    limit: int = 10,
    store: Optional[ApprovalStore] = None,
) -> List[Dict[str, Any]]:
    """Read-only status of the tenant's recent concierge proposals.

    Lets the model answer "is the login done / what happened" truthfully
    from post-approval state instead of guessing from the approval prompt.
    Each entry carries the terminal status (pending/executed/rolled_back/
    rejected/expired) plus a short outcome note (error text or the act's
    page-text summary) when one exists. Never executes or mutates anything.
    """
    store = store or ApprovalStore(_DB)
    rows = store.recent_for_tenant(tenant, limit=max(1, min(int(limit or 10), 25)))
    # Map any still-active mission back to the proposal that created it, so a
    # proposal row can tell the model the *mission_id* to pass to
    # concierge_browser_step instead of the model reusing the proposal_id
    # (which is never a valid mission id and stalled the whole mission).
    mission_by_proposal = {
        m.get("origin_proposal_id"): m["mission_id"]
        for m in store.active_missions_for_tenant(tenant, limit=50)
        if m.get("origin_proposal_id")
    }
    out: List[Dict[str, Any]] = []
    for r in rows:
        item: Dict[str, Any] = {
            "proposal_id": r["proposal_id"],
            "kind": r["kind"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        mission_id = mission_by_proposal.get(r["proposal_id"])
        if mission_id:
            item["mission_id"] = mission_id
        detail = r.get("result_detail") or {}
        if detail.get("error"):
            item["outcome"] = str(detail["error"])[:240]
        elif isinstance(detail.get("act_result"), dict):
            ar = detail["act_result"]
            outcome_note = ""
            if ar.get("status"):
                outcome_note = f"act_status={ar['status']}"
            summary = str(ar.get("summary") or "")[:240].replace("\n", " ")
            if summary:
                outcome_note = (outcome_note + " | " if outcome_note else "") + summary
            item["outcome"] = outcome_note or None
        elif detail.get("verification"):
            v = detail["verification"]
            item["outcome"] = f"verify={v.get('verdict')} ({v.get('confidence', 0)})"
        out.append(item)
    return out


def propose_concierge_act(
    tenant: str,
    kind: str,
    args: Dict[str, Any],
    tenants: TenantStore,
    audit: AuditLog,
    store: Optional[ApprovalStore] = None,
    vault: Optional[Vault] = None,
) -> Dict[str, Any]:
    """Validate + store a write-tier act proposal awaiting human approval.

    Writes are never self-executing: the model only *proposes*, a human must
    approve via the Discord ``ActionApprovalView``. This function performs
    all pre-execution validation (tenant enabled AND tier-gated, domain
    allowlisted, steps well-formed + navigate targets gated) and persists a
    pending proposal carrying the validated allowlist so execution re-checks
    against exactly what was gated here.
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
    ttl = _approval_ttl(status)

    tier = str(status.get("tier") or "read")
    if tier not in ("write", "spend"):
        audit.append(
            actor=tenant, action="act_refused", tenant=tenant,
            subject=kind,
            detail={
                "reason": f"{kind} requires write or spend tier (tenant tier: {tier})",
                "kind": kind,
            },
        )
        raise ConciergeToolError(
            f"concierge tier '{tier}' cannot request write-tier '{kind}' "
            "(needs write or spend)"
        )

    if kind == "fill_form" and not args.get("steps"):
        # Browser fill-form missions are agentic after approval. If the model
        # follows the concise schema, persist only a marker so approval starts
        # with a live observation instead of requiring a blind plan.
        steps = [{"action": "screenshot"}]
    else:
        try:
            steps = _validate_steps(args.get("steps"), allowed_domains=allowed)
        except ActActionError as exc:
            audit.append(
                actor=tenant, action="act_refused", tenant=tenant,
                subject=kind, detail={"reason": str(exc)[:300], "kind": kind},
            )
            raise ConciergeToolError(str(exc)) from exc

    try:
        url = _act_target_url(kind, args, allowed)
    except ReadGateError as exc:
        audit.append(
            actor=tenant, action="act_refused", tenant=tenant,
            subject=kind, detail={"reason": str(exc)[:300], "kind": kind},
        )
        raise ConciergeToolError(str(exc)) from exc

    # Every step-level navigate must stay on the act's own target host. This
    # refuses the model's redirect-through-another-allowlisted-domain habit
    # at propose time — before a human ever sees an approval prompt.
    try:
        _validate_step_navigations(steps, allowed, target_host=_host_of(url))
    except (ReadGateError, ActActionError) as exc:
        audit.append(
            actor=tenant, action="act_refused", tenant=tenant,
            subject=kind, detail={"reason": str(exc)[:300], "kind": kind},
        )
        raise ConciergeToolError(str(exc)) from exc

    domain = str(args.get("domain") or "").strip().lower()
    vault_obj = vault or Vault()
    try:
        protected_steps = _protect_secret_steps(steps, tenant, domain, vault_obj)
    except ConciergeToolError as exc:
        audit.append(
            actor=tenant, action="act_refused", tenant=tenant,
            subject=kind, detail={"reason": str(exc)[:300], "kind": kind},
        )
        raise

    audit.append(
        actor=tenant, action="act_proposed", tenant=tenant,
        subject=kind, detail={"kind": kind, "url": url, "steps": len(protected_steps)},
    )

    uid = tenant.split(":", 1)[1] if tenant.startswith("user:") else ""
    safe_args = dict(args)
    safe_args["steps"] = protected_steps
    proposal_store = store or ApprovalStore()
    proposal_id = proposal_store.create(
        tenant=tenant, uid=uid, kind=kind, args=safe_args,
        url=url, steps=protected_steps, approval_ttl=ttl,
        allowed_domains=allowed,
    )
    return {
        "proposal_id": proposal_id,
        "kind": kind,
        "url": url,
        "steps": protected_steps,
        "status": "pending",
        "approval_ttl": ttl,
        "message": (
            f"Write-tier act '{kind}' submitted for your approval at {url}. "
            f"Check Discord for the approval prompt."
        ),
    }


__all__ = [
    "request_concierge_action",
    "request_concierge_action_async",
    "request_credential_capture",
    "propose_concierge_act",
    "tool_result_json",
    "ConciergeToolError",
]
