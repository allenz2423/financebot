# Delilah Reliability and Tool-Calling Architecture Roadmap

Status: implementation roadmap and working memory

This document records the review, the decisions made during the design discussion, the agent critiques, the current code facts, and the implementation order. It is intentionally prescriptive: future code changes should be checked against this document rather than relying on prompt instructions or memory.

## Implementation status

Phase 1 and the first fallback-safety increment are now implemented:

- src/services/claim_evidence.py detects explicit completed-action claims and matches them against successful legacy or enriched tool traces.
- src/services/llm.py applies a final claim gate, performs one tool-free repair attempt, and uses a deterministic safe fallback if the repair still makes an unsupported claim.
- Live previews are disabled by default behind ADVISOR_LIVE_PREVIEW=1 until the preview path is receipt-aware; this prevents unverified streamed prose from reaching the user before final validation.
- src/services/tool_contract.py assigns Delilah-owned IDs to fallback or malformed provider calls while preserving provider IDs when present.
- Fallback and sentinel calls are marked with private native/fallback origin metadata; that metadata is stripped from provider history but retained at execution.
- Fallback side-effecting tools are rejected before dispatch. Existing read-only fallback/form recovery remains available.
- turn_tool_trace now records status, complete, call_id, receipt_id, and origin in addition to name and ok.
- src/services/tool_receipts.py now provides an explicit SQLite receipt store with PREPARED -> STARTED -> CONFIRMED/FAILED/UNKNOWN lifecycle transitions. The advisor writes the pre-dispatch lifecycle before invoking a tool and marks terminal persistence failure as UNKNOWN.
- src/services/provider_protocol.py now parses OpenAI-compatible and Ollama stream events without treating prose as calls, and preserves finish_reason. Provider-round metadata is recorded with route profile, requested model, provider order, native-call count, and latency.
- src/services/route_profiles.py adds explicit Delilah-owned model/provider profile parsing and rejects automatic model aliases for tool-enabled profiles. OpenRouter requests now send no-failover/parameter-required provider controls when using that transport.
- Autonomous wakeups now default to local Ollama inference; WAKEUP_PROVIDER and WAKEUP_MODEL remain explicit overrides.
- src/services/browser_evidence.py now distinguishes confirmed observation, navigation, mutation, and approval-required browser events; the live-state guards consume confirmed observation evidence instead of stubs.
- src/services/tool_results.py provides compact typed envelopes with status, completeness, facts, provenance, side-effect class, and evidence references.
- Implicit inline and background model-memory persistence is disabled by default behind DELILAH_INLINE_MEMORY and DELILAH_BACKGROUND_MEMORY, preventing silent model-derived mutations during migration.
- The persisted authoritative turn metadata now includes the final claim decision and unsupported-claim fingerprints alongside provider-round and receipt correlation data.
- src/services/tool_grants.py provides single-use, expiring, target-bound grants with argument constraints and approval enforcement.
- src/services/progress_policy.py provides progress/unknown-outcome decisions; the advisor stops after repeated unknown external outcomes without imposing a normal-workflow round cap.
- src/services/result_contracts.py now filters approved fact fields for high-risk/common tools before they enter result envelopes.
- ReceiptStore now persists provider rounds, claim decisions, receipts, and their turn correlation in structured SQLite tables.
- ReceiptStore now also persists semantic-router decisions: proposal mode, authorization/fail-open result, allowed candidates, score, and latency, all keyed by turn and round.
- Execution creates and consumes a target-bound grant immediately before receipt preparation and dispatch.
- The semantic router now has an explicit `TOOL_ROUTER_LIVE=1` promotion path. It can only narrow the current controller schema, preserves required/controller escape tools, fails open on retrieval failure, and rejects an unauthorized dispatch before execution.
- OpenRouter automatic model aliases are rejected; the compose default selects an explicit model/profile with no provider failover. Cloud/local choice remains configurable, while autonomous wakeups default local for cost containment.
- A deterministic prompt-behavior battery covers false claims, failed/unknown calls, typed results, and wrong routing. `tests/test_local_model_e2e.py` provides a runnable native-tool Ollama workflow when `DELILAH_LOCAL_E2E=1` is set.
- Kev integration is now isolated in `src/services/kev_decision.py`, uses the upstream `/v1/systemone` and `/v1/models` contracts, sends bounded tool descriptions, validates the closed typed response, supports explicit local/cloud selection, and records shadow/enforcing decisions without replacing receipts or policy gates. See `docs/kev-integration.md`.
- Focused validation currently passes: 88 tests (one opt-in local-model test skipped), including claim-evidence, tool-contract, receipt lifecycle, provider protocol, route profiles, typed results, result contracts, browser evidence, grants, progress policy, form-recovery, Gmail batching, router live/shadow behavior, and browser-service tests. Compilation passes. Local PDF form discovery and bounded DNS resolution now pass their isolated tests. The broader suite still contains external-network acceptance paths (notably `tests/test_forms.py::test_live_find_government_forms_georgia`), so it is not counted as a full-suite product pass without network fixtures.

