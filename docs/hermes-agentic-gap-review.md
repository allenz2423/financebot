# Hermes + Muse → Delilah: 15-Point Agentic Roadmap

Date: 2026-09-25

Scope: current Delilah `main` working tree compared with the active Hermes checkout at `/tmp/hermes-agent-crossref.Wc626R`, plus public Hermes/Muse material and Muse research. Delilah is an open-source project for self-hosting, from a personal install to a deployment shared with friends; architecture should support both. This is a roadmap and cross-reference, not an implementation plan approval.

## Executive diagnosis

Delilah is now substantially better at proving that a requested tool ran, but it is still primarily a single-turn advisor with a large legacy loop. Hermes feels more agentic because it surrounds the model with durable work state: explicit tasks, delegation, session recall, background completion, and recovery state. Muse adds a second useful direction: plan/execute/reflect/memorize, peer verification, step-level steering, preference learning, secure task environments, and a controlled way to create missing capabilities.

Delilah already has the right safety foundation—authoritative financial tools, receipts, call IDs, grants, claim evidence, catalog validation, monitors, reminders, and `SessionStore`. The next leap is to put durable orchestration around those primitives instead of adding more prompt pressure or more code to `llm.py`.

## Deployment constraint: make capacity configurable

Self-hosted installations will vary: CPU-only, consumer GPUs, larger local servers, or remote model APIs. Do not assume one hardware profile. Keep scheduling and concurrency configurable per deployment, with bounded queues, backpressure, cancellation, and a conservative default. A single saturated model server may turn concurrent child-agent calls into queueing rather than throughput, while a larger host or remote-provider pool may benefit from parallel work.

Measure representative prompt sizes, time to first token, tokens per second, end-to-end latency, CPU/GPU utilization, VRAM, and queue wait to help each operator tune worker count and model routing. This is deployment tuning, not a prerequisite for designing delegation. Background jobs must work with one worker; they may use more when configured. Allow child work to use a cheaper/local model or a separately configured provider when the operator chooses, while preserving the same authorization and evidence contracts.

Ship a documented safe default so a laptop install needs no tuning: one model worker, one active child task, deterministic verification enabled, independent LLM review disabled, and interactive browser automation disabled until configured. Keep model routing explicit; child work uses the configured provider unless the operator sets another route.

Support both personal and friend-shared deployments from the start: user-scoped records, credentials, task histories, grants, and memory must not leak across users. Data isolation is not enough for shared use: one person's long run must not starve other users' interactive turns. Use per-user queues with fair scheduling (for example, round-robin between users), plus configurable per-user limits on active work and queued jobs. Keep the initial policy simple and document its defaults; hosts can tune quotas for their model capacity.

## What Meta Muse makes concrete for Delilah

These are the product-level gaps suggested by Meta's [Muse overview](https://ai.meta.com/muse/) and [launch description](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/). The 15-item roadmap below gives the implementation-level breakdown.

### A. Goals that keep moving forward

Problem: Muse turns a stated goal into an action plan, tracks progress, and suggests the next useful step. Delilah can handle tool-backed turns and domain monitors, but it has no durable general goal whose unfinished steps keep advancing across conversations.

Proposed Sol: Add persistent goals linked to task runs and steps, with measurable outcomes, next actions, evidence, and user-visible progress. On each relevant event or scheduled wake-up, resume the next safe step and tell the user what changed.

### B. Work that continues in the background

Problem: Muse continues long-running work after the app closes and returns when it finishes, something changes, or it needs permission. Delilah has reminders and monitors, but they do not cover arbitrary multi-step tasks with durable progress and completion delivery.

Proposed Sol: Build the durable job and delivery path in roadmap item 3 around a single worker queue first. A job should resume from saved state, retry recoverable failures, and deliver a concise completion or approval request into the user's conversation; add concurrent workers only if measured capacity supports them.

### C. A connected tool that can safely fill a capability gap

Problem: Muse works across connected apps and says it can build a missing tool. Delilah has a broad tool catalog, but tool discovery can miss available capabilities, and a missing integration currently tends to end the task rather than produce a tested, installable path forward.

