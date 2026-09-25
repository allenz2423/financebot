# Delilah Tool-Router Research — Complete Findings

Date: 2026-09-19

This document consolidates everything learned from the Jev investigation and the Delilah Qdrant tool-routing proof of concept. The v3 section is authoritative. The v2 section is retained as historical context. The earlier `docs/tool-router-poc-writeup.md` is preliminary historical material.

## Executive conclusion

Use semantic retrieval as a constrained candidate-generation layer, not as an autonomous tool selector.

The current evidence supports this architecture:

```text
user request
    -> deterministic domain gate
    -> read/mutate permission partition
    -> hybrid lexical + embedding retrieval
    -> absolute score floor / no-tool gate
    -> small candidate schema set
    -> existing Delilah advisor and state gate
    -> tool execution and verification
```

Do not let the router execute tools, grant permissions, select a single tool autonomously, or widen the tools already allowed by Delilah's controller.

## Current v3 status

The method-agnostic v3 evaluation fixes the remaining methodological defects from v2:

- retrieval, safety, calibration, and detailed rankings are computed independently for embedding, lexical, and hybrid methods;
- adversarial browser-gate negatives are included;
- browser-domain evaluation is restricted to concierge tools rather than generic web fetch/scrape tools;
- CSV output includes method-specific ranks and primary/acceptable labels;
- read/mutate partitioning and browser-gate behavior are measured explicitly.

Current v3 results:

| method | primary top-1 | acceptable top-8 | MRR |
|---|---:|---:|---:|
| embedding | 37% | 94% | 0.516 |
| lexical BM25 | 41% | 81% | 0.525 |
| hybrid | **49%** | **94%** | **0.610** |

The v3 battery contains 79 labeled tool requests, 58 read requests, 21 writes, 30 no-tool requests, 5 ambiguous follow-ups, and 9 concierge/browser-intent requests.

Unfiltered hybrid retrieval exposes a mutating candidate in 3/58 read queries at top-1 and 33/58 at top-8. After read/mutate partitioning, mutating candidates are 0 at top-1, top-3, and top-8 for every retrieval method. This validates partitioning as a mandatory safety boundary.

The deterministic browser gate achieves 9/9 recall and 1/70 false positives on the current curated battery. Its one false positive is the deliberately ambiguous `Am I logged in to my bank app?` case.

Method-specific in-sample no-tool floors are:

| method | floor | precision | recall | FPR |
|---|---:|---:|---:|---:|
| embedding | 0.5684 | 0.89 | 0.92 | 0.30 |
| lexical BM25 | 7.7072 | 0.96 | 0.87 | 0.10 |
| hybrid | 0.3738 | 0.94 | 0.97 | 0.17 |

These floors are method-specific and in-sample; they are not production thresholds yet.

## Jev investigation

Repository reviewed: `browser-use/jev-ultrafast` and its `browser-harness` dependency.

### What Jev is

Jev is a browser agent with a structured, indexed action space. It observes visible page controls and asks a hosted TypeSafe model to choose an operation and target. Supported operations include click, type, select, scroll, wait, done, and blocked.

The loop has several strong engineering properties:

- code-owned element IDs instead of model-generated selectors;
- page freshness and stale-decision checks;
- visibility and disabled-control checks;
- center-point hit testing with `elementFromPoint` before input;
- bounded action/model-call budgets;
- action recording before post-action observation;
- independent outcome verification for the demo's `DONE` state.

### Why Jev is not suitable for Delilah concierge

Jev's decision-maker is a hosted TypeSafe API. Each step sends page state, visible text, indexed elements, recent actions, and task context to `api.typesafe.ai`. The chooser is not pluggable in the current implementation.

Jev also attaches through Browser Harness to an existing Chrome profile. Its README states that owned tabs share the existing Chrome profile. Delilah concierge intentionally uses disposable, isolated, per-tenant browser sessions with scoped profiles, vault injection, approval state, audit records, and verification.