## Core invariant

**No successful tool receipt means no claim that the tool action happened.**

The stronger operational form is:

- No action claim without a confirmed, matching receipt.
- No sensitive or result-fact claim without receipt-backed provenance.
- No externally visible side effect without a durable pre-dispatch record.
- A provider's text saying that it called a tool is never evidence that the tool ran.
- A parsed tool call is a request, not an execution.
- Authorization is not execution.
- Execution is not confirmation until the tool returns a valid result and the receipt is durably recorded.

The receipt/evidence layer must be common infrastructure underneath the existing artifact, browser, approval, memory, and other narrow safeguards. Existing safeguards should be retained and adapted to emit evidence; they should not be bypassed or replaced by a weaker generic check.

## Problem being fixed

Delilah currently has inconsistent tool-call behavior across Ollama, OpenAI-compatible providers, and OpenRouter. In particular:

- Ollama is comparatively good at native tool calling.
- OpenAI-compatible providers/models sometimes produce ordinary text that claims a tool was called without producing a structured tool call.
- OpenRouter adds a routing layer whose automatic failover/model selection can select an unintended provider or a low-throughput route.
- Fallback parsing accepts several textual syntaxes and currently makes it possible for model prose to be interpreted inconsistently.
- A final prose response can be accepted even when it contains a promise or claim that no corresponding tool execution supports.
- Browser evidence guards exist, but `_observed_live_browser()` and `_browser_tool_ran()` are stubs returning `False`.
- Tool traces are mostly ad hoc dictionaries recorded after execution, rather than durable lifecycle records.
- Tool results are not consistently typed or provenance-bearing.
- The controller loop is monolithic, making protocol, policy, execution, rendering, and receipt behavior hard to reason about.
- Tool presentation, routing, authorization, execution, and user-visible claims are not yet expressed as one deterministic state machine.

## Decisions made

### Protocol and fallback policy

1. Native structured calls are the canonical executable path.
2. Fallback parsing is isolated from native parsing and is never treated as equivalent merely because it found JSON-like text.
3. Native-only mutations are the recommended safety policy.
4. Text fallback may remain available for safe, idempotent legacy reads or form recovery where explicitly allowed.
5. Cloud fallback mutation calls are not executed.
6. If the model does not obey the native-call requirement for a mutation:
   - record the malformed/non-native request;
   - reject it as an executable mutation;
   - issue one repair prompt requiring a native structured call;
   - if the repair still fails, return a safe blocked response and do not mutate.
7. The initial plan had considered executing all normalized calls, but agent review rejected that as unsafe for mutations. The final policy is native-only for mutation side effects.

### Provider and OpenRouter routing

1. Delilah owns the model/provider route decision.
2. OpenRouter automatic routing/failover is not trusted as the policy decision for tool-enabled workflows.
3. Routes must be explicit, validated profiles containing the intended model and provider.
4. Reject ambiguous aliases such as automatic/free model pools for tool routes unless explicitly approved by a separate profile.
5. Request provider controls equivalent to `allow_fallbacks: false`, `require_parameters: true`, and an explicit provider allowlist/order where supported.
6. Record both the requested route and the actual provider/model returned by the gateway.
7. Delilah may switch to another explicit approved route before a receipt is prepared; it must not silently switch after a side effect has entered its execution lifecycle.
8. On failure, use a deliberate route retry policy: retry the same route when appropriate, otherwise choose another Delilah-approved profile, or fail safely. Never let an implicit gateway failover decide.