Proposed Sol: Improve semantic tool search first (item 5). If nothing suitable exists, create a capability proposal with a scoped adapter/workflow, tests, permission request, and isolated trial; activate it only after explicit approval (item 15).

### D. A secure computer for the agent's work

Problem: Muse gives the agent a persistent VM and browser for web tasks, with credentials stored so the agent can use them without reading the secrets. Delilah has a credential vault and browser-state handling, but those are not yet one isolated task environment with a narrow network and tool boundary. A full always-on VM could also add unnecessary resource overhead for Delilah's mostly short financial tasks.

Proposed Sol: Use an on-demand isolated browser worker/container with explicit CPU, memory, and time limits—not a dedicated full VM per user. Apply a task-scoped capability manifest (item 14) to browser state, files, network egress, and executor-only credential handles; persist encrypted browser state only when needed for continuity, then stop the worker and expire/revoke its lease. Escalate to a microVM only when a workflow's threat model justifies the extra cost. Keep financial tools behind Delilah's existing authorization and evidence checks.

### E. Approval at the action boundary, with a readable audit trail

Problem: Muse presents planned and completed actions, asks before sensitive actions, and lets users set access by connected app. Delilah has receipts, grants, and approval primitives, but the receipt trail is not yet a consistent user-facing task history, and its grant path must distinguish target binding from a real human approval.

Proposed Sol: Show each planned step and its actual execution receipt in a task timeline. Before a gated action, persist the exact operation, target, data scope, and consequences; offer once/always/deny choices where policy permits; require a matching approval record before execution. Keep model narration separate from verified events.

### F. Memory that improves suggestions and can be corrected

Problem: Muse remembers details shared once, uses them to personalize future work, and lets the user ask it to forget. Delilah has Active World Model memory, but learned preferences, sourced facts, and task lessons are not yet a clear user-managed profile that explains how a memory influenced an action.

Proposed Sol: Store preferences and durable facts with provenance, confidence, sensitivity, and expiry; show which saved item affected a plan; provide inspect/edit/forget controls; and run the reflection step in item 10 only after task verification. Never silently promote an uncertain inference into a user fact.

## Roadmap

### 1. Durable task and plan objects

Problem: Hermes exposes `todo_list` with ordered items, status transitions, parent subtasks, revisions, and verified completion. Delilah has progress policies and audit state, but no general task object that survives retries, restarts, or context compaction. The active conversation still depends heavily on `SESSION_HISTORY` and loop-local variables.

Proposed Sol: Add `task_runs` and `task_steps` to `SessionStore`, including step IDs, dependencies, completion criteria, evidence references, retry count, status, and `next_action`. Expose a small model-facing `task_list` tool. Require a durable plan for requests with three or more actions, artifacts, or external side effects.

### 2. Bounded delegation and child agents

Problem: Hermes has `delegate_task` with isolated child context, batches, bounded depth/concurrency, budgets, and live list/steer/stop control. Delilah's `AgentRuntime` is currently an adapter around the legacy runner, not a real worker runtime; “summon agents” is not an executable capability.

Proposed Sol: Add a bounded delegation interface with configurable worker count and provider/model routing. Give each child a self-contained goal, user-scoped restricted toolset, budget, and structured result. Ship with one model worker and one active child task; operators with capacity can enable parallel children or route them to another model/server. Disallow recursive delegation by default and retain parent-side result verification.

### 3. General background execution and completion delivery

Problem: Delilah has reminders and financial monitors, but not a general substrate for long-running work. Hermes claims jobs, tracks detached executions, reaps stale work, and delivers completion through a fresh turn.

Proposed Sol: Add durable job records and completion delivery to the existing reminder/monitor scheduler. Save task state, prevent duplicate claims, track progress and bounded retries, and deliver terminal status. Support configurable worker counts and leases so a basic single-process install stays simple while shared or higher-capacity deployments can run multiple workers safely. In shared mode, schedule fairly across users and cap each user's active and queued work so background jobs cannot indefinitely delay interactive turns.

### 4. Durable session recall as a model capability

Problem: Delilah writes session, turn, message, and tool-call records, but the active prompt still primarily uses the in-memory history window. There is no model-facing session search or automatic recall when a user refers to an older decision, open task, or prior result. Hermes explicitly provides `session_search` and tells the agent to recall before asking the user to repeat context.

