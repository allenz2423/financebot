"""
Model Context Protocol (MCP) Web Browser Server.

Wraps headless Chromium / Browserless / Playwright into a standardized
MCP server exposing SSE and streamable HTTP endpoints for Delilah Financial OS.
"""

import os
import re
import logging
from html import unescape
from typing import Optional, Dict, Any

import httpx
from mcp.server.mcpserver import MCPServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-web-browser")

BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://browserless:3000").rstrip("/")
PORT = int(os.getenv("PORT", "3000"))

mcp = MCPServer("Delilah Web Browser")


def _clean_html_to_text(html: str) -> str:
    """Extract readable text from HTML, discarding script and style blocks."""
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", no_scripts)
    clean = re.sub(r"\s+", " ", unescape(clean)).strip()
    return clean


@mcp.tool()
async def browser_navigate(url: str, timeout_ms: int = 30000) -> str:
    """
    Navigates to a URL, waits for network idle, and returns extracted text.
    """
    if not url.lower().startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    endpoint = f"{BROWSERLESS_URL}/content?stealth=true"
    payload = {
        "url": url,
        "gotoOptions": {
            "waitUntil": "networkidle2",
            "timeout": timeout_ms
        }
    }

    try:
        async with httpx.AsyncClient(timeout=(timeout_ms / 1000) + 5) as client:
            resp = await client.post(endpoint, json=payload)
            if resp.status_code != 200:
                return f"Error: Browser returned HTTP {resp.status_code}: {resp.text[:300]}"
            text = _clean_html_to_text(resp.text)
            return text[:8000] if text else "Page rendered but contained no visible text."
    except Exception as exc:
        return f"Error navigating to {url}: {type(exc).__name__}: {exc}"


@mcp.tool()
async def browser_wait_and_scrape(url: str, wait_for_selector: Optional[str] = None, timeout_ms: int = 30000) -> str:
    """
    Renders dynamic JS-heavy pages, waits for a specific CSS selector, and returns page text.
    """
    if not url.lower().startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    endpoint = f"{BROWSERLESS_URL}/content?stealth=true"
    payload: Dict[str, Any] = {
        "url": url,
        "gotoOptions": {
            "waitUntil": "networkidle2",
            "timeout": timeout_ms
        }
    }
    if wait_for_selector:
        payload["waitForSelector"] = {"selector": wait_for_selector, "timeout": timeout_ms}

    try:
        async with httpx.AsyncClient(timeout=(timeout_ms / 1000) + 5) as client:
            resp = await client.post(endpoint, json=payload)
            if resp.status_code != 200:
                return f"Error: Browser returned HTTP {resp.status_code}: {resp.text[:300]}"
            text = _clean_html_to_text(resp.text)
            return text[:8000] if text else "Page rendered but contained no visible text."
    except Exception as exc:
        return f"Error rendering {url}: {type(exc).__name__}: {exc}"


@mcp.tool()
async def browser_extract_links(url: str, timeout_ms: int = 30000) -> str:
    """
    Renders page and extracts all hyperlinked URLs and link titles.
    """
    if not url.lower().startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    endpoint = f"{BROWSERLESS_URL}/content?stealth=true"
    payload = {
        "url": url,
        "gotoOptions": {
            "waitUntil": "networkidle2",
            "timeout": timeout_ms
        }
    }

    try:
        async with httpx.AsyncClient(timeout=(timeout_ms / 1000) + 5) as client:
            resp = await client.post(endpoint, json=payload)
            if resp.status_code != 200:
                return f"Error: Browser returned HTTP {resp.status_code}"
            
            # Extract links
            from urllib.parse import urljoin
            links = []
            for match in re.finditer(r'<a\s+(?:[^>]*?\s+)?href=(["\'])(.*?)\1[^>]*>(.*?)</a>', resp.text, re.DOTALL | re.IGNORECASE):
                href = match.group(2).strip()
                title = re.sub(r'<[^>]+>', '', match.group(3)).strip()
                if href and not href.startswith(("javascript:", "mailto:", "#")):
                    full_url = urljoin(url, href)
                    links.append(f"- {title or 'Link'}: {full_url}")
            
            return "\n".join(links[:50]) if links else "No links found on page."
    except Exception as exc:
        return f"Error extracting links from {url}: {type(exc).__name__}: {exc}"


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "service": "mcp-web-browser"})


if __name__ == "__main__":
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    logger.info(f"Starting MCP Web Browser Server on port {PORT} with Browserless backend at {BROWSERLESS_URL}...")
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.sse_app(transport_security=security, host="0.0.0.0")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
