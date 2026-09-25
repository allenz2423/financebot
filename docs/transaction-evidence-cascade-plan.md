# Transaction Evidence-Controlled Classification Plan

## Objective

Make transaction categorization a provenance-controlled cascade. Merchant
identification and category selection are separate decisions. No processor
token, weak search result, registry cache, KEV prediction, or local-LLM answer
may become accounting truth without passing the controller's evidence and
policy gates.

## Required workflow

```text
transaction
  -> descriptor resolver
  -> trusted registry check
       human-confirmed/curated -> deterministic candidate
       auto-cache/unknown       -> continue
  -> KEV dimensions and constrained route
       confident -> policy candidate
       uncertain -> identity search
  -> typed search evidence gate
       strong identity -> local LLM adjudication
       weak/ambiguous/no identity -> abstain or local LLM with abstention
  -> closed-taxonomy local-LLM result
  -> policy gate
       accepted -> classification-only apply + audit
       uncertain/invalid/conflicting -> human review
  -> human answer -> correction log + optional trusted alias promotion
```

`Uncategorized Purchase` is an outcome of abstention, never a normal KEV
choice. Processor evidence is metadata, never merchant identity evidence.

## Implementation phases

### 1. Descriptor and provenance

- Add a canonical descriptor object containing raw text, processor tokens,
  merchant candidate, location/reference tokens, and normalization version.
- Ensure registry, search, KEV, and local LLM all consume the same descriptor.
- Return registry source, trust tier, evidence URL, and update time.
- Permit deterministic bypass only for `human_confirmed` and `curated` records.
- Treat `auto_cache`, model, imported, and heuristic records as advisory.

### 2. Typed identity evidence

- Search exact merchant candidate strings, not processor-prefixed descriptors.
- Preserve exact-query intent or validate exact phrase presence in returned text.
- Exclude `AplPay`, `Apple Pay`, `PayPal`, `Square`, `Stripe`, `Venmo`,
  `Clover`, `TST`, `SP`, and similar rail tokens from identity scoring.
- Return `NO_IDENTITY`, `WEAK_IDENTITY`, `STRONG_IDENTITY`, or
  `CONFIRMED_IDENTITY`, plus detailed reasons and source metadata.
- Reject processor-only, generic-token-only, and unrelated-domain results
  before they reach an LLM.

### 3. Unified cascade

- Keep KEV dimensions-first and constrained taxonomy routing.
- Route uncertain KEV results through identity search, then the existing local
  classifier with a new structured closed-taxonomy contract.
- Remove the separate quick-search-to-KEV retry behavior from the operational
  fallback path, or make it an optional evidence-enrichment stage only.
- Make local LLM results explicitly accept, abstain, or human-review.
- Never mark a transaction evaluated merely because an LLM returned text.

### 4. Human review and learning

- Persist a durable review request when identity or category evidence is weak.
- Ask separately for merchant identity and category when necessary.
- Apply human answers through the correction/audit path.
- Promote an alias or registry record only from explicit human confirmation.
- Preserve the original prediction and all rejected evidence.

### 5. Evidence lineage and observability

Record per attempt:

- descriptor normalization and processor tokens;
- registry match, source, trust tier, and category;
- search query, query kind, provider, URLs, snippets, and evidence scores;
- KEV model, request IDs, dimensions, candidates, probabilities, and margins;
- local model, validated output, taxonomy path, confidence, and abstention;
- policy version, final decision, human review, latency, and errors.

Do not log complete financial histories or unnecessary sensitive values.

## Test plan

The repository currently collects more than 400 tests. The implementation will
add focused coverage for:

- descriptor parsing and processor removal;
- registry trust tiers and emoji/legacy normalization;
- exact search query construction;
- processor-only and generic-token rejection;
- strong phrase and first-party evidence acceptance;
- conflicting and weak search evidence;
- KEV constrained routing and abstention;
- local LLM schema, taxonomy, confidence, and evidence gates;
- human review creation and correction promotion;
- no ledger mutation on failed/uncertain paths;
- audit/evidence lineage correlation;
- regression cases for `AplPay WE ARE SISTERBROOKLYN`, Google One,
  PureGym, Discord, Shippo, plan fees, and transfers;
- SearXNG/KEV smoke tests where services are available.

## Completion criteria

- A processor-only SearXNG result cannot unlock classification.
- `auto_cache` cannot bypass KEV/search/human review.
- Weak identity causes abstention or human review, never silent evaluation.
- Trusted human-confirmed merchants can still classify deterministically.
- All internal categories are canonical, plain-text hierarchical paths.
- Full existing test suite passes or unrelated pre-existing failures are
  explicitly reported.
- At least 200 tests execute in the final verification run.
- An ntfy notification reports the implementation and test results.

## Deferred reliability item: unlocked-transaction audit must complete mutations

Priority: lower than the taxonomy/evidence work, but required before this
workflow is treated as reliable.

### Observed failure

