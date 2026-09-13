import pytest
from unittest.mock import AsyncMock, patch
from src.services.mcp_client import MCPClientManager
from src.services.browserless import scrape_rendered_page


@pytest.mark.asyncio
async def test_mcp_client_manager_init():
    manager = MCPClientManager()
    assert "web-browser" in manager._servers
    assert manager._servers["web-browser"].endswith("/sse")


@pytest.mark.asyncio
async def test_scrape_rendered_page_invalid_url():
    res = await scrape_rendered_page("not-a-valid-url")
    assert "error" in res
    assert "Invalid URL" in res["error"]


@pytest.mark.asyncio
async def test_scrape_rendered_page_mcp_success():
    mock_mcp_res = {
        "status": "success",
        "content": "Rendered Dynamic Content from MCP Web Browser",
        "is_error": False,
    }
    with patch("src.services.mcp_client.mcp_manager.call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_mcp_res
        res = await scrape_rendered_page("https://example.com/jobs")
        assert res.get("status") == "success"
        assert res.get("provider") == "mcp-web-browser"
        assert "Rendered Dynamic Content" in res.get("text", "")


@pytest.mark.asyncio
async def test_scrape_rendered_page_fallback_on_mcp_failure():
    with patch("src.services.mcp_client.mcp_manager.call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"status": "error", "error": "MCP service offline"}
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_http:
            mock_http.return_value.status_code = 200
            mock_http.return_value.text = "<html><body>Fallback direct scraped text</body></html>"
            res = await scrape_rendered_page("https://example.com/calendar")
            assert res.get("status") == "success"
            assert res.get("provider") == "browserless-fallback"
            assert "Fallback direct scraped text" in res.get("text", "")
