import pytest
from unittest.mock import AsyncMock, patch
from src.services.negotiator import generate_negotiation_script

@pytest.mark.asyncio
async def test_negotiation_script_generation():
    mock_search = AsyncMock()
    mock_search.return_value = {
        "results": [
            {"snippet": "Get Spectrum Internet for only $49.99/mo for 12 months in 11201."}
        ]
    }
    with patch("src.services.negotiator.search_searxng", mock_search):
        res = await generate_negotiation_script(
            user_id='342385739952160769',
            service_name='Spectrum Internet',
            current_price=80.0,
            zip_code='11201'
        )
        
        assert "status" in res
        assert "script" in res
        
        script = res["script"]
        assert "NEGOTIATION DOSSIER" in script
        assert "Live Web Intelligence" in script
        assert "Level 1" in script
        assert "Level 3" in script
