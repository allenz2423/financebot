import pytest
from unittest.mock import AsyncMock

from src.services.llm import _configured_llm_provider


@pytest.mark.parametrize("value, expected", [
    ("ollama", "ollama"),
    (" OPENAI ", "openai"),
])
def test_configured_llm_provider_normalizes_supported_provider(value, expected):
    assert _configured_llm_provider(value, setting="LLM_PROVIDER") == expected


@pytest.mark.parametrize("value", ["", "openrouter", "kev", "unknown"])
def test_configured_llm_provider_rejects_unroutable_provider(value):
    with pytest.raises(ValueError, match="LLM_PROVIDER must be 'ollama' or 'openai'"):
        _configured_llm_provider(value, setting="LLM_PROVIDER")


@pytest.mark.asyncio
async def test_invalid_provider_fails_before_history_summarization_is_scheduled(
    monkeypatch,
):
    import src.services.llm as llm

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    schedule = AsyncMock()
    monkeypatch.setattr(llm, "schedule_work", schedule)

    assert await llm._summarize_history_block("bounded test context") == ""
    schedule.assert_not_awaited()
