"""Capture redaction pipeline (B0.

Policy (decided): secrets are redacted **in audit-stored screenshots**;
the live noVNC session sees them raw.  Redaction happens at capture, before
anything is written to the audit chain.  This module owns the value-level masks
(reused by capture responses and audit callers) and region-based screenshot
redaction for the B2/B3 visual gate.
"""

from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.security.vault import SECRET_FIELD_TYPES, mask_display


def is_secret_field(field_type: str) -> bool:
    return field_type in SECRET_FIELD_TYPES


def redact_value(field_type: str, value: Any) -> str:
    """Mask a field value for logs/audit/DM paths.  text/url pass through
    (they are not secret by type;their values never appear in audit anyway
    because payloads there are masks-only by construction)."""
    return mask_display(field_type, value)


def redact_payload(payload: Dict[str, str]) -> Dict[str, str]:
    """Map "<type>|<label>" -> masked value for audit/log consumption."""
    out: Dict[str, str] = {}
    for key, value in payload.items():
        ftype, label = key.split("|", 1) if "|" in key else (key, key)
        out[label] = redact_value(ftype, value)
    return out


def _hex(region: Sequence[int]) -> str:
    return ":".join(f"{int(v):02x}" for v in region)


def screenshot_region_from_field(field_type: str, origin: Optional[Sequence[int]] = None) -> List[Tuple[int, int, int, int]]:
    """Default redaction region guidance for a secret field (used by the visual
    gate later); origin is (x, y) top-left, returns [(x, y, w, h)...]."""

    x, y = (origin or (0, 0))
    if field_type == "card_pan":
        return [(x, y, 260, 28)]
    if field_type in ("card_cvv", "otp", "captcha", "password"):
        return [(x, y, 120, 28)]
    if field_type == "card_exp":
        return [(x, y, 90, 28)]
    return [(x, y, 200, 28)]


def redact_screenshot(bytes_data: bytes, regions: Sequence[Tuple[int, int, int, int]]):
    """Draw opaque boxes over ``regions`` (x, y, w, h) in an image.

    Returns new PNG bytes (the input is never mutated), and the region manifest
    with stable hashes for the audit chain."""

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise RuntimeError("PIL is required for screenshot redaction")

    img = Image.open(io.BytesIO(bytes_data)).convert("RGB")
    draw = ImageDraw.Draw(img)
    redacted: List[Dict[str, Any]] = []
    for region in regions:
        x, y, w, h = [max(0, int(v)) for v in region]
        draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
        redacted.append({"x": x, "y": y, "w": w, "h": h, "hash": _hex(region)})
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), redacted


def strip_otp_from_error(text: str) -> str:
    """Never let a submitted value surface in an error message: collapse any
    4-8 digit runs (OTP/captcha-ish) into a mask."""

    if not text:
        return text
    return re.sub(r"\b\d{4,8}\b", "••••", str(text))


__all__ = [
    "is_secret_field",
    "redact_value",
    "redact_payload",
    "screenshot_region_from_field",
    "redact_screenshot",
    "strip_otp_from_error",
]