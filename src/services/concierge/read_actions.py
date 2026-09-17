"""B1 read-tier executor (read_actions).

Turns a read kind (order_status / tracking / price_watch) + scoped args into
a gated URL (browser_gate), fetches it through an injected client
(browserless / concierge browser), and returns a MASK-ONLY result: bounded
text, no raw secrets, plus an optional screenshot for DM attachment.

Audit discipline (matches the gate's contract in browser_gate): a refused
attempt is audited as ``read_refused`` before the error propagates; a
successful read writes one ``read_<kind>`` row with the target URL and the
output's footprint — never the page body.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from typing import Any, Callable, Dict, List, Optional

from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_gate import ReadGateError, resolve_action_target

_PAN_LIKE = re.compile(r"\b\d{13,19}\b")
_SECRET_KEYS = ("password", "secret", "token", "api_key", "authorization", "pan", "cvv")

MAX_SUMMARY_CHARS = 2000


class ReadActionError(ValueError):
    pass


def _mask_value(value: str) -> str:
    """Mask literal secret-looking values in read output (PAN runs)."""
    text = value if isinstance(value, str) else str(value)
    text = _PAN_LIKE.sub("\u2022" * 4, text)
    return text


def _mask_dict(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively mask any value whose key looks secret-bearing."""
    out: Dict[str, Any] = {}
    for key, val in obj.items():
        lowered = str(key).lower()
        if any(p in lowered for p in _SECRET_KEYS):
            out[key] = "\u2022" * 4
            continue
        if isinstance(val, dict):
            out[key] = _mask_dict(val)
        elif isinstance(val, list):
            out[key] = [_mask_dict(v) if isinstance(v, dict) else v for v in val]
        elif isinstance(val, str):
            out[key] = _mask_value(val)
        else:
            out[key] = val
    return out


def _summary_from_text(text: str) -> str:
    """Bounded, single-line summary: never the raw page body."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return "(no readable text on page)"
    masked = _mask_value(collapsed)
    return masked[:MAX_SUMMARY_CHARS]


def _finalize_read(
    kind: str,
    url: str,
    result: Dict[str, Any],
    tenant: str,
    audit: Optional[AuditLog],
    include_screenshot: bool,
) -> Dict[str, Any]:
    """Shared post-fetch path: mask, footprint, audit row, mask-only dict."""
    if not isinstance(result, dict):
        raise ReadActionError("read fetcher must return a dict")
    text = result.get("text") or ""
    screenshot = result.get("screenshot")
    screenshot_bytes = screenshot if isinstance(screenshot, bytes) else None
    if include_screenshot is False:
        screenshot_bytes = None

    summary = _summary_from_text(text)
    footprint = hashlib.sha256((str(text) or "").encode("utf-8")).hexdigest()[:16]
    detail = {
        "url": url,
        "summary_len": len(summary),
        "text_hash16": footprint,
        "screenshot": bool(screenshot_bytes),
    }
    seq = None
    if audit is not None:
        seq = audit.append(
            actor=tenant, action="read_" + kind, tenant=tenant,
            subject=url, detail=detail,
        )
    return {
        "kind": kind,
        "url": url,
        "status": "ok",
        "summary": summary,
        "text_chars": len(str(text)),
        "screenshot_png": screenshot_bytes,
        "audit_seq": seq,
    }


def _gate(kind: str, args: Dict[str, Any], allowed_domains: List[str],
          tenant: str, audit: Optional[AuditLog]) -> str:
    """Gate a read attempt; audited refusal with the reason preserved."""
    try:
        return resolve_action_target(kind, args, allowed_domains)
    except ReadGateError as exc:
        if audit is not None:
            audit.append(
                actor=tenant, action="read_refused", tenant=tenant,
                subject=kind, detail={"reason": str(exc)[:300], "kind": kind},
            )
        raise ReadActionError(str(exc)) from exc


def perform_read_action(
    kind: str,
    args: Dict[str, Any],
    allowed_domains: List[str],
    tenant: str,
    fetcher: Callable[[str], Dict[str, Any]],
    audit: Optional[AuditLog] = None,
    include_screenshot: bool = True,
) -> Dict[str, Any]:
    """Run one audited read: gate -> fetch -> mask -> return.

    ``fetcher(url)`` returns ``{"text": str, "screenshot": bytes|None}``; the
    executor never inspects the page beyond ``text`` and the screenshot blob.
    The returned dict is mask-only and safe for chat/model context.
    """
    url = _gate(kind, args, allowed_domains, tenant, audit)
    return _finalize_read(
        kind, url, fetcher(url), tenant, audit, include_screenshot,
    )


async def perform_read_action_async(
    kind: str,
    args: Dict[str, Any],
    allowed_domains: List[str],
    tenant: str,
    fetcher: Callable[[str], Any],
    audit: Optional[AuditLog] = None,
    include_screenshot: bool = True,
) -> Dict[str, Any]:
    """Async twin of ``perform_read_action``: awaits a coroutine fetcher.

    The gate, masking, footprint, and audit-row behavior is identical; only
    the fetch step awaits instead of blocking on the caller's loop.
    """
    url = _gate(kind, args, allowed_domains, tenant, audit)

    async def _fetch(url: str) -> Dict[str, Any]:
        result = fetcher(url)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise ReadActionError("read fetcher must return a dict")
        return result

    return _finalize_read(
        kind, url, await _fetch(url), tenant, audit, include_screenshot,
    )


__all__ = [
    "perform_read_action",
    "perform_read_action_async",
    "ReadActionError",
    "_mask_dict",
    "_summary_from_text",
]