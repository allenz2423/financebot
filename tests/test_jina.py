import pytest
from unittest.mock import AsyncMock, patch, MagicMock

@pytest.mark.asyncio
async def test_jina_search_mock():
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Sample Jina Search Content"
        mock_get.return_value = mock_response

        import httpx
        async with httpx.AsyncClient() as client:
            res = await client.get("https://s.jina.ai/latest tech news")
            assert res.text == "Sample Jina Search Content"