Jev's MVP explicitly excludes frames, shadow roots, canvas, uploads, pop-up tabs, nested scrolling, and arbitrary keyboard widgets. Frames are particularly important for Delilah's PayPal, DataDome, CAPTCHA, and 2FA flows.

Jev has no native integration with:

- Delilah's vault and secret-injection policy;
- tenant isolation;
- approval and TTL state machines;
- screenshot redaction;
- hash-chained audit records;
- VLM verification;
- concierge browser missions;
- local/self-hosted advisor routing.

Its published speed result is a narrow Google Flights demonstration, not evidence for credentialed, adversarial, or frame-heavy merchant flows.

### Jev ideas worth borrowing

1. Pre-mutation occlusion validation.
2. Freshness validation immediately before mutation, including after waits or text generation.
3. Recording the action before observing the result.
4. Stable code-owned element handles.
5. Never allowing model output to become selectors, coordinates, JavaScript, or shell commands.

## First Delilah POC

File: `scripts/tool_router_poc.py`

The first POC indexed `CAPABILITY_REGISTRY` from `src/core/discovery.py` into a separate Qdrant collection named `delilah_tool_router_poc`.

It indexed 86 unique tools and evaluated 114 hand-written natural-language requests across spending, balances, transactions, planning, debt, web research, Gmail, memory, sandbox, reminders, monitoring, and concierge-like requests.

It generated:

- `data/tool-router-poc/tool_router_results.txt`
- `data/tool-router-poc/tool_router_results.csv`

### Why the first POC was not decision-grade

The model actually receives 178 callable tools from `BOT_TOOLS_SCHEMA`, while the registry contained only 86. The first POC therefore indexed only about 48% of the callable tool set.

It omitted all four concierge/browser tools:

- `request_concierge_action`;
- `concierge_browser_step`;
- `concierge_act_status`;
- `request_credential_capture`.

It also omitted many other tool surfaces. Consequently, browser/order/tracking rankings had no correct answer available.

The first POC also had no labeled ground truth, no accuracy metrics, no safety metrics, and no calibrated threshold. Its 912-row output was useful for exploration but not evaluation.

## Corrected v1 evaluation

Files:

- `scripts/tool_router_eval.py`;
- `data/tool-router-poc/tool_router_eval.md`;
- `data/tool-router-poc/tool_router_eval.csv`;
- `docs/tool-router-eval-corrected.md`.

The corrected evaluator rebuilt cards from all 178 entries in `BOT_TOOLS_SCHEMA`, used real function descriptions and parameter names, and added labeled queries, acceptable-tool sets, MRR, mutation checks, score-margin analysis, and a deterministic browser gate.

The initial corrected run used 59 labeled read/write queries, 5 no-tool queries, and 4 ambiguous follow-ups.

Its main finding was that embeddings retrieve the right tool within a shortlist much more reliably than they rank it first. It also showed that browser/concierge intent was poorly represented by embedding similarity and needed deterministic routing.

## Hardened v2 evaluation (superseded by v3)

The v2 evaluator is the current authoritative research implementation:

- `scripts/tool_router_eval.py`;
- `data/tool-router-poc/tool_router_eval_v2.md`;
- `data/tool-router-poc/tool_router_eval_v2.csv`;
- `docs/tool-router-eval-corrected.md`.

### v2 catalog and battery

- Catalog: 178 tools from `BOT_TOOLS_SCHEMA`.
- Labelled read/write requests: 74.
- No-tool calibration requests: 30.
- Ambiguous follow-ups: 5, excluded from accuracy.
- Embedding model: local `qwen3-embedding:0.6b`.
- Vector size: 1024.
- Collection: `delilah_tool_router_eval`.
- The evaluator supports `--recreate` so stale Qdrant points do not survive catalog changes.
- Catalog and query-set hashes are recorded in the report.

### Tool classification

The v2 evaluator uses Delilah's authoritative `MUTATION_TOOLS` set plus an explicit action set.

Mode-sensitive tools are classified as conditional rather than automatically mutating:

