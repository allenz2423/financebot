# Tool-router evaluation — v3, method-agnostic (authoritative)

Date: 2026-09-19
Branch: `feat/semantic-memory-rag`
Supersedes v2 and the preliminary `docs/tool-router-poc-writeup.md`. The Jev
verdict and the "retrieval-only" architecture recommendation stand.

## TL;DR

1. **Hybrid retrieval wins and embeddings are the weakest top-1 method.**
   Primary top-1: embedding 37% / lexical 41% / **hybrid 49%**; MRR 0.516 / 0.525
   / **0.610**. All three reach ~81–94% at top-8.
2. **Read/mutate partitioning is mandatory — and it works.** Unfiltered, hybrid
   exposes a mutating tool to 3/58 read queries at top-1 and 33/58 at top-8.
   After partitioning the candidate pool by the state gate, **mutating
   candidates on read queries = 0 for every method.**
3. **The browser gate is what makes the mandate tractable.** Deterministic gate:
   **9/9 recall, 1/70 false positives**; restricted to the concierge tool set,
   **6/9** gate-positive queries route to the primary tool (vs ~1–2 by embedding
   alone).
4. **Each method has its own no-tool floor.** Embedding 0.568, lexical 7.71
   (BM25 scale), hybrid 0.374 — the lexical/hybrid floors give the best FPR
   (0.10 / 0.17) at high recall (0.87 / 0.97).
5. **Failure mode is surface vocabulary, not semantics.** "package" →
   `install_python_package`; "spend" → `get_safe_to_spend_metrics`. This is why
   lexical beats embeddings on top-1 and why gates are needed.

## Why v3 (the v2 defect)

v2 fixed catalog and labels but still **mixed methods**: the headline compared
three methods while safety and the no-tool floor were computed on the embedding
ranking only. v3 computes retrieval, safety, calibration, and detailed rankings
**separately per method**, and also **after the proposed read/mutate partition
and the browser gate** — exactly the comparison the recommendation depends on.

## Battery

79 labelled (58 read / 21 write), 30 no-tool, 5 ambiguous (excluded);
9 browser-intent, 70 non-browser. Includes the reviewer's adversarial
negatives ("Am I on track with my budget?", "Track my spending this month.",
"What is the price of this transaction?", "What merchant is this charge from?",
"Am I logged in to my bank app?"). Catalog = **178** tools from
`BOT_TOOLS_SCHEMA`.

## Retrieval (per method)

| method | primary top-1 | acc@1 | acc@3 | acc@5 | acc@8 | MRR |
|---|---|---|---|---|---|---|
| embedding | 29/79 = 37% | 52% | 80% | 85% | 94% | 0.516 |
| lexical (BM25) | 32/79 = **41%** | 52% | 67% | 75% | 81% | 0.525 |
| **hybrid** | **39/79 = 49%** | 61% | 81% | 89% | 94% | **0.610** |

After read/mutate partition:

| method | primary top-1 | acc@3 | acc@8 | MRR |
|---|---|---|---|---|
| embedding | 39% | 81% | 91% | 0.543 |
| lexical | 39% | 70% | 84% | 0.523 |
| hybrid | **51%** | 82% | 91% | **0.621** |

## Safety: mutating candidate on read queries (per method, 58 read)

| method | top-1 | top-3 | top-8 | mean shortlist share |
|---|---|---|---|---|
| embedding | 8 | 19 | 43 | 0.15 |
| lexical | 3 | 13 | 41 | 0.15 |
| hybrid | 3 | 15 | 33 | 0.12 |

**After read/mutate partition: 0 / 0 / 0 for every method.** Partitioning the
candidate pool by the state gate removes the entire class of unsafe candidates.

## No-tool floor (per method, in-sample)

| method | floor | precision | recall | FPR |
|---|---|---|---|---|
| embedding | 0.5684 | 0.89 | 0.92 | 0.30 |
| lexical (BM25) | 7.7072 | 0.96 | 0.87 | 0.10 |
| hybrid | 0.3738 | 0.94 | 0.97 | 0.17 |

In-sample; a held-out calibration/test split is still required (§Open).

## Browser gate

- recall on 9 browser-intent queries: **9/9**
- false positives on 70 non-browser queries: **1/70** — the deliberate
  adversarial `Am I logged in to my bank app?` (genuinely ambiguous; the gate
  cannot tell it from a real login check without more context).
- **gate + concierge-domain restriction: 6/9** gate-positive queries route to
  the primary tool.

Hybrid top-1 for the browser cases (all wrong without the gate):

| query | hybrid top-1 | primary |
|---|---|---|
| Find the tracking information for my package. | `install_python_package` ⚠ | `request_concierge_action` |
| Where is my package, has it shipped yet? | `install_python_package` ⚠ | `request_concierge_action` |
| Log into amazon and checkout my cart. | `log_lifestyle_context` | `request_concierge_action` |
| Log me into my paypal account. | `add_planned_transaction` ⚠ | `request_concierge_action` |
| Set up my login for this site. | `set_user_timezone` | `request_credential_capture` |

"package" → Python package is the canonical collision that defeats retrieval.

## Recommended pipeline

```
real BOT_TOOLS_SCHEMA
  -> tool metadata: risk / read-mutate mode / domain / aliases
  -> deterministic domain gate (browser cues; query_targets_mail for gmail)
  -> read/mutate partition (search only the permitted side)
  -> hybrid retrieval (BM25 + embedding)
  -> top-k candidate schemas (k≈8)
  -> existing advisor + state gate make the final call
```

Do not wire retrieval into the advisor loop until the read/mutate partition and
the browser gate exist — both are validated here (partition → 0 unsafe; gate →
9/9).

## Still open (honest scope)

- **No-tool floor is in-sample**; needs a held-out calibration/test split.
- Battery is **curated** (79 tool queries over 178 tools), not 3–5 paraphrases
  per tool; some families are thin.
- **nDCG** not computed; acceptable sets are binary and only MRR is reported.
- Gate is **English-only and hand-written**; the 1/70 FP shows ambiguous
  login phrasing needs context, not a regex.
- **Alias/synonym boosting** not implemented (BM25 baseline only).
- Gmail domain gate (`query_targets_mail`) not yet measured in this harness.

## Artifacts

- `scripts/tool_router_eval.py` — v3, method-agnostic (`--recreate` for a clean index).
- `data/tool-router-poc/tool_router_eval_v3.md` — full report (per-method detail, browser cases, failures).
- `data/tool-router-poc/tool_router_eval_v3.csv` — per-tool scores + `rank_*` / `is_primary` / `is_acceptable`.
- `data/tool-router-poc/tool_router_eval*.{md,csv}` — v1/v2 (superseded).