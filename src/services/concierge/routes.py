"""FastAPI capture page (B0: token-gated GET + POST,, no-body-logging.



Follows the /linkbank/{session_id} precedent: GET renders the form from the
stored intent (token NOT consumed); POST redeems once, stores vault fields
encrypted, discards ephemeral fields, and returns a MASK-ONLY success object.

 No submitted value ever appears in a response, log, error,, or audit:
handlers never print POST bodies, and error text carries only exception messages
(which name labels,, never values).

The router is mountable on any FastAPI app (tests mount it on a bare app;
production wires it via ``register_capture_routes`` on ``src.core.state.app``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.security.vault import Vault, DEFAULT_DB_PATH as _VAULT_DB
from src.services.concierge.capture_schema import CaptureIntentError, build_capture_page
from src.services.concierge.capture_submit import process_capture_submission
from src.services.concierge.capture_tokens import CaptureTokenError, CaptureTokenStore

capture_router = APIRouter(prefix="/concierge/capture")


def _html(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"></head>"
        "<body style=\"font-family:sans-serif;max-width:520px;margin:40px auto;padding:0 16px;\">"
        + body + "</body></html>"
    )


def _input_type(field_type: str) -> str:
    if field_type == "password":
        return "password"
    if field_type == "url":
        return "url"
    return "text"


def _resolve_tenant(request: Request):
    """Interaction-derived tenant: signed cookie set at issue;dev query fallback."""
    cookie = request.cookies.get("concierge_tenant")
    if cookie:
        return cookie
    return request.query_params.get("tenant")


@capture_router.get("/{token}")
def serve_capture_page(token: str, request: Request):
    store = request.app.state.concierge_token_store
    tenant = _resolve_tenant(request)
    try:
        intent = store.peek(token, tenant)
    except CaptureTokenError:
        return HTMLResponse(
            _html("Invalid or expired session", "<h2>Invalid or expired session.</h2>"),
            status_code=404,
        )
    try:
        page = build_capture_page(intent)
    except CaptureIntentError:
        return HTMLResponse(
            _html("Invalid or expired session", "<h2>Invalid or expired session.</h2>"),
            status_code=404,
        )
    fields_html = []
    for f in page["fields"]:
        required = " required" if f["required"] else ""
        fields_html.append(
            "<label>" + f["label"] + "</label><br>"
            + "<input type=\"" + _input_type(f["type"]) + "\" name=\"" 
            + f["key"] + "\"" + required + " autocomplete=\"off\" "
            + "style=\"width:100%;padding:8px;margin:4px 0 12px;box-sizing:border-box;\"><br>"
        )
    scope = ", ".join(page["consumer_scope"])
    return HTMLResponse(_html(
        page["label"],
        "<h2>" + page["label"] + "</h2>"
        + "<p><em>" + page["explanation"] + "</em></p>"
        + "<p>Used only on: <strong>" + scope + "</strong></p>"
        + "<form method=\"POST\" action=\"/concierge/capture/" + token + "\">"
        + "".join(fields_html)
        + "<button type=\"submit\">Submit</button></form>"
    ))


@capture_router.post("/{token}")
async def submit_capture(token: str, request: Request):
    store = request.app.state.concierge_token_store
    vault = request.app.state.concierge_vault
    tenant = _resolve_tenant(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"}, status_code=400)
    try:
        result = process_capture_submission(store, vault, token, tenant, body)
    except (CaptureTokenError, CaptureIntentError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(result)


def register_capture_routes(app, store=None, vault=None) -> None:
    """Mount the capture router on an app, wiring state dependencies with
    production defaults (data/concierge.db)."""

    if store is None:
        store = CaptureTokenStore(_VAULT_DB)
    if vault is None:
        vault = Vault(_VAULT_DB)
    app.state.concierge_token_store = store
    app.state.concierge_vault = vault
    app.include_router(capture_router)


__all__ = ["capture_router", "register_capture_routes"]