### Cloud and local inference

Cloud and local inference are both supported execution modes. This is a cost and operational choice, not a quality moral distinction.

- Interactive workflows may use the selected cloud or local route.
- Autonomous wakeups should default to local inference so a failure/death spiral cannot consume paid inference indefinitely.
- The autonomous provider/model policy must be explicit and separately observable.
- Cloud spend budgets, route profiles, and emergency disable controls remain available for interactive use.

### Router authorization

The router may be allowed to determine both the relevant tool and its permission, but only under strict constraints. “The router can never authorize” is not an invariant by itself.

The safe model is:

- The registry defines the tool and permission metadata.
- The router emits a single-use, target-bound grant.
- The grant contains the tool, normalized target/resource, operation, allowed arguments or argument constraints, risk class, expiration/turn ID, and approval requirement.
- The dispatcher revalidates the grant and tool schema immediately before execution.
- A router cannot grant a broader capability than the registry/policy permits.
- Grants are not broad turn-wide authority; the selected design is multiple narrow grants for multiple selected targets when each target has been independently normalized and allowed.
- Approval-required and high-impact operations still require their approval state even if the router identifies the correct tool.

### Loop and budget policy

Do not impose a simplistic global round/call cap on normal interactive workflows. Legitimate workflows can require many steps.

Instead detect pathological behavior using progress and resource policies:

- repeated identical call fingerprints;
- repeated failed calls with no changed inputs;
- repeated malformed tool-call repairs;
- deadlock/no-progress rounds;
- repeated refreshes with no changed state;
- unknown side-effect outcomes;
- autonomous deadline and work budget;
- cloud spend budget;
- maximum concurrent work;
- per-tool retry policy and cooldowns;
- emergency kill switch;
- alert-rate limits.

Parallelism is explicit:

- parallelize only safe, declared-independent reads;
- execute writes, browser actions, approval-required calls, and fallback calls sequentially;
- mixed batches are sequential unless a specific safe-read subset is extracted.

### Semantic routing

Embeddings/classification should own semantic intent and domain routing. Deterministic logic remains necessary for:

- security boundaries;
- permission and approval checks;
- exact tool-name and schema validation;
- high-risk mutation detection;
- idempotency and deduplication;
- safety-critical negative constraints;
- final evidence/claim validation.

Ambiguous classifier results should not guess. They should narrow the offered tools, ask a clarification, or route to a safe read-only path. Embeddings are not authorization by themselves.

### Presentation, authorization, and execution

These are separate phases with a controlled relationship:

1. Presentation determines what tools are shown/offered for the current request.
2. Authorization determines which target-bound operations may be attempted.
3. Execution validates the grant and schema, writes the lifecycle record, invokes the tool, and produces a receipt.

The router may participate in presentation and may issue a constrained grant, but execution is the final enforcement boundary. Tool schemas, risk policy, result contracts, route eligibility, and grant validation must remain independently testable. Avoid one god-object registry.

### Typed results and receipts

Tools should migrate to a result/receipt envelope rather than returning arbitrary strings:

```text
ToolResultEnvelope {
  schema_version
  tool_name
  call_id
  receipt_id
  ok                 # execution completed according to the tool contract
  complete           # result is complete; false means partial/in-progress/unknown
  status             # prepared | started | confirmed | partial | failed | unknown
  error              # typed, redacted error when applicable
  summary            # compact model-facing text
  facts              # bounded, typed facts approved by the result contract
  provenance         # source, timestamps, resource/target references, confidence
  side_effect        # none | read | mutation | external_unknown
  evidence_refs      # references to durable records, not unbounded payloads
  redaction          # what was removed before model/user presentation
}
```

Semantics:

