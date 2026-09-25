import asyncio
import pytest

from src.services.search import search_merchant


def _run(coro):
    return asyncio.run(coro)


def test_merchant_search_rejects_processor_only_result(monkeypatch):
    calls = []

    async def fake_search(query, **kwargs):
        calls.append((query, kwargs))
        return {
            "query": query,
            "results": [{
                "title": "Apple Community Apple Pay support",
                "snippet": "AplPay payment descriptor help",
                "url": "https://discussions.apple.com/thread/1",
            }],
        }

    monkeypatch.setattr("src.services.search.search_searxng", fake_search)
    result = _run(search_merchant("AplPay WE ARE SISTERBROOKLYN", scrape=False))
    assert result["identity_status"] == "NO_IDENTITY"
    assert result["results"] == []
    assert calls[0][0] == '"we are sisterbrooklyn"'
    assert calls[0][1]["preserve_exact_quotes"] is True


def test_merchant_search_accepts_exact_identity(monkeypatch):
    async def fake_search(query, **kwargs):
        return {
            "query": query,
            "results": [{
                "title": "We Are Sisterbrooklyn grocery market",
                "snippet": "About We Are Sisterbrooklyn",
                "url": "https://wearesisterbrooklyn.example/about",
                "engines": ["bing"],
            }],
            "time_range": None,
        }

    monkeypatch.setattr("src.services.search.search_searxng", fake_search)
    result = _run(search_merchant("AplPay WE ARE SISTERBROOKLYN", scrape=False))
    assert result["identity_status"] == "STRONG_IDENTITY"
    assert result["merchant_candidate"] == "we are sisterbrooklyn"
    assert len(result["results"]) == 1


def test_merchant_search_does_not_accept_generic_word(monkeypatch):
    async def fake_search(query, **kwargs):
        return {"results": [{"title": "The official store", "snippet": "Retail website", "url": "https://example.com"}]}

    monkeypatch.setattr("src.services.search.search_searxng", fake_search)
    result = _run(search_merchant("AplPay STORE", scrape=False))
    assert result["identity_status"] == "NO_IDENTITY"


@pytest.mark.parametrize("descriptor", ["", "   ", "AplPay", "PayPal"])
def test_empty_or_processor_only_descriptor_is_safe(monkeypatch, descriptor):
    async def fake_search(query, **kwargs):
        return {"results": []}

    monkeypatch.setattr("src.services.search.search_searxng", fake_search)
    result = _run(search_merchant(descriptor, scrape=False))
    assert result["identity_status"] == "NO_IDENTITY"
    assert result["results"] == []


def test_search_failure_does_not_create_evidence(monkeypatch):
    async def fake_search(query, **kwargs):
        raise RuntimeError("SearXNG offline")

    monkeypatch.setattr("src.services.search.search_searxng", fake_search)
    result = _run(search_merchant("AplPay SISTERBROOKLYN", scrape=False))
    assert result["identity_status"] == "NO_IDENTITY"
    assert "no_strong_identity_evidence" in result["identity_reasons"]
