"""Shadow tool-router instrumentation: retrieval, provenance, record math."""

import asyncio
import json

import pytest

import src.services.llm as llm
import src.services.tool_router as tr
from src.services.search import _google_public_export_url


@pytest.fixture(autouse=True)
def _clean_router():
    tr.reset_for_tests()
    yield
    tr.reset_for_tests()


# ---------------------------------------------------------------------------
# Catalog / cards
# ---------------------------------------------------------------------------

def test_tool_cards_cover_full_unique_catalog():
    cards = tr.tool_cards()
    names = [c["name"] for c in cards]
    assert len(names) == len(set(names))
    schema_names = {
        (e.get("function") or {}).get("name") for e in llm.BOT_TOOLS_SCHEMA
    }
    schema_names.discard(None)
    assert set(names) == schema_names
    assert tr.known_names() == schema_names
    assert all(c["class"] in ("read", "mutate", "conditional") for c in cards)


def test_google_public_export_detection_is_fetch_layer_only():
    doc = _google_public_export_url(
        "https://docs.google.com/document/d/abc-123/edit?tab=t.0"
    )
    assert doc == (
        "https://docs.google.com/document/d/abc-123/export?format=txt",
        "Google Docs text export",
    )
    sheet = _google_public_export_url(
        "https://docs.google.com/spreadsheets/d/sheet_123/edit?gid=42#gid=42"
    )
    assert sheet == (
        "https://docs.google.com/spreadsheets/d/sheet_123/export?format=csv&gid=42",
        "Google Sheets CSV export",
    )


def test_classify_partitions():
    assert tr.classify("run_python_sandbox") == "conditional"
    assert tr.classify("request_user_form") == "conditional"
    assert tr.classify("search_gmail") == "read"
    mutation_names = sorted(llm.MUTATION_TOOLS)
    assert mutation_names, "MUTATION_TOOLS unexpectedly empty"
    assert tr.classify(mutation_names[0]) == "mutate"


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def test_bm25_ranks_relevant_tool_high():
    cards = tr.tool_cards()
    bm = tr.BM25([c["name"] for c in cards], [c["text"] for c in cards])
    scores = bm.scores("search my email for the latest electric bill")
    ranked = sorted(scores, key=lambda n: scores[n], reverse=True)
    assert "search_gmail" in ranked[:10]


def test_minmax_is_per_query():
    assert tr._minmax({"a": 10.0, "b": 20.0}, ["a", "b"]) == {"a": 0.0, "b": 1.0}
    assert tr._minmax({"a": 5.0, "b": 5.0}, ["a", "b"]) == {"a": 0.0, "b": 0.0}


def test_propose_hybrid_uses_embeddings(monkeypatch):
    emb = {c["name"]: 0.0 for c in tr.tool_cards()}
    emb["search_gmail"] = 1.0

    async def _fake_emb(text, limit):
        return emb

    monkeypatch.setattr(tr, "index_ready", lambda: True)
    monkeypatch.setattr(tr, "_emb_scores", _fake_emb)

    out = asyncio.run(tr.propose("find my electric bill email", tr.mode_signature("ordinary", False)))
    assert out["method"] == "hybrid"
    assert out["fallback_reason"] is None
    assert out["raw_topk"][0]["name"] == "search_gmail"
    assert out["read_topk"] and all(tr.tool_class(n) in ("read", "conditional") for n in out["read_topk"])
    assert all(tr.tool_class(n) in ("mutate", "conditional") for n in out["write_topk"])


def test_propose_degrades_to_lexical_when_embedding_fails(monkeypatch):
    async def _boom(text, limit):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(tr, "index_ready", lambda: True)
    monkeypatch.setattr(tr, "_emb_scores", _boom)

    out = asyncio.run(tr.propose("find the tracking for my package"))
    assert out["method"] == "lexical"
    assert out["fallback_reason"].startswith("RuntimeError")
    assert out["raw_topk"], "lexical fallback must still rank candidates"


def test_propose_lexical_when_index_not_ready(monkeypatch):
    monkeypatch.setattr(tr, "index_ready", lambda: False)
    out = asyncio.run(tr.propose("what is my checking balance"))
    assert out["method"] == "lexical"
    assert out["fallback_reason"] == "index_not_ready"
    assert out["raw_topk"]