- `request_concierge_action` — read-tier for order status, tracking, and price watch; write-tier for actions such as send email or fill form;
- `concierge_browser_step` — observation versus click/type/navigation depends on the action;
- `request_credential_capture` — issues a secure capture link;
- `request_user_form`;
- `run_python_sandbox`;
- `run_shell`.

This is materially safer than classifying tools from name prefixes.

### Retrieval results

| method | primary top-1 | acceptable top-1 | acceptable top-8 | MRR |
|---|---:|---:|---:|---:|
| embedding (`qwen3-embedding:0.6b`) | 38% | 50% | 93% | 0.530 |
| lexical BM25 | 43% | 54% | 84% | 0.555 |
| 50/50 hybrid | **54%** | 62% | **96%** | **0.648** |

Interpretation:

- Embeddings alone are the weakest top-1 method.
- Lexical matching is strong because tool names and descriptions share vocabulary with user requests.
- Embeddings provide broader semantic recall.
- The hybrid method is complementary and materially better than either method alone.
- Even hybrid top-1 at 54% is not safe for autonomous selection.
- Hybrid acceptable top-8 at 96% supports using retrieval to narrow the schema set.

The reported top-k metric is acceptable-tool recall, not exact primary-tool accuracy. The evaluator distinguishes the primary expected tool from acceptable alternatives and reports MRR for primary rank.

### Safety results before partitioning

For 53 read requests, using embedding ranking:

- strict mutating tool in top-1: 8/53 = 15%;
- strict mutating tool in top-3: 18/53 = 34%;
- strict mutating tool in top-8: 39/53 = 74%;
- mean strict-mutating share of the top-8 shortlist: 0.15;
- conditional tool in top-3: 5/53.

The 74% top-8 exposure rate is the key safety result. A raw top-8 retrieval shortlist is unsafe for read requests unless a read/mutate partition is applied first.

The current v2 safety section reports embedding rankings. Hybrid safety must be measured separately before hybrid retrieval is used in runtime.

### Calibration results

The in-sample no-tool sweep used 74 tool requests and 30 no-tool requests.

For embedding top-1 scores:

- best-F1 floor: approximately 0.56;
- precision at that floor: 0.88;
- recall: 0.95;
- false-positive rate: 0.33;
- a floor around 0.65 reduces FPR to approximately 0.13 but reduces recall to approximately 0.78.

These thresholds are not production-ready because they are in-sample and currently embedding-specific. Hybrid scores need their own calibration, and calibration must be repeated after domain and read/mutate filtering.

Also, min-max normalization is performed independently per query for hybrid ranking. Hybrid scores are therefore useful for ranking but should not be treated as globally comparable until a separate score calibration is defined.

### Browser/domain gate (v2 historical result)

The v2 evaluator implements a deterministic English browser-intent gate for cues such as:

- login/sign in;
- checkout/cart;
- order status;
- tracking/package/shipment/delivery;
- price drop/price watch;
- merchant site;
- add to cart.

On the curated battery:

- browser-intent recall: 9/9;
- false positives on non-browser labeled requests: 0/65.

The gate fixes embedding failures such as:

- “Log me into my paypal account” returning no concierge tool in the top eight;
- order/tracking queries drifting toward transaction or merchant tools;
- price-watch requests drifting toward refund analysis or budgeting.

This result is encouraging but not yet adversarial. The gate is English-only and must be tested against more paraphrases, ambiguous words, and financial false positives.

## V3 remaining limitations

1. Hybrid and method-specific safety are measured, but the evaluator is not yet wired into the runtime permission controller.
2. No-tool floors are in-sample; held-out calibration and test splits are still required.
3. The battery is curated and has 79 labeled tool requests rather than 3–5 examples per each of 178 tools.
4. No nDCG or graded relevance metric is reported.
5. The browser gate is English-only and has only one deliberate false positive; it needs a larger adversarial and paraphrase suite.
6. Gmail-domain gating is not yet measured in this harness.
7. Alias/synonym boosting is not yet implemented beyond the BM25 baseline.
8. Tool cards do not yet contain a fully curated alias vocabulary or all domain/risk metadata needed for runtime policy decisions.
9. The 0-after-partition safety result validates the evaluator filter, but runtime integration still needs tests proving the same invariant at schema-injection and execution boundaries.

