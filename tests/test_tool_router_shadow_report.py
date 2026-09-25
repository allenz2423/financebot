"""Stage-A report semantics: accounting, labeling, and gate behavior."""

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "tool_router_shadow_report.py"
_spec = importlib.util.spec_from_file_location("tool_router_shadow_report", str(_SCRIPT))
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def _rec(**kw):
    base = {
        "mode": "ordinary",
        "restricted": False,
        "router_inert": False,
        "chosen": [],
        "method": "hybrid",
        "cache_hit": False,
        "latency_ms": 50.0,
        "projected_reduction_pct": 30.0,
        "browser_gate": False,
        "top1_score": 0.7,
        "chosen_any_in_topk": False,
        "chosen_any_in_partition_topk": False,
        "chosen_any_in_counterfactual_topk": False,
        "error": None,
    }
    base.update(kw)
    return base


def test_empty_input_is_safe():
    m = report.analyze([])
    assert m["total_records"] == 0
    assert m["counterfactual_recall_top8"] == 0.0
    assert m["p95_latency_ms"] == 0.0
    # nothing to measure must not crash and must not divide by zero
    report.gate(m)


def test_timeout_counts_as_error_not_as_routed():
    records = [
        _rec(chosen=[{"name": "query_spending", "class": "read"}],
             chosen_any_in_counterfactual_topk=True),
        # timed-out proposal: no method, so not "routed", but must still be an error
        _rec(chosen=[{"name": "query_spending", "class": "read"}],
             method=None, error="propose_timeout"),
    ]
    m = report.analyze(records)
    assert m["routed_rounds"] == 1
    assert m["errors"] == 1
    assert m["timeouts"] == 1
    assert report.gate(m)["fallback_exception_free"] is False
    assert m["counterfactual_recall_top8"] == 1.0


def test_unknown_chosen_names_are_surfaced():
    m = report.analyze([
        _rec(chosen=[{"name": "a", "class": "read"}], chosen_unknown_names=["bogus"]),
    ])
    assert m["unknown_chosen"] == 1
    assert "outside the catalog" in report.render(m, report.gate(m))


def test_recall_labeling_and_no_tool_detection():
    records = [
        _rec(chosen=[{"name": "a", "class": "read"}], chosen_any_in_counterfactual_topk=False),
        _rec(chosen=[], method="hybrid", top1_score=0.05),  # no-tool round
    ]
    m = report.analyze(records)
    assert m["ordinary_rounds_with_calls"] == 1
    assert m["routed_rounds"] == 1
    assert m["counterfactual_recall_top8"] == 0.0
    assert m["no_tool_rounds"] == 1
    assert m["no_tool_top1_score_median"] == 0.05


def test_restricted_rounds_are_excluded_from_ordinary_metrics():
    records = [
        _rec(mode="audit", restricted=True, router_inert=True, chosen=[{"name": "search_web", "class": "read"}]),
        _rec(chosen=[{"name": "a", "class": "read"}], chosen_any_in_counterfactual_topk=True),
    ]
    m = report.analyze(records)
    assert m["ordinary_rounds"] == 1
    assert m["restricted_inert_rounds"] == 1
    assert m["routed_rounds"] == 1


def test_is_read_turn_classification():
    assert report._is_read_turn({"chosen": [{"class": "read"}, {"class": "conditional"}]})
    assert not report._is_read_turn({"chosen": [{"class": "read"}, {"class": "mutate"}]})
    assert not report._is_read_turn({"chosen": []})
    # conditional-only (e.g. run_python_sandbox) is write-kind in the eval
    assert not report._is_read_turn({"chosen": [{"class": "conditional"}]})


def test_gate_requires_minimum_sample():
    tiny = report.analyze([
        _rec(chosen=[{"name": "a", "class": "read"}], chosen_any_in_counterfactual_topk=True),
    ])
    assert report.gate(tiny)["sample_sufficient"] is False

    enough = report.analyze(
        [_rec(chosen=[{"name": "a", "class": "read"}],
              chosen_any_in_counterfactual_topk=True)] * 200
    )
    assert enough["ordinary_tool_calls"] == 200
    # 200 turns but only 200 calls still misses the >= 500 call bar
    assert report.gate(enough)["sample_sufficient"] is False

    three = report.analyze(
        [_rec(chosen=[{"name": "a", "class": "read"}, {"name": "b", "class": "read"},
                      {"name": "c", "class": "read"}])] * 200
    )
    assert three["ordinary_tool_calls"] == 600
    assert report.gate(three)["sample_sufficient"] is True


def test_gate_requires_embeddings_exercised():
    # every routed round degraded to lexical: not a healthy shadow run
    all_lexical = report.analyze([
        _rec(method="lexical", fallback_reason="index_not_ready",
             chosen=[{"name": "a", "class": "read"}]),
    ])
    assert report.gate(all_lexical)["embeddings_exercised"] is False

    hybrid = report.analyze([
        _rec(method="hybrid", chosen=[{"name": "a", "class": "read"}]),
    ])
    assert report.gate(hybrid)["embeddings_exercised"] is True


def test_p95_indexing():
    assert report._p95([]) == 0.0
    assert report._p95([1.0]) == 1.0
    # nearest-rank p95 of 1..100 is the 95th value
    assert report._p95([float(i) for i in range(1, 101)]) == 95.0


def test_render_reports_counterfactual_recall():
    m = report.analyze([
        _rec(chosen=[{"name": "a", "class": "read"}], chosen_any_in_counterfactual_topk=True),
    ])
    text = report.render(m, report.gate(m))
    assert "Counterfactual gated top-8 recall" in text
    assert "Raw top-8 recall" in text
