import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def memory_store(tmp_path, monkeypatch):
    """Use a legacy-shaped DB under tmp_path; never open data/finances.db."""
    db_path = tmp_path / "memory-test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE delilah_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                category TEXT NOT NULL DEFAULT 'general',
                content TEXT NOT NULL,
                importance TEXT DEFAULT 'normal',
                source TEXT DEFAULT 'bot',
                entity_key TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)

    from src.core import state

    monkeypatch.setattr(state, "DB_PATH", str(db_path))
    from src.db import memory

    monkeypatch.setattr(memory, "DB_PATH", str(db_path))
    memory.init_epistemic_memory_schema()
    return memory, db_path


@pytest.fixture
def deterministic_embedding(memory_store):
    memory, _ = memory_store
    with patch.object(memory, "_get_embedding", new=AsyncMock(return_value=[1.0, 0.0])) as embed:
        yield embed


def _rows(db_path, query, params=()):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(query, params).fetchall()


def test_schema_upgrade_is_idempotent_for_legacy_database(memory_store):
    memory, db_path = memory_store

    memory.init_epistemic_memory_schema()
    columns = {row[1] for row in _rows(db_path, "PRAGMA table_info(delilah_memories)")}

    assert {"memory_type", "provenance_type", "confidence", "evidence_refs", "embedding"} <= columns
    assert {"expires_at", "sensitivity", "dedupe_key"} <= columns


@pytest.mark.asyncio
async def test_save_defaults_to_no_expiry_and_no_sensitivity(memory_store, deterministic_embedding):
    memory, db_path = memory_store

    await memory.save_epistemic_memory("owner-a", "Keep the default memory.")

    row = _rows(
        db_path,
        "SELECT expires_at, sensitivity, dedupe_key FROM delilah_memories WHERE user_id = ?",
        ("owner-a",),
    )[0]
    assert row == (None, None, None)


@pytest.mark.asyncio
async def test_explicit_expiry_and_sensitivity_round_trip(memory_store, deterministic_embedding):
    memory, db_path = memory_store

    await memory.save_epistemic_memory(
        "owner-a",
        "Prefers a quiet workspace.",
        memory_type="preference",
        provenance_type="user_stated",
        confidence=1.0,
        expires_at="2099-02-03T04:05:06+00:00",
        sensitivity="personal",
    )

    row = _rows(
        db_path,
        "SELECT expires_at, sensitivity FROM delilah_memories WHERE user_id = ?",
        ("owner-a",),
    )[0]
    assert row == ("2099-02-03 04:05:06", "personal")
    result = await memory.semantic_search_memory("owner-a", "quiet workspace")
    assert "Sensitivity: personal" in result
    assert "Prefers a quiet workspace." in result


@pytest.mark.asyncio
async def test_semantic_search_excludes_expired_memories(memory_store, deterministic_embedding):
    memory, _ = memory_store

    await memory.save_epistemic_memory(
        "owner-a", "Expired secret phrase", expires_at="2000-01-01T00:00:00Z"
    )
    await memory.save_epistemic_memory(
        "owner-a", "Current secret phrase", expires_at="2099-01-01T00:00:00Z"
    )

    result = await memory.semantic_search_memory("owner-a", "secret phrase")

    assert "Current secret phrase" in result
    assert "Expired secret phrase" not in result


@pytest.mark.asyncio
async def test_lesson_search_is_owner_scoped_and_excludes_other_memory_types(
    memory_store, deterministic_embedding
):
    memory, _ = memory_store
    await memory.save_epistemic_memory(
        "owner-a", "Verify the confirmation page after submission.",
        memory_type="lesson", dedupe_key="lesson-a",
    )
    await memory.save_epistemic_memory(
        "owner-a", "The user lives in Brooklyn.", memory_type="fact",
    )
    await memory.save_epistemic_memory(
        "owner-b", "Another user's procedure.", memory_type="lesson",
    )

    result = await memory.semantic_search_memory(
        "owner-a", "confirmation page", memory_type="lesson"
    )

    assert "Verify the confirmation page" in result
    assert "Brooklyn" not in result
    assert "Another user's procedure" not in result