Proposed Sol: Add a bounded `search_session_history` tool backed by `SessionStore.search_messages`. Automatically issue a cheap recall query for continuation phrases and older-task references. Inject recalled messages as evidence with session/turn IDs and freshness, while treating `SESSION_HISTORY` as only a short-lived prompt cache.

### 5. Semantic tool discovery with authoritative authorization

Problem: Delilah's ordinary tool exposure can be narrowed by first-match keywords and limited exploration. A truthful model can still appear inert when the capability it needs is not in the lexical seed. Hermes uses a registry, toolsets, availability checks, and dynamic tool search.

Proposed Sol: Add registry-owned `search_tools(query, task_context)`. Return a bounded set of authorized tool definitions, availability reasons, side-effect labels, and examples. Use semantic retrieval to find candidates, but keep controller authorization, required-tool inclusion, and grants authoritative.

### 6. Explicit autonomy contracts for vague requests

Problem: Delilah's “minimum sufficient tools” guidance prevents runaway work, but it can under-interpret requests such as “just do whatever,” “take care of this,” or “review my situation.” There is no typed distinction between execute, investigate/report, plan, monitor, continue, and await-decision modes.

Proposed Sol: Classify every turn into an intent contract containing `mode`, `objective`, `allowed_initiative`, `required_evidence`, `completion_criteria`, and mutation permissions. For an open-ended financial request, select a deterministic default workflow—refresh data, inspect liquidity/debt/bills, then identify the top actions—and show the selected scope in status. Never expand into destructive mutations without approval.

### 7. Durable phase-based recovery

Problem: Delilah has strong local safeguards—required tools, receipts, empty-response retries, repetitive-call breakers, artifact watchdogs, and progress policy—but most recovery state lives inside `_chat_with_delilah_impl`. A provider switch or process restart cannot reconstruct the exact plan, current step, pending approval, or retry class.

Proposed Sol: Move the turn state machine into a durable `TurnController` with phases `received → planning → executing → awaiting_child → awaiting_user → verifying → delivering → completed/partial/failed`. Persist each phase transition and next action before an external operation. Follow Hermes' pattern of persisting the assistant tool-call block before executing the tool.

### 8. Reusable workflow and skill registry

Problem: Hermes has reusable, versioned skills that can be loaded into context. Delilah has domain prompt text, Active World Model claims, and knowledge-base storage, but a successful email triage, monthly close, or job-research procedure is not captured as an executable playbook.

Proposed Sol: Start with a small versioned set of declarative workflows containing trigger hints, required tools, steps, guardrails, and result schema. Keep facts in the World Model, procedures in workflows, and transient progress in task runs. Add signing or third-party workflow installation only if workflows become externally authored or shared across users.

### 9. First-class await and resume protocol

Problem: Delilah has forms and approval/grant primitives, but a missing user decision is usually expressed as prose. That makes the next message look like a fresh turn instead of a continuation of blocked work.

Proposed Sol: Add `await_user`/`request_decision` as a durable task state with a structured question, allowed answer shape, expiration, and resume token. Route the next user message back into the blocked task, preserve the plan, and continue at the exact pending step.

### 10. Plan–execute–reflect–memorize loop

Problem: Muse research describes a Plan–Execute–Reflect–Memorize cycle. Delilah can compress context and store claims, but it does not have an explicit post-task reflection stage that evaluates what worked, what failed, and what should be reused.

Proposed Sol: After verification, run a bounded reflector that extracts only durable lessons: `[DECISION]`, `[FACT]`, `[LESSON]`, failed-tool patterns, and workflow improvements. Store each item with provenance, confidence, sensitivity, and expiry. Never turn an unverified model inference into a user fact.

### 11. Verifier, peer-review, and repair gates

Problem: Muse's open architecture uses planners, workers, inspectors, and peer reviewers; the research literature also emphasizes deterministic verification and verifier-guided repair. Delilah has claim evidence and audit gates, but no generic reviewer pass for multi-step outputs such as financial summaries, reconciliations, or generated artifacts.

Proposed Sol: Add deterministic checks for required receipts, data freshness, arithmetic invariants, policy constraints, and requested deliverables. On failure, create a structured repair step. Keep a separate read-only model reviewer optional and configurable because it adds inference cost; retain deterministic verification across deployment sizes.

