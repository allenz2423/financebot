#!/usr/bin/env python3
"""Stage-A (shadow) analyzer for the semantic tool-router.

Reads the JSONL emitted by ``src/services/tool_router.py`` and reports the
shadow promotion gate from ``docs/tool-router-loop-contract.md``. Shadow data is
counterfactual: it can measure retrieval quality, projected schema reduction,
latency and degradation, but it CANNOT prove the causal claims (no increase in
execution failures, no prune-caused stalls) — those belong to the Stage-B canary.

Usage:

    python scripts/tool_router_shadow_report.py [--dir data/tool-router-shadow]
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

# Stage-A thresholds (see contract "Promotion gates").
TH_RECALL_TOP8 = 0.90          # partition/domain counterfactual top-8 recall
TH_REDUCTION = 20.0            # median projected reduction on ordinary read turns (%)
TH_P95_LATENCY_MS = 400.0      # warm p95 retrieval latency
TH_MIN_TURNS = 200             # contract: >= 200 eligible ordinary tool-call turns
TH_MIN_CALLS = 500             # contract: >= 500 tool calls


def load_records(directory: str) -> list[dict]:
    records: list[dict] = []
    for path in sorted(glob.glob(str(Path(directory) / "*.jsonl"))):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def _is_read_turn(rec: dict) -> bool:
    """Ordinary read turn: at least one read call and no mutating call.

    Conditional tools are quasi-read (concierge reads), but ``run_python_sandbox``
    / ``run_shell`` are write-kind in the eval, so a conditional-only turn must
    not count toward the read-turn reduction population.
    """
    classes = [c.get("class") for c in rec.get("chosen", [])]
    if not classes:
        return False
    return "read" in classes and set(classes) <= {"read", "conditional"}


def analyze(records: list[dict]) -> dict:
    ordinary = [r for r in records if r.get("mode") == "ordinary"]
    with_calls = [r for r in ordinary if r.get("chosen")]
    routed = [r for r in with_calls if r.get("method")]

    recall_hits = sum(
        1 for r in routed if r.get("chosen") and r["chosen"][0].get("in_topk")
    )
    recall_any = sum(1 for r in routed if r.get("chosen_any_in_topk"))
    partition_recall = sum(1 for r in routed if r.get("chosen_any_in_partition_topk"))
    counterfactual_recall = sum(
        1 for r in routed if r.get("chosen_any_in_counterfactual_topk")
    )
    domain_routed = [r for r in routed if r.get("browser_gate")]
    domain_recall = sum(1 for r in domain_routed if r.get("chosen_any_in_domain_topk"))

    read_turns = [r for r in routed if _is_read_turn(r)]
    reductions = [r.get("projected_reduction_pct", 0.0) for r in read_turns]

    warm = [
        r.get("latency_ms")
        for r in routed
        if not r.get("cache_hit") and r.get("latency_ms") is not None
    ]

    lexical_fallbacks = [r for r in routed if r.get("method") == "lexical"]
    # A failed proposal has no method, so it is not part of `routed`. Count
    # errors over all ordinary records or a timeout can falsely look like a
    # clean run.
    errors = [r for r in ordinary if r.get("error")]
    timeouts = [r for r in ordinary if r.get("error") == "propose_timeout"]
    inert = [r for r in records if r.get("router_inert")]
    no_tool = [r for r in ordinary if not r.get("chosen") and r.get("method")]
    no_tool_scores = [float(r["top1_score"]) for r in no_tool if r.get("top1_score") is not None]

    return {
        "total_records": len(records),
        "ordinary_rounds": len(ordinary),
        "ordinary_rounds_with_calls": len(with_calls),
        "ordinary_tool_calls": sum(len(r.get("chosen") or ()) for r in ordinary),
        "routed_rounds": len(routed),
        "restricted_inert_rounds": len(inert),
        "recall_top8": (recall_hits / len(routed)) if routed else 0.0,
        "recall_top8_any": (recall_any / len(routed)) if routed else 0.0,
        "partition_recall_top8": (partition_recall / len(routed)) if routed else 0.0,
        "counterfactual_recall_top8": (
            counterfactual_recall / len(routed) if routed else 0.0
        ),
        "browser_domain_recall_top8": (domain_recall / len(domain_routed)) if domain_routed else 0.0,
        "browser_domain_rounds": len(domain_routed),
        "read_turns": len(read_turns),
        "median_reduction": statistics.median(reductions) if reductions else 0.0,
        "p95_latency_ms": _p95([float(v) for v in warm if v is not None]),
        "cache_hits": sum(1 for r in routed if r.get("cache_hit")),
        "lexical_fallbacks": len(lexical_fallbacks),
        "errors": len(errors),
        "timeouts": len(timeouts),
        "no_tool_rounds": len(no_tool),
        "no_tool_top1_score_median": statistics.median(no_tool_scores) if no_tool_scores else None,
        "no_tool_top1_score_max": max(no_tool_scores) if no_tool_scores else None,
        "unknown_chosen": sum(len(r.get("chosen_unknown_names") or ()) for r in ordinary),
        "method_mix": dict(
            sorted(
                {m: sum(1 for r in routed if r.get("method") == m)
                 for m in {r.get("method") for r in routed if r.get("method")}}.items()
            )
        ),
    }


def gate(m: dict) -> dict:
    return {
        # A verdict below the contract's minimum sample is not decision-grade.
        "sample_sufficient": (
            m["ordinary_rounds_with_calls"] >= TH_MIN_TURNS
            and m["ordinary_tool_calls"] >= TH_MIN_CALLS
        ),
        "counterfactual_recall_top8>=0.90": (
            m["counterfactual_recall_top8"] >= TH_RECALL_TOP8
        ),
        "median_reduction>=20%": m["median_reduction"] >= TH_REDUCTION,
        "warm_p95<=400ms": m["p95_latency_ms"] <= TH_P95_LATENCY_MS,
        # `fallback_exception_free` only sees propose errors; a total embedding
        # outage degrades silently to lexical, so require the hybrid path to have
        # actually run at least once.
        "fallback_exception_free": m["errors"] == 0,
        "embeddings_exercised": m["method_mix"].get("hybrid", 0) > 0 or m["routed_rounds"] == 0,
    }


def render(m: dict, passed: dict) -> str:
    ok = lambda b: "PASS" if b else "FAIL"  # noqa: E731
    lines = [
        "# Tool-router shadow report (Stage A)\n",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Records: **{m['total_records']}** "
        f"(ordinary {m['ordinary_rounds']}, with tool calls {m['ordinary_rounds_with_calls']}, "
        f"restricted/inert {m['restricted_inert_rounds']})\n",
        f"- Sample gate (≥ {TH_MIN_TURNS} turns, ≥ {TH_MIN_CALLS} calls): "
        f"{m['ordinary_rounds_with_calls']} turns / {m['ordinary_tool_calls']} calls — "
        f"{ok(passed['sample_sufficient'])}"
        + ("" if passed["sample_sufficient"] else
           " **below minimum — verdict is NOT decision-grade**") + "\n",
        "## Retrieval\n",
        f"- Routed rounds: {m['routed_rounds']} | method mix: {m['method_mix']}",
        f"- Raw top-8 recall (primary chosen): **{m['recall_top8']:.1%}**",
        f"- Top-8 recall (any chosen): {m['recall_top8_any']:.1%}\n",
        f"- Partitioned top-8 recall (any chosen): {m['partition_recall_top8']:.1%}",
        f"- Counterfactual gated top-8 recall: **{m['counterfactual_recall_top8']:.1%}** "
        f"(gate ≥ {TH_RECALL_TOP8:.0%}) — "
        f"{ok(passed['counterfactual_recall_top8>=0.90'])}",
        f"- Browser-domain top-8 recall: {m['browser_domain_recall_top8']:.1%} "
        f"({m['browser_domain_rounds']} browser-gated rounds)\n",
        "## Projected schema reduction (ordinary read turns)\n",
        f"- Read turns: {m['read_turns']}",
        f"- Median projected reduction: **{m['median_reduction']:.1f}%** "
        f"(gate ≥ {TH_REDUCTION:.0f}%) — {ok(passed['median_reduction>=20%'])}\n",
        "## Performance / degradation\n",
        f"- Warm p95 latency: **{m['p95_latency_ms']:.0f} ms** "
        f"(gate ≤ {TH_P95_LATENCY_MS:.0f} ms) — {ok(passed['warm_p95<=400ms'])}",
        f"- Cache hits: {m['cache_hits']}",
        f"- Lexical fallbacks: {m['lexical_fallbacks']} | errors: {m['errors']} "
        f"(timeouts {m['timeouts']}) — {ok(passed['fallback_exception_free'])}\n",
        "## No-tool rounds\n",
        f"- No-tool rounds with a proposal: {m['no_tool_rounds']}",
        f"- Top-1 score median/max: {m['no_tool_top1_score_median']} / {m['no_tool_top1_score_max']} "
        "(descriptive only; no held-out floor applied)\n",
        "## Dispatch\n",
        f"- Chosen names outside the catalog (hallucinated/unknown): {m['unknown_chosen']} "
        "(not the baseline dispatch-refusal count — that is not yet instrumented)\n",
        "## Not measurable in shadow (defer to Stage-B canary)\n",
        "- No increase in execution failures / rollbacks.",
        "- No prune-caused stalls or discovery pressure increase.\n",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="data/tool-router-shadow")
    ap.add_argument("--out", default=None, help="markdown output path")
    args = ap.parse_args(argv)

    records = load_records(args.dir)
    if not records:
        print(f"No shadow records found under {args.dir}")
        return 0

    m = analyze(records)
    passed = gate(m)
    report = render(m, passed)
    print(report)

    out = Path(args.out) if args.out else Path(args.dir) / f"report_{datetime.now(timezone.utc):%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\nWrote {out}")
    return 0 if all(passed.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
