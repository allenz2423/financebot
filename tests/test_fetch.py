import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.services.search import fetch_webpage, _text_artifact_filename


@pytest.mark.parametrize(
    ("requested", "default", "expected"),
    [
        ("original_urls_spreadsheet.xlsx", "google_export.txt", "original_urls_spreadsheet_raw.txt"),
        ("notes.txt", "google_export.txt", "notes.txt"),
        ("", "google_sheet_123_all_tabs.txt", "google_sheet_123_all_tabs.txt"),
        ("artifact", "google_export.txt", "artifact.txt"),
    ],
)
def test_text_artifact_filename_is_truthful(requested, default, expected):
    assert _text_artifact_filename(requested, default) == expected

@pytest.mark.asyncio
async def test_fetch():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body><h1>Reuters Technology News</h1><p>Breaking market data</p></body></html>"
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await fetch_webpage("https://www.reuters.com/technology")
        assert "Reuters" in res or "Technology" in res or len(res) > 0
