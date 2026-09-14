import pytest
import sqlite3
import json
from src.core.state import DB_PATH
from src.services.world_model import (
    upsert_entity,
    resolve_entities,
    assert_claim,
    get_entity_subgraph,
    build_world_model_context,
    explain_claim
)

@pytest.fixture
def clean_test_entities():
    # Setup test entities
    upsert_entity(
        entity_id="test_org:acme_corp",
        entity_type="institution",
        canonical_name="Acme Corporation",
        aliases=["Acme", "Acme Inc"],
        attributes={"industry": "Logistics", "rating": "A"}
    )
    upsert_entity(
        entity_id="test_person:bob",
        entity_type="person",
        canonical_name="Bob Smith",
        aliases=["Bob"],
        attributes={"title": "Contractor"}
    )
    yield
    # Teardown
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM kg_claims WHERE subject_id LIKE 'test_%' OR object_id LIKE 'test_%'")
        c.execute("DELETE FROM kg_entities WHERE entity_id LIKE 'test_%'")
        c.execute("DELETE FROM kg_search_fts WHERE target_id LIKE 'test_%'")
        conn.commit()

def test_entity_resolution(clean_test_entities):
    matches = resolve_entities("I want to work at Acme Inc with Bob")
    assert "test_org:acme_corp" in matches
    assert "test_person:bob" in matches

def test_claim_assertion_and_bi_temporal_retraction(clean_test_entities):
    # 1. Assert initial claim
    cid1 = assert_claim(
        subject_id="test_person:bob",
        predicate="employed_by",
        object_id="test_org:acme_corp",
        provenance_type="DIRECT_OBSERVATION",
        source_authority=5,
        valid_from="2026-01-01 00:00:00"
    )
    
    # Verify cid1 is active
    subgraph = get_entity_subgraph(["test_person:bob"], depth=1)
    assert len(subgraph["claims"]) == 1
    assert subgraph["claims"][0]["claim_id"] == cid1

    # 2. Update claim (Bob leaves Acme)
    cid2 = assert_claim(
        subject_id="test_person:bob",
        predicate="employed_by",
        scalar_value="Resigned",
        provenance_type="USER_STATED",
        source_authority=4,
        valid_from="2026-09-01 00:00:00"
    )

    # Verify previous claim was retracted and new claim is active
    subgraph2 = get_entity_subgraph(["test_person:bob"], depth=1)
    assert len(subgraph2["claims"]) == 1
    assert subgraph2["claims"][0]["claim_id"] == cid2
    assert subgraph2["claims"][0]["scalar_value"] == "Resigned"

    # Verify cid1 was superseded (valid_to set) and is no longer current
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT tx_retracted_at, valid_to FROM kg_claims WHERE claim_id = ?", (cid1,))
        retracted_at, valid_to = c.fetchone()
        assert retracted_at is None
        assert valid_to is not None

def test_provenance_explainability(clean_test_entities):
    cid = assert_claim(
        subject_id="test_org:acme_corp",
        predicate="annual_budget",
        scalar_value="100000.00",
        provenance_type="DOCUMENT",
        source_authority=4,
        evidence_refs=["acme_annual_report_2026.pdf"]
    )

    explanation = explain_claim(cid)
    assert explanation["claim_id"] == cid
    assert explanation["provenance_type"] == "DOCUMENT"
    assert explanation["source_authority"] == 4
    assert "acme_annual_report_2026.pdf" in explanation["evidence_refs"]

def test_context_assembler_budget(clean_test_entities):
    ctx = build_world_model_context("Acme Corp budget query", max_tokens=180)
    assert "ACTIVE WORLD MODEL CONTEXT" in ctx
    assert "test_org:acme_corp" in ctx
    # Estimate token length (under 300 words guaranteed)
    word_count = len(ctx.split())
    assert word_count < 150

def test_deterministic_cash_flow_simulation(clean_test_entities):
    from src.services.world_model import simulate_deterministic_cash_flow
    result = simulate_deterministic_cash_flow(user_id="1", days_ahead=30)
    assert "starting_cash" in result
    assert "ending_cash" in result
    assert "minimum_projected_cash" in result
    assert result["days_projected"] == 30