## Recommended implementation design

### Stage 1: deterministic gates

Before retrieval, classify the request into one or more domains:

- concierge/browser;
- Gmail/email;
- financial reads;
- financial mutations;
- reminders/monitoring;
- memory/world model;
- research/sandbox.

The browser gate should be hard routing for browser/concierge requests, just as the existing `query_targets_mail()` function gates Gmail retrieval.

### Stage 2: permission partition

Use Delilah's current controller state and approval mode to choose the allowed catalog partition:

- read-only tools;
- mutation tools;
- conditional tools that require argument-level inspection;
- concierge read-tier tools;
- concierge approval/write tools.

The router must never add tools outside this allowed partition.

### Stage 3: hybrid retrieval

Build cards from `BOT_TOOLS_SCHEMA` with:

- name;
- real description;
- parameter names;
- required parameters;
- aliases and common user phrases;
- domain;
- read/mutate/conditional class;
- approval/risk class.

Run BM25/lexical and local embedding retrieval, then combine rankings or normalized scores. Candidate retrieval should produce a small set such as top-8 after gating.

### Stage 4: score floor and fallback

Use an absolute score floor only after method-specific held-out calibration. On low confidence:

- fall back to existing progressive discovery;
- ask a clarification question;
- or expose a broader but still permission-filtered schema set.

Do not use top1−top2 margin as the primary confidence signal; v1/v2 score geometry showed small, noisy margins.

### Stage 5: existing advisor remains authoritative

Inject candidate schemas into the existing advisor prompt. The advisor still decides:

- which tool to call;
- the arguments;
- whether multiple calls are needed;
- whether evidence is sufficient;
- whether to stop or ask the user.

The router should not produce executable arguments or permission decisions.

## Next evaluation pass

V3 completed the evaluator-side comparison and filtering work. Before runtime integration:

1. Use held-out calibration and test splits for method-specific score floors.
2. Add a 3–5-query-per-tool stratified suite for high-value tools.
3. Add more adversarial browser-gate negatives and non-English/paraphrased positives.
4. Add nDCG or graded relevance scoring.
5. Measure the existing progressive-discovery baseline against hybrid retrieval.
6. Implement the same domain gate and read/mutate partition at the runtime schema-injection boundary.
7. Add invariant tests proving the router cannot widen the controller's allowed tool set.
8. Only then consider a feature-flagged, read-only advisor integration.

## Artifacts

### Research and writeups

- `docs/tool-router-complete-findings.md` — this consolidated document.
- `docs/tool-router-eval-corrected.md` — authoritative v3 evaluation writeup.
- `docs/tool-router-poc-writeup.md` — preliminary/superseded first-POC writeup.

### Evaluators and POCs

- `scripts/tool_router_eval.py` — corrected/hardened v3 evaluator.
- `scripts/tool_router_poc.py` — original 86-tool exploratory POC.

### Reports and data

- `data/tool-router-poc/tool_router_eval_v2.md` — v2 report with rankings and metrics.
- `data/tool-router-poc/tool_router_eval_v2.csv` — v2 machine-readable scores.
- `data/tool-router-poc/tool_router_eval_v3.md` — v3 method-agnostic report.
- `data/tool-router-poc/tool_router_eval_v3.csv` — v3 machine-readable scores and ranks.
- `data/tool-router-poc/tool_router_eval.md` — earlier corrected evaluation report.
- `data/tool-router-poc/tool_router_eval.csv` — earlier corrected evaluation data.
- `data/tool-router-poc/tool_router_results.txt` — original exploratory output.
- `data/tool-router-poc/tool_router_results.csv` — original exploratory CSV.

## Runtime impact

No runtime advisor, concierge, browser, vault, approval, or security code was changed by this research. The changes are evaluator scripts, reports, and documentation only. The POCs use separate Qdrant collections and do not execute Delilah tools.
