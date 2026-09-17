"""CaptureIntent schema validation (B0).

One generic engine: any validated CaptureIntent renders the same page.  Rules:
unknown field types rejected, never coerced; tenant/token/delivery_mode can
never be model-supplied; card_cvv/captcha are forced ephemeral; card types
only under kind=payment; consumer_scope is a validated domain list, immutable
after capture.  session_profile is NOT a form intake (noVNC flow instead).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from src.security.vault import (
    FIELD_TYPES,
    FORCED_EPHEMERAL,
    KIND_DELIVERY_MODE,
    KINDS,
    PAYMENT_ONLY_FIELDS,
)

MODEL_PAYLOAD_KEYS = frozenset({"label", "explanation", "fields", "kind", "consumer_scope"})
FIELD_DECL_KEYS = frozenset({"type", "label", "required", "retention"})
MAX_FIELDS = 12
FORM_KINDS = ("secret", "payment")  # session_profile uses the noVNC profile flow


class CaptureIntentError(ValueError):
    pass


_DOMAIN_RE = re.compile(r"^[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?(\.[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?)*$")


def _check_domain(d: str) -> bool:
    d = d.lower()
    return bool(d) and len(d) <= 253 and ".." not in d and bool(_DOMAIN_RE.match(d))


def validate_capture_intent(data: Any, tenant: str) -> Dict[str, Any]:
    """Validate a model-proposed CaptureIntent into a normalized intent dict.

    ``tenant`` is caller-derived (interaction-derived identity) and can never
    be overridden from the payload.
    """
    if not isinstance(data, dict):
        raise CaptureIntentError("capture intent must be a JSON object")
    unknown = set(data.keys()) - MODEL_PAYLOAD_KEYS
    if unknown:
        raise CaptureIntentError(
            "capture intent declares disallowed keys "
            + ", ".join(sorted(unknown))
            + " (tenant, token, delivery_mode are never model-supplied)"
        )
    if not isinstance(tenant, str) or not tenant.strip():
        raise CaptureIntentError("capture intent requires a caller-derived tenant")

    label = str(data.get("label") or "").strip()
    if not label or len(label) > 200:
        raise CaptureIntentError("capture intent label must be 1-200 chars")
    explanation = str(data.get("explanation") or "").strip()
    if len(explanation) > 2000:
        raise CaptureIntentError("capture intent explanation must be <= 2000 chars")

    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise CaptureIntentError("capture intent kind must be one of " + ", ".join(sorted(KINDS)))
    if kind not in FORM_KINDS:
        raise CaptureIntentError(
            "kind " + kind + " is not a form intake: session profiles are captured "
            "via the noVNC login flow, not the generic form"
        )

    fields_in = data.get("fields")
    if not isinstance(fields_in, list) or not fields_in:
        raise CaptureIntentError("capture intent fields must be a non-empty array")
    if len(fields_in) > MAX_FIELDS:
        raise CaptureIntentError("capture intent fields must have <= " + str(MAX_FIELDS) + " entries")

    fields: List[Dict[str, Any]] = []
    seen_labels: set[str] = set()
    for i, raw in enumerate(fields_in):
        if not isinstance(raw, dict):
            raise CaptureIntentError("field " + str(i) + " must be an object")
        unknown = set(raw.keys()) - FIELD_DECL_KEYS
        if unknown:
            raise CaptureIntentError(
                "field " + str(i) + " declares disallowed keys " + ", ".join(sorted(unknown))
            )
        ftype = str(raw.get("type") or "").strip().lower()
        if ftype not in FIELD_TYPES:
            raise CaptureIntentError(
                "field " + str(i) + " type " + ftype + " is unknown (rejected, never coerced)"
            )
        flabel = str(raw.get("label") or ftype).strip()
        if not flabel or len(flabel) > 120:
            raise CaptureIntentError("field " + str(i) + " label must be 1-120 chars")
        flabel_key = flabel.lower()
        if flabel_key in seen_labels:
            raise CaptureIntentError("duplicate field label " + flabel)
        seen_labels.add(flabel_key)
        retention = str(raw.get("retention") or "vault").strip().lower()
        if retention not in ("vault", "ephemeral"):
            raise CaptureIntentError("field " + str(i) + " retention must be vault|ephemeral")
        if ftype in FORCED_EPHEMERAL and retention == "vault":
            raise CaptureIntentError(
                "field " + str(i) + " type " + ftype + " is forced ephemeral and can never be vaulted"
            )
        if ftype in PAYMENT_ONLY_FIELDS and kind != "payment":
            raise CaptureIntentError("field " + str(i) + " type " + ftype + " requires kind=payment")
        fields.append({
            "key": ftype + "|" + flabel,
            "type": ftype,
            "label": flabel,
            "required": bool(raw.get("required", True)),
            "retention": retention,
        })

    scope_in = data.get("consumer_scope")
    if not isinstance(scope_in, list) or not scope_in:
        raise CaptureIntentError("capture intent consumer_scope must be a non-empty array of domains")
    consumer_scope: List[str] = []
    for d in scope_in:
        d = str(d or "").strip().lower().lstrip(".")
        if not _check_domain(d):
            raise CaptureIntentError("capture intent consumer_scope domain " + d + " is invalid")
        consumer_scope.append(d)

    return {
        "label": label,
        "explanation": explanation,
        "kind": kind,
        "delivery_mode": KIND_DELIVERY_MODE[kind],
        "consumer_scope": consumer_scope,
        "fields": fields,
    }


def build_capture_page(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Render data for the generic page (masks/structure only, no values ever)."""
    return {
        "label": intent["label"],
        "explanation": intent["explanation"],
        "kind": intent["kind"],
        "delivery_mode": intent["delivery_mode"],
        "consumer_scope": list(intent["consumer_scope"]),
        "fields": [
            {"key": f["key"], "label": f["label"], "type": f["type"], "required": f["required"]}
            for f in intent["fields"]
        ],
    }


__all__ = [
    "CaptureIntentError",
    "validate_capture_intent",
    "build_capture_page",
    "MODEL_PAYLOAD_KEYS",
    "FIELD_DECL_KEYS",
    "MAX_FIELDS",
    "FORM_KINDS",
]