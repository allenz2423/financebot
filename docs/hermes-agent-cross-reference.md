# Hermes Agent ↔ Delilah Cross-Reference and Proposed Changes

Date: 2026-09-25
Scope: the current `/root/financebot` working tree, including uncommitted files, compared with the current `main` branch of [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent).

For a focused review of why Delilah still feels less agentic than Hermes, see [`hermes-agentic-gap-review.md`](hermes-agentic-gap-review.md).

## Executive summary

Delilah should not be rewritten as Hermes Agent. The finance-specific parts of Delilah are the differentiator: deterministic SQLite-backed calculations, Plaid synchronization, transaction identity checks, audit workflows, evidence/provenance, the Active World Model, and financial-domain isolation. Hermes is useful as an architectural reference for the runtime around that domain.

The main gap is not capability. It is the lack of a small, stable runtime boundary around a very large implementation:

- `src/services/llm.py` is 11,317 lines and currently contains prompt construction, provider streaming, tool schemas, routing, audit-state enforcement, dispatch, result normalization, persistence, compression, Discord rendering, and recovery logic.
- Tool metadata and execution are split across `BOT_TOOLS_SCHEMA`, `NEW_50_TOOLS_SCHEMA`, `ADVISOR_TOOLS_DISPATCH`, `EXPECTED_TOOL_NAMES`, `MUTATION_TOOLS`, aliases, discovery tables, and intent pre-seeding.
- Conversation state is primarily an in-memory `SESSION_HISTORY` map, restored from a denormalized `chat_history` table. Receipts are more structured, but session/message lineage is not.
- `src/core/state.py` owns global connections, cursors, runtime settings, task state, schema setup, and session state. This makes the system harder to test, restart safely, or expose through another interface.

Hermes addresses these concerns with a platform-agnostic `AIAgent`, a self-registering `ToolRegistry`, separate provider/prompt/turn phases, SQLite session storage with FTS5 and lineage, pluggable context/memory providers, toolsets, skills/plugins, and first-class cron/gateway components. These are good boundaries to adopt incrementally.

## Implemented in this refactor pass

The following Hermes-derived stability pieces are now present in the working tree, not just proposed:

- [`src/agent/runtime.py`](../src/agent/runtime.py) and [`src/agent/events.py`](../src/agent/events.py) provide a platform-neutral turn facade with normalized messages, channel/thread-scoped session keys, lifecycle hooks, cancellation/error events, and context-local turn/session identity.
- [`src/agent/context.py`](../src/agent/context.py) provides bounded stable/context/volatile context blocks with provenance, sensitivity, freshness, and deterministic rendering order.
- [`src/services/tool_registry.py`](../src/services/tool_registry.py) provides centralized tool definitions, defensive schema generation, duplicate detection, provider-call normalization, pre-dispatch argument validation, async dispatch, and bounded typed result envelopes. The financial advisor-tool family is exposed through `ADVISOR_TOOL_REGISTRY` in [`src/services/llm.py`](../src/services/llm.py).
- [`src/db/session_store.py`](../src/db/session_store.py) and [`src/db/migrations/006_durable_sessions.py`](../src/db/migrations/006_durable_sessions.py) add scoped durable sessions, turns, messages, tool-call lifecycle records, idempotency, bounded retention, SQLite writer locking/WAL, FTS5 exact recall, and a safe LIKE fallback. The compatibility `chat_with_delilah()` facade now creates and closes these records on every turn where the store is available.
- [`src/services/authorization.py`](../src/services/authorization.py) separates policy authorization, human approval, and target-bound execution grants, with exact request fingerprints, expiry, replay protection, and approval mismatch checks. Existing legacy controller gates remain authoritative while the migration proceeds.
- [`src/services/provider_adapter.py`](../src/services/provider_adapter.py) normalizes OpenAI SSE and Ollama JSON streams into typed text, reasoning, tool-call, finish, usage, and error events.
- Tool-call lifecycle records are written at `running` and terminal states from the existing execution loop, while receipt persistence remains the authoritative financial side-effect ledger during migration.
- [`src/services/tool_catalog.py`](../src/services/tool_catalog.py) now validates the full model-facing catalog at startup: malformed schemas, duplicate names, expected-name drift, and registered advisor handlers missing from the advertised catalog fail closed. The test suite also statically checks that every advertised tool has either a legacy dispatch branch or a migrated registry handler.
- The shared registry now fails closed for empty/`None` results, explicit error envelopes, and legacy error strings; numeric zero remains a valid result. Provider call IDs are repaired when duplicated within a response and replayed IDs are rejected before a second side effect.
- The monitor pacing evaluator now takes one clock snapshot and forwards it into the budgeting calculation; its test is deterministic instead of depending on the day on which the suite happens to run.