def test_schedule_index_is_non_blocking_and_cooldown_guarded(monkeypatch):
    calls = []

    async def _fake_build():
        calls.append(1)

    monkeypatch.setattr(tr, "ensure_index", _fake_build)
    monkeypatch.setattr(tr, "index_ready", lambda: False)

    # no running event loop -> no-op, never raises
    tr.schedule_index()
    assert tr._index_task is None

    async def _main():
        tr.schedule_index()
        assert tr._index_task is not None
        # a second call while the build is in flight must not spawn another
        tr.schedule_index()
        await tr._index_task

    asyncio.run(_main())
    assert calls == [1]


def test_schedule_index_noop_when_ready(monkeypatch):
    monkeypatch.setattr(tr, "index_ready", lambda: True)
    tr.schedule_index()
    assert tr._index_task is None


# ---------------------------------------------------------------------------
# Flag / sampling
# ---------------------------------------------------------------------------

def test_shadow_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("TOOL_ROUTER_SHADOW", raising=False)
    assert tr.shadow_enabled() is False
    monkeypatch.setenv("TOOL_ROUTER_SHADOW", "1")
    assert tr.shadow_enabled() is True
    monkeypatch.setenv("TOOL_ROUTER_SHADOW", "off")
    assert tr.shadow_enabled() is False


def test_live_router_is_explicitly_opt_in_and_never_expands_schema(monkeypatch):
    monkeypatch.delenv("TOOL_ROUTER_LIVE", raising=False)
    assert tr.live_enabled() is False
    monkeypatch.setenv("TOOL_ROUTER_LIVE", "true")
    assert tr.live_enabled() is True

    decision = tr.authorize_live_proposal(
        {
            "raw_topk": [{"name": "search_gmail", "score": 0.9}],
            "read_topk": ["search_gmail"],
            "write_topk": ["send_push_alert"],
            "domain_topk": ["search_gmail"],
        },
        offered_names={"search_gmail", "send_push_alert", "end_turn"},
        required_tools={"send_push_alert"},
    )
    assert decision["allowed_names"] <= {"search_gmail", "send_push_alert", "end_turn"}
    assert {"search_gmail", "send_push_alert", "end_turn"} <= decision["allowed_names"]


def test_live_router_fails_open_when_proposal_is_unavailable():
    offered = {"search_web", "end_turn"}
    decision = tr.authorize_live_proposal(None, offered_names=offered)
    assert decision["decision"] == "fail_open"
    assert decision["allowed_names"] == offered


def test_sampling_one_in_n(monkeypatch):
    monkeypatch.setenv("TOOL_ROUTER_SHADOW_SAMPLE", "3")
    assert tr.sample_hit(3) is True
    assert tr.sample_hit(4) is False
    monkeypatch.setenv("TOOL_ROUTER_SHADOW_SAMPLE", "1")
    assert all(tr.sample_hit(i) for i in range(1, 5))


def test_env_flag_parser_accepts_truthy_and_never_crashes(monkeypatch):
    from src.core import state as st
    monkeypatch.setenv("_TEST_FLAG", "true")
    assert st._env_int_flag("_TEST_FLAG", 0) == 1
    monkeypatch.setenv("_TEST_FLAG", "ON")
    assert st._env_int_flag("_TEST_FLAG", 0) == 1
    monkeypatch.setenv("_TEST_FLAG", "false")
    assert st._env_int_flag("_TEST_FLAG", 1) == 0
    monkeypatch.setenv("_TEST_FLAG", "3")
    assert st._env_int_flag("_TEST_FLAG", 0) == 3
    monkeypatch.setenv("_TEST_FLAG", "garbage")
    assert st._env_int_flag("_TEST_FLAG", 5) == 5
    monkeypatch.delenv("_TEST_FLAG", raising=False)
    assert st._env_int_flag("_TEST_FLAG", 2) == 2


# ---------------------------------------------------------------------------
# Provenance (set union is lossy)
# ---------------------------------------------------------------------------

def _record(*, seed, loads, chosen, offered, topk=()):
    turn = tr.begin_turn("a query", intent_seed=set(seed), sampled=True)
    tr.observe_load_tool_schemas(turn, list(loads))
    proposed = None
    if topk:
        proposed = {
            "browser_gate": False,
            "mode_signature": "mode=ordinary;browser_gate=0",
            "method": "hybrid",
            "fallback_reason": None,
            "latency_ms": 12.0,
            "cache_hit": False,
            "raw_topk": [
                {"name": n, "rank": i + 1, "score": 1.0, "class": tr.tool_class(n)}
                for i, n in enumerate(topk)
            ],
            "read_topk": [],
            "write_topk": [],
        }
    return tr.build_record(
        turn=turn, round_id=0, mode="ordinary", restricted=False,
        chosen=list(chosen), offered_names=set(offered), required_tools=None,
        proposed=proposed,
    )