- `ok=true, complete=true, status=confirmed` is the normal receipt that can support a successful action claim.
- `ok=true, complete=false, status=partial` supports only explicitly partial claims.
- `ok=false` supports a failure claim, not a success claim.
- `status=unknown` means the external effect may have occurred and must never be reported as definitely absent or definitely successful.
- Existing string-returning tools can be wrapped by an adapter that puts the string into `summary`, marks `facts` empty, and assigns a conservative status. The model receives the compact summary; the controller retains the full evidence reference.
- Start with result contracts for roughly 10–15 high-risk or commonly misrepresented tools.

External side effects need a durable lifecycle such as:

`PREPARED -> STARTED -> CONFIRMED`

with terminal alternatives `FAILED` or `UNKNOWN`. A timeout or append failure after dispatch is `UNKNOWN`, not success or failure by assumption.

### Memory automation

`auto_extract_and_persist_claims()` is currently an out-of-band mutation. It must eventually become a controller-owned receipt-producing action with policy and consent. During migration it should be disabled, shadowed, or explicitly gated; it must not silently persist model-inferred claims merely because a response was generated.

## The 20 review items and concrete implications

### 1. Large tool surface

Issue: too many tools are presented and permissioned as one flat surface.

Solution: partition tools by domain and permission/risk class. Build registry metadata for domain, operation, side-effect level, approval requirement, route eligibility, result contract, idempotency, and autonomous eligibility. The router selects a small relevant set, but the dispatcher and policy layer enforce the final boundary.

### 2. Monolithic advisor loop

Issue: the controller currently combines provider streaming, protocol parsing, routing, policy, dispatch, rendering, memory, and finalization.

Solution: split responsibilities into controller, provider adapter, protocol normalizer, policy/grant evaluator, executor, receipt/evidence ledger, and claim gate. The first release should use a narrow interception seam rather than a complete workflow-graph rewrite:

`provider output -> normalized call/text -> existing dispatch -> receipt wrapper -> final evidence gate`

### 3. Multiple tool-call protocols

Issue: native OpenAI-compatible, Ollama, XML/JSON/bare-argument, and Python-like fallback paths behave differently.

Solution: normalize provider output into one internal representation, preserve whether it was native or fallback, and make the executor policy depend on that origin. Native structured calls are canonical. Fallbacks are isolated, explicitly allowlisted, and cannot silently authorize mutations.

### 4. False action claims

Issue: model prose can say it searched, sent, checked, saved, changed, or verified something without a tool receipt.

Solution: a final claim gate compares action claims against successful matching receipts. If unsupported, issue one tool-free repair prompt using only approved evidence; if still unsupported, replace the answer with a deterministic safe statement. No unsupported claim may be rendered as final output. Live previews must not expose unverified action-capable prose.

Action claims and result facts are separate classes. A successful receipt for “searched Gmail” does not automatically authorize a claim about a particular balance, email body, or external state unless the result contract supplies that fact and provenance.

### 5. Existing narrow safeguards

Issue: artifact/browser/approval guards are narrow and duplicated.

Solution: keep each domain-specific guard, but make it emit/consume common evidence:

- artifact guard records artifact ID, operation, path/resource, result, and receipt;
- browser guard records observation/navigation/mutation/approval state and evidence refs;
- approval guard records request, decision, approver, scope, and expiry;
- memory guard records consent/policy and persisted-claim receipt.

The universal layer owns lifecycle, correlation, redaction, and final claim decisions. Domain guards still own domain semantics. A migration adapter can wrap legacy traces until each tool is converted.

### 6. Browser guard and `_browser_tool_ran()`

Issue: `_observed_live_browser()` and `_browser_tool_ran()` currently return `False`, and the current exposed path does not establish a complete browser mission capability.

Solution: do not infer browser evidence from model text. Each browser tool must emit a typed event:

- `observation`: URL/page ID, timestamp, visible text/heading/title hash, screenshot or DOM evidence reference, no mutation;
- `navigation`: from/to URL, redirect chain, page identity, timestamp;
- `mutation`: target, action, precondition, approval/grant, started/confirmed/unknown status;
- `approval_required`: requested action, scope, pending approval ID, decision.