### 12. Semantic traces and mixed-initiative steering

Problem: Muse research supports multiple trace levels, references to specific steps, user feedback, and mixed-initiative repair. Delilah streams text and status updates, but does not maintain a durable step-level trace that the user or parent agent can steer.

Proposed Sol: Give every task step a stable ID, status, inputs, outputs, evidence, and explanation. Add `steer_task(step_id, correction)` and `inspect_task(task_id, detail_level)`. Let the user redirect one step without destroying completed work, and record the steering event in the audit trail.

### 13. Personal operating profile and preference learning

Problem: Muse is designed to learn from conversations and reflect; M.U.S.E. distinguishes worker profiles from user profiles. Delilah stores useful world-model claims, but lacks a deliberate preference/policy profile for risk tolerance, autonomy limits, notification preferences, preferred models, and presentation style.

Proposed Sol: Add an explicit, user-scoped preference profile with inspect/edit/forget controls and provenance. In friend-shared deployments, isolate profiles and memories by user. Use preferences to choose defaults, never to bypass grants or safety gates; keep inferred preference learning opt-in.

### 14. Secure task environments and capability leases

Problem: Meta describes Muse running in a secure virtual machine, while Hermes restricts child contexts and available tools. Delilah has browser/sandbox concepts and tool grants, but no unified per-task environment boundary or capability lease. A dedicated always-on VM per user may be excessive for Delilah's workload.

Proposed Sol: When interactive browser automation is added, run it in an on-demand isolated worker/container with configurable CPU, memory, and runtime limits. Scope its browser profile, credentials, and network access to the task and user; keep credentials executor-only; persist encrypted browser state only when continuity is needed; then stop the worker. Use a microVM only if a deployment's threat model requires the stronger boundary. Keep financial authorization and receipt checks in the host runtime.

### 15. Safe capability and skill creation

Problem: Meta's Muse materials describe building missing tools, and M.U.S.E. treats skills and worker profiles as extensible capabilities. Delilah can discover and load existing tools, but cannot turn a repeated missing-capability diagnosis into a safe reusable adapter or workflow.

Proposed Sol: When the same missing integration recurs, let Delilah draft an adapter/workflow proposal with permissions and acceptance checks. Keep implementation and registry activation as an explicit operator action. Until installed and verified, the agent may report the proposal but must not claim the capability ran.

## What is already strong in Delilah

- Domain-specific authoritative tools and financial isolation are stronger than a generic agent.
- Required-tool inference and named `tool_choice` address the observed Plaid failure.
- Receipts, call IDs, result envelopes, claim evidence, grants, and catalog validation provide a solid execution-truth foundation.
- Audit workflows have more deterministic state enforcement than a generic todo list.
- `SessionStore` is a viable foundation for durable tasks, session search, and background execution.
- Monitor/reminder functionality is a useful seed for a general job system.

## Recommended implementation order

1. Implement the continuity slice: durable task state and phase recovery (items 1 and 7), session recall (item 4), and await/resume (item 9).
2. Add bounded background jobs and completion delivery (item 3).
3. Add configurable delegation and worker/provider routing (item 2); profile capacity so operators can tune the defaults for their hardware and model providers.
4. Add deterministic verification and repair as the default (item 11), with independent model review off by default; then tool discovery and lightweight autonomy defaults (items 5 and 6).
5. Add task traces and reusable workflows where they remove recurring friction (items 8 and 12).
6. Develop reflection, user-controlled memory, browser isolation, and capability proposals with user scoping and configurable resource limits (items 10 and 13–15). Make inferred preference learning and independent model review optional due to privacy and inference cost.

## Concrete step-by-step implementation plan

This plan reuses Delilah's existing session, tool-call, receipt, and authorization records. It does not introduce a parallel `tool_attempts` ledger or claim that queue priority can preempt an active model call.

### Step 0 — Define two real workflows and their action contracts

