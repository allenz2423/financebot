"""Capture submission path (B0: validate + store-or-discard, invariant 10).

The server-side handler shared by the FastAPI route and tests: validate the
POST body against the STORED intent, split by retention (ephemeral values are
discarded with no promotion path; vault values are encrypted into one vault
record), and return a response that contains masks + refs + labels ONLY —
the submitted value appears nowhere, even incidentally.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.security.vault import Vault, VaultError
from src.services.concierge.capture_schema import CaptureIntentError
from src.services.concierge.capture_tokens import CaptureTokenStore

MAX_VALUE_LENGTH = 4086


def validate_submission(intent: Dict[str, Any], submitted: Any) -> Dict[str, str]:
    """Validate the form POST body against the stored intent.

    Unknown submitted keys are rejected (never silently ignored — a coerced
    extra field could smuggle a value past the declared schema).  Error text
    names labels only, never values.
    """
    if not isinstance(submitted, dict):
        raise CaptureIntentError("capture submission must be a JSON object")
    declared = {f["key"]: f for f in intent["fields"]}
    unknown = set(submitted.keys()) - set(declared.keys())
    if unknown:
        raise CaptureIntentError(
            "capture submission contains undeclared fields " + ", ".join(sorted(unknown))
        )
    payload: Dict[str, str] = {}
    for key, spec in declared.items():
        raw = submitted.get(key, "")
        if not isinstance(raw, str):
            raw = str(raw)
        raw = raw.strip()
        if spec["required"] and not raw:
            raise CaptureIntentError("field '" + spec["label"] + "' is required")
        if len(raw) > MAX_VALUE_LENGTH:
            raise CaptureIntentError("field '" + spec["label"] + "' exceeds max length")
        if raw:
            payload[key] = raw
    return payload


def process_capture_submission(
    store: CaptureTokenStore,
    vault: Vault,
    token: str,
    tenant: str,
    submitted: Any,
) -> Dict[str, Any]:
    """Full server-side submission path: redeem once, validate, split retention.

    Returns a success object whose only credential-shaped content is masks and
    vault refs (invariant 10).  Raises CaptureTokenError/CaptureIntentError on
    any failure; failures never echo submitted values.
    """
    intent = store.redeem(token, tenant)
    payload = validate_submission(intent, submitted)

    ephemeral_labels: List[str] = []
    vault_fields: Dict[str, str] = {}
    for key, value in payload.items():
        _, flabel = key.split("|", 1)
        spec = next(f for f in intent["fields"] if f["key"] == key)
        if spec["retention"] == "ephemeral":
            ephemeral_labels.append(flabel)  # value discarded here, nothing stored
        else:
            vault_fields[key] = value

    stored: List[Dict[str, Any]] = []
    if vault_fields:
        try:
            rec = vault.store_fields(
                tenant=tenant,
                kind=intent["kind"],
                label=intent["label"],
                fields=vault_fields,
                consumer_scope=intent["consumer_scope"],
            )
        except VaultError as exc:
            raise CaptureIntentError(str(exc)) from exc
        stored.append({
            k: rec[k] for k in ("vault_ref", "kind", "delivery_mode", "display", "consumer_scope")
        })

    return {
        "ok": True,
        "kind": intent["kind"],
        "stored": stored,
        "ephemeral_fields": ephemeral_labels,  # labels only — no values, ever
        "consumer_scope": list(intent["consumer_scope"]),
    }


__all__ = ["validate_submission", "process_capture_submission", "MAX_VALUE_LENGTH"]