"""B2 VLM self-verify: post-execution assertion against screenshots.

After an approved act fires, we don't trust the model's own description of the
outcome. Instead we (optionally) run a local VLM over the post-action
screenshot and assert that key elements the model claimed would appear are
actually present in the rendered page — mirroring the B1 read-path
verification contract (``verify_claim``). If no VLM is available, we fall
back to a lightweight DOM-text assertion against the act's ``assert_text``
steps, which is still an improvement over blind trust.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.services.concierge.browser_driver import screenshot_page
from src.services.concierge.browser_gate import validate_target_url


def _expected_strings(steps: List[Dict[str, Any]]) -> List[str]:
    """Collect human-readable text from assert_text steps in a plan."""
    out: List[str] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("action") == "assert_text":
            sel = str(step.get("sel") or "")
            expected = str(step.get("value") or sel)
            if expected:
                out.append(expected)
    return out


async def _describe_image_b64(img_b64: str, max_tokens: int = 200) -> str:
    """Send a base64 PNG to the local Ollama vision path; return description."""
    import base64 as _b64
    import os as _os
    import httpx as _httpx
    from src.core.state import OLLAMA_URL, ADVISOR_MODEL

    raw = img_b64
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        async with _httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(
                OLLAMA_URL,
                json={
                    "model": ADVISOR_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are a careful screen reader. Describe what is visible in the attached screenshot."},
                        {"role": "user", "content": "What text and UI elements appear in this screenshot?", "images": [raw]},
                    ],
                    "stream": False,
                    "keep_alive": _os.getenv("MODEL_KEEP_ALIVE", "5m"),
                    "options": {
                        "temperature": 0.1,
                        "num_predict": max_tokens,
                    },
                    "think": False,
                },
            )
            if res.status_code != 200:
                return ""
            return res.json().get("message", {}).get("content", "").strip()
    except Exception:
        return ""


async def _vlm_verify(url: str, expected: List[str], timeout_ms: int = 30000) -> Dict[str, Any]:
    """Attempt VLM-based screen assertion; degrade gracefully to text."""
    if not expected:
        return {"verdict": "no_assertions", "confidence": 0.0, "detail": "no assert_text steps"}

    screenshot = await screenshot_page(url, timeout_ms=timeout_ms)
    if not screenshot:
        return {"verdict": "no_screenshot", "confidence": 0.0, "detail": "could not capture screenshot"}

    import base64 as _b64
    img_b64 = _b64.b64encode(screenshot).decode("ascii")
    description = await _describe_image_b64(img_b64)
    if not description:
        return {
            "verdict": "unverified-text-fallback",
            "confidence": 0.0,
            "detail": f"expected strings: {expected} (VLM unavailable)",
        }
    desc = description.lower()
    present = [s for s in expected if s.lower() in desc]
    absent = [s for s in expected if s.lower() not in desc]
    if not absent:
        return {
            "verdict": "verified",
            "confidence": 0.85,
            "detail": f"VLM asserts all expected strings present: {present}",
        }
    return {
        "verdict": "disputed",
        "confidence": 0.4,
        "detail": f"VLM found expected={present} missing={absent}",
    }


async def verify_act(
    url: str,
    steps: List[Dict[str, Any]],
    page_text: str = "",
    timeout_ms: int = 30000,
    allowed_domains: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Public entry: assert that expected outcomes are visible post-execution.

    ``allowed_domains`` (when given) re-gates the re-navigation target before
    the verification screenshot, so the post-execution browser window is held
    to the same SSRF/allowlist rules as the act itself.
    """
    if allowed_domains:
        validate_target_url(url, allowed_domains)
    expected = _expected_strings(steps)
    if expected and page_text:
        # Fast path: if all expected strings are already in the page text,
        # we don't need a VLM round-trip.
        lower = page_text.lower()
        if all(s.lower() in lower for s in expected):
            return {"verdict": "verified", "confidence": 0.9, "detail": "page text matches all assertions"}
    return await _vlm_verify(url, expected, timeout_ms=timeout_ms)


__all__ = ["verify_act"]