Work: Use workflows that already exist in the code: read-only Gmail search followed by reading selected messages and summarizing them; and state-changing monitor creation followed by listing the rule to verify it. `search_gmail`/`read_gmail_message` are in the exposed schema and dispatcher, and monitor create/list are registered tools. For each call, record owner scope, read/write effect, required grant or approval, idempotency support, postcondition/evidence, and safe retry behavior. If an operation cannot be reconciled after an ambiguous outcome, mark it as manual-recovery-only.

| Workflow action | Owner/effect | Authorization | Retry behavior | Evidence/reconciliation |
| --- | --- | --- | --- | --- |
| `search_gmail` | Current Delilah user; read-only Gmail query | User's connected Gmail OAuth identity; no mutation grant | Safe to repeat; a timeout does not change mailbox state | Confirmed call receipt plus returned message IDs/headers |
| `read_gmail_message` | Same user; read-only message fetch | Same user's Gmail OAuth identity; email body remains untrusted input | Safe to repeat | Confirmed call receipt, message ID, and fetched body; summary cites the source message IDs |
| `monitor_create_natural_rule` | Current Delilah user; inserts a local monitor rule that may later generate alerts | Contract requires an explicit user request, current-user scope, and the mutation authorization/receipt path. The current dispatcher issues its internal grant automatically; that is not a separate human approval or proof that the user explicitly requested this exact action, so a resumable workflow must enforce that binding before dispatch | Insert is not idempotent. Within advisor tool calls, an unresolved `started`/`unknown` receipt from either monitor-insert tool blocks both tool paths for that user, including across turns/restarts; inspect owner-scoped rules and manually reconcile before retrying. Never auto-retry an ambiguous result. Discord `!monitor` commands are outside this guarded workflow and remain a follow-up gap | Confirmed receipt and `monitor_list_rules` read-back of the matching owner-scoped ID plus normalized name/kind/config. If the intended row is not uniquely identifiable, require manual reconciliation |
| `monitor_add_rule` | Same user; inserts a local monitor rule through structured rule fields | Same explicit-user-request/current-user-scope contract and automatically issued internal grant caveat as `monitor_create_natural_rule` | Non-idempotent insert; the same cross-tool unresolved-receipt guard blocks replay, including a switch to/from natural-language creation. Discord `!monitor` commands remain outside the guarded advisor-tool workflow | Confirmed receipt plus owner-scoped `monitor_list_rules` read-back of the matching rule ID and normalized name/kind/config; otherwise manual reconciliation |
| `monitor_list_rules` | Same user; read-only local rule lookup | Owner-scoped read | Safe to repeat | Confirmed call receipt and rule ID/name/kind/config; absence or multiple plausible matches is not proof of successful creation |

The monitor action is intentionally a local state change rather than an email send or financial transfer. It exercises task recovery without introducing an unsupported external mutation. Its eventual alert delivery is outside this first workflow's acceptance gate. The receipt guard is deliberately conservative: both `monitor_create_natural_rule` and `monitor_add_rule` insert into the same owner-scoped rules table, so either tool's `started`/`unknown` receipt blocks both insertion paths until manual reconciliation; the guard does not guess whether an insert committed or silently clear the ambiguity.

Reuse: `SessionStore` already records tool calls with call IDs and argument identity. `ReceiptStore` already records prepared/started/confirmed/failed/unknown outcomes. `authorization.py` has typed approval and target-bound grant rules, but pending approval persistence must be connected to task state.

Dependency: None.

Acceptance gate: Both workflows have an explicit action/evidence/retry table. No side effect is automatically retried after an ambiguous outcome unless its provider idempotency or a read-back check makes replay safe.

### Step 1 — Add durable task, step, and event records

Work: Add migrations and `SessionStore` APIs for `task_runs`, `task_steps`, and append-only `task_events`. Store stable owner ID, conversation/session reference, parent task ID, objective, status, lane, enqueue/update times, current step, wait reason, cancellation request, result reference, and version. Store step ordering/dependencies, status, next action, retry policy, per-step version, and links to existing `tool_calls` and `tool_receipts` IDs. Make state transitions atomic compare-and-swap updates conditioned on the expected status and version, so two workers cannot claim/advance the same step. Keep queue ordering on task/step rows initially; add a separate queue table only if measured contention requires it.

