"""user:current normalization (B0): the LLM-facing placeholder must resolve
onto the caller's real tenant partition so world-model isolation never hides
the claims. Runs against a throwaway knowledge-graph DB."""

import sqlite3

import pytest

import src.services.world_model as wm

SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases TEXT,
    attributes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kg_claims (
    claim_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_id TEXT,
    scalar_value TEXT,
    provenance_type TEXT NOT NULL,
    source_authority INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    tx_asserted_at TEXT DEFAULT (datetime('now')),
    tx_retracted_at TEXT,
    parent_claim_ids TEXT,
    evidence_refs TEXT,
    is_scenario INTEGER DEFAULT 0,
    immutable INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS kg_search_fts USING fts5(
    target_id UNINDEXED,
    target_type UNINDEXED,
    title,
    content,
    tags,
    tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS kg_dossiers (
    doc_id TEXT PRIMARY KEY,
    primary_entity_id TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


@pytest.fixture
def wm_db(tmp_path, monkeypatch):
    db = tmp_path / "wm.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(wm, "DB_PATH", str(db))
    monkeypatch.setattr(wm, "get_primary_user_id", lambda: "777")
    return str(db)


def _subjects(db, column, value):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT subject_id FROM kg_claims WHERE " + column + " = ?", (value,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _entity_ids(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT entity_id FROM kg_entities").fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_assert_claim_normalizes_user_current(wm_db):
    wm.assert_claim("user:current", "owns", object_id="uc_test:item_a", owner_user_id="777")
    assert _subjects(wm_db, "predicate", "owns") == ["user:777"]


def _prime(wm_db):
    """Mirror production: the entity is upserted (normalized), then claims attach."""
    wm.upsert_entity("user:current", "person", "Primary Human")


def test_claim_readable_under_real_tenant(wm_db):
    _prime(wm_db)
    wm.assert_claim("user:current", "owns", object_id="uc_test:item_b", owner_user_id="777")
    res = wm.get_world_model_entity("user:current", user_id="777")
    assert res["entity"]["id"] == "user:777"
    assert any(c["object_id"] == "uc_test:item_b" for c in res["claims"])


def test_other_tenant_cannot_read(wm_db):
    _prime(wm_db)
    wm.assert_claim("user:current", "owns", object_id="uc_test:item_c", owner_user_id="777")
    res = wm.get_world_model_entity("user:current", user_id="888")
    assert "error" in res


def test_fallback_to_primary_when_owner_omitted(wm_db):
    wm.assert_claim("user:current", "owns", object_id="uc_test:item_d")
    assert _subjects(wm_db, "object_id", "uc_test:item_d") == ["user:777"]


def test_upsert_entity_normalizes(wm_db):
    wm.upsert_entity("user:current", "person", "Primary Human")
    ids = _entity_ids(wm_db)
    assert "user:777" in ids
    assert "user:current" not in ids


def test_legacy_user_current_rows_are_claimed_by_the_caller(wm_db):
    conn = sqlite3.connect(wm_db)
    conn.execute(
        "INSERT INTO kg_entities (entity_id, entity_type, canonical_name) "
        "VALUES (?, 'person', 'Old Row')",
        ("user:current",),
    )
    conn.commit()
    conn.close()
    assert "user:777" in wm.resolve_entities("user:current", user_id="777")


def test_cross_tenant_visibility_of_normalized_claims(wm_db):
    _prime(wm_db)
    wm.assert_claim("user:current", "owns", object_id="uc_test:item_e", owner_user_id="777")
    assert "user:777" in wm.resolve_entities("user:current", user_id="777")
    assert "user:777" not in wm.resolve_entities("user:current", user_id="666")