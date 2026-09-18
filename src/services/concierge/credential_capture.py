"""Self-service credential capture issuer (closes the missing-link gap).

The capture page and token store existed, but nothing in production ever
issued a token — so a user could never actually store a login through the
bot. This is the sanctioned issuer: tenant-bound, the domain must be on the
tenant allowlist, the intent goes through the SAME ``capture_schema``
validation as the POST side, and the plaintext token appears ONLY in the
returned URL — audit rows (the caller's job) must carry domain/label, never
the token.

The URL carries the tenant as a query param because the capture page
resolves tenant from a cookie or the fallback query param (no cookie is
ever set by the bot).
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List

from src.services.concierge.browser_gate import domain_matches
from src.services.concierge.capture_schema import CaptureIntentError, validate_capture_intent
from src.services.concierge.capture_tokens import CaptureTokenStore, TOKEN_TTL
from src.services.concierge.tenants import TenantStore

DEFAULT_CAPTURE_URL = os.getenv("CONCIERGE_CAPTURE_URL", "http://127.0.0.1:8000")

# Bearer capture URLs are not persisted in plaintext. This process-local cache
# prevents retries from issuing a new link every time the model repeats itself
# during the same short capture window. A restart safely requires a new link.
_PENDING_CAPTURE_LINKS: Dict[tuple[str, str, str], tuple[float, str]] = {}


def forget_pending_capture(tenant: str, domain: str, db_path: str = "") -> None:
    _PENDING_CAPTURE_LINKS.pop(
        (str(tenant), str(domain).lower(), str(db_path)), None
    )


class CaptureRefused(ValueError):
    pass


# Labels land in the rendered HTML page, so the charset is hard-constrained
# (first char alnum) to keep model-supplied labels injection-free.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./+()-]{0,39}$")
_DENIED_LABEL_WORDS = ("card", "cvv", "pan", "ssn", "social", "pin",
                       "billing", "debit", "credit", "expiry", "iban", "routing")
# Secret-kind vaultable field types (payments excluded by the schema anyway).
_ALLOWED_FIELD_TYPES = frozenset({"text", "password", "otp", "token", "url"})
DEFAULT_FIELDS = [
    {"type": "text", "label": "Email", "required": True},
    {"type": "password", "label": "Password", "required": True},
]


def _normalize_domain(raw: Any) -> str:
    d = str(raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d


def _validate_fields(fields: Any) -> List[Dict[str, Any]]:
    """Validate model-supplied form fields for a credential capture.

    Raises ``CaptureRefused`` on: empty/oversized lists, unknown types,
    unsafe or blank labels, and payment/identity-shaped labels — the
    capture page is for login credentials, never card/SSN intake.
    """
    if not isinstance(fields, list) or not fields:
        raise CaptureRefused("capture requires at least one form field")
    if len(fields) > 6:
        raise CaptureRefused("capture supports at most 6 fields")
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(fields):
        if not isinstance(raw, dict):
            raise CaptureRefused("field " + str(i) + " must be an object")
        ftype = str(raw.get("type") or "").strip().lower()
        if ftype not in _ALLOWED_FIELD_TYPES:
            raise CaptureRefused(
                "field " + str(i) + " type must be one of "
                + ", ".join(sorted(_ALLOWED_FIELD_TYPES))
            )
        flabel = str(raw.get("label") or "").strip()
        if not flabel or not _LABEL_RE.match(flabel):
            raise CaptureRefused(
                "field " + str(i) + " label must be 1-40 safe characters"
            )
        low = flabel.lower()
        if any(w in low for w in _DENIED_LABEL_WORDS):
            raise CaptureRefused(
                "field " + str(i) + " label '" + flabel
                + "' looks like a payment/identity field — credential capture "
                "is for login credentials only"
            )
        out.append({
            "type": ftype,
            "label": flabel,
            "required": bool(raw.get("required", True)),
        })
    labels = [f["label"].lower() for f in out]
    if len(set(labels)) != len(labels):
        raise CaptureRefused("capture fields must have distinct labels")
    return out


def build_credential_capture(
    tenant: str,
    raw_domain: Any,
    tenants: TenantStore,
    tokens: CaptureTokenStore,
    base_url: str = "",
    fields: Any = None,
) -> Dict[str, Any]:
    """Validate + issue one credential-capture token for a tenant's own
    domain; returns the capture URL and mask-only metadata.

    ``fields`` is optional model-supplied form schema (label/type/required);
    when omitted it defaults to Email + Password. Raises ``CaptureRefused``
    for: a missing/invalid domain, a disabled tenant, a domain that is not
    on the tenant allowlist, unsafe/unknown fields, or a capture intent that
    fails schema validation. The plaintext token exists only inside the
    returned URL.
    """
    domain = _normalize_domain(raw_domain)
    if not domain:
        raise CaptureRefused(
            "capture requires a domain (for example, `!concierge creds example.com`)"
        )
    if not tenants.is_enabled(tenant):
        raise CaptureRefused(
            "concierge is not enabled for you yet (an admin must run `!concierge enable`)"
        )
    allowed = tenants.status(tenant).get("allow_domains") or []
    if not any(domain_matches(a, domain) for a in allowed):
        raise CaptureRefused(
            f"domain {domain!r} is not on your concierge allowlist "
            f"(allowed: {', '.join(sorted(allowed)) or 'none'})"
        )
    if not base_url:
        base_url = os.getenv("CONCIERGE_CAPTURE_URL") or DEFAULT_CAPTURE_URL
    capture_fields = _validate_fields(fields) if fields is not None else DEFAULT_FIELDS
    try:
        intent = validate_capture_intent({
            "label": domain + " login",
            "explanation": "Sign-in credentials stored for " + domain
                           + " — encrypted at rest, used only on " + domain + ".",
            "kind": "secret",
            "consumer_scope": [domain],
            "fields": capture_fields,
        }, tenant)
    except CaptureIntentError as exc:
        raise CaptureRefused(str(exc)) from exc
    cache_key = (str(tenant), domain, str(tokens.db_path))
    now = time.time()
    cached = _PENDING_CAPTURE_LINKS.get(cache_key)
    if cached and cached[0] > now:
        url = cached[1]
        return {
            "url": url,
            "domain": domain,
            "expires_seconds": max(1, int(cached[0] - now)),
            "reused": True,
        }
    token = tokens.issue(tenant, intent)
    url = f"{base_url.rstrip('/')}/concierge/capture/{token}?tenant={tenant}"
    _PENDING_CAPTURE_LINKS[cache_key] = (now + TOKEN_TTL.total_seconds(), url)
    return {
        "url": url,
        "domain": domain,
        "expires_seconds": int(TOKEN_TTL.total_seconds()),
        "reused": False,
    }


__all__ = [
    "build_credential_capture", "forget_pending_capture",
    "CaptureRefused", "DEFAULT_CAPTURE_URL",
]