def test_provenance_records_dual_source():
    turn = tr.begin_turn("q", intent_seed={"search_gmail"}, sampled=True)
    tr.observe_load_tool_schemas(turn, ["search_gmail"])  # already seeded -> set union is a no-op
    rec = tr.build_record(
        turn=turn, round_id=0, mode="ordinary", restricted=False,
        chosen=["search_gmail"], offered_names={"search_gmail"}, required_tools=None,
        proposed=None,
    )
    assert rec["explicit_load_count"] == 1
    assert rec["chosen"][0]["provenance"] == "both"


def test_provenance_labels():
    rec = _record(
        seed={"a"}, loads={"b"}, chosen=["a", "b", "c", "z"],
        offered={"a", "b", "c"},
    )
    prov = {c["name"]: c["provenance"] for c in rec["chosen"]}
    assert prov["a"] == "intent_seed"
    assert prov["b"] == "explicit_load"
    assert prov["c"] == "core_or_baseline"
    assert prov["z"] == "unobserved"


# ---------------------------------------------------------------------------
# Offering math (O_router approximation) + restricted inert
# ---------------------------------------------------------------------------

def test_projected_reduction_math():
    rec = _record(
        seed={"b", "c"}, loads=set(), chosen=[],
        offered={"a", "b", "c", "d"}, topk=["x"],
    )
    # core_est = offered - seed = {a, d}; projected = {a, d, x} => (4-3)/4 = 25%
    assert rec["projected_core_count"] == 2
    assert rec["projected_offered_count"] == 3
    assert rec["projected_reduction_pct"] == 25.0


def test_baseline_only_discovery_is_visible():
    rec = _record(
        seed=set(), loads=set(), chosen=[],
        offered={"a", "b"}, topk=["b"],
    )
    # core_est = {a, b}; projected = {a, b} => no reduction
    assert rec["projected_reduction_pct"] == 0.0


def test_projection_preserves_explicit_loads():
    # F = C ∪ L: a tool the model explicitly loaded must survive even if it is
    # not in the router's top-k. Subtracting loads would overstate reduction.
    rec = _record(seed={"b"}, loads={"w"}, chosen=[], offered={"a", "b", "w"}, topk=["x"])
    # core_est = (offered - seed) | loads = {a, w}; projected = {a, w, x} => 0%
    assert rec["projected_core_count"] == 2
    assert rec["projected_offered_count"] == 3
    assert rec["projected_reduction_pct"] == 0.0


def test_projection_uses_domain_gated_candidates_when_gated():
    cards = tr.tool_cards()
    read_name = next(c["name"] for c in cards if c["class"] == "read")
    turn = tr.begin_turn("log into amazon", intent_seed=set(), sampled=True)
    proposed = {
        "browser_gate": True,
        "method": "hybrid", "fallback_reason": None, "latency_ms": 5.0,
        "cache_hit": False,
        # raw_topk includes a non-domain tool the gated router would never add
        "raw_topk": [
            {"name": read_name, "rank": 1, "score": 0.9, "class": "read"},
            {"name": "search_web", "rank": 2, "score": 0.8, "class": "read"},
        ],
        "read_topk": [read_name, "search_web"],
        "write_topk": [],
        "domain_topk": [read_name],
    }
    rec = tr.build_record(
        turn=turn, round_id=0, mode="ordinary", restricted=False, chosen=[],
        offered_names={read_name}, required_tools=None, proposed=proposed,
    )
    # gated candidate set = domain_topk = {read_name}; core = {read_name};
    # projected = {read_name} => (1-1)/1 = 0%; search_web must not be added.
    assert rec["gated_candidate_count"] == 1
    assert rec["projected_offered_count"] == 1
    assert rec["projected_reduction_pct"] == 0.0


def test_restricted_round_is_inert():
    turn = tr.begin_turn("log into amazon", intent_seed=set(), sampled=True)
    rec = tr.build_record(
        turn=turn, round_id=0, mode="audit", restricted=True, chosen=["search_web"],
        offered_names={"search_web"}, required_tools=["search_web"],
        proposed=None, router_inert=True,
    )
    assert rec["restricted"] is True
    assert rec["router_inert"] is True
    assert rec["method"] is None
    assert rec["candidate_count"] == 0
    assert rec["required_in_baseline"] == [True]


def test_chosen_unknown_names_flags_hallucinations():
    rec = _record(seed=set(), loads=set(), chosen=["not_a_real_tool"], offered={"a"})
    assert rec["chosen_unknown_names"] == ["not_a_real_tool"]


# ---------------------------------------------------------------------------
# Record writing
# ---------------------------------------------------------------------------