The three delegated implementation agents were instructed to actively cross-reference the current Hermes checkout at `/tmp/hermes-agent-crossref.Wc626R`, specifically `agent/conversation_loop.py`, `tools/registry.py`, `model_tools.py`, `hermes_state.py`, `hermes_state_schema.py`, `hermes_state_search.py`, and the approval/provider modules. Their changes were reviewed and integrated in the shared working tree; this is an adaptation of patterns, not a wholesale copy of Hermes code.

Verification after integration: `546 passed, 1 skipped` with five non-fatal deprecation warnings.

## Ensuring a tool was actually called: Hermes pattern

This was checked directly against the active Hermes checkout, especially [`model_tools.py`](https://github.com/nousresearch/hermes-agent/blob/main/model_tools.py), [`agent/turn_loop_errors.py`](https://github.com/nousresearch/hermes-agent/blob/main/agent/turn_loop_errors.py), [`agent/turn_final_response.py`](https://github.com/nousresearch/hermes-agent/blob/main/agent/turn_final_response.py), and [`agent/agent_runtime_helpers.py`](https://github.com/nousresearch/hermes-agent/blob/main/agent/agent_runtime_helpers.py).

Hermes treats “the model said it would call a tool” and “the tool ran” as different states:

1. A structured tool call enters the loop with a call ID. Hermes may support compatibility fallbacks, but the loop still converts them into a real normalized call before dispatch.
2. `handle_function_call()` routes through the registry and invokes the handler. It emits a post-call lifecycle event on success, blocked execution, and exceptions; an exception becomes a tool error result rather than silently becoming assistant prose.
3. The executor appends a tool-result message tied to the exact `tool_call_id`. If processing fails after an assistant tool-call frame was appended, the outer-loop error handler synthesizes error results for every unanswered call ID.
4. Before the next provider request, Hermes sanitizes the transcript: orphan results are dropped, duplicate IDs are removed, and unanswered assistant calls receive bounded stubs. This preserves the provider’s call/result protocol and prevents history from falsely implying that an unrelated result answered a call.
5. Hermes has bounded final-response stall guards. A short response ending in “I will now…” with no tool call is re-prompted to act; a degenerate fragment after tool results is also re-prompted. These guards are bounded and do not claim that the tool ran.

Delilah already implements the equivalent execution evidence in its legacy loop:

- Native, fallback, and sentinel calls are normalized into call objects with Delilah-owned IDs.
- A receipt is persisted as `prepared` and `started` before dispatch, then becomes `confirmed`, `failed`, or `unknown` after dispatch.
- `turn_tool_trace` is populated from the execution/receipt path, not from model narration. Required-tool, artifact, live-browser, and audit gates inspect that trace.
- The exact call ID is attached to the appended `role=tool` result, and durable session tool-call rows are recorded at running and terminal states.

The remaining behavioral distinction is intentional but important: ordinary Delilah chat accepts a substantive no-tool answer, while Hermes’ stall guards are primarily aimed at responses that announce a future action or collapse after tool work. For workflows where a tool is mandatory, Delilah must pass `required_tools`; the final answer is rejected until the trace contains a successful execution for every required name. A model claiming “I checked” without a matching successful trace entry is therefore not evidence that the tool ran.

For explicit Plaid-sync requests, this enforcement is now automatic. [`src/services/tool_intent.py`](../src/services/tool_intent.py) infers `sync_plaid_accounting` as a required tool; the intent/schema path exposes it even when the generic “sync” route would otherwise select reconciliation tools; and the OpenAI-compatible path sets a named `tool_choice` while that requirement is pending. The runtime contract also tells local models that an action request is an immediate native-call obligation. [`src/services/tool_execution_evidence.py`](../src/services/tool_execution_evidence.py) classifies empty results, unknown tools, dispatcher errors, and Plaid workflow failure text as unsuccessful, so the completion guard cannot turn an attempted-but-failed sync into a success claim. This guarantees a native-call attempt and evidence-gated reporting; it cannot guarantee that an external bank/provider succeeds.

Recommended next hardening is to extract this into a reusable `ToolExecutionEvidence`/`TurnCompletionPolicy` module and make all high-confidence claims require a matching confirmed receipt, rather than relying on scattered string guards. The current receipt and trace implementation is the safe migration seam for that extraction.

### Recommended order

1. Make tool registration, authorization metadata, and dispatch derive from one registry.
2. Extract a platform-neutral agent runtime and split the current turn loop into phases.
3. Replace `SESSION_HISTORY` as the live source of truth with durable session/message/turn records and searchable recall.
4. Introduce explicit context tiers and a pluggable memory/retrieval interface.
5. Generalize reminders/monitoring into durable automation jobs with idempotency and delivery adapters.
6. Add skills/plugins and optional delegation only after the preceding boundaries are stable.

## Sources reviewed

Hermes documentation and source:

- [Hermes architecture guide](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- [Hermes tools and toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
- [Hermes skills system](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes persistent memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- [Hermes scheduled tasks](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Hermes context files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)
- [`agent/conversation_loop.py`](https://github.com/nousresearch/hermes-agent/blob/main/agent/conversation_loop.py)
- [`tools/registry.py`](https://github.com/nousresearch/hermes-agent/blob/main/tools/registry.py)
- [`model_tools.py`](https://github.com/nousresearch/hermes-agent/blob/main/model_tools.py)
- [`hermes_state.py`](https://github.com/nousresearch/hermes-agent/blob/main/hermes_state.py)
- [`hermes_state_schema.py`](https://github.com/nousresearch/hermes-agent/blob/main/hermes_state_schema.py)
- [`hermes_state_search.py`](https://github.com/nousresearch/hermes-agent/blob/main/hermes_state_search.py)

Delilah implementation anchors:

- [Architecture overview](../README.md#architecture)
- [`src/services/llm.py`](../src/services/llm.py)
- [`src/core/state.py`](../src/core/state.py)
- [`src/core/discovery.py`](../src/core/discovery.py)
- [`src/services/advisor_tools.py`](../src/services/advisor_tools.py)
- [`src/services/tool_router.py`](../src/services/tool_router.py)
- [`src/services/tool_receipts.py`](../src/services/tool_receipts.py)
- [`src/services/tool_contract.py`](../src/services/tool_contract.py)
- [`src/services/tool_grants.py`](../src/services/tool_grants.py)

## Capability comparison

| Area | Hermes Agent | Current Delilah | Finding and proposed direction |
|---|---|---|---|
| Entry points | One agent core serves CLI, gateway, ACP, batch, API server, and library callers. | Discord is the primary surface; `main.py` starts Discord and FastAPI health, while `src/bot/commands.py` owns message ingestion and rendering. | Extract an `AgentRuntime`/`AdvisorSession` that accepts a normalized message and emits events. Keep Discord as the first adapter; do not add more surfaces until the core is independent of Discord. |
| Agent loop | `AIAgent` delegates to phase modules for prompt assembly, provider calls, retries, tool rounds, compression, and finalization. | `_chat_with_delilah_impl` owns almost the entire turn lifecycle, including special audit branches and delivery. | Split the loop into `TurnContext`, `PromptBuilder`, `ProviderClient`, `ToolRound`, `TurnPolicy`, `Persistence`, and `Delivery`. Preserve the current audit controller as a policy implementation, not as the runtime itself. |
| Providers | Runtime provider resolution maps provider/model to API mode, credentials, and endpoint. | Local/cloud behavior, stream parsing, temperature selection, routing profiles, fallbacks, and model controls are spread across `llm.py`, `state.py`, and commands. | Define a provider protocol with `complete()`/`stream()` plus normalized events. Move provider selection and credentials behind one resolver. `provider_protocol.py` is a useful seed. |
| Tools | Self-registering modules declare schema, handler, toolset, availability checks, async behavior, result limits, and dynamic schema overrides. | The schema is manually assembled in `llm.py`; handlers are separately mapped in `advisor_tools.py`; names are checked again by `EXPECTED_TOOL_NAMES`. | Adopt a Delilah `ToolRegistry` modeled on Hermes. Generate offered schemas, dispatch, mutation/read partitions, aliases, result contracts, and availability reports from the same registration record. |
| Tool discovery | Toolsets are selectable per platform/session. Tool search can expose a bounded subset of a larger catalog. MCP tools are dynamically registered. | `explore_domain` and `load_tool_schemas` implement hierarchical disclosure; `tool_router.py` adds BM25/Qdrant ranking and staged live authorization; intent keyword maps pre-seed tools. | Keep progressive disclosure, but make domain/toolset metadata registry-owned. Use semantic routing only to narrow an already-authorized catalog. Remove duplicated intent maps once registry metadata can support retrieval. |
| Sessions | SQLite-backed sessions and messages have platform/session keys, parent/child lineage, status, compression metadata, and FTS5 search. | `SESSION_HISTORY[user_id]` is the live conversation; `chat_history` stores user/final/tool/mutation rows without a durable session or turn foreign key. | Add `sessions`, `turns`, and `messages` with `session_key`, `channel_id`, `thread_id`, `parent_turn_id`, status, and model/provider metadata. Keep `chat_history` as a migration source only. |
| Recall/search | Session search is a first-class FTS5 feature, with sanitization, bounded queries, rebuild/repair, and compaction-aware search. | Delilah has Qdrant semantic memory, world-model retrieval, RAG, and web-result indexing, but conversational recall is recent-window based. | Separate exact conversational recall from semantic financial/world-model recall. Add SQLite FTS5 for messages and retain Qdrant for claims, dossiers, and research. |
| Context | Prompt assembly has stable/context/volatile tiers; context engines and compressors are replaceable. | `build_advisor_context()` injects live financial summaries; AWM context and recent history are assembled in `llm.py`; background compression mutates in-memory state. | Introduce explicit context blocks with source, freshness, sensitivity, and token budget. Cache stable instructions; assemble volatile account facts and task state per turn. |
| Memory | Memory is a provider interface, with built-in persistence and optional providers/plugins. | The Active World Model is a stronger domain memory than Hermes for financial claims, while chat history and RAG are separate stores. | Keep AWM as the authoritative financial memory. Add interfaces around it so the agent can request `recall_claims`, `recall_sessions`, or `recall_research` without knowing Qdrant/SQLite details. |
| Security/approval | Hermes has DM pairing, command approval, dangerous-command detection, and isolated terminal backends. | Delilah has user-scoped SQL, Gmail-only context isolation, SSRF protections, sandbox isolation, transaction identity checks, audit allowlists, tool grants, and native-only mutation rules. | Delilah is already stronger in finance-specific authorization. Formalize the existing policy as per-tool risk metadata and add real approval states for high-impact mutations; do not replace its audit gates with generic command approval. |
| Tool result handling | Registry dispatch normalizes result types and bounds error bodies. | Delilah has result envelopes, facts contracts, evidence refs, receipts, and progress policy, but these are added around a large dispatch branch. | Promote `ToolResultEnvelope` to the mandatory dispatch return type. Make result size, status, side effect, provenance, and idempotency part of registry metadata. |
| Interruptibility | Interrupt-and-redirect is part of the agent loop and gateway lifecycle. | Discord commands support status, peek, pause, cancel, a per-user active task, and a bounded side-message queue. | Keep the current UX. Move task ownership and cancellation tokens into the platform-neutral runtime so another adapter can use the same behavior. |
| Scheduling | Cron creates a fresh agent with a prompt, attached skills, schedule state, and platform delivery. | Reminders and monitoring are background loops tied to the bot/database, with push notification support and scheduled reminder tools. | Unify reminders, monitor rules, and future reports under durable `automation_jobs` with a fresh session per run, retry/backoff, lease/claim, idempotency key, and delivery adapter. |
| Skills/plugins | Markdown skills are procedural memory; plugins can add tools, hooks, commands, platforms, memory, or context engines. | No equivalent runtime extension boundary; capabilities are mostly built into the monolithic codebase and prompt. | Add a narrow skills layer first for repeatable finance workflows. Add plugins only after registry and lifecycle contracts exist. Never allow an arbitrary skill to bypass financial grants. |
| Delegation | `delegate_task` starts isolated child agents with scoped toolsets and lineage. | No general subagent boundary; current parallelism is mostly `asyncio.gather` for disjoint work. | Do not prioritize autonomous subagents. First add deterministic parallel tool batches. If delegation is later added, default to read-only, bounded, user-scoped children with no credential access or mutation grants. |
| Observability | Session records, model usage, tool calls, gateway state, and FTS maintenance are durable and queryable. | Receipts and router decisions are durable, while status/active tasks/queues are mostly in memory; many runtime traces are text in `chat_history`. | Make `turns`, `tool_calls`, `tool_receipts`, provider rounds, policy decisions, and final claim audits queryable by `turn_id`. Keep human-readable Discord traces as a projection. |

## Findings in more detail

### 1. The current tool system has the highest leverage for a Hermes-style improvement

Delilah already has the right ingredients:

- hierarchical capability groups in [`src/core/discovery.py`](../src/core/discovery.py);
- large manually declared schemas in [`llm.py:1047`](../src/services/llm.py:1047);
- a second financial-tool schema/dispatch catalog in [`advisor_tools.py`](../src/services/advisor_tools.py);
- schema drift detection in [`llm.py:3441`](../src/services/llm.py:3441);
- validate-before-execute checks in [`tool_contract.py`](../src/services/tool_contract.py);
- result contracts, receipts, grants, and progress policy in the new service modules;
- staged semantic routing in [`tool_router.py:1`](../src/services/tool_router.py:1).

The problem is that the system has to maintain several representations of the same capability. `EXPECTED_TOOL_NAMES` catches some drift, but it does not make drift impossible. A newly added handler can still need edits to the schema, expected names, mutation set, discovery registry, aliases, result contracts, and prompt intent map.

Hermes’ registry makes a tool declaration the source of truth and derives the provider schema and dispatch table. Delilah should copy that idea while adding finance-specific metadata:

```python
registry.register(
    name="batch_correct_transactions",
    toolset="ledger.audit",
    schema={...},
    handler=batch_correct_transactions,
    side_effect="mutation",
    risk="high",
    requires_user_approval=True,
    idempotency_key=lambda args: ...,
    user_scope="required",
    result_contract=...
)
```

The registry should derive:

- provider-facing JSON schemas;
- toolset/domain discovery cards;
- read/mutation/conditional partitions;
- `MUTATION_TOOLS` and fallback restrictions;
- aliases and deprecation warnings;
- result/error-size limits;
- availability checks;
- audit/financial policy hooks;
- router catalog hashes.

This would let `tool_router.py` depend on a stable catalog interface instead of importing `BOT_TOOLS_SCHEMA` from `llm.py` (currently [`tool_router.py:135`](../src/services/tool_router.py:135)). That import direction is a concrete coupling problem: the routing layer cannot be tested independently of the monolithic LLM module.

### 2. The live semantic router is useful, but one part is currently inert

The router has a sensible staged design: shadow mode is observational, live mode can only narrow the already-offered schema, and retrieval failures fall back to lexical ranking ([`tool_router.py:1-13`](../src/services/tool_router.py:1)). That is safer than allowing vector retrieval to grant arbitrary tools.

However, `browser_gate()` currently always returns `False` and `GATE_DOMAIN` is empty ([`tool_router.py:364-365`](../src/services/tool_router.py:364)). Consequently, the `domain_topk` path is not enforcing a browser/domain restriction. Either:

1. implement a real domain gate from registry metadata and explicit intent, with tests for browser/account/file boundaries; or
2. remove the unused gate terminology and treat it as a future extension rather than implying that it is active.

Do not use retrieval score as the security boundary. Hermes’ toolset/availability filtering and Delilah’s controller allowlists should remain authoritative; semantic routing may reduce prompt size and choose candidates, but it should never create a permission.

### 3. Receipts and grants are valuable, but grants currently mean correlation, not approval

The current execution path issues a grant, immediately consumes it, and passes `approved=True` ([`llm.py:7896`](../src/services/llm.py:7896)). This proves that the call was bound to the current turn/target and prevents reuse, but it does not ask a human to approve anything. That distinction should be explicit in the names and documentation.

Proposed model:

- `AuthorizationDecision`: policy result before execution (`allow`, `deny`, `needs_approval`, `needs_fresh_read`, `needs_confirmation`).
- `ExecutionGrant`: short-lived, target-bound capability created only after policy approval.
- `ToolReceipt`: prepared → started → succeeded/failed/unknown, persisted before side effects.
- `ApprovalRequest`: durable record with user, action summary, target, expiry, and Discord interaction/message ID.

Low-risk reads can remain automatically allowed. High-risk operations—deleting data, changing ledger classifications, syncing external accounts, sending notifications, or any future external write—should have explicit policy metadata and, where appropriate, a user confirmation path. Existing audit gates must still run even after confirmation.

### 4. Session persistence is the main reliability gap compared with Hermes

Current session state is keyed only by user in `SESSION_HISTORY` ([`state.py:923`](../src/core/state.py:923)), loaded from a `chat_history` table with four kinds of rows ([`state.py:982`](../src/core/state.py:982)), and updated at the end of the turn ([`llm.py:10569`](../src/services/llm.py:10569)). This works for one active Discord conversation, but it makes these cases ambiguous:

- the same user in multiple channels or threads;
- an interrupted turn followed by a restart;
- two processes sharing the database;
- an audit continuation that needs exact prior controller state;
- searching prior conversations by phrase or date;
- reconstructing which provider round produced a result;
- deciding whether a tool call was completed, interrupted, or merely requested.

Implement a small normalized session store rather than copying all of Hermes’ schema:

```text
sessions(id, user_id, session_key, platform, channel_id, thread_id, status, created_at, updated_at)
turns(id, session_id, parent_turn_id, prompt, status, provider, model, started_at, ended_at)
messages(id, session_id, turn_id, role, content, tool_name, call_id, created_at, compacted_at)
tool_calls(id, turn_id, round_id, call_id, tool_name, arguments_json, status, receipt_id)
```

Add FTS5 over user/assistant messages, excluding large tool payloads from the index. Keep Qdrant for semantic claims and web research. The current receipt tables can be linked by `turn_id` rather than serving as a separate observability island.

### 5. Context should become an explicit, testable pipeline

Delilah already uses progressive disclosure and a financial ground-truth index in [`build_advisor_context()`](../src/services/llm.py:534). It also has Gmail-only context isolation before prompt construction ([`llm.py:4125-4197`](../src/services/llm.py:4125)). Those are good decisions and should become policy objects instead of branches inside the loop.

Adopt three explicit tiers, similar to Hermes’ stable/context/volatile prompt model:

1. **Stable**: identity, safety policy, financial truth rules, tool protocol, and compact domain instructions. Hash and cache this.
2. **Context**: user-approved world-model claims, relevant session recall, saved research, and context files/artifacts. Each block carries provenance and sensitivity.
3. **Volatile**: current balances, live tool results, audit controller state, current time, and pending approvals. Rebuild every turn/round.

Every block should expose `source`, `observed_at`, `expires_at`, `authority`, `token_cost`, and `sensitivity`. This will make it possible to test “Gmail-only turn contains no financial block” without inspecting a giant prompt string.

### 6. The agent loop should be extracted without weakening the finance controller

Hermes’ major architectural win is that the agent loop is a reusable core and platform differences live at the edges. Delilah currently has the opposite shape: Discord delivery, message history, prompt assembly, provider calls, and tool policy are all connected through `_chat_with_delilah_impl`.

A safe extraction sequence:

```text
Discord on_message
  -> normalize MessageEvent
  -> AgentRuntime.run_turn(TurnRequest)
  -> RuntimeEvent stream
  -> DiscordDeliveryAdapter
```

`AgentRuntime` should own:

- turn lifecycle and cancellation;
- provider resolution and normalized stream events;
- prompt/context assembly;
- tool schema snapshots;
- tool authorization and receipts;
- policy transitions and progress decisions;
- durable persistence.

The Discord adapter should own:

- attachment/voice preprocessing;
- placeholder and streaming message rendering;
- Discord command routing;
- button/modal interactions;
- channel-specific delivery limits.

The audit state machine should remain a separate `TurnPolicy` implementation. That keeps its hard allowlist, research gates, batch-only mutations, fresh verification, and end gate intact while making normal chat easier to reason about.

### 7. Provider normalization is already started and should be completed

`provider_protocol.py` correctly distinguishes structured native tool calls from ordinary prose. The next step is to make all provider behavior conform to a single interface:

```python
class ProviderClient(Protocol):
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]: ...
    async def cancel(self, request_id: str) -> None: ...
```

`ProviderRequest` should carry model, route profile, messages, tools, temperature, context limit, and request ID. `ProviderEvent` should normalize text, reasoning, native tool calls, usage, finish reason, and provider errors. Provider fallback must be an explicit policy decision recorded in the turn, not an incidental branch inside the loop.

### 8. Scheduling is close in concept, but needs durable job semantics

Delilah already has reminders, watchdogs, monitoring rules, and push delivery. Hermes’ cron model is a useful distinction: a scheduled task is a fresh agent run with an isolated prompt/context, then a delivery result and next-run state.

Unify the current scheduled capabilities around:

- `job_id`, owner, schedule, timezone, prompt/action, target platform/channel;
- a lease/claim so two workers cannot run the same job;
- `next_run_at`, `last_run_at`, `attempt_count`, `last_status`, and `last_error`;
- idempotency key per scheduled occurrence;
- bounded retries and backoff;
- a fresh session/turn by default;
- explicit data scope (for example, financial read-only versus mutation-capable);
- delivery receipts for Discord and ntfy.

Keep deterministic monitor evaluation separate from LLM jobs. A budget threshold should continue to run without an LLM; the LLM should only be used to explain or deliver a result when configured.

### 9. Skills are a better next extension point than more prompt text

Hermes uses skills as procedural, discoverable instructions that can be attached to a turn. Delilah currently places many workflow rules directly in the enormous system prompt and in keyword-driven intent maps.

Create finance skills such as:

- `transaction-audit`;
- `affordability-check`;
- `merchant-research`;
- `receipt-reconciliation`;
- `weekly-financial-briefing`;
- `tax-deduction-review`.

Each skill should declare required toolsets, allowed side-effect level, required evidence, completion criteria, and output format. A skill is guidance, not authorization: the registry policy and transaction/audit controller must still decide what may execute.

Plugins should come later and be limited to explicit registration hooks. A plugin must not be able to silently shadow a financial tool, bypass user scoping, or obtain credentials merely by being importable.

### 10. Delegation should be deliberately constrained in a financial assistant

Hermes’ isolated delegation is useful for broad research and parallel coding tasks. In Delilah, unconstrained subagents would create difficult questions about financial context leakage, duplicate mutations, credentials, and audit completion.

Before adding delegation, use the current deterministic parallel batching for disjoint reads. If delegation becomes necessary, require:

- a child session and parent/child lineage;
- read-only default;
- an allowlisted toolset snapshot;
- no inherited Gmail/Plaid credentials unless explicitly scoped;
- no mutation grants inherited from the parent;
- bounded wall-clock, token, and tool-call budgets;
- structured child result envelopes;
- parent-side evidence and completion checks.

## Proposed target architecture

```text
Discord / future adapters
          |
     MessageEvent
          v
   AgentRuntime / AIAgent facade
     |       |        |        |
 Context  Provider  Policy   SessionStore
 Engine   Resolver  Engine   (SQLite + FTS5)
     |       |        |        |
     +-------+--------+--------+
                    |
              ToolRegistry
          /       |       \
      schemas  auth/grants  dispatch
                    |
          financial services / MCP / sandbox
                    |
       SQLite ledger + AWM + Qdrant + artifacts
```

The important boundary is that `AgentRuntime` does not import Discord and the tool registry does not import `llm.py`. Financial services remain ordinary Python functions with explicit user/connection arguments.

## Phased implementation plan

### Phase 0 — consolidate the current work (low risk)

- Define one canonical `ToolDefinition` dataclass.
- Make `ToolResultEnvelope`, `ResultContract`, `ToolReceipt`, and `ProgressPolicy` part of one dispatch pipeline.
- Rename the current grant to distinguish target binding from human approval, or add the missing approval state.
- Add tests for every tool definition: schema, handler, user scope, side effect, result contract, and idempotency behavior.
- Make router records include the registry generation/catalog hash, not only the schema-derived hash.
- Implement or remove the currently inert `browser_gate()`/`GATE_DOMAIN` path.

### Phase 1 — registry and toolsets (highest ROI)

- Add `src/agent/registry.py` or `src/tools/registry.py`.
- Migrate a small vertical slice first: `get_current_financial_position`, `get_recent_transactions`, `search_web`, `batch_correct_transactions`, and `send_push_alert`.
- Generate schemas and dispatch from registrations.
- Preserve `explore_domain` and `load_tool_schemas`, but derive their cards from registry toolsets.
- Migrate the remaining tools incrementally and delete duplicate catalogs only after parity tests pass.

### Phase 2 — runtime extraction

- Add `src/agent/runtime.py`, `turn.py`, `prompt.py`, `providers.py`, `policies.py`, and `events.py`.
- Move provider stream parsing and route resolution behind `ProviderClient`.
- Move Discord rendering to an adapter.
- Keep `chat_with_delilah()` as a compatibility facade while tests and commands migrate.
- Add cancellation tests at provider wait, tool execution, persistence, and delivery boundaries.

### Phase 3 — durable sessions and exact recall

- Add normalized sessions/turns/messages/tool-calls tables with migrations.
- Key sessions by user plus platform/channel/thread, not user alone.
- Persist the current turn before final delivery where possible, with terminal status updates.
- Add FTS5 exact search with bounded/sanitized queries.
- Link receipts and router decisions to the same durable turn ID.
- Treat in-memory state as a cache, never the only source of truth.

### Phase 4 — context and memory providers

- Introduce `ContextBlock` and `ContextEngine` interfaces.
- Keep AWM/Qdrant as the financial memory provider and SQLite FTS5 as session recall.
- Add freshness/sensitivity/token-budget fields and test the Gmail-only boundary.
- Replace large prompt keyword branches with skill/toolset/context policies.

### Phase 5 — automation and optional extensibility

- Migrate reminders and monitor schedules to durable jobs with leases/idempotency.
- Add skills with explicit toolset/risk/completion metadata.
- Add a plugin API only for registered tools/hooks/context providers.
- Consider read-only delegation after metrics show that deterministic parallel calls are insufficient.

## Suggested acceptance criteria

The refactor is ready to continue when:

- adding one tool requires one registration and one handler, not edits to several parallel lists;
- a tool cannot execute without a registry entry, a valid schema, user scope, and a result contract;
- a process restart can resume or clearly mark every in-flight turn;
- exact prior conversation recall works without loading the entire history into memory;
- Gmail-only turns are proven by context-block tests to contain no financial facts or session history;
- audit workflows still enforce fresh verification, research completion, batch-only mutation, and end gates;
- receipts, provider rounds, policy decisions, and final claims can be queried by `turn_id`;
- semantic routing can fail without either granting an unauthorized tool or blocking a legitimate controller-required tool;
- all high-impact mutations have explicit risk/approval semantics and remain user-scoped;
- Discord remains responsive when placeholder delivery, provider streaming, a tool backend, or final rendering stalls.

## Bottom line

Hermes’ most valuable lesson for Delilah is architectural: one agent core, one tool registry, explicit session storage, replaceable context/memory, and adapters at the edges. Its broad platform/tool/plugin surface is not the immediate target.

Delilah should preserve its finance-specific strengths and use Hermes-style boundaries to make them composable, restartable, searchable, and testable. The first concrete change should be a registry-backed vertical slice plus a durable turn/session ID threaded through provider rounds, tool receipts, policy decisions, and final persistence.
