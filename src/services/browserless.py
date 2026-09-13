"""
Browserless deep scraping helper for Delilah Financial OS.
Offloads JavaScript rendering and screenshot capture to the headless Chromium container.
"""

import os
import httpx
from typing import Dict, Any, Optional

BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://browserless:3000").rstrip("/")

async def scrape_rendered_page(url: str, wait_for_selector: Optional[str] = None, timeout_ms: int = 30000) -> Dict[str, Any]:
    """
    Renders a dynamic JS-heavy page via MCP Web Browser service (with graceful fallback).
    Extracts rendered HTML and text while bypassing simple client-side blocks.
    """
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "Invalid URL: Must start with http:// or https://"}

    # 1. Primary: Attempt execution via Model Context Protocol (MCP) service
    try:
        from src.services.mcp_client import mcp_manager
        mcp_res = await mcp_manager.call_tool(
            "browser_wait_and_scrape",
            {"url": url, "wait_for_selector": wait_for_selector, "timeout_ms": timeout_ms},
            server_name="web-browser",
            timeout_s=(timeout_ms / 1000) + 5,
        )
        if mcp_res.get("status") == "success" and not mcp_res.get("is_error"):
            content = mcp_res.get("content", "")
            if content and not content.startswith("Error"):
                return {
                    "url": url,
                    "status": "success",
                    "text": content[:8000],
                    "text_length": len(content),
                    "provider": "mcp-web-browser",
                }
    except Exception:
        pass

    # 2. Resilient Fallback: Direct Browserless HTTP invocation
    endpoint = f"{BROWSERLESS_URL}/content"
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
        async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 5) as client:
            resp = await client.post(endpoint, json=payload)
            if resp.status_code != 200:
                return {"error": f"Browserless returned HTTP {resp.status_code}: {resp.text[:300]}"}
            
            html = resp.text
            # Lightweight text extraction
            import re
            from html import unescape
            no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r"<[^>]+>", " ", no_scripts)
            clean_text = re.sub(r"\s+", " ", unescape(clean_text)).strip()
            
            return {
                "url": url,
                "status": "success",
                "text": clean_text[:8000],
                "text_length": len(clean_text),
                "provider": "browserless-fallback",
            }
    except Exception as exc:
        return {"error": f"Failed to scrape via Browserless: {type(exc).__name__}: {exc}"}