The controller determines that an observation happened only when the execution trace contains a confirmed observation receipt with the matching browser resource/page. Navigation alone is not an observation of page content. An observation does not prove a mutation. A mutation with `UNKNOWN` status cannot support a success claim. The stub should be replaced only after browser tools provide these receipts; until then, unsupported live-browser claims remain blocked.

### 7. Fallback calls without IDs

Issue: fallback syntax may have no OpenAI-compatible call ID, and a mutation could execute before result/history append fails.

Solution: generate a Delilah-owned `call_id` at normalization time using a cryptographically strong/random or deterministic-per-request ID containing turn correlation, provider round, and ordinal. Generate a separate `receipt_id` at execution lifecycle creation. Never use a missing provider ID as permission to execute untracked work.

Safe lifecycle:

1. Normalize fallback call and assign `call_id`.
2. Validate tool, arguments, origin, route, and permission.
3. Persist `PREPARED` before dispatch.
4. Persist `STARTED` immediately before invocation.
5. Invoke with idempotency key `receipt_id`/operation fingerprint where supported.
6. Persist `CONFIRMED`, `FAILED`, or `UNKNOWN` with the result.
7. Append model conversation/history only after the receipt exists; if append fails, retain the durable receipt and mark history synchronization pending.
8. On retry, consult the receipt/idempotency key first; never blindly repeat an unknown mutation.

For legacy read-only fallback, the same IDs and receipt are required, but the risk is lower. A cloud fallback mutation is rejected. A model claim cannot be supported by parsing alone.

### 8. OpenRouter/OpenAI-compatible protocol

Issue: “OpenAI-compatible” is not identical behavior across providers, and the current parser/normalizer is spread through the loop.

Solution: define a provider adapter interface that emits:

```text
ProviderRound {
  provider
  requested_model
  actual_model
  route/profile
  messages
  offered_tools
  native_tool_calls[]
  text
  finish_reason
  usage/latency
  raw_reference
}
```

The adapter must parse `choices[].message.tool_calls`, streamed `delta.tool_calls`, arguments that arrive in fragments, and `finish_reason` without interpreting prose as native calls. The normalizer then emits `NormalizedCall {call_id, origin=native|fallback, name, arguments, provider_round_id}` and `NormalizedText` separately. Fallback parsing runs only if explicitly enabled for that route and output mode. It must not convert an ordinary claim into an executed mutation.

### 9. `finish_reason`

Issue: finish reason is currently not consistently retained through the provider round.

Solution: preserve it in `ProviderRound`, trace records, and observability. It is needed to distinguish normal stop, tool-call continuation, length truncation, content filtering, and provider failure. It must not be treated as evidence of execution.

### 10. OpenRouter model/provider routing

Issue: automatic routing/failover can select an unintended provider/model combination or a low-throughput provider.

Solution: explicit Delilah route profiles pin model plus provider policy. Validate profiles at startup. For tool routes, disallow automatic aliases and uncontrolled free-model lists, require supported tool parameters, disable gateway fallback where possible, and record the actual route. On failure, apply Delilah's explicit fallback profile or fail safely.

### 11. Cloud versus local inference

Issue: treating the choice as “local good/cloud bad” would remove a legitimate user control and misunderstand the autonomous cost concern.

Solution: retain both modes and expose route/cost policy. Make autonomous wakeups local by policy, with deadline/work budgets and no uncontrolled paid retry spiral. Interactive cloud inference remains supported and observable.

### 12. Prompt overload

Issue: critical behavior is currently over-specified in prompts and can be ignored or hallucinated.

Solution: move enforcement into typed controller state, normalized call origin, grants, receipts, budgets, and claim gates. Keep prompts for task guidance, formatting, and repair instructions, not for security-critical enforcement.

### 13. Keyword/domain routing

Issue: a growing keyword pile is brittle and will misroute semantically similar requests.

Solution: embeddings/classification rank domain/intent candidates. Deterministic rules enforce safety, permissions, exact schemas, and high-risk constraints. Use confidence thresholds and ambiguity handling: narrow to read-only, ask a question, or request confirmation. Do not turn embeddings into authority.

### 14. Tool router/authority

Issue: forbidding router authorization categorically may be unnecessarily restrictive.

