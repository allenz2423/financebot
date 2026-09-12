import pytest
from unittest.mock import AsyncMock, patch
from src.services.search import search_searxng

@pytest.mark.asyncio
async def test_search_direct_url():
    res = await search_searxng("https://example.com/news/123", scrape=False)
    assert "direct_url" in res
    assert res["direct_url"] == "https://example.com/news/123"

@pytest.mark.asyncio
async def test_search_query_mocked():
    mock_results = [
        {
            "title": "Election Results 2024",
            "url": "https://example.com/results",
            "snippet": "Latest election results and coverage.",
            "engine": "bing",
            "engine_rank": 1,
        }
    ]
    with patch("src.services.search._searxng_engine_query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = mock_results
        res = await search_searxng("who won the 2024 election?", scrape=False)
        assert "results" in res
        assert len(res["results"]) > 0
        assert res["results"][0]["title"] == "Election Results 2024"
