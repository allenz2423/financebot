"""Safety and behavior tests for the Gmail-only batch prefetch path."""

import pytest
import src.services.llm as llm


def _call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


@pytest.mark.asyncio
async def test_prefetches_all_gmail_reads_in_batch(monkeypatch):
    seen = []

    def fake_run(name, args, user_id):
        seen.append((name, args, user_id))
        return f"result:{name}:{args.get('message_id', args.get('query', ''))}"

    monkeypatch.setattr(llm, "_run_gmail_read_sync", fake_run)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(llm.asyncio, "to_thread", fake_to_thread)
    batch = [
        _call("search_gmail", {"query": "internship", "max_results": 20}),
        _call("read_gmail_message", {"message_id": "m1"}),
        _call("read_gmail_message", {"message_id": "m2"}),
    ]

    results = await llm._prefetch_gmail_batch(batch, "user-1", allowed=True)

    assert list(results) == [0, 1, 2]
    assert results[1] == "result:read_gmail_message:m1"
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_prefetch_is_disabled_for_mixed_or_restricted_batches(monkeypatch):
    called = False

    def fake_run(name, args, user_id):
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(llm, "_run_gmail_read_sync", fake_run)
    gmail = [_call("read_gmail_message", {"message_id": "m1"})] * 2
    mixed = gmail + [_call("get_current_financial_position", {})]

    assert await llm._prefetch_gmail_batch(mixed, "user-1", allowed=True) == {}
    assert await llm._prefetch_gmail_batch(gmail, "user-1", allowed=False) == {}
    assert called is False