Solution: permit narrow target-bound grants from the router, constrained by registry policy and revalidated by execution. Grants are single-use, scoped, expiring, correlated to a turn, and cannot bypass approval or result/evidence requirements.

### 15. Tool presentation versus authorization

Issue: tool availability, permission, and execution are currently easy to conflate.

Solution: retain distinct records and checks even if one router component computes more than one decision. Presentation is what the model can ask for; authorization is what Delilah permits for a specific target; execution is what actually happened. The final claim gate uses execution receipts, never presentation or authorization alone.

### 16. Loop limits

Issue: arbitrary global limits break valid long workflows.

Solution: use progress/failure/deadlock/repetition controls, operation-specific retries, cooldowns, idempotency, autonomous deadlines, concurrency limits, unknown-effect limits, spend budgets, and a kill switch. Track repeated fingerprints and no-progress state. Use a configurable emergency ceiling only as a last-resort circuit breaker, not as normal workflow semantics.

### 17. Typed tool results

Issue: arbitrary strings cannot reliably support evidence or distinguish complete/partial/unknown outcomes.

Solution: introduce the envelope above, adapters for legacy strings, compact summaries for the model, bounded typed facts for claim validation, and durable evidence references for the controller/audit layer. Migrate high-risk tools first.

### 18. Testing

Use three levels:

1. Direct unit tests for deterministic tools, argument validation, normalization, grants, receipts, claim matching, lifecycle transitions, and loop fingerprints.
2. One substantial end-to-end local-model workflow test exercising actual provider behavior, native calls, fallback rejection, receipt creation, and final rendering.
3. A prompt-level behavioral battery covering false tool claims, hallucinated calls, wrong routing, failed calls, partial/unknown results, malformed calls, repair behavior, long workflows, repeated work, and cloud/local route policy.

The E2E test should be deterministic enough to run in CI with a controlled local model or replay fixture, while a less deterministic extended local-model suite can run separately. Tests must assert both user-visible text and trace/receipt state.

### 19. Observability

Record a correlated event ledger per turn/round:

- turn ID, conversation/user ID, autonomous/interactive mode;
- requested route, provider, requested model, actual model/provider;
- provider round ID, finish reason, latency, usage, retries;
- offered tools and their registry versions;
- router classification, candidates, confidence, ambiguity;
- grants, target/resource scope, approval state, expiry;
- raw/normalized calls, call origin, generated IDs, arguments hash/redaction;
- dispatch lifecycle: prepared, started, confirmed, failed, unknown;
- receipt ID, result contract version, status, facts/provenance/evidence refs;
- browser/artifact/memory domain evidence decisions;
- claim fingerprints, supported/unsupported decisions, repair attempts;
- final response decision and safe fallback reason;
- loop progress/repetition/budget decisions.

Use one correlation chain such as:

`turn_id -> provider_round_id -> normalized_call_id -> receipt_id -> evidence_ref -> claim_decision_id`

The ledger should be durable and redacted. Do not blindly add high-volume writes to the current shared SQLite patterns: introduce explicit migrations, WAL/locking policy, one ledger-writer abstraction, secure references for large/raw payloads, and retention/redaction rules.

### 20. Implementation roadmap

The detailed phase order is below.

## Phase 0: freeze behavior and create seams

Goals:

- preserve current dispatcher/workflow behavior;
- add tests around current native and fallback behavior;
- create explicit feature flags for safe rollout:
  - `receipt_write_shadow`;
  - `claim_detection_shadow`;
  - `claim_block_preview`;
  - `claim_block_final`;
  - `native_only_cloud`;
  - `route_profiles`;
  - `router_read_grants`.

Existing malformed history should not be retroactively repaired. Mark pre-migration history as legacy and apply strict protocol rules from a new turn marker.

## Phase 1: evidence gate and receipt-aware output

This is the first implementation slice.

New abstractions:

- `ClaimEvidenceGate` / `ClaimDecision` in a small, pure service module;
- initial action-claim matcher for explicit first-person/past-tense actions;
- successful-receipt matcher over current traces;
- deterministic safe fallback and repair-prompt builder;
- preview policy that does not expose unverified tool-capable prose.

Changes:

