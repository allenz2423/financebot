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

    # Verify cid1 exists in database with tx_retracted_at populated
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT tx_retracted_at FROM kg_claims WHERE claim_id = ?", (cid1,))
        retracted_at = c.fetchone()[0]
        assert retracted_at is not None

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