An unlocked-transaction request successfully performed merchant research for
three records, but repeated turns continued to run `search_web` and reported
that the records were “ready” without executing the requested classification
and lock mutations. The eventual response correctly admitted that no lock had
run, but the controller still failed to transition from research to execution
after the user explicitly said “go”.

The failure is not merely a missing tool call. It is a workflow-state and
claim-authority problem:

```text
research completed
  -> mutation intent was not preserved or re-read
  -> mutation tools were unavailable/blocked
  -> the loop repeated research
  -> the user received progress language instead of a terminal blocked result
```

### Required behavior

For a request such as “classify the unlocked transactions and lock them”:

1. Fetch a fresh, user-scoped set of unlocked transaction rows.
2. Research only merchants that actually need evidence.
3. Produce a structured classification proposal for each row, retaining the
   row ID, transaction fingerprint, evidence references, and proposed path.
4. Re-fetch the rows immediately before mutation and reject stale, missing, or
   changed records rather than acting on old IDs.
5. Apply classification through the existing correction/audit path, if the
   policy permits it.
6. Call the lock mutation with the exact successfully classified row IDs.
7. Verify the mutation result from authoritative database state, not from the
   model's prose.
8. Return one receipt per row with `classified`, `locked`, `skipped`, or
   `unknown` status and the reason for any non-success.

If mutation tools are unavailable, denied, or fail, the controller must stop
the research loop and return a terminal blocked result that says exactly which
side effect did not happen. It must not repeat search merely because the
requested mutation was not executable.

### State-machine change

Represent this workflow explicitly rather than relying on conversational
continuation:

```text
DISCOVER_UNLOCKED
  -> RESEARCH_REQUIRED
  -> CLASSIFICATION_PROPOSED
  -> FRESHNESS_CHECK
  -> CLASSIFICATION_APPLY
  -> LOCK_APPLY
  -> LOCK_VERIFY
  -> COMPLETE
```

Failure states are terminal for the current turn:

```text
STALE_ROWS | CLASSIFICATION_FAILED | LOCK_FAILED | LOCK_UNKNOWN | BLOCKED
```

Each transition must be recorded in the turn/receipt trace. A transition back
to `RESEARCH_REQUIRED` is allowed only when new evidence is requested or a
specific row changed; an ordinary “go” confirmation must never restart the
same research cycle.

### Tool and receipt implications

- Add an explicit workflow intent such as `classify_and_lock_unlocked`, or
  persist a controller-owned pending mutation plan keyed by user and turn.
- Give the plan an idempotency key derived from the fresh row IDs,
  transaction fingerprints, classification version, and requested operation.
- Require `batch_correct_transactions` and `batch_lock_transactions` to emit
  durable receipts containing attempted IDs, accepted IDs, rejected IDs,
  database verification results, and any unknown outcomes.
- If correction succeeds but the lock call or result append fails, retain the
  correction receipt and mark the lock state `unknown`; never claim that the
  rows are locked and never blindly retry without consulting the receipt.
- A successful search receipt can support an evidence claim only. It cannot
  satisfy a classification or lock claim.

### Test coverage to add

- Research completes, then classification and lock tools execute once.
- A repeated “go” does not repeat search or duplicate mutations.
- Mutation tools being unavailable produces `BLOCKED`, not another research
  round.
- Stale row IDs are rejected after the pre-mutation re-fetch.
- Partial classification/lock results produce per-row receipts.
- A successful classification followed by an unknown lock outcome cannot be
  rendered as “locked”.
- A result/history append failure leaves the side-effect receipt recoverable.
- Final user-visible text is allowed to say “locked” only after authoritative
  lock verification.

### Implemented controller fixes

- Enabled the previously disabled `save_known_merchant` state transition.
- Preserved audit research receipts across turns so autonomous wakeups do not
  reject valid prior research as if it never happened.
- Corrected the missing/typoed batch-tool names in the audit allowlists.
- Added an explicit `pending_lock_ids` phase: successful batch correction
  permits only the matching batch lock call next.
- Required a fresh transaction getter after merchant persistence and after
  locking.
- Converted no-tool narration and premature `end_turn` into a precise next
  action instead of allowing the controller to finish or restart research.

## Generic capability: bounded research packets

The implementation also adds a generic read-only `research_topic` tool. It is
deliberately domain-neutral and can support technical investigation, product
comparison, organization research, travel planning, fact checking, and
merchant identity work.

Its contract is:

```text
query variants
  -> bounded parallel search
  -> URL canonicalization and deduplication
  -> score-ranked source cards
  -> bounded fetch of top sources
  -> source IDs, excerpts, URLs, query provenance, and failure status
```

The tool does not synthesize an unsupported conclusion, persist claims, or
silently treat a search result as truth. It returns an evidence packet so the
existing controller can inspect source quality, conflicts, and freshness. The
fetch budget, source count, query count, and excerpt size are capped in code;
failed fetches remain visible in the result rather than disappearing.

This gives Delilah a reusable research primitive that is more powerful than a
single search call while preserving the existing web authorization and
evidence boundaries.