- evaluate final text against `turn_tool_trace` before rendering;
- if unsupported, perform one tool-free repair attempt;
- if still unsupported, publish a safe response that says the action could not be verified;
- stop provisional live previews for tool-enabled/action-capable rounds until they are evidence-aware;
- leave existing domain guards active and feed their trace into the common gate;
- add unit tests for supported action claims, unsupported claims, partial/failed/unknown receipts, and repair fallback.

The first slice intentionally does not pretend to solve full result-fact provenance or browser mission evidence. Those are subsequent phases.

## Phase 2: normalized provider rounds

Files/components expected to change:

- `src/services/llm.py`: extract provider streaming/parsing behind adapters;
- new provider-round/normalizer module;
- preserve `finish_reason`, provider/model/route, native calls, and fragmented arguments;
- normalize native and fallback calls with explicit origin and Delilah-owned IDs;
- add OpenAI-compatible/OpenRouter/Ollama parser tests with recorded fixtures.

Acceptance criteria:

- no prose path can silently become a native mutation call;
- all normalized calls have IDs;
- fallback origin is visible in the trace;
- finish reason survives to observability.

## Phase 3: lifecycle receipts and durable ledger

New abstractions:

- `ToolReceipt` and lifecycle transition validator;
- `ReceiptStore`/single ledger-writer abstraction;
- idempotency key and fingerprint helper;
- legacy string result adapter.

Changes:

- write `PREPARED` before any side effect;
- write `STARTED` immediately before invocation;
- write terminal status after invocation;
- represent append/history failures as synchronization problems, never as permission to forget a receipt;
- use `UNKNOWN` after uncertain external outcomes;
- add explicit database migration, WAL/locking, redaction, retention, and secure payload references.

Acceptance criteria:

- a successful mutation remains trackable if result/history append fails;
- retries consult the prior receipt/idempotency key;
- no side effect has an untracked execution window.

## Phase 4: native-only mutation policy and route profiles

Changes:

- classify tool risk and fallback eligibility in registry metadata;
- reject fallback-origin mutations, especially on cloud routes;
- issue one native-call repair prompt, then block safely;
- add explicit Delilah-owned provider/model profiles;
- validate OpenRouter provider controls and record actual route;
- keep local/cloud selection explicit and make autonomous local policy visible.

Acceptance criteria:

- malformed textual mutation requests never execute;
- intended native mutation still executes and produces a receipt;
- gateway routing cannot silently choose an unapproved route.

## Phase 5: grants, router staging, and tool surface

New/updated abstractions:

- `ToolSchemaRegistry`;
- `ToolRiskPolicy`;
- `ToolResultContract`;
- `ToolRouteEligibility`;
- `TargetBoundGrant`;
- router classification result.

Do not combine these into one god object.

Stage router rollout:

1. shadow classification;
2. presentation-only narrowing;
3. read-only grants;
4. conditional/approval-aware grants;
5. recommendations;
6. narrow mutation grants.

Make the registry authoritative. Add CI/startup checks for mismatches with old tool lists, especially mutation, approval, and browser tools.

## Phase 6: typed result contracts and domain evidence

Migrate 10–15 high-risk/common tools first. Extend artifact/browser/approval/memory tools to emit evidence refs and typed facts. Implement browser observation/navigation/mutation/approval distinctions only after actual browser tools expose enough data. Convert memory extraction to an explicit policy-controlled receipt action.

Acceptance criteria:

- result claims are supported by typed facts/provenance, not merely a successful tool name;
- partial and unknown outcomes are reflected accurately;
- browser claims require the correct observation receipt;
- background memory cannot silently mutate state.

## Phase 7: progress budgets and autonomous safety

Add repetition/deadlock fingerprints, progress state, retry/cooldown policies, autonomous deadlines, local-only wakeup defaults, work/concurrency limits, unknown-effect handling, cloud spend budgets, alert limits, and emergency stop. Add policy constraints for autonomous operations: no browser mutations, no financial mutations except approved/rule-backed operations, no silent sensitive memory persistence, and human review for unknown effects.

## Phase 8: full observability and behavioral evaluation

Make the correlated ledger queryable. Add dashboards/reports for:

