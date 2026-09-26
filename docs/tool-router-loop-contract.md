# Tool-router ↔ tool-loop contract

Date: 2026-09-19
Branch: `feat/semantic-memory-rag`
Status: **shadow instrumentation and opt-in live narrowing are implemented;
model-callable semantic discovery is also available on ordinary turns.** Shadow
remains off by default and live narrowing remains behind `TOOL_ROUTER_LIVE=1`.
`search_tools` reuses the same router for registry-owned retrieval and may add a
bounded set of controller-permitted schemas to the next round. Existing
controller, grant, receipt, and claim gates remain authoritative.

Companion documents:

- `docs/tool-router-eval-corrected.md` — v3 evaluation;
- `docs/tool-router-complete-findings.md` — consolidated research findings.

This document is the authoritative, code-verified contract. Section "Code
anchors" and "Verification log" record the exact source lines each claim rests
on, so the contract can be re-checked as the code moves.

## Summary

The router is a **retrieval component** that may replace the common-path
candidate seeding done by `_INTENT_TOOL_MAP`. In shadow mode it proposes a
bounded candidate set for evaluation. In explicitly promoted live mode it also
narrows the already-offered schema and rejects a dispatch outside that set.
The existing controller, grants, receipt lifecycle, and final claim gate still
decide whether execution and user-visible claims are allowed. The router is not
an execution path or termination controller.

## Decision

```text
turn text
  -> determine current controller mode (authorization universe G)
  -> apply deterministic domain and permission partitions
  -> retrieve router candidates from the full catalog A
  -> apply eligibility filter E to router candidates (offerability)
  -> preserve fixed/preserved/required/escape tools that the mode allows
  -> advisor receives the resulting schemas and chooses
  -> runtime validates and executes the chosen call   # dispatch = authority
  -> tool result changes loop state
  -> next round recomputes the mode and re-applies the same process
```