def test_counterfactual_comparison(clean_test_entities):
    from src.services.world_model import run_counterfactual_comparison
    report = run_counterfactual_comparison(
        user_id="1",
        scenario_name="MacBook Cash Buyout Test",
        overrides={"starting_cash_delta": -4082.01},
        days_ahead=30
    )
    assert "COUNTERFACTUAL SCENARIO ANALYSIS" in report
    assert "Baseline Ending Cash" in report
    assert "Alternate Ending Cash" in report
    assert "Net Delta" in report

def test_audit_world_model_health():
    from src.services.world_model import audit_world_model_health, record_prediction
    pid = record_prediction(
        model_name="fws_cap_v1",
        target_entity="org:cuny_hpc",
        predicted_property="fws_cap_date",
        predicted_value=20261029.0,
        target_valid_time="2026-10-29",
        input_claim_ids=["claim_test_1"]
    )
    assert pid.startswith("pred_")

    health = audit_world_model_health()
    assert health["status"] == "HEALTHY"
    assert health["entity_count"] >= 5
    assert health["active_claims_count"] >= 5

def test_get_world_model_entity_and_dossier(clean_test_entities):
    from src.services.world_model import get_world_model_entity, get_world_model_dossier
    res = get_world_model_entity("Acme Corporation")
    assert "entity" in res
    assert res["entity"]["id"] == "test_org:acme_corp"
    assert "claims" in res

    # Check non-existent
    missing = get_world_model_entity("non_existent_entity_xyz")
    assert "error" in missing

    # Check dossier query
    dossier = get_world_model_dossier("cuny_aid_disbursement_2026")
    assert "doc_id" in dossier
    assert "CSI Fall 2026" in dossier["title"]

def test_search_world_model():
    from src.services.world_model import search_world_model
    results = search_world_model("CUNY aid tuition")
    assert len(results) > 0
    found_targets = [r["target_id"] for r in results]
    assert any("cuny" in t for t in found_targets)

def test_retract_world_model_claim(clean_test_entities):
    from src.services.world_model import assert_claim, retract_world_model_claim, get_entity_subgraph
    cid = assert_claim(
        subject_id="test_person:bob",
        predicate="security_clearance",
        scalar_value="Level 3",
        provenance_type="DIRECT_OBSERVATION",
        source_authority=5
    )
    # Verify active
    sub1 = get_entity_subgraph(["test_person:bob"])
    assert any(c["claim_id"] == cid for c in sub1["claims"])

    # Retract
    ret_res = retract_world_model_claim(cid)
    assert ret_res["status"] == "RETRACTED"

    # Verify no longer active in subgraph
    sub2 = get_entity_subgraph(["test_person:bob"])
    assert not any(c["claim_id"] == cid for c in sub2["claims"])

def test_parse_xml_and_dot_tool_calls():
    from src.services.llm import BOT_TOOLS_SCHEMA
    import re, json

    KNOWN_TOOLS = {tool["function"]["name"] for tool in BOT_TOOLS_SCHEMA}
    
    xml_sample = """
    Thinking about the query...
    <tool_call>
    <function=get_world_model_entity>
    <parameter=entity_id_or_name>cuny_hpc</parameter>
    </function>
    </tool_call>
    """

    # Mimic the parser logic
    xml_pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
    parsed_calls = []
    for match in re.finditer(xml_pattern, xml_sample, re.DOTALL):
        body = match.group(1).strip()
        fn_match = re.search(r"<function=([a-zA-Z0-9_]+)>(.*?)(?:</function>|$)", body, re.DOTALL)
        if fn_match:
            candidate = fn_match.group(1).strip()
            if candidate in KNOWN_TOOLS:
                args = {}
                fn_body = fn_match.group(2).strip()
                for p in re.finditer(r"<parameter=([a-zA-Z0-9_]+)>(.*?)(?:</parameter>|$)", fn_body, re.DOTALL):
                    args[p.group(1).strip()] = p.group(2).strip()
                parsed_calls.append({"function": {"name": candidate, "arguments": args}})

    assert len(parsed_calls) == 1
    assert parsed_calls[0]["function"]["name"] == "get_world_model_entity"
    assert parsed_calls[0]["function"]["arguments"]["entity_id_or_name"] == "cuny_hpc"