- hallucinated call attempts;
- unsupported claim blocks;
- native/fallback rates;
- route/provider failures;
- unknown side effects;
- repeated work/deadlocks;
- repair success/failure;
- autonomous spend and termination causes.

Run the direct unit suite, local-model E2E workflow, and prompt battery on every relevant change.

## Current code landmarks

These are the known integration points from the review and should be rechecked as code moves:

- `src/services/llm.py` still contains the orchestration loop in `_chat_with_delilah_impl` beginning around line 3906; provider parsing, receipts, grants, result contracts, claim evidence, progress policy, and router authorization now have explicit interception modules around that loop.
- `_observed_live_browser()` and `_browser_tool_ran()` delegate to `src/services/browser_evidence.py`; they no longer treat model prose or navigation alone as observation evidence.
- Artifact guard regexes and browser-claim regexes are in the same service near the guard helpers.
- `_PARALLEL_GMAIL_READ_TOOLS` currently limits explicit parallel Gmail reads; preserve this safe subset while adding explicit mixed-batch policy.
- `stream_generator` chooses providers, constructs OpenAI-compatible payloads, parses streamed OpenAI/Ollama output, and accumulates tool calls.
- OpenRouter model selection retains cooldown/retry behavior for explicit candidates, while route profiles pin the tool route and automatic model aliases are rejected.
- `parse_fallback_tool_call` supports JSON, bare arguments, XML, and Python-like syntax; preserve existing form-recovery tests while constraining mutation execution.
- `turn_tool_trace` is currently an in-memory list of dictionaries, and tool execution logging is ad hoc and mostly post-dispatch.
- final text passes through the claim-evidence gate in both tool and no-tool branches; unsupported completed-action claims receive one repair attempt and then a deterministic safe fallback.
- final rendering occurs after memory extraction and live-preview deletion; the evidence gate must run before final user-visible output and before any model-derived persistence based on that output.
- inline and background model-derived memory persistence are policy-controlled and disabled by default via `DELILAH_INLINE_MEMORY` and `DELILAH_BACKGROUND_MEMORY`.
- `src/services/tool_contract.py` currently contains tool-call repetition and argument validation helpers and can host or support normalization helpers, but should not become a god object.
- `src/core/state.py` has existing shared SQLite connection/cursor patterns; `ReceiptStore` now owns explicit receipt/provider/claim/router tables and lifecycle transitions on that connection.

## First code change checklist (completed)

Before modifying the monolithic loop:

- add the pure claim/evidence module;
- add tests that run without a provider or Discord;
- inspect all preview/final render call sites;
- preserve existing `test_form_tool_recovery.py`, Gmail batching, tool-contract, and router tests;
- compile and run focused tests;
- then wire the final gate and preview policy behind feature flags;
- document any behavior change in this file.

## Non-goals for the first slice

- do not rewrite the entire advisor loop;
- do not implement a complete browser agent mission;
- do not infer browser evidence from prose;
- do not make OpenRouter automatic routing authoritative;
- do not add arbitrary global loop caps;
- do not silently execute fallback mutations;
- do not make background model memory extraction a hidden side effect;
- do not claim that a successful tool name alone proves every fact in the final response;
- do not bulk-repair old malformed histories;
- do not replace existing narrow safeguards before their evidence adapters exist.

## Definition of done for the safety foundation

The foundation is working when:

1. A model saying “I checked/sent/saved/updated…” without a matching confirmed receipt cannot reach the user as a successful claim.
2. A native structured mutation has a durable, correlated lifecycle receipt.
3. A fallback mutation is rejected and receives at most one native-call repair attempt.
4. A missing provider call ID is replaced by a Delilah-owned ID before any execution decision.
5. A result/history append failure cannot erase or hide a dispatched side effect.
6. Provider, model, route, finish reason, call origin, authorization, execution status, receipt, and final claim decision are correlated.
7. Existing safe read parallelism and domain-specific guards remain intact.
8. Local/cloud selection remains explicit, and autonomous wakeups do not silently consume paid inference.
9. Unit, local-model E2E, and prompt-level behavioral tests cover false claims and hallucinated calls.