def test_write_record_appends_jsonl_without_prompt_text(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_LOG_DIR", tmp_path)
    prompt = "log into amazon and checkout my cart"
    turn = tr.begin_turn(prompt, intent_seed=set(), sampled=True)
    rec = tr.build_record(
        turn=turn, round_id=0, mode="ordinary", restricted=False,
        chosen=["request_concierge_action"],
        offered_names={"request_concierge_action"}, required_tools=None, proposed=None,
    )
    tr.write_record(rec)
    tr.write_record(rec)

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["turn_id"] == turn["turn_id"]
    assert parsed["query_hash"] == turn["query_hash"]
    assert prompt not in files[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Domain partition + counterfactual recall fields
# ---------------------------------------------------------------------------

def _hybrid(**emb_overrides):
    all_names = [c["name"] for c in tr.tool_cards()]
    emb = {n: 0.0 for n in all_names}
    emb.update(emb_overrides)

    async def _fake_emb(text, limit):
        return emb

    return _fake_emb


def test_propose_domain_topk_restricted_when_gated(monkeypatch):
    monkeypatch.setattr(tr, "index_ready", lambda: True)
    monkeypatch.setattr(tr, "browser_gate", lambda q: True)
    monkeypatch.setattr(tr, "GATE_DOMAIN", frozenset({"search_gmail"}))
    monkeypatch.setattr(tr, "_emb_scores", _hybrid(
        **{"search_web": 0.99, "search_gmail": 0.98}
    ))
    out = asyncio.run(tr.propose("log into amazon and checkout my cart"))
    assert out["browser_gate"] is True
    assert out["domain_topk"], "gated query must still yield domain candidates"
    assert set(out["domain_topk"]) <= {"search_gmail"}
    assert "search_web" not in out["domain_topk"]


def test_propose_domain_topk_is_full_ranking_when_not_gated(monkeypatch):
    monkeypatch.setattr(tr, "index_ready", lambda: True)
    monkeypatch.setattr(tr, "_emb_scores", _hybrid(**{"search_gmail": 1.0}))
    out = asyncio.run(tr.propose("how much did I spend on restaurants last month"))
    assert out["browser_gate"] is False
    assert out["domain_topk"] == [c["name"] for c in out["raw_topk"]]


def test_build_record_counterfactual_fields():
    cards = tr.tool_cards()
    read_name = next(c["name"] for c in cards if c["class"] == "read")
    mut_name = next(c["name"] for c in cards if c["class"] == "mutate")
    turn = tr.begin_turn("log into amazon", intent_seed=set(), sampled=True)
    proposed = {
        "browser_gate": True,
        "mode_signature": "mode=ordinary;browser_gate=1",
        "method": "hybrid",
        "fallback_reason": None,
        "latency_ms": 10.0,
        "cache_hit": False,
        "top1_score": 0.8,
        "raw_topk": [
            {"name": read_name, "rank": 1, "score": 0.8, "class": "read"},
            {"name": mut_name, "rank": 2, "score": 0.7, "class": "mutate"},
        ],
        "read_topk": [read_name],
        "write_topk": [mut_name],
        "domain_topk": [read_name],  # gated: only the concierge-domain read is eligible
    }
    rec = tr.build_record(
        turn=turn, round_id=0, mode="ordinary", restricted=False,
        chosen=[read_name, mut_name], offered_names={read_name, mut_name},
        required_tools=None, proposed=proposed,
    )
    by_name = {c["name"]: c for c in rec["chosen"]}
    assert by_name[read_name]["in_partition_topk"] is True
    assert by_name[read_name]["in_domain_topk"] is True
    assert by_name[read_name]["in_counterfactual_topk"] is True
    assert by_name[mut_name]["in_partition_topk"] is True
    assert by_name[mut_name]["in_domain_topk"] is False
    assert by_name[mut_name]["in_counterfactual_topk"] is False  # gated out
    assert rec["chosen_any_in_counterfactual_topk"] is True
    assert rec["top1_score"] == 0.8
    assert rec["domain_topk"] == [read_name]


def test_timeout_record_is_not_routed():
    turn = tr.begin_turn("how much did I spend", intent_seed=set(), sampled=True)
    rec = tr.build_record(
        turn=turn, round_id=0, mode="ordinary", restricted=False,
        chosen=["query_spending"], offered_names={"query_spending"},
        required_tools=None, proposed=None, error="propose_timeout",
    )
    assert rec["method"] is None
    assert rec["error"] == "propose_timeout"
    assert rec["chosen_any_in_counterfactual_topk"] is False
