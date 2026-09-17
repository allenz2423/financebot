"""B2 write-tier action executor (audited + gated + mask-only).

Reads were layer 4; writes are layer B2.  The trust model flips: a write
never fires unless a human approves it, so the executor itself is
allowlist-gated and audited (never trusted to self-authorize), and the model
only ever *proposes* a plan — it does not press buttons.

Discipline, mirrored from ``read_actions``:
  * ``domain`` is allowlisted per tenant (reuse ``browser_gate`` rules).
  * a refusal is audited as ``act_refused`` *before* the error propagates.
  * a successful act writes one ``act_<kind>`` audit row with the target URL
    and the output footprint (hash, screenshot flag) — never raw text.
  * outputs are mask-only: secret-bearing keys and PAN-shaped digit runs are
    redacted before they reach a result dict.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional

from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import ReadGateError, _check_domain, validate_target_url
from src.services.concierge.read_actions import (
    _mask_value,
    _summary_from_text,
    _PAN_LIKE,
    _PAN_GROUPED,
    _TOKEN_LIKE,
)

# Write-tier kinds. Read kinds ("order_status"... ) stay in browser_gate.READ_KINDS.
ACT_KINDS = frozenset({"send_email", "schedule_event", "fill_form"})

# Each act plan is a list of validated steps. Only these action verbs are
# permitted; selectors must reference the allowlisted host.
VALID_STEP_ACTIONS = frozenset({"navigate", "click", "type", "screenshot", "assert_text"})
_SECRET_KEYS = (
    "password", "pwd", "secret", "token", "api_key", "authorization",
    "bearer", "pan", "cvv", "ccv", "cvc", "card", "otp", "captcha",
)
MAX_STEP_TEXT = 4096
MAX_SUMMARY_CHS = 2000

# Type-step value classification, most specific first. Maps selector/key
# substrings to a vault field type; "ephemeral" fields (cvv/captcha) can
# never be persisted and re-entry is not implemented yet.
_SECRET_FIELD_SNIFF = (
    ("ephemeral", ("cvv", "cvc", "ccv", "captcha")),
    ("card_pan", ("pan", "card")),
    ("card_exp", ("exp", "expiry", "expiration")),
    ("otp", ("otp", "2fa", "totp", "authenticator")),
    ("token", ("token", "api_key", "authorization", "bearer")),
    ("password", ("password", "pwd", "passwd")),
)

# Per-kind required arg keys (everything else is rejected up front).
_ACT_ARG_KEYS = {
    "send_email": ("domain",),
    "schedule_event": ("domain",),
    "fill_form": ("domain",),
}

# Post-execution receipt extraction (B2 completion: execute -> verify -> receipt).
# Receipts are transaction proof, not secrets, but PAN-shaped substrings are
# defensively masked before anything leaves the act result / audit row.
_RECEIPT_TOTAL_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{2})?)")
_RECEIPT_CONFIRM_RE = re.compile(
    r"(?:order|confirmation|confirm|ref|reference|invoice|receipt|txn?|transaction)"
    r"\s*#?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{3,15})",
    re.IGNORECASE,
)


def _mask_receipt(value: str) -> str:
    """Mask secret-shaped substrings in a receipt field; pass others through.

    Applies the shared masker (PAN runs, grouped account runs, token shapes)
    so e.g. an "Invoice ref: sk-1234abcd" page line cannot surface a raw
    credential in the Discord receipt line.
    """
    return _mask_value(value)


def _extract_receipt(text: str) -> Dict[str, str]:
    """Pull a (masked) confirmation token and dollar total from page text.

    Returns a dict with whichever of `confirmation`/`total` are present; an
    empty dict when neither matches.
    """
    out: Dict[str, str] = {}
    m = _RECEIPT_CONFIRM_RE.search(text or "")
    if m:
        out["confirmation"] = _mask_receipt(m.group(1))
    m = _RECEIPT_TOTAL_RE.search(text or "")
    if m:
        out["total"] = m.group(1)
    return out


def _act_outcome(res: Dict[str, Any]) -> str:
    """Classify a perform_act result for the proposal status machine.

    ``executed`` when the act completed cleanly; ``rolled_back`` when it
    errored mid-flight. A rolled_back act discards its disposable browser
    session (see ``ActionApprovalView._approve`` finally -> actuator.close)
    and is NOT marked complete — stale or partial acts can't be mistaken
    for a successful write. The model only ever sees this via the audit
    trail + Discord DM, never raw output.
    """
    if res.get("status") == "ok":
        return "executed"
    return "rolled_back"


class ActActionError(ValueError):
    pass


class Actuator:
    """Minimal browser actuator contract.

    The real implementation drives the disposable Chromium over CDP via
    Playwright (``chromium.connect_over_http``).  Tests supply a stub.
    """

    def navigate(self, url: str) -> None: ...  # pragma: no cover
    def click(self, selector: str) -> None: ...  # pragma: no cover
    def type_text(self, selector: str, value: str) -> None: ...  # pragma: no cover
    def screenshot(self) -> bytes: ...  # pragma: no cover
    def get_text(self) -> str: ...  # pragma: no cover
    def element_visible(self, selector: str) -> bool: ...  # pragma: no cover


def _secret_field_type(step: Dict[str, Any]) -> Optional[str]:
    """Classify a type step's value for secret handling.

    Returns a vault field type (``card_pan``/``card_exp``/``otp``/``token``/
    ``password``/``text``-style) when the value must be vault-injected,
    ``"ephemeral"`` when it can never be persisted (cvv/captcha), or None
    when the value is not secret-bearing and can be stored as-is.
    """
    if not isinstance(step, dict) or step.get("action") != "type":
        return None
    value = str(step.get("value") or "")
    if _PAN_LIKE.search(value) or _PAN_GROUPED.search(value):
        return "card_pan"
    if _TOKEN_LIKE.search(value):
        return "token"
    sel = (str(step.get("sel") or "") + str(step.get("key") or "")).lower()
    for ftype, needles in _SECRET_FIELD_SNIFF:
        for needle in needles:
            if needle in sel:
                return ftype
    return None


def _step_vault_refs(steps: List[Dict[str, Any]]) -> List[str]:
    """Collect the vault refs referenced by an act plan (for lifecycle mgmt)."""
    return [
        str(step["vault_ref"]) for step in steps or []
        if isinstance(step, dict) and step.get("vault_ref")
    ]


def _validate_step_navigations(steps: List[dict], allowed_domains: List[str]) -> None:
    """Gate every step-level navigate target: allowlist + SSRF/DNS-public.

    Refused targets raise ReadGateError BEFORE any actuator work starts, so a
    tampered or hostile plan can never drive the browser toward an internal
    host (ollama/qdrant/browserless/… or cloud metadata).
    """
    for i, step in enumerate(steps):
        if step.get("action") != "navigate":
            continue
        validate_target_url(str(step.get("sel") or ""), allowed_domains)


def _validate_steps(steps: Any, allowed_domains: Optional[List[str]] = None) -> List[dict]:
    if not isinstance(steps, list) or not steps:
        raise ActActionError("act args must include a non-empty 'steps' list")
    cleaned: List[dict] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ActActionError(f"step[{i}] must be an object")
        action = step.get("action")
        if action not in VALID_STEP_ACTIONS:
            raise ActActionError(f"step[{i}] action '{action}' is not a permitted act verb")
        if action == "type":
            value = str(step.get("value") or "")
            if len(value) > MAX_STEP_TEXT:
                raise ActActionError(f"step[{i}] type value exceeds {MAX_STEP_TEXT} chars")
        if "sel" not in step and action not in ("screenshot", "assert_text"):
            raise ActActionError(f"step[{i}] '{action}' requires a 'sel' selector")
        if action == "navigate" and allowed_domains:
            try:
                validate_target_url(str(step.get("sel") or ""), allowed_domains)
            except ReadGateError as exc:
                raise ActActionError(f"step[{i}] navigate target refused: {exc}") from exc
        cleaned.append(step)
    return cleaned


def _act_target_url(kind: str, args: dict, allowed_domains: List[str]) -> str:
    domain = str(args.get("domain") or "").strip().lower()
    if not _check_domain(domain):
        raise ReadGateError("act args require a valid domain")
    candidate = "https://" + domain
    return validate_target_url(candidate, allowed_domains)


def _mask_step_texts(steps: List[dict]) -> List[dict]:
    """Mask values whose selector/key looks secret-bearing; PAN-shape always."""
    out: List[dict] = []
    for step in steps:
        s = dict(step)
        if "value" in s and isinstance(s.get("value"), str):
            lowered = str(s.get("selector", s.get("sel", ""))).lower() + str(
                s.get("key", "")
            ).lower()
            if any(p in lowered for p in _SECRET_KEYS):
                s["value"] = "\u2022" * 4
            else:
                s["value"] = _mask_value(s["value"])
        out.append(s)
    return out


def perform_act(
    kind: str,
    args: dict,
    allowed_domains: List[str],
    tenant: str,
    actuator: "Actuator",
    audit: Optional[AuditLog] = None,
    include_screenshot: bool = True,
    resolve_secret: Optional[Callable[[str], str]] = None,
) -> dict:
    """Run one audited write action (proposal side; execution is approved elsewhere).

    Gating is identical to reads (allowlist + SSRF): the initial URL AND every
    step-level navigate target are validated against the tenant allowlist +
    DNS-public rules BEFORE any actuator work, and a refusal is audited as
    ``act_refused``. Secret-bearing type values are never typed raw: they
    arrive via a ``vault_ref`` resolved by ``resolve_secret`` (vault-injected
    at execution); a secret-shaped value without a ref fails closed. The
    actuator drives the approved plan inside the disposable browser. Returns
    a MASK-ONLY dict.
    """
    if kind not in ACT_KINDS:
        raise ActActionError(
            "act kind must be one of " + ", ".join(sorted(ACT_KINDS)) + " (write-tier only)"
        )
    if not isinstance(args, dict):
        raise ActActionError("act args must be a dict")
    for key in _ACT_ARG_KEYS[kind]:
        if key not in args:
            raise ActActionError(f"{kind} requires '{key}' arg")
    steps = _validate_steps(args.get("steps"))

    try:
        _validate_step_navigations(steps, allowed_domains)
        url = _act_target_url(kind, args, allowed_domains)
    except ReadGateError as exc:
        if audit is not None:
            audit.append(
                actor=tenant, action="act_refused", tenant=tenant,
                subject=kind, detail={"reason": str(exc)[:300], "kind": kind},
            )
        raise ActActionError(str(exc)) from exc

    err: Optional[str] = None
    try:
        actuator.navigate(url)
        if include_screenshot:
            actuator.screenshot()
        for i, step in enumerate(steps):
            action = step["action"]
            if action == "navigate":
                actuator.navigate(step["sel"])
            elif action == "click":
                actuator.click(step["sel"])
            elif action == "type":
                value = str(step.get("value") or "")
                vault_ref = step.get("vault_ref")
                if vault_ref:
                    if resolve_secret is None:
                        raise ActActionError(
                            f"step[{i}] {step['sel']!r} has vault_ref but no resolver"
                        )
                    value = resolve_secret(str(vault_ref))
                elif _secret_field_type(step) is not None:
                    raise ActActionError(
                        f"step[{i}] secret-bearing type value for {step['sel']!r} "
                        "must be vault-injected (vault_ref missing)"
                    )
                actuator.type_text(step["sel"], value)
            elif action == "screenshot":
                if include_screenshot:
                    actuator.screenshot()
            elif action == "assert_text":
                if not actuator.element_visible(step["sel"]):
                    raise ActActionError(f"assert_text failed for {step['sel']}")
                expected = str(step.get("value") or "")
                if expected and expected not in (actuator.get_text() or ""):
                    raise ActActionError(
                        f"assert_text failed for {step['sel']}: "
                        f"expected {expected!r} not on page"
                    )
    except Exception as exc:  # noqa: BLE001 — surface but keep audit + mask
        err = f"{type(exc).__name__}: {exc}"

    text = actuator.get_text() or ""
    summary = _summary_from_text(text)[:MAX_SUMMARY_CHS] or "(no result text)"
    receipt = _extract_receipt(text)
    post_screenshot = actuator.screenshot() if include_screenshot else None
    footprint = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
    if audit is not None:
        audit.append(
            actor=tenant, action="act_" + kind, tenant=tenant, subject=url,
            detail={
                "url": url,
                "steps": len(steps),
                "summary_len": len(summary),
                "text_hash16": footprint,
                "screenshot": bool(post_screenshot),
                "ok": err is None,
                **({"error": str(err)[:200]} if err else {}),
                **({"receipt": receipt} if receipt else {}),
            },
        )
    return {
        "kind": kind,
        "url": url,
        "status": "ok" if err is None else "error",
        "error": err,
        "summary": summary,
        "text_hash16": footprint,
        "steps": _mask_step_texts(steps),
        "screenshot_png": post_screenshot,
        "receipt": receipt,
    }


__all__ = [
    "ACT_KINDS",
    "ActActionError",
    "Actuator",
    "perform_act",
    "_validate_steps",
    "_validate_step_navigations",
    "_secret_field_type",
    "_step_vault_refs",
]