@pytest.mark.asyncio
async def test_exact_source_fact_and_decision_search_are_type_scoped(
    memory_store, deterministic_embedding
):
    memory, _ = memory_store
    await memory.save_epistemic_memory(
        "owner-a", "[USER-STATED DECISION] I prefer no expiry.",
        memory_type="decision", provenance_type="user_stated",
        evidence_refs=["user_message:origin:offsets"],
    )
    await memory.save_epistemic_memory(
        "owner-a", "[WEB-RESEARCH FACT] Exact source quote.",
        memory_type="fact", provenance_type="web_research_quote",
        evidence_refs=["web_source:receipt:call", "source_url:https://example.org"],
    )
    await memory.save_epistemic_memory(
        "owner-a", "A workflow procedure.", memory_type="lesson",
    )

    result = await memory.semantic_search_memory(
        "owner-a", "source quote", memory_type=("fact", "decision")
    )

    assert "USER-STATED DECISION" in result
    assert "WEB-RESEARCH FACT" in result
    assert "user_message:origin:offsets" in result
    assert "source_url:https://example.org" in result
    assert "workflow procedure" not in result


@pytest.mark.asyncio
async def test_dedupe_is_owner_scoped_and_concurrency_safe(memory_store):
    memory, db_path = memory_store
    entered = 0
    both_entered = asyncio.Event()

    async def delayed_embedding(_text):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        return [1.0, 0.0]

    with patch.object(memory, "_get_embedding", new=delayed_embedding):
        await asyncio.gather(
            memory.save_epistemic_memory("owner-a", "First repeat", dedupe_key="task:42:lesson"),
            memory.save_epistemic_memory("owner-a", "Concurrent repeat", dedupe_key="task:42:lesson"),
        )
        # A later invocation (e.g. after re-entry/restart) is deduplicated by
        # the durable index, not by process-local state.
        await memory.save_epistemic_memory(
            "owner-a", "Repeat after restart", dedupe_key="task:42:lesson"
        )
        await memory.save_epistemic_memory(
            "owner-b", "Other owner lesson", dedupe_key="task:42:lesson"
        )

    rows = _rows(
        db_path,
        "SELECT user_id, content FROM delilah_memories WHERE dedupe_key = ? ORDER BY user_id",
        ("task:42:lesson",),
    )
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"owner-a", "owner-b"}
    assert sum(row[0] == "owner-a" for row in rows) == 1


@pytest.mark.asyncio
async def test_local_embedding_failure_does_not_fall_back_to_remote(memory_store, monkeypatch):
    memory, _ = memory_store
    from src.services import qdrant_client

    monkeypatch.setattr(qdrant_client, "_get_embedding_backend", lambda: "local")
    monkeypatch.setattr(qdrant_client, "_get_ollama_embed_url", lambda: "http://ollama.test/api/embed")
    with patch("httpx.AsyncClient", side_effect=RuntimeError("local unavailable")), \
         patch.object(qdrant_client, "get_embedding", new=AsyncMock()) as shared_embedding:
        vector = await memory._get_embedding("private memory text")

    assert vector == [0.0] * 768
    shared_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_cloud_embedding_uses_shared_configured_stack(memory_store, monkeypatch):
    memory, _ = memory_store
    from src.services import qdrant_client

    monkeypatch.setattr(qdrant_client, "_get_embedding_backend", lambda: "cloud")
    with patch.object(
        qdrant_client, "get_embedding", new=AsyncMock(return_value=[0.5, 0.5])
    ) as shared_embedding:
        vector = await memory._get_embedding("explicitly remote embedding")

    assert vector == [0.5, 0.5]
    shared_embedding.assert_awaited_once_with("explicitly remote embedding")
