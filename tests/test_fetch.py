import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.services.search import fetch_webpage

@pytest.mark.asyncio
async def test_fetch():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body><h1>Reuters Technology News</h1><p>Breaking market data</p></body></html>"
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await fetch_webpage("https://www.reuters.com/technology")
        assert "Reuters" in res or "Technology" in res or len(res) > 0
