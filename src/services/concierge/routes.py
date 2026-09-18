"""FastAPI capture page (B0: token-gated GET + POST, no-body-logging.



Follows the /linkbank/{session_id} precedent: GET renders the form from the
stored intent (token NOT consumed); POST redeems once, stores vault fields
encrypted, discards ephemeral fields, and returns a MASK-ONLY success object.

 No submitted value ever appears in a response, log, error, or audit:
handlers never print POST bodies, and error text carries only exception messages
(which name labels, never values).

The router is mountable on any FastAPI app (tests mount it on a bare app;
production wires it via ``register_capture_routes`` on ``src.core.state.app``).
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from src.security.vault import Vault, DEFAULT_DB_PATH as _VAULT_DB
from src.services.concierge.capture_schema import CaptureIntentError, build_capture_page
from src.services.concierge.capture_submit import process_capture_submission
from src.services.concierge.capture_tokens import CaptureTokenError, CaptureTokenStore
from src.services.concierge.browser_sessions import BrowserSessionError, BrowserSessionStore

capture_router = APIRouter(prefix="/concierge/capture")
vnc_router = APIRouter(prefix="/concierge/vnc")


def _vnc_session(session_id: str):
    try:
        session = BrowserSessionStore(_VAULT_DB).get(session_id)
    except BrowserSessionError:
        return None
    # A completed login remains attached to its headed Chromium process so
    # agentic actions and the user's VNC view share the same browser.
    return session if session.get("status") in {"open", "done"} else None


@vnc_router.get("/{session_id}")
async def vnc_entry(session_id: str):
    if _vnc_session(session_id) is None:
        return JSONResponse({"detail": "VNC session not found or closed"}, status_code=404)
    return RedirectResponse("/concierge/vnc/" + session_id + "/vnc.html")


@vnc_router.get("/{session_id}/{asset_path:path}")
async def vnc_http_proxy(session_id: str, asset_path: str, request: Request):
    """Proxy noVNC HTTP assets to the tenant's disposable browser container."""
    session = _vnc_session(session_id)
    if session is None:
        return JSONResponse({"detail": "VNC session not found or closed"}, status_code=404)
    target = "http://" + session["container_name"] + ":6080/" + asset_path
    if request.url.query:
        target += "?" + request.url.query
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "connection"}
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            upstream = await client.request(request.method, target, headers=headers)
    except httpx.HTTPError:
        return JSONResponse({"detail": "VNC session is unavailable"}, status_code=502)
    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in {"content-length", "connection", "transfer-encoding"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@vnc_router.websocket("/{session_id}/{asset_path:path}")
async def vnc_websocket_proxy(session_id: str, asset_path: str, websocket: WebSocket):
    """Tunnel noVNC's websocket through the existing HTTPS reverse proxy."""
    session = _vnc_session(session_id)
    if session is None:
        await websocket.close(code=1008, reason="VNC session not found or closed")
        return
    await websocket.accept()
    target = "ws://" + session["container_name"] + ":6080/" + asset_path
    if websocket.url.query:
        target += "?" + websocket.url.query
    try:
        async with websockets.connect(
            target,
            open_timeout=10,
            close_timeout=5,
            ping_interval=None,
            max_size=None,
        ) as upstream:
            async def browser_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            async def client_to_browser():
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            tasks = [
                asyncio.create_task(browser_to_client()),
                asyncio.create_task(client_to_browser()),
            ]
            _done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            # Always retrieve both task results.  In particular, websockify
            # may close without a WebSocket close frame during a VNC auth
            # failure; leaving that exception unobserved makes the proxy look
            # alive while the noVNC canvas is already dead.
            await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, websockets.WebSocketException, OSError):
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


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
    # The form action must carry the tenant: it is an absolute path, so the
    # ?tenant= from the page URL would otherwise be dropped on submit (the
    # bot never sets a cookie, so the query param is the tenant channel).
    action = "/concierge/capture/" + token + "?tenant=" + tenant
    return HTMLResponse(_html(
        page["label"],
        "<h2>" + page["label"] + "</h2>"
        + "<p><em>" + page["explanation"] + "</em></p>"
        + "<p>Used only on: <strong>" + scope + "</strong></p>"
        + "<form method=\"POST\" action=\"" + action + "\">"
        + "".join(fields_html)
        + "<button type=\"submit\">Submit</button></form>"
    ))


@capture_router.post("/{token}")
async def submit_capture(token: str, request: Request):
    store = request.app.state.concierge_token_store
    vault = request.app.state.concierge_vault
    tenant = _resolve_tenant(request)
    ctype = request.headers.get("content-type", "").lower()
    # The capture page is a plain HTML form: accept urlencoded submissions as
    # the browser sends them, alongside JSON for API/test clients.
    try:
        if "application/json" in ctype:
            body = await request.json()
        else:
            body = dict(await request.form())
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid request body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"}, status_code=400)
    try:
        result = process_capture_submission(store, vault, token, tenant, body)
    except (CaptureTokenError, CaptureIntentError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # Mask-only audit row (invariant 10): labels + refs, never values.
    try:
        audit = request.app.state.concierge_audit
    except AttributeError:
        from src.services.concierge.audit import AuditLog
        from src.security.vault import DEFAULT_DB_PATH as _VAULT_DB
        audit = AuditLog(_VAULT_DB)
    detail = {
        "kind": result.get("kind"),
        "stored": [s["vault_ref"] for s in result.get("stored", [])],
        "ephemeral": result.get("ephemeral_fields", []),
    }
    try:
        audit.append(actor=tenant, action="capture_stored", tenant=tenant,
                     subject="capture", detail=detail)
    except Exception:
        pass
    if "application/json" in ctype:
        return JSONResponse(result)
    return HTMLResponse(_html(
        "Submitted",
        "<h2>Submitted ✓</h2>"
        + "<p>Credentials stored encrypted at rest and reusable for future "
        + "logins on " + ", ".join(result["consumer_scope"]) + ". "
        + "You can close this tab now.</p>",
    ))


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
    app.include_router(vnc_router)


__all__ = ["capture_router", "register_capture_routes"]
