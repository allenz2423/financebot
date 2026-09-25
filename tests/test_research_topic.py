import asyncio

import pytest

from src.services.research_topic import build_research_packet


@pytest.mark.asyncio
async def test_research_packet_deduplicates_queries_and_urls():
    searched = []
    fetched = []

    async def search(query, **kwargs):
        searched.append(query)
        return {"results": [
            {"title": f"{query} source", "url": "https://example.com/a", "snippet": "a", "fused_score": 2},
            {"title": "second", "url": "https://example.com/b", "snippet": "b", "fused_score": 1},
        ]}

    async def fetch(url, **kwargs):
        fetched.append((url, kwargs["max_chars"]))
        return f"excerpt for {url}"

    packet = await build_research_packet(
        "alpha", ["alpha", "beta"], search_func=search, fetch_func=fetch,
        max_sources=3, fetch_top=2, max_chars=1234,
    )
    assert searched == ["alpha", "beta"]
    assert packet["source_count"] == 2
    assert packet["fetched_count"] == 2
    assert [source["source_id"] for source in packet["sources"]] == ["source_1", "source_2"]
    assert len(fetched) == 2
    assert all(size == 1234 for _, size in fetched)
    assert "[source_1]" in packet["evidence_text"]


@pytest.mark.asyncio
async def test_research_packet_preserves_search_and_fetch_failures():
    async def search(query, **kwargs):
        if query == "bad":
            raise RuntimeError("search down")
        return {"results": [{"title": "Good", "url": "https://example.com/good", "snippet": "ok"}]}

    async def fetch(url, **kwargs):
        raise TimeoutError("slow source")

    packet = await build_research_packet(
        "bad", ["good"], search_func=search, fetch_func=fetch,
        max_sources=2, fetch_top=1,
    )
    assert packet["status"] == "success"
    assert packet["search_errors"][0]["query"] == "bad"
    assert packet["sources"][0]["fetch_status"] == "failed"
    assert packet["sources"][0]["fetched"] is False


@pytest.mark.asyncio
async def test_research_packet_caps_queries_sources_fetches_and_chars():
    calls = {"search": 0, "fetch": 0}

    async def search(query, **kwargs):
        calls["search"] += 1
        return {"results": [
            {"title": str(i), "url": f"https://example.com/{query}/{i}", "snippet": "x"}
            for i in range(20)
        ]}

    async def fetch(url, **kwargs):
        calls["fetch"] += 1
        return "x" * 50000

    packet = await build_research_packet(
        queries=[f"q{i}" for i in range(20)], search_func=search, fetch_func=fetch,
        max_sources=100, fetch_top=100, max_chars=100000,
    )
    assert calls["search"] == 5
    assert packet["source_count"] == 8
    assert calls["fetch"] == 8
    assert all(len(source["excerpt"]) <= 12000 for source in packet["sources"])


@pytest.mark.asyncio
async def test_research_packet_no_query_is_explicitly_invalid():
    async def never(*args, **kwargs):
        raise AssertionError("should not run")

    packet = await build_research_packet(search_func=never, fetch_func=never)
    assert packet == {"status": "invalid", "error": "query or queries is required", "sources": []}


@pytest.mark.asyncio
async def test_research_packet_does_not_fetch_unrequested_sources():
    async def search(query, **kwargs):
        return {"results": [
            {"title": "one", "url": "https://example.com/1", "snippet": "1", "fused_score": 3},
            {"title": "two", "url": "https://example.com/2", "snippet": "2", "fused_score": 2},
        ]}

    fetched = []

    async def fetch(url, **kwargs):
        fetched.append(url)
        return "ok"

    packet = await build_research_packet("q", search_func=search, fetch_func=fetch, fetch_top=1)
    assert fetched == ["https://example.com/1"]
    assert packet["sources"][1]["fetch_status"] == "not_requested"


@pytest.mark.asyncio
async def test_research_packet_fetches_concurrently():
    active = 0
    maximum = 0

    async def search(query, **kwargs):
        return {"results": [
            {"title": str(i), "url": f"https://example.com/{i}", "snippet": "x", "fused_score": 10 - i}
            for i in range(4)
        ]}

    async def fetch(url, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return "ok"

    await build_research_packet("q", search_func=search, fetch_func=fetch, fetch_top=4)
    assert maximum > 1
