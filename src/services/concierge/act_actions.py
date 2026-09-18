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
from urllib.parse import urlparse

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
VALID_STEP_ACTIONS = frozenset({"navigate", "click", "type", "scroll", "screenshot", "assert_text"})
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
    def scroll(self, value: str = "down", selector: str = "") -> None: ...  # pragma: no cover

    def heal(self) -> bool:
        """Clear a known interstitial (bot-check/captcha) blocking the flow.

        Auto-pilot continuation hook: returns True if a blocker was found and
        dismissed (so ``perform_act`` retries the failed step), False when the
        page is clear. Stubs inherit this no-op; the real browser actuator
        clicks through curated checkpoint markers (never arbitrary elements).
        """
        return False

    def storage_state(self) -> bytes:
        """Return serialized browser storage state (cookies + local storage)."""
        return b"{}"

    def restore_storage_state(self, state_bytes: bytes) -> None:
        """Restore browser storage state (cookies + local storage)."""
        return None

    def close(self) -> None:
        """Best-effort teardown of the disposable browser session.

        Never raises; the real actuator closes its CDP context/browser/loop.
        """
        return None


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


def _host_of(url: Any) -> str:
    """Bare host of a URL, www-stripped + lowercased (mission scope keying).

    Mirrors ``approval_store._host_of`` so execute-time navigation gating and
    mission scoping agree on what "the same site" means.
    """
    if not url:
        return ""
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


def _validate_step_navigations(
    steps: List[dict], allowed_domains: List[str], target_host: Optional[str] = None,
) -> None:
    """Gate every step-level navigate target: allowlist + SSRF/DNS-public.

    Refused targets raise ReadGateError BEFORE any actuator work starts, so a
    tampered or hostile plan can never drive the browser toward an internal
    host (ollama/qdrant/browserless/… or cloud metadata). With ``target_host``
    given, step navigations must ALSO stay on that host (www-stripped): a
    redirect-through-another-allowlisted-domain step is refused as incoherent
    with the act's own target.
    """
    for i, step in enumerate(steps):
        if step.get("action") != "navigate":
            continue
        target = str(step.get("sel") or "")
        validate_target_url(target, allowed_domains)
        if target_host and _host_of(target) != target_host:
            raise ActActionError(
                f"step[{i}] navigate target {target!r} leaves the act's target "
                f"host {target_host!r} — step navigations must stay on the act's "
                "host (no cross-domain redirects)"
            )


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
        # Models occasionally emit the semantically equivalent
        # {"action": "navigate", "url": "https://..."} form. Normalize it
        # to the canonical selector field before validation. The URL still
        # passes the normal allowlist, SSRF, and same-host gates below.
        if action == "navigate" and not step.get("sel"):
            # Navigation targets are commonly emitted as ``url`` by models,
            # while some tool adapters use ``target`` or ``value``. Normalize
            # these generic names before validation; navigation still passes
            # through the same allowlist, SSRF, and same-host checks.
            for field in ("url", "target", "value"):
                if step.get(field):
                    step = dict(step)
                    step["sel"] = step[field]
                    break
        # The agentic browser tool calls this field ``selector`` while the
        # approval/act schema historically called it ``sel``. Accept both and
        # canonicalize before any validation or persistence so a harmless
        # naming mismatch cannot reject an otherwise valid approved action.
        if "sel" not in step and step.get("selector"):
            step = dict(step)
            step["sel"] = step["selector"]
        if action == "type":
            value = str(step.get("value") or "")
            if len(value) > MAX_STEP_TEXT:
                raise ActActionError(f"step[{i}] type value exceeds {MAX_STEP_TEXT} chars")
            if "vault:" in value:
                raise ActActionError(
                    f"step[{i}] type value must be literal text — a 'vault:' token "
                    "({{vault:...}} or bare vault:s_...:field) is not typed directly; "
                    "reference the stored value via vault_ref on the step, never inline"
                )
        if "sel" not in step and action not in ("scroll", "screenshot", "assert_text"):
            raise ActActionError(f"step[{i}] '{action}' requires a 'sel' selector")
        if action == "scroll" and not step.get("sel") and not step.get("value"):
            raise ActActionError(f"step[{i}] 'scroll' requires a selector or value")
        if action == "navigate" and allowed_domains:
            target = str(step.get("sel") or "").strip()
            if not target.lower().startswith(("http://", "https://")):
                # Check if target is a domain name or domain/path
                first_allowed = allowed_domains[0] if allowed_domains else "amazon.com"
                if target.startswith("/"):
                    target = f"https://{first_allowed}{target}"
                elif any(target.split("/")[0].endswith(d) for d in allowed_domains):
                    target = f"https://{target}"
                else:
                    target = f"https://{first_allowed}/{target.lstrip('/')}"
                step["sel"] = target
            try:
                validate_target_url(str(step.get("sel") or ""), allowed_domains)
            except ReadGateError as exc:
                raise ActActionError(f"step[{i}] navigate target refused: {exc}") from exc
        cleaned.append(step)
    return cleaned


