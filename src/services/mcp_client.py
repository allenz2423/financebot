"""
Model Context Protocol (MCP) Client Manager for Delilah Financial OS.

Provides asynchronous connection pooling, protocol negotiation, and robust
tool calling over stdio and SSE transports to decoupled MCP sidecars.
"""

import os
import logging
from typing import Any, Dict, List, Optional
import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger("delilah.mcp")

MCP_WEB_BROWSER_URL = os.getenv("MCP_WEB_BROWSER_URL", "http://mcp-web-browser:3000/sse")


class MCPClientManager:
    """Manages connections to remote MCP servers."""

    def __init__(self):
        self._servers: Dict[str, str] = {
            "web-browser": MCP_WEB_BROWSER_URL
        }

    async def is_server_available(self, server_name: str = "web-browser", timeout_s: float = 2.0) -> bool:
        """Lightweight health check against the MCP server endpoint."""
        url = self._servers.get(server_name)
        if not url:
            return False
        try:
            # Check the base server URL (e.g. http://mcp-web-browser:3000)
            base_url = url.split("/sse")[0]
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(f"{base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def list_tools(self, server_name: str = "web-browser") -> List[Dict[str, Any]]:
        """List all tools available on the specified MCP server."""
        url = self._servers.get(server_name)
        if not url:
            return []

        try:
            async with sse_client(url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "input_schema": getattr(tool, "input_schema", getattr(tool, "inputSchema", {})),
                        }
                        for tool in result.tools
                    ]
        except Exception as exc:
            logger.warning(f"[MCP] Failed to list tools from {server_name}: {exc}")
            return []

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        server_name: str = "web-browser",
        timeout_s: float = 35.0,
    ) -> Dict[str, Any]:
        """
        Executes a tool on the remote MCP server via SSE transport.
        Returns a dict containing 'status', 'content' or 'error'.
        """
        url = self._servers.get(server_name)
        if not url:
            return {"status": "error", "error": f"Unknown MCP server '{server_name}'"}

        try:
            async with sse_client(url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    res = await session.call_tool(tool_name, arguments=arguments)
                    
                    # Consolidate text content returned from the MCP tool
                    text_parts = []
                    for content in res.content:
                        if hasattr(content, "text"):
                            text_parts.append(content.text)
                        else:
                            text_parts.append(str(content))
                    
                    combined_text = "\n".join(text_parts).strip()
                    return {
                        "status": "success",
                        "content": combined_text,
                        "is_error": getattr(res, "isError", False),
                    }
        except Exception as exc:
            logger.error(f"[MCP] call_tool '{tool_name}' on {server_name} failed: {exc}")
            return {
                "status": "error",
                "error": f"MCP execution failed: {type(exc).__name__}: {exc}"
            }


# Singleton instance
mcp_manager = MCPClientManager()