# ============================================================
# Vector DB consistency (multi-value owns, Qdrant sync, reconciliation)
# ============================================================

import asyncio

@pytest.fixture
def no_vector_network(monkeypatch):
    """Stub out Qdrant/embedding calls so background sync tasks never hit the network."""
    import src.services.qdrant_client as qclient

    async def fake_index(claim_id, subject_id, predicate, value, authority, user_id):
        return None

    async def fake_delete(claim_id, collection_name=None):
        return True

    monkeypatch.setattr(qclient, "index_single_claim", fake_index)
    monkeypatch.setattr(qclient, "delete_claim_point", fake_delete)
    return qclient

def _active_claims_for(subject_id, predicate):
    """Query the same path production retrieval uses: get_entity_subgraph,
    which filters on tx_retracted_at IS NULL AND valid_from <= now
    AND (valid_to IS NULL OR valid_to > now)."""
    sub = get_entity_subgraph([subject_id], depth=1)
    return {
        c["claim_id"]: c.get("scalar_value") or c.get("object_id")
        for c in sub["claims"]
        if c.get("predicate") == predicate
    }


def test_multi_value_owns_accumulates(clean_test_entities, no_vector_network):
    # Regression: 'owns' used to be single-slot, so asserting a second
    # possession silently closed the first's validity window. The old test
    # only checked tx_retracted_at IS NULL — which superseded claims still
    # satisfy — so it passed while the bug was live. Query the production
    # retrieval path instead.
    cid1 = assert_claim(
        subject_id="test_person:bob",
        predicate="owns",
        scalar_value="Clive Christian 1872 Masculine",
    )
    cid2 = assert_claim(
        subject_id="test_person:bob",
        predicate="owns",
        scalar_value="Creed Aventus",
    )
    assert cid1 != cid2

    active = _active_claims_for("test_person:bob", "owns")
    assert active == {cid1: "Clive Christian 1872 Masculine", cid2: "Creed Aventus"}

    # Re-asserting the same value stays idempotent — no duplicate claim.
    cid3 = assert_claim(
        subject_id="test_person:bob",
        predicate="owns",
        scalar_value="Creed Aventus",
    )
    assert cid3 == cid2
    active2 = _active_claims_for("test_person:bob", "owns")
    assert active2 == {cid1: "Clive Christian 1872 Masculine", cid2: "Creed Aventus"}


def test_single_slot_predicate_supersedes(clean_test_entities, no_vector_network):
    # A "one" cardinality predicate must close the prior claim's validity
    # window, so only the newest value is current.
    cid1 = assert_claim(
        subject_id="test_person:bob",
        predicate="security_clearance",
        scalar_value="Level 3",
    )
    cid2 = assert_claim(
        subject_id="test_person:bob",
        predicate="security_clearance",
        scalar_value="Level 5",
    )
    assert cid1 != cid2

    active = _active_claims_for("test_person:bob", "security_clearance")
    assert active == {cid2: "Level 5"}


def test_unknown_predicate_defaults_to_one(clean_test_entities, no_vector_network):
    # An unmapped predicate must behave single-slot — the safer failure mode
    # than silently accumulating contradictory claims.
    cid1 = assert_claim(
        subject_id="test_person:bob",
        predicate="totally_unknown_predicate",
        scalar_value="value_a",
    )
    cid2 = assert_claim(
        subject_id="test_person:bob",
        predicate="totally_unknown_predicate",
        scalar_value="value_b",
    )
    assert cid1 != cid2

    active = _active_claims_for("test_person:bob", "totally_unknown_predicate")
    assert active == {cid2: "value_b"}