Do not duplicate raw tool arguments, receipt lifecycle, or approval decisions in another execution ledger. An idempotent `call_id` protects Delilah's persisted call record from conflicting reuse; it does not guarantee an external provider deduplicates the side effect.

Dependency: Step 0 defines the fields and ownership requirements.

Acceptance gate: Task/step state survives restart; every lookup is owner-scoped; task steps can link to current call and receipt records; competing updates against the same version allow only one transition; migration does not change existing tool execution behavior.

### Step 2 — Build a resumable task state machine and migrate one workflow

Work: Add internal operations to create, get/list, cancel, resume, and transition tasks. Use states such as `queued`, `running`, `waiting_user`, `waiting_approval`, `verifying`, `needs_reconciliation`, `succeeded`, `partial`, `failed`, and `cancelled`. Persist the task/step transition before dispatch. Migrate the read-only workflow first, then the monitor create/read-back workflow. On restart, resume undispatched steps, continue after confirmed receipts, and move to `needs_reconciliation` when the last side-effect receipt is `started` or `unknown` and the outcome cannot be established.

Connect a reply only to a task that is waiting on a matching question. Bind approval to the exact action fingerprint using existing authorization types; persist the pending question/approval link as a task event. Surface `needs_reconciliation` in the task status and proactively notify on the next user interaction or scheduled status sweep, without repeating alerts. Unrelated messages start normal turns.

Dependency: Step 1.

Acceptance gate: Restart at each transition resumes the right step without repeating confirmed work. An unknown side effect is visibly `needs_reconciliation`, proactively surfaced, and not replayed. A matching user reply resumes a waiting task; an unrelated reply does not.

### Step 3 — Route all model work through one capacity-aware scheduler

Work: Replace or integrate the current in-memory `ACTIVE_ADVISOR_TASKS` / `PENDING_ADVISOR_MESSAGES` admission path with persisted scheduling. Every interactive advisor inference and background inference using the same provider capacity must enter the same controller; inventory classifier and direct-provider paths so they cannot silently bypass its concurrency limit. Use separate FIFO lanes per owner for `interactive` and `background` tasks.

At each dispatch boundary, rotate across owners with ready work. For a selected owner, dispatch the lane that has waited longest since that lane's previous dispatch; break ties in favor of interactive. After one bounded unit completes, checkpoint and requeue unfinished work. A unit is one model inference or one tool batch/step, preserving current batching where safe. This gives fair dispatch opportunities and service to both lanes; it is not a wall-clock response guarantee.

Use two enforcement points: the scheduler decides order and fairness; a bounded semaphore at the shared provider-client/request boundary enforces the hard per-provider concurrency ceiling even if a future or overlooked caller bypasses scheduler ordering. Provider routes with separate capacity pools may have separate semaphores. Inventory every model call site and route it through the scheduler for fairness; the client cap remains defense in depth for capacity.

Defaults: one model worker; one active task per owner; provider output limits/timeouts come from the selected provider configuration. Per-owner queue limits and worker count are configurable. Do not invent global token, duration, retry, or lane-ratio constants before measuring workloads.

Cancellation is cooperative at dispatch boundaries. An in-flight generation or tool operation cannot be interrupted unless that provider/tool supports cancellation. The scheduler may prioritize the next interactive dispatch, but it cannot preempt work already running.

Dependency: Steps 1–2; provider-call inventory and timing instrumentation can proceed alongside Step 0.

Acceptance gate: Interactive and background paths use the same capacity controller when they share a provider. Owner dispatch rotates instead of draining one owner's backlog. For one owner with both lanes continuously ready, both receive dispatches and interactive wins a wait-time tie. Every known caller is scheduler-routed, while a deliberately bypassing caller still cannot exceed the provider-client semaphore. A queued turn runs at the next eligible boundary, with no preemption or latency guarantee claimed. Queue wait, inference time, tool time, and cancellations are observable.

### Step 4 — Add bounded delegation as child tasks on that scheduler

Work: Represent a delegated task as a normal `task_run` with `parent_task_id`, inherited owner, narrower tools/grants, explicit budget, and structured result. Parent waiting releases its worker slot. Children use the same queue, authorization, receipts, recovery, and cancellation paths. Disable recursive delegation initially. Permit parallel workers or alternate model routes only through operator configuration and provider-specific concurrency limits.