The router replaces only the intent-map seed `I` (see "Exact offering
contract"). It does not replace the tool loop, its controller, its permission
gates, or progressive discovery.

## Code anchors (verified 2026-09-19)

All references are `src/services/llm.py` unless noted. These are the facts the
contract depends on; re-verify them if the code moves.

| fact | anchor |
|---|---|
| catalog: `BOT_TOOLS_SCHEMA` (**178** tools) | `llm.py:959` |
| authoritative mutation set `MUTATION_TOOLS` | `llm.py:3557` |
| Gmail-only allowlist `_GMAIL_ONLY_TOOLS` | `llm.py:3869` |
| turn entry `_chat_with_delilah_impl` (`prompt_text` param) | `llm.py:3946–3947` |
| audit activity check `_audit_is_active()` | `llm.py:4693` |
| gate `_tool_schema_for_mode()` | `llm.py:4699` |
| — fresh-verification fixed allowlist | `llm.py:4707` |
| — ordinary path: `core_tools.union(dynamically_loaded_tools)` | `llm.py:4767` |
| — Gmail-only branch | `llm.py:4765` |
| — audit-active branch (ignores dynamic set) | `llm.py:4775+` |
| `consecutive_no_tool_rounds` init | `llm.py:5621` |
| `_prompt_lower = str(prompt_text ...).lower()` | `llm.py:5857` |
| intent map `_INTENT_TOOL_MAP` | `llm.py:5871` |
| intent pre-seed into `dynamically_loaded_tools` | `llm.py:6017–6021` |
| `live_state_nudges` / `browser_action_nudges` | `llm.py:6110` / `6113` |
| main loop `while True` | `llm.py:6143` |
| defensive re-declare of `dynamically_loaded_tools` | `llm.py:6116` |
| concierge-only narrowing route | `llm.py:6177` |
| `MAX_TOTAL_TOOL_CALLS` guard | `llm.py:6186` |
| required-tool post-execution assertions | `llm.py:6566 / 6770 / 6891 / 6913` |
| `load_tool_schemas` unions into the dynamic set | `llm.py:7336` |
| `MAX_TOTAL_TOOL_CALLS` definition (default 100; 0 = unlimited) | `src/core/state.py:70` |
| `_get_embedding_*` / `ensure_collection` / `upsert_points` | `src/services/qdrant_client.py` |
| `READ_KINDS` (order_status/tracking/price_watch) | `src/services/concierge/browser_gate.py` |

## Current loop facts

Delilah's loop is `while True` in `_chat_with_delilah_impl` (`llm.py:6143`).

- `_tool_schema_for_mode()` is recomputed **every round** (`llm.py:4699`, called
  at `6171`).
- `dynamically_loaded_tools` persists and grows within a turn (`llm.py:6017`,
  re-declared defensively at `6116`).
- `load_tool_schemas` unions tools into that same set (`llm.py:7336`).
- `_INTENT_TOOL_MAP` (`llm.py:5871`) pre-seeds the ordinary dynamic set from
  keywords, **first matching entry wins**, before the loop (`llm.py:6017–6021`).
- Restricted routes replace or narrow the ordinary offering with fixed sets
  (see "Restricted modes").
- The loop has no-tool nudges (`live_state_nudges`, `llm.py:6110`),
  browser-action nudges (`browser_action_nudges`, `llm.py:6113`), user
  interruption (`USER_INTERRUPTS`), `end_turn`, and an optional
  `MAX_TOTAL_TOOL_CALLS` limit (`llm.py:6158`, default 100 at
  `state.py:70`).

## Model-callable semantic discovery (Item 5)

`search_tools(query, task_context)` is a model-facing control tool on the
ordinary path. It delegates retrieval and response shaping to
`ADVISOR_TOOL_REGISTRY.search_tools`; the registry invokes the existing
hybrid/lexical router, intersects results with the controller-provided scope,
and returns at most six canonical schemas with declared examples only. The
selected names are added to the dynamic schema offering for the next round.

The model-supplied context is search text only and cannot widen the allowed
name set. Delegated turns use `CURRENT_ALLOWED_TOOLS`; input-only replies use
their existing read-only allowlist; audit, fresh-verification, and Gmail-only
modes intersect with their fixed schemas. Missing dependency/credential checks
are reported as unknown availability, and unclassified effects are `unknown`—
neither discovery nor metadata is approval. The ordinary tool loop still
performs argument validation, grant/authorization checks, receipt recording,
and side-effect execution. Retrieval failure returns an explicit unavailable
result and preserves the existing `explore_domain` / `load_tool_schemas`
fallback.

Therefore the router should seed the ordinary path and leave the loop's
per-round recomputation and discovery escape hatch intact.

## Exact offering contract

Define these sets for every round:

```text
A  = full callable catalog = KNOWN_TOOLS = BOT_TOOLS_SCHEMA (178 tools)
G  = mode-level authorization universe: A on ordinary turns; the mode's fixed
     allowlist on restricted modes (audit / gmail_only / fresh-verification)
E  = context/tenant/state eligibility (offerability): tools the current
     tenant/state would accept at dispatch. Argument-dependent for conditional
     tools; not yet a centralized pre-dispatch filter in the code
P  = current permission partition: read, write, mixed, or conditional
D  = current deterministic domain partition
R  = required_tools for this turn/phase
C  = fixed core tools the current ordinary mode exposes (= core_tools literal)
Esc = permitted loop escape tools (end_turn, explore_domain, load_tool_schemas,
     enable_reasoning, verify_claim, …) — a subset of C
I  = tools contributed only by the current _INTENT_TOOL_MAP seed
L  = tools explicitly loaded this turn via load_tool_schemas
F  = mode-gated preserved baseline that must remain when I is replaced
     (F = C ∪ L ∪ {required tools already preserved}); F ⊆ G for every mode
T  = router top-k candidates retrieved from A, filtered by P ∩ D
B  = the current baseline offering returned by _tool_schema_for_mode()
O  = the offered set produced this round (see O_router below)
```

At ordinary-path turn start (before any tool call, so `L = ∅`):

```text
B = F ∪ I = C ∪ I
```

The router replaces `I`; it does not preserve the intent-map seed.

```text
F ⊆ G                                   # required for every mode
O_router = F ∪ (T ∩ G ∩ E)              # general form
# ordinary mode: G = A, so this reduces to
O_router = F ∪ (T ∩ E)
# restricted modes: router inactive
O_router = fixed_allowlist
```

`F ⊆ G` is not decorative: in the general form `F` is **unioned**, not
intersected, so if `F` contained a tool forbidden in the active mode it would
bypass `G`. `F` must therefore be the *mode-gated* preserved baseline (ordinary:
broad core tools; restricted: the mode's own allowlist, where the router is
inert and `O` is exactly the fixed allowlist).

The router is never allowed to compute an offering outside `G`. The router must
not replace `O_router` with `T`: it must not remove fixed/preserved tools,
retained required tools, `end_turn`, or permitted discovery tools just because
they were absent from the retrieval result.

### Eligibility `E`, the offerability filter, and dispatch authority

`E` is a **conservative suppressor**, not an eligibility authority: it cannot
inspect arguments, so it can only *prevent additions*, never establish that a
call will succeed. It does not exist today as a centralized pre-dispatch filter
— tenant gating (e.g. the concierge gate) lives *inside* tool implementations.
Two consequences:

1. **A tool-name-level `E` cannot be exact for conditional tools.** Eligibility
   for `request_concierge_action` / `concierge_browser_step` depends on the
   *arguments* (read vs write kind), so no set of tool names captures it. A
   tool-level `E` can only conservatively exclude tools that are never eligible;
   it cannot guarantee every offered call is accepted.
2. **`E` must be recomputed every round**, like `G` — tenant/state can change
   mid-loop (e.g. audit activation).

**Scope decision — does `E` apply to preserved tools or only to router
additions?** Today the code *deliberately* offers tenant-gated tools every round
and lets dispatch refuse: `request_concierge_action` sits in `core_tools`
("offered every round … the tenant gate refuses for other users"). Filtering the
preserved set with `E` would reverse that deliberate choice and is a **behavior
change**. The conservative first implementation therefore applies `E` only to
router-added candidates:

```text
O_router = F ∪ (T ∩ G ∩ E)
```

The broader form `(F ∪ T) ∩ G ∩ E` is a later, separately-justified change.

**Invariant — dispatch remains the security authority.** Offer-time filtering is
an optimization and UX guard unless it is *proven equivalent* to dispatch
authorization. `E` cannot be proven equivalent for argument-dependent
conditional tools, so dispatch checks must not be weakened or removed on the
grounds that the router already filtered. Naming: call this the **offerability
filter** if it is built; **optional preflight eligibility** if the first
implementation tolerates dispatch-rejected calls.

### Authorization versus presentation (the critical split)

`_tool_schema_for_mode()` returns the schemas **presently offered**; on the
ordinary path that is `C ∪ dynamically_loaded_tools` (`llm.py:4767`). It is not,
by itself, the authorization boundary. Treating `G` as the literal offering `B`
would make `T ∩ G` contain only tools already offered by the intent seed, so the
router could never add candidates — a ranking observer, not a replacement for
`_INTENT_TOOL_MAP`.

What the code actually authorizes, per mode:

- **Ordinary turns — authorization is the full catalog.** The dispatch guard
  rejects only names outside `KNOWN_TOOLS` (`llm.py:3500`, the 178-tool
  `BOT_TOOLS_SCHEMA`; guard `llm.py:7057`). The execution block states it
  directly: "Normal chat: all KNOWN_TOOLS are permitted" (`llm.py:7088–7089`).
  So on ordinary turns `G = A = KNOWN_TOOLS`. The router's candidate pool is the
  **full catalog**, not a curated subset.
- **Audit turns — authorization is the controller allowlist.** Enforced as a
  hard tool allowlist (`llm.py:6382`) and an execution block (`llm.py:7098`),
  both guarded by `_audit_is_active()`.
- **Fresh-verification / Gmail-only — fixed allowlists** enforced by their
  guards (`llm.py:6349`; `_GMAIL_ONLY_TOOLS` at `llm.py:3869`).

Consequences:

1. The abstraction needed is an **authorization/presentation split**, not a new
   curated policy list. Expose a `tool_authorization_for_mode()` returning `A`
   on ordinary turns and the fixed allowlist on restricted modes; keep
   `_tool_schema_for_mode()` as the offering builder. On ordinary turns
   `G = A`; on restricted modes `G == B`.
2. The router's candidate pool is `A`; its **output** is filtered by the
   eligibility set `E` (see "Eligibility `E` …"). `E` is not a centralized
   filter today — tenant gating lives *inside* tool implementations (the
   concierge tenant gate). Ranking over `A` is correct; offering a tool the
   tenant would refuse yields a wasted round or stall. In the first
   implementation `E` gates only router additions (`O_router = F ∪ (T ∩ G ∩ E)`);
   dispatch remains the security authority regardless.
3. Shadow's ordinary-turn counterfactual top-k must therefore be measured over
   `A`, not over the pre-router offering. The v3 evaluation already ranked over
   the 178-tool `BOT_TOOLS_SCHEMA`, so the eval methodology already matches
   this; the earlier `T ∩ _tool_schema_for_mode()` phrasing contradicted it.

The `authorized_tool_universe()` idea is correct in spirit but must not invent a
*policy-filtered* subset that does not exist: on ordinary turns that universe is
simply `A`. The one genuinely new thing to build is the explicit split between
"what is authorized" (`A` / fixed allowlist) and "what is offered".

### Required-tools handling (current behavior and target contract)

`required_tools` is **not** a generic union term in `_tool_schema_for_mode()`
today. It appears in exactly two roles:

- the `required_tools == {"concierge_browser_step"}` case is a special
  **narrowing** route (`llm.py:6149`);
- the general checks at `llm.py:6566 / 6770 / 6891 / 6913` assert required tools
  were **executed** and nudge/continue when they were not.

Required tools therefore do **not** generally enter the offered schema set
merely because they appear in `required_tools`. The router must not silently
implement a new `R → offered schemas` behavior. For the initial integration,
required tools are preserved only when already in `F` and permitted by `G`:

```text
R_preserved = R ∩ F ∩ G
O_router    = ((F ∪ T) ∩ G) ∪ R_preserved      # ≡ (F ∪ T) ∩ G
```

The second expression is intentionally equivalent to the first: it makes the
invariant visible rather than pretending `R` is an existing offering source. If
a required tool exists only because it was in the intent seed `I`, the router
must classify it as preserved before replacing `I`, or retain it through the
required-tool route. It must not silently drop it.

If a future design decides every required tool must be injected into the
offered schemas, that is a **separate controller change**, not router
integration or shadow mode. It must specify how `_tool_schema_for_mode()`
authorizes those tools, how restricted modes behave, and how the
post-execution assertions interact with the new offering.

### Mode-dependent `F`

The ordinary path subtracts world-model read tools when `awm_context` is
injected (`llm.py:4767–4771`). `F` must respect that: it is the set the active
mode actually exposes, not a static catalog slice.

## Provenance: set union is lossy

The runtime stores ordinary dynamic tools in **one** `set`,
`dynamically_loaded_tools`, with exactly two writers: the intent pre-seed
(`llm.py:6018`) and `load_tool_schemas` (`llm.py:7336`). "Exactly two writers"
bounds *where* mutation happens; it does **not** recover the information a set
throws away. Set membership is idempotent: if a tool is already present, a later
`load_tool_schemas([...])` for the same tool mutates nothing.

```text
I = {search_gmail, fetch_webpage}        # intent seed
load_tool_schemas(["search_gmail"])      # explicit request; set is unchanged
```

A snapshot + diff observes **no** evidence that `search_gmail` was explicitly
requested. If that is ignored:

- projected schema reduction is overstated — an explicitly requested tool is
  treated as disposable intent seed and dropped from `O_router`;
- the router may erase a tool the model explicitly asked for;
- the report cannot separate intent-map recovery from explicit discovery, and
  the counterfactual recall metric inherits the same error;
- a tool can legitimately have **dual provenance** (intent-seeded *and*
  explicitly loaded).

Therefore:

- a snapshot + diff is **approximate telemetry only**. It can prove a tool was
  *newly* added; it can never prove a present tool was *not* explicitly
  requested — absence of mutation is not evidence of absence;
- exact provenance needs a **per-turn local provenance map** — not durable
  global state — seeded from the pre-loop snapshot and updated by observing the
  `load_tool_schemas` dispatch (`llm.py:7335–7336`, where `args["tool_names"]`
  is recorded as *requested*, unfiltered):

  ```text
  provenance = {t: {"intent_seed"} for t in intent_seed_snapshot}
  # on each load_tool_schemas dispatch, for name in args["tool_names"]:
  #     provenance.setdefault(name, set()).add("explicit_load")
  ```

- provenance is a **source set** per tool: `intent_seed`, `explicit_load`, both,
  or `required`/`core`;
- when only the snapshot is available, label a present tool
  `intent_seed (explicit unknown)` rather than asserting it was not explicitly
  loaded;
- `explicit_load` — including dual provenance — promotes a tool into `F`, so the
  router cannot erase a tool the model explicitly requested.

This keeps shadow **zero-behavior-change** (no offering, set, or guard is
altered) while no longer being zero-code-touch: it observes the
`load_tool_schemas` dispatch site. The future `I → T` replacement consumes this
per-turn map directly; making it a first-class runtime field rather than a
shadow-only local is the separate durable change.

## Restricted modes that remain authoritative

The router must not alter these paths, in either offering or telemetry:

- fresh verification uses its fixed verification allowlist (`llm.py:4707`);
- Gmail-only turns use `_GMAIL_ONLY_TOOLS` (`llm.py:3869`, branch `4765`);
- active audits use the audit controller's phase-specific allowlist
  (`llm.py:4775+`, which ignores `dynamically_loaded_tools`);
- the required-only concierge browser route narrows to the existing browser
  route and its permitted companions (`llm.py:6149`).

For restricted modes, `G` already ignores the dynamic set and returns a fixed
state-specific set. The router is a no-op enrichment layer there — it may
collect telemetry, but it must not add or remove offered schemas.

"Never prune blocking tools" does not override a restricted-mode allowlist; it
means the router must not remove a blocking tool the active gate intentionally
exposes.

## Partitioning rules

Partitioning is a safety boundary, not merely a retrieval optimization.

1. Determine the mode and permission policy from controller state, not from the
   router's similarity score.
2. Determine the domain with deterministic gates where a domain has dangerous
   collisions. Concierge/browser cues route to the concierge domain; Gmail uses
   the existing mail-intent gate pattern (`query_targets_mail`).
3. Search only the catalog permitted by mode + permission + domain.
4. Keep conditional tools separate. `request_concierge_action` may be read-tier
   or write-tier depending on arguments; `concierge_browser_step` may observe or
   mutate depending on action. The router must not infer permission from the
   tool name alone.
5. Intersect the result with the policy universe `G` again immediately before
   schema offering; do not use the current offering `B` as a substitute for
   `G`.

The v3 evaluation found unfiltered hybrid retrieval placed a mutating candidate
in the top-8 for 33/58 read queries; after read/mutate partitioning, mutating
candidates were zero at top-1, top-3, and top-8 for every method. That zero is
enforcement by partitioning, **not** evidence that raw retrieval is safe.

## Router query contract

The router query may contain:

```text
credential-redacted prompt_text            # llm.py:3947, already redacted
mode=ordinary|gmail_only|audit|fresh_verification
permission_partition=read|write|mixed|conditional
browser_gate=true|false
domain_partition=<structured domain label>
```

The router query must **not** contain:

- raw passwords, tokens, payment data, or inline credentials;
- tool arguments;
- tool results;
- browser `[PAGE TEXT]`;
- scraped page text or screenshots;
- model-generated tool instructions;
- `required_tools` names as semantic query text.

`required_tools` remain hard constraints and logged fields. Embedding their
names into the query would hand the router the expected answer and corrupt the
recall measurement.

The router is retrieval-only, so prompt injection in a tool result cannot issue
instructions to it. It can still shift a query embedding toward a decoy tool —
the blast radius is bounded, not zero. If a future implementation adds an LLM
reranker, raw tool/page content becomes a hard security boundary and must stay
out of that reranker too.

## Retrieval method

The method under test is hybrid lexical + local embedding retrieval. The v3
evaluation found:

- embedding: 37% primary top-1, 94% acceptable top-8, MRR 0.516;
- lexical BM25: 41% primary top-1, 81% acceptable top-8, MRR 0.525;
- hybrid: 49% primary top-1, 94% acceptable top-8, MRR 0.610.

The router must **not** use hybrid top-1 as autonomous selection; its job is a
bounded candidate set. Lexical-only retrieval is the required degraded path if
Qdrant or the embedding backend is unavailable. A router failure becomes lexical
retrieval or a no-op, never an exception in the live loop.

## Caching and round behavior

Cache raw retrieval rankings by:

```text
(router_version, catalog_hash, prompt_hash, mode_signature, method)
```

The raw ranking may be reused across rounds when the user goal and method are
unchanged. The current gate, permission partition, domain partition, required
tools, and escape tools must be **re-applied every round**. Do not cache a gated
offering as if it were a raw ranking — mode can change mid-loop, including when
an audit activates after a tool call (`llm.py:6132`). Do not pay for another
embedding merely because the loop advanced with the same goal and mode.

## Safeguards (all required)

1. **Intersect, never widen the gate.** Router output is intersected with the
   current allowed set before it can affect ordinary offerings.
2. **Never persist unfiltered candidates.** Only permitted candidates are added
   to the ordinary dynamic set, and that set is re-filtered every round.
3. **Never prune permitted blockers.** Fixed/preserved tools, required tools
   retained in `F`, core tools, and permitted escape tools survive retrieval
   misses. The router does not invent a new required-tool injection path.
4. **Never route on page text.** Use the redacted user goal and structured loop
   state only.
5. **Preserve hard-coded routes.** Concierge browser, audit, Gmail-only, and
   fresh-verification routes remain controller-owned.
6. **Fail closed for permission, fail open for availability.** A policy/gate
   failure must not add tools; a router service failure may fall back to lexical
   retrieval or no-op.
7. **Bound work.** Router calls are async and timeout-bound; the flag-off path
   performs zero router imports and zero router work.

## Degradation matrix

| condition | required behavior |
|---|---|
| flag off | zero router work, zero router imports; loop byte-identical |
| embedding backend unavailable | lexical-only BM25 |
| Qdrant unavailable | lexical-only BM25 |
| router call times out | skip this round's telemetry; offerings unchanged |
| gate raises while computing `G` | no router candidate added; log; loop continues |
| shadow log write fails | swallow; loop unaffected |
| concurrent users, cold index | lazy build under a lock; existing turns unaffected |

A router failure must never raise into, or terminate, the live loop.

## Shadow mode

Off by default. Does not modify offered schemas. Observational only.

### What shadow can prove

- whether the advisor's actual selected tool would have appeared in raw top-k;
- whether it would have appeared in the shadow partition/domain
  counterfactual top-k;
- projected offered-schema count and reduction;
- router latency and cache behavior;
- baseline discovery/fallback behavior;
- real-traffic no-tool and repeated-call rates;
- which current routes are outside router scope.

### What shadow cannot prove

Because shadow does not change offerings, it cannot causally prove that enabling
the router would not create stalls, no-tool rounds, or repeated calls. Those
require a later controlled canary.

Shadow can identify *potential* prune-caused stalls: cases where the actual
selected tool is absent from the proposed gated candidate/fixed/required set.
That is not the same as observing a router-caused stall.

## Shadow query and sampling

Run every eligible ordinary turn during the initial measurement window, with a
configurable 1-in-N sampling knob for load reduction later. The promotion
dataset distinguishes ordinary eligible turns from restricted turns rather than
mixing them.

The embedding/query work must be async, timeout-bound, and error-swallowed. A
shared lazy index/client is guarded by a lock so concurrent users do not rebuild
the 178-card catalog.

## JSONL instrumentation contract

Append-only records under `data/tool-router-shadow/`. Use a separate analysis
script; do not compute aggregates in the hot path. Rotate or bound the log in
deployment so telemetry cannot grow without limit.

### Identity and reproducibility

- `schema_version`, `router_version`, `catalog_hash`, `query_hash`;
- opaque `turn_id` and `round_id`;
- timestamp and deployment/instance id for latency analysis.

### Controller context

- current mode;
- permission partition;
- deterministic domain / browser-gate result;
- required tools, and whether each was already in the baseline offering;
- permitted escape tools;
- whether the route was restricted and therefore router-inert.

### Baseline behavior

- intent-map seed `I` (via pre-loop snapshot, see "Provenance");
- fixed/preserved set `F`;
- tools explicitly requested via `load_tool_schemas` this turn (`explicit_load`),
  captured from the dispatch args — including names already present in the set;
- per-turn provenance map (source set per tool: `intent_seed` / `explicit_load` /
  both / `required` / `core`);
- current offered-schema count;
- actual selected tool, if any;
- selected-tool provenance: `core` | `required` | `intent_seed` |
  `explicit_load` | `intent_seed+explicit_load` (dual) | `intent_seed (explicit
  unknown)` | `previously discovered` | `newly loaded` | `other`;
- whether `explore_domain` / `load_tool_schemas` was used;
- discovery provenance and fallback reason.

### Counterfactual router behavior

- method used (`hybrid` | `lexical_fallback`) and fallback reason;
- raw top-k tool names and ranks;
- partitioned/domain counterfactual top-k tool names;
- whether the actual selected tool was in raw top-k / counterfactual top-k;
- projected preserved-tool count (incl. required tools already in `F`);
- projected router candidate count;
- projected post-router offered count;
- projected schema reduction percentage;
- score/floor decision.

### Performance and loop outcomes

- embedding latency, lexical latency, Qdrant latency, total router latency;
- cache hit/miss;
- tool-call count, round count, repeated identical-call count, no-tool-round
  count;
- baseline stall indicator;
- potential prune-miss indicator;
- baseline dispatch-rejection count/rate (calls refused at dispatch by
  tenant/policy gates) — this **sizes the `E` decision**: if the baseline is
  already ~0, an offerability filter adds complexity for little gain;
- tool success/rollback status when already available to the loop.

**Never** log prompt text, page text, tool arguments, credentials, raw tool
results, or screenshots. Tool names, ranks, hashes, and aggregate-safe state
labels are the maximum intended detail.

### As implemented (shadow-v1) — emitted field names

The intent above is the target; the shipped record is the following subset.
Emitted keys:

`schema_version`, `router_version`, `ts`, `turn_id`, `round_id`, `query_hash`,
`catalog_hash`, `catalog_size`, `mode`, `restricted`, `router_inert`,
`browser_gate`, `mode_signature`, `required_tools`, `required_in_baseline`,
`intent_seed_count`, `explicit_load_count`, `offered_count`, `end_turn`, `chosen`,
`method`, `fallback_reason`, `candidate_count`, `gated_candidate_count`,
`raw_topk`, `read_topk`, `write_topk`, `domain_topk`, `chosen_any_in_topk`,
`chosen_any_in_domain_topk`, `chosen_any_in_partition_topk`,
`chosen_any_in_counterfactual_topk`, `projected_core_count`,
`projected_offered_count`, `projected_reduction_pct`,
`projected_discovery_reduction_pct`, `chosen_unknown_names`, `latency_ms`,
`cache_hit`, `top1_score`, `error`.

Each `chosen[]` entry: `name`, `class`, `in_offering`, `provenance`
(`intent_seed` | `explicit_load` | `both` | `core_or_baseline` | `unobserved`),
`in_topk`, `in_domain_topk`, `in_partition_topk`, `in_counterfactual_topk`,
`rank`.

`chosen_unknown_names` is the set of chosen names absent from the catalog
(hallucinated/unknown). It is **not** the baseline *dispatch-refusal* count the
target list asks for — that needs instrumentation at the dispatch site and
remains deferred; the `E` sizing question therefore cannot be answered from
shadow-v1 telemetry alone.

Deliberately **not yet emitted** (planned, but not required for Stage A): the
instance/deployment id; the embedding/lexical/Qdrant latency split (only total
`latency_ms`); the full per-tool provenance map (only chosen tools are labeled);
explicit `explore_domain` / `load_tool_schemas` usage flags (inferred from
`explicit_load_count` and provenance); repeated-identical-call and no-tool counts
(derivable from `round_id` + `chosen`); the baseline stall / prune-miss
indicators; tool success/rollback; and the score/floor decision (the raw
`top1_score` is recorded so a floor can be calibrated offline).

**Labeling discipline.** `raw_topk` is *raw* retrieval; `in_partition_topk` /
`in_domain_topk` / `in_counterfactual_topk` are the partitioned and
browser-domain-restricted counterfactuals. `in_partition_topk` is membership in
`read_topk ∪ write_topk` (equivalent to per-tool-class partition, since a read
tool never appears in `write_topk`); `in_counterfactual_topk` additionally
requires membership in `domain_topk` on a browser-gated turn. Neither is the
full `G ∩ E` result — `E` is deferred — so report recall as *counterfactual*,
never as "gated" recall.

**Known leniency (do not over-read the recall metric).** `in_partition_topk` is
membership in `read_topk ∪ write_topk`, i.e. the union of *both* permission
pools. The eval (`tool_router_eval.py:allow`) instead partitions by the *query's*
kind, so a wrong-class tool (a mutating tool chosen on a read-intent turn — the
safety case the eval measures) is checked against the pool that happens to
contain it and counts as a hit. Runtime has no a-priori query kind, so this
self-selection is inherent to shadow: `counterfactual_recall_top8` is an
upper-bound-style counterfactual, **not** a safety-correct recall. A strict
query-kind partition is part of the deferred `E` work.

## Projected schema reduction

Do not compare raw router top-k with the entire 178-tool catalog. The meaningful
counterfactual is the offering the loop would actually receive:

```text
O_baseline = current _tool_schema_for_mode() offering
O_router   = (F ∪ gated_router_candidates) ∩ G
reduction  = 1 - |O_router| / |O_baseline|
```

For restricted modes, `O_router == O_baseline` by design and the turn is not an
ordinary router opportunity. A router must not claim reduction by removing
required or core tools. A `required_tools` entry absent from `O_baseline` is an
existing loop/controller condition to log and investigate — it is not silently
added by the router. On ordinary turns, tools the model loads through
`load_tool_schemas` become part of `F` for subsequent rounds; the router may not
erase them merely because they were not in its own top-k.

**Shadow approximation (as implemented).** `projected_offered` is
`core_est ∪ gated_topk` where `core_est = (offered − I) ∪ L` (preserving
explicit loads, subtracting only the intent seed `I`) and `gated_topk` is the
domain-gated candidate set (`domain_topk` on a browser-gated turn, else the raw
top-k). This matches `O_router = F ∪ (T ∩ G ∩ E)` to the extent shadow can: the
permission partition `T ∩ …` and `E` are still approximated by the unpartitioned
top-k on non-gated turns. Subtracting explicit loads here was a bug: it dropped
preserved tools from `O_router` and could falsely clear the reduction gate.

## Promotion gates

Two stages, because shadow cannot prove causal safety.

### Stage A — shadow gate (counterfactual, from the JSONL)

Evaluate ≥ 200 eligible turns and ≥ 500 tool calls, ordinary and restricted
modes reported separately. Require:

- actual advisor tool appears in the shadow partition/domain counterfactual
  top-8 on ≥ 90% of eligible tool-call rounds; this is not yet the full
  `G ∩ E` result because those offer-time filters remain deferred;
- median projected offered-schema reduction on ordinary **read** turns ≥ 20%
  (a read turn = ≥ 1 read call and no mutating call; conditional-only turns such
  as a lone `run_python_sandbox` are write-kind in the eval and excluded);
- router warm p95 latency ≤ 400 ms;
- lexical fallback available and exception-free;
- the hybrid path actually exercised (a total embedding outage degrades silently
  to lexical with no `error`, so an all-lexical run must not pass);
- no material increase in projected fallback/discovery pressure;
- all invariants below hold in the telemetry.

The Stage-A report enforces the sample minimum (`sample_sufficient`: ≥ 200
eligible turns **and** ≥ 500 tool calls) and the embedding-health check
(`embeddings_exercised`) as gate keys; a verdict below the minimum sample is
reported as **not decision-grade** and the analyzer exits non-zero.

"Actual tool in counterfactual top-8" is recall of *current* behavior, not proof
the router picks the correct tool or that full offerability has been enforced.
Primary-tool correctness still requires
labeled evaluation and later outcome metrics.

### Stage B — controlled canary gate

Enable only for a small ordinary **read-only** cohort. Require:

- zero router-caused prune stalls;
- no increase in no-tool rounds;
- no increase in repeated identical calls;
- no increase in tool failures or rollbacks attributable to offering changes;
- no permission or restricted-mode violations;
- schema/token reduction at or above the shadow estimate;
- immediate flag disable.

Only after Stage B passes should broader ordinary read-only traffic be enabled.
Concierge writes, spend actions, audits, fresh verification, and other
restricted paths remain out of scope until separately evaluated.

## Invariants (never violated)

- the router never removes a required tool already in `F` and allowed by the
  active gate;
- the router never silently injects a required tool the current controller did
  not offer;
- the router never widens the controller's allowed tool set;
- the router never grants execution permission;
- the router never changes approval, tenant, vault, SSRF, audit, or verification
  policy;
- the router never receives raw page text or secrets;
- the router never turns model/page text into executable selectors, arguments,
  code, or shell commands;
- restricted modes remain controller-owned;
- router failure cannot raise into or terminate the live loop;
- the router is never the only path to a needed tool, because progressive
  discovery remains available where the active gate permits it;
- the actual execution path remains the existing runtime validator and tool
  dispatcher;
- offer-time eligibility filtering (`E`) never replaces or weakens dispatch
  authorization — dispatch remains the security authority;
- `F ⊆ G` for every mode: the preserved baseline is already mode-gated, so the
  union `F ∪ (T ∩ G ∩ E)` cannot bypass `G`;
- router additions are exactly `T ∩ G ∩ E`;
- restricted modes receive no router additions — `O = fixed_allowlist`;
- conditional tools remain governed by their argument-aware runtime gates.

## Instrumentation comparison set

The shadow analysis must compare, by mode and turn type:

- current `_INTENT_TOOL_MAP` seed `I`;
- current progressive-discovery usage;
- router raw top-k;
- router partition/domain counterfactual top-k;
- actual advisor selection;
- projected offered-schema count;
- rounds, tool calls, repeated calls, no-tool rounds, and latency.

It should answer:

1. Does the router contain the tool the current advisor used?
2. Does it reduce schemas without removing fixed/required/escape tools?
3. Does it reduce discovery round-trips?
4. Does it preserve loop completion and safety behavior in the canary?
5. Does it improve the ordinary read-only path enough to justify its complexity?

## Open items (not yet decided)

- the concrete "acceptable projected miss rate" threshold for Stage A;
- the measurement-window duration and minimum per-mode sample;
- whether restricted modes are logged at all, or fully excluded from the
  promotion dataset;
- held-out calibration of the no-tool floor (the v3 floor is in-sample);
- build the offerability filter `E`, or declare it optional preflight and
  tolerate dispatch-rejected calls — decide from the baseline
  dispatch-rejection rate the shadow log records;
- multi-user concurrency and log-integrity handling;
- feature-flag granularity (single flag vs method/cohort flags).

## Non-goals

- multilingual (or otherwise non-English) browser/gate cues — the gate is
  English-only and hand-written;
- alias/synonym boosting (BM25 baseline only);
- any LLM/listwise reranker;
- injecting `required_tools` into offered schemas (a separate controller change);
- changing restricted-mode offerings;
- changing approval, vault, SSRF, audit, or verification policy.

## Verification log

2026-09-19 — this revision re-checked every code claim above against the
checked-out source:

- `_tool_schema_for_mode()` recomputed per round; ordinary path unions
  `dynamically_loaded_tools` (`llm.py:4699/4767`); Gmail-only (`4765`),
  fresh-verification (`4707`), and audit (`4775+`) branches ignore it.
- `required_tools` is never a union term; only a narrowing route (`6177`) and
  post-execution assertions (`6566/6770/6891/6913`).
- `dynamically_loaded_tools` has exactly two writers (`6018`, `7336`); but set
  union is lossy — an explicit `load_tool_schemas` for an already-present tool
  is unobservable by snapshot + diff. Exact provenance therefore needs a
  per-turn map fed by the `load_tool_schemas` dispatch args (`7335–7336`), not
  the diff alone.
- Ordinary-turn execution authorization is the **full catalog**: dispatch
  rejects only names outside `KNOWN_TOOLS` (`llm.py:3500`, guard `7057`; comment
  "all KNOWN_TOOLS are permitted" at `7088–7089`). The hard tool allowlists
  (`6382`, `7098`) and the fresh-verification guard (`6349`) are mode-restricted,
  not ordinary. So the ordinary authorization universe is `A`; the router must
  draw candidates from `A`, not from the pre-router offering.
- `MAX_TOTAL_TOOL_CALLS` default 100, 0 = unlimited (`state.py:70`).
- v3 retrieval figures and the 33/58 hybrid top-8 unsafe count match
  `docs/tool-router-eval-corrected.md`.

2026-09-19 — **implementation of the shadow path** (`TOOL_ROUTER_SHADOW`,
default `0`). Files: `src/services/tool_router.py` (new),
`src/core/state.py` (flags `TOOL_ROUTER_SHADOW` / `TOOL_ROUTER_SHADOW_SAMPLE`),
`src/services/llm.py` (three gated hooks), `scripts/tool_router_shadow_report.py`
(new), `tests/test_tool_router_shadow.py` (new, 16 tests).

- Hooks (all inside `shadow_enabled()`; no offering, guard, or dispatch change):
  turn-init snapshot after the intent pre-seed (`llm.py:6025–6052`); per-round
  record after tool-call normalization and `end_turn` stripping
  (`llm.py:6312–6377`); provenance observation at the `load_tool_schemas`
  dispatch (`llm.py:~7336`).
- Provenance is a per-turn map, per this contract: the dispatch args are
  observed directly, so an explicit load of an already-seeded tool is recorded
  (`provenance = intent_seed | explicit_load | both | core_or_baseline |
  unobserved`).
- Restricted modes (audit / fresh-verification / gmail-only / the required-only
  concierge continuation) are inert: `router_inert: true`, no retrieval.
- Deviations from the eval harness, recorded deliberately: (1) lexical scores
  are min-max normalised **per query** (the eval normalised across the whole
  battery, which is unavailable at runtime); (2) the tool-card index is built by
  a **background task** (`schedule_index`), never on a turn's critical path, so
  a hung embedding/Qdrant backend cannot make every turn pay `INDEX_TIMEOUT_S`
  — `propose()` degrades to lexical until the index is ready; (3) the BM25 index
  is cached per catalog revision.
- Verified in-container (image rebuilt, `--force-recreate`): shadow+report tests
  green; full non-browser suite 634 passed; warm hybrid retrieval 42–50 ms,
  cache hit 0.1 ms, cold index build ≈3–5 s (background, off the turn path);
  flag-off leaves no `data/tool-router-shadow/` directory and imports no router
  code; flag-on writes `data/tool-router-shadow/YYYY-MM-DD.jsonl` and the
  Stage-A report runs.
- Still deferred to enablement (Stage B): the A/G authorization-presentation
  split, the eligibility filter `E`, and any change to offered schemas.

2026-09-19 — **measurement-layer corrections** (same shadow path, no runtime
change): the Stage-A report was overstating what it measured.

- Recall is now labeled honestly: `raw_topk` (raw retrieval) vs
  partitioned (`read_topk ∪ write_topk`) vs browser-domain-restricted vs the
  combined *counterfactual* top-k. The promotion gate uses the counterfactual
  metric and does **not** claim to be the deferred `G ∩ E` filter
  (`tool_router_shadow_report.py`; record fields `in_partition_topk`,
  `in_domain_topk`, `in_counterfactual_topk`, `chosen_any_in_*`).
- Timeout accounting fixed: a failed proposal has no `method` and so is not
  "routed"; errors/timeouts are now counted over all ordinary records
  (`tool_router_shadow_report.py:analyze`). Previously a timeout could pass the
  "exception-free" gate.
- No-tool rounds and their top-1 score are reported descriptively (no held-out
  floor applied yet).
- Embedding fallback path repaired (the double `await` in `_index_cards`).
- Added `tests/test_tool_router_shadow_report.py` (report accounting/labeling)
  and counterfactual/domain/partition coverage in
  `tests/test_tool_router_shadow.py`.
- Re-verified in-container: **72 passed** across the router, report, and
  regression subsets (`test_progressive_disclosure`, `test_advisor_tools`,
  `test_llm_browser_loop`). The earlier browser-loop "hang" was host-environment
  only; it passes in the container in <1 s.

2026-09-19 — **second review pass: correctness + honesty fixes** (still shadow,
no runtime change).

- **Projection bug (fixed):** `core_est` subtracted explicit loads from the
  preserved set, so a loaded-but-not-top-k tool was dropped from `O_router` and
  the ≥20% reduction gate could falsely pass. Now `core_est = (offered − I) ∪ L`
  (see "Shadow approximation" above); candidate additions use the domain-gated
  set, not the raw top-k. New tests `test_projection_preserves_explicit_loads`,
  `test_projection_uses_domain_gated_candidates_when_gated`.
- **Recall leniency documented:** `in_partition_topk` is both-pool union
  membership, an upper-bound counterfactual, not a query-kind-partitioned
  safety recall (see "Known leniency" above).
- **Sample gate added:** `gate()` now requires ≥ 200 eligible turns and ≥ 500
  tool calls (`sample_sufficient`); below that the report is marked
  **not decision-grade** and `main()` exits non-zero. A total embedding outage
  (all-lexical, no `error`) is caught by the new `embeddings_exercised` gate key.
- **Read-turn definition tightened:** `_is_read_turn` now requires ≥ 1 read call
  and no mutating call, so a conditional-only turn (e.g. lone
  `run_python_sandbox`, write-kind in the eval) no longer inflates the
  read-turn reduction population.
- **Restricted-mode telemetry:** the required-only concierge browser
  continuation (`required_tools == {"concierge_browser_step"}`, `llm.py:6177`)
  is now labeled `mode="concierge_continuation"` and treated as restricted, so
  the router is inert in both offering *and* telemetry for it (per "Restricted
  modes that remain authoritative").
- **Honest field rename:** `dispatch_rejected` → `chosen_unknown_names`
  (hallucinated/unknown chosen names — *not* the baseline dispatch-refusal
  count the target telemetry list asks for). Added `gated_candidate_count`.
- **Flag parser hardened (`src/core/state.py`):** `TOOL_ROUTER_SHADOW=true`
  previously raised `ValueError` at import; `_env_int_flag` now accepts
  `true/yes/on` (and `false/no/off`) and never crashes on a non-numeric value,
  matching `tool_router.shadow_enabled()`. Test
  `test_env_flag_parser_accepts_truthy_and_never_crashes`.
- Still deferred (unchanged): the A/G authorization-presentation split, the
  eligibility filter `E` (incl. a strict query-kind permission partition), any
  offered-schema change, and true baseline dispatch-refusal instrumentation.

2026-09-19 — **third review pass: hook correctness + latency** (still shadow, no
runtime change; independent read-only review of the three `llm.py` hooks).

- **Fresh-verification mode was dead code (fixed):** the hook tested
  `_audit_is_active()` before `require_fresh_verification`, but
  `require_fresh_verification ⇒ audit_batch_mode ⇒ _audit_is_active()`, so every
  fresh-verify round was mislabeled `audit` and no `fresh_verification` rows
  existed. The hook now mirrors `_tool_schema_for_mode()` precedence (fresh
  verification first).
- **Index build moved off the turn's critical path (fixed):** the turn-init hook
  `await`ed `ensure_index()` under `INDEX_TIMEOUT_S`; a hung embedding/Qdrant
  backend never set `_index_hash`, so *every* sampled turn paid up to 20 s.
  `schedule_index()` now runs the build as a background task (with a retry
  cooldown) and `propose()` degrades to lexical until the index is ready.
  Tests `test_schedule_index_is_non_blocking_and_cooldown_guarded`,
  `test_schedule_index_noop_when_ready`.
- **Confirmed sound by the review:** flag-off is provably inert (no router
  import, no writes, loop byte-identical; router referenced only inside the two
  guarded sites); every hook variable is bound at its site; round accounting is
  exactly once per model round (no skips/double-counts); restricted modes do no
  retrieval; provenance recovers the lossy set-union via the real dispatch args.
- **Anchors refreshed** in the reference tables (loop `6143`, dispatch `7336`,
  concierge narrowing `6177`, tool-schema call `6171`).
- Minor, accepted: `write_record` does a small synchronous append on the loop,
  and `mode_signature` carries only `(mode, browser_gate)` (partition fields are
  re-applied at `build_record`, so measurement is unaffected).