async def test_supersession_does_not_delete_vector_points(clean_test_entities, no_vector_network, monkeypatch):
    # Under the new semantics, asserting a second claim on the same predicate
    # SUPERSEDES the first (valid_to set) — it does NOT retract it, so its vector
    # point must be KEPT (it's valid history). Only explicit retraction deletes.
    deleted = []

    async def recording_delete(claim_id, collection_name=None):
        deleted.append(claim_id)
        return True

    monkeypatch.setattr(no_vector_network, "delete_claim_point", recording_delete)

    cid1 = assert_claim(
        subject_id="test_person:bob",
        predicate="security_clearance",
        scalar_value="Level 3",
    )
    assert_claim(
        subject_id="test_person:bob",
        predicate="security_clearance",
        scalar_value="Level 5",
    )
    await asyncio.sleep(0)
    assert deleted == []

    from src.services.world_model import retract_world_model_claim
    cid3 = assert_claim(
        subject_id="test_person:bob",
        predicate="security_clearance",
        scalar_value="Level 7",
    )
    await asyncio.sleep(0)
    deleted.clear()
    res = retract_world_model_claim(cid3)
    assert res["status"] == "RETRACTED"
    await asyncio.sleep(0)
    assert cid3 in deleted

async def test_rag_context_filters_retracted_vector_hits(clean_test_entities, no_vector_network, monkeypatch):
    from src.services.world_model import build_semantic_world_model_context

    cid = assert_claim(
        subject_id="test_person:bob",
        predicate="favorite_fragrance",
        scalar_value="Clive Christian 1872",
    )
    await asyncio.sleep(0)

    async def fake_search(query, limit=5, user_id=None, collection_name=None):
        return [{
            "score": 0.9,
            "id": "point_stale",
            "payload": {
                "domain": "world_model_claim",
                "claim_id": cid,
                "subject_id": "test_person:bob",
                "predicate": "favorite_fragrance",
                "value": "Clive Christian 1872",
                "authority": 5,
            },
        }]

    monkeypatch.setattr(no_vector_network, "search_vectors", fake_search)

    # Claim is active: the vector hit should surface in RAG context.
    ctx_active = await build_semantic_world_model_context("what fragrance does bob own", max_tokens=200)
    assert "Clive Christian" in ctx_active

    # After retraction, a stale Qdrant point must NOT leak into RAG context.
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE kg_claims SET tx_retracted_at = datetime('now') WHERE claim_id = ?",
            (cid,),
        )
        conn.commit()
    ctx_retracted = await build_semantic_world_model_context("what fragrance does bob own", max_tokens=200)
    assert "Clive Christian" not in ctx_retracted

async def test_reconcile_user_vectors_converges(clean_test_entities, no_vector_network, monkeypatch):
    from src.services.qdrant_client import reconcile_user_vectors

    cid_active = assert_claim(
        subject_id="test_person:bob",
        predicate="favorite_fragrance",
        scalar_value="Creed Aventus",
    )
    cid_missing = assert_claim(
        subject_id="test_person:bob",
        predicate="owns",
        scalar_value="Tom Ford Oud Wood",
    )
    await asyncio.sleep(0)

    async def fake_scroll(user_id, collection_name=None):
        return [
            {"id": "stale-point-1", "claim_id": "claim_no_longer_exists", "domain": "world_model_claim"},
            {"id": "active-point-1", "claim_id": cid_active, "domain": "world_model_claim"},
            {"id": "snapshot-point", "claim_id": None, "domain": "financial_position"},
        ]

    deleted_ids = []

    async def fake_delete_batch(point_ids, collection_name=None):
        deleted_ids.extend(point_ids)
        return len(point_ids)

    reindexed = []

    async def fake_index(claim_id, subject_id, predicate, value, authority, user_id):
        reindexed.append(claim_id)

    monkeypatch.setattr(no_vector_network, "_scroll_user_point_ids", fake_scroll)
    monkeypatch.setattr(no_vector_network, "_delete_point_ids", fake_delete_batch)
    monkeypatch.setattr(no_vector_network, "index_single_claim", fake_index)

    result = await reconcile_user_vectors("test_person:bob")

    # Stale claim point deleted; snapshot point untouched.
    assert deleted_ids == ["stale-point-1"]
    # The missing active claim gets re-indexed; the already-indexed one does not.
    assert cid_missing in reindexed
    assert cid_active not in reindexed
    assert result["status"] == "RECONCILED"
    assert result["stale_deleted"] == 1
    assert result["active_claims"] >= 2