Dependency: Steps 1–3.

Acceptance gate: Parent and child can be inspected/cancelled and survive restart. Child permissions never exceed the parent. Child side effects link to ordinary receipt evidence. With one worker, child work is sequential; with more workers, observed concurrency stays within configured provider limits.

### Step 5 — Add action-specific verification and repair

Work: For each migrated tool, define the proof of completion: confirmed receipt, provider reference, or a fresh read-back/postcondition with a stated freshness window. Keep `unknown` distinct from `failed`. Repair only if the operation is known not to have happened or is safe to repeat; otherwise stop and ask for reconciliation.

Dependency: Step 0's action contracts and Steps 1–2's receipt links.

Acceptance gate: No user-facing success claim without the action's required proof. Failed and ambiguous outcomes have different handling. Repair cannot replay a non-idempotent ambiguous operation.

### Step 6 — Tune limits and expand from observed use

Work: Use queue, provider-round, tool receipt, cancellation, retry, and reconciliation data to tune worker counts, per-owner caps, deadlines, and model routing. Add more background jobs, workflows, review models, or browser workers only when a real use case needs them. Document the safe defaults and exactly which provider settings affect execution.

Dependency: A working slice through Steps 2–5.

Acceptance gate: Operators can distinguish queue delay from model/tool delay, tune limits without code changes, and run shared deployments without cross-owner task/data leakage or starvation. Any increased concurrency retains authorization, evidence, provider limits, and cancellation semantics.

## Sources and cross-reference anchors

Hermes implementation anchors:

- [`todo_tool.py`](https://github.com/nousresearch/hermes-agent/blob/main/tools/todo_tool.py#L191-L281) — durable task-list behavior.
- [`delegate_tool.py`](https://github.com/nousresearch/hermes-agent/blob/main/tools/delegate_tool.py#L440-L532) — delegation, budgets, batches, and control.
- [`async_delegation.py`](https://github.com/nousresearch/hermes-agent/blob/main/tools/async_delegation.py#L1-L90) — background delegation records and completion handling.
- [`session_search_tool.py`](https://github.com/nousresearch/hermes-agent/blob/main/tools/session_search_tool.py#L619-L652) — model-facing session search.
- [`model_tools.py`](https://github.com/nousresearch/hermes-agent/blob/main/model_tools.py#L213-L321) — registry and tool selection.
- [`turn_tool_round.py`](https://github.com/nousresearch/hermes-agent/blob/main/agent/turn_tool_round.py#L118-L162) — persist tool-call state before execution.
- [`turn_final_response.py`](https://github.com/nousresearch/hermes-agent/blob/main/agent/turn_final_response.py#L135-L239) — bounded continuation and stall guards.

Muse references:

- [Meta Muse overview](https://ai.meta.com/muse/) — multi-step action, audit trail, connected apps, and building missing tools.
- [Meta announcement: Introducing Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/amp/) — secure VM, browser action, learning, reflection, and background work.
- [MUSE Protocol](https://github.com/myths-labs/muse) and its [protocol specification](https://github.com/myths-labs/muse/blob/main/docs/PROTOCOL.md) — Markdown-based persistent context with explicit facts, decisions, and lessons.
- [M.U.S.E. open-source architecture](https://github.com/A-C-I-SOFTWARE-AND-DEVELOPMENT/M.U.S.E) — task graphs, specialists, reviewers, audit ledger, profiles, and skills.
- [MUSE: A Unified Agentic Harness](https://openreview.net/pdf/d9be11a62f860ecb1b8be2b784af8e60e4245966.pdf) — plan/execute/reflect/memorize and deterministic verification patterns.
- [Interactive Meta-Agent research](https://arxiv.org/abs/2608.16181) — semantic traces, user feedback, repair, and mixed-initiative steering.

## Bottom line

Delilah does not need a more aggressive system prompt to feel agentic. It needs durable agency: a remembered objective, an explicit next step, bounded parallel work, a way to keep working after the current response, verification before claims, and a reliable way to resume after failure or user input. Hermes provides concrete runtime patterns; Muse adds reflection, steering, personalization, secure execution, and capability evolution. Delilah already has the financial execution evidence needed to adopt those patterns safely.