def _act_target_url(kind: str, args: dict, allowed_domains: List[str]) -> str:
    domain = str(args.get("domain") or "").strip().lower()
    # tolerate a scheme/path in the domain arg (model habit): strip to bare host
    domain = re.sub(r"^https?://", "", domain).split("/")[0].split("?")[0].rstrip(".")
    if not _check_domain(domain):
        raise ReadGateError("act args require a valid domain")
    # Browser missions may start at a user/model-selected path (for example a
    # sign-in page) instead of always opening the host root.  The path remains
    # constrained to the declared, allowlisted domain; this is generic URL
    # handling, not a site-specific route.
    explicit_url = str(args.get("url") or "").strip()
    if explicit_url:
        candidate = explicit_url
    else:
        raw_path = str(args.get("path") or "").strip().lstrip("/")
        candidate = "https://" + domain + ("/" + raw_path if raw_path else "")
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
        url = _act_target_url(kind, args, allowed_domains)
        # Every step-level navigate must stay on the act's own target host
        # (www-stripped) — redirects through other allowlisted domains are
        # refused, not executed.
        _validate_step_navigations(steps, allowed_domains, target_host=_host_of(url))
    except (ReadGateError, ActActionError) as exc:
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
            # Auto-pilot continuation: each step may be retried once after
            # ``heal()`` clears a known interstitial (Amazon AVS bot-check,
            # captcha claimView) that intercepted the flow — so the approved
            # act keeps going instead of rolling back at the first deviation.
            attempt = 0
            while True:
                try:
                    if action == "heal":
                        actuator.heal()
                    elif action == "navigate":
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
                    elif action == "scroll":
                        actuator.scroll(
                            value=str(step.get("value") or "down"),
                            selector=str(step.get("sel") or ""),
                        )
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
                    break
                except Exception:
                    attempt += 1
                    if attempt >= 2:
                        raise
                    try:
                        healed = actuator.heal()
                    except Exception:  # noqa: BLE001 — heal is best-effort
                        healed = False
                    if not healed:
                        raise
    except Exception as exc:  # noqa: BLE001 — surface but keep audit + mask
        err = f"{type(exc).__name__}: {exc}"

    text = actuator.get_text() or ""
    summary = _summary_from_text(text)[:MAX_SUMMARY_CHS] or "(no result text)"
    # A screenshot-only fill_form approval merely opens/attaches the live
    # mission. Incidental prices or words on the homepage are not transaction
    # evidence, so do not manufacture a receipt before a mutation occurs.
    marker_only = len(steps) == 1 and steps[0].get("action") == "screenshot"
    has_mutation = any(
        step.get("action") in {"click", "type"}
        for step in steps
        if isinstance(step, dict)
    )
    receipt = _extract_receipt(text) if (has_mutation or not marker_only) else {}
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
