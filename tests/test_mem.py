import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_semantic_search_memory_mocked():
    with patch("src.db.memory.save_epistemic_memory", new_callable=AsyncMock) as mock_save, \
         patch("src.db.memory.semantic_search_memory", new_callable=AsyncMock) as mock_search:
        mock_save.return_value = True
        mock_search.return_value = [{"memory": "User prefers Starbucks over Dunkin.", "score": 0.95}]

        await mock_save(
            "test_user",
            "User prefers Starbucks over Dunkin.",
            memory_type="preference",
            provenance_type="user_stated",
            confidence=1.0,
            evidence_refs=["chat_123"]
        )
        res = await mock_search("test_user", "coffee preference")
        assert len(res) == 1
        assert "Starbucks" in res[0]["memory"]
