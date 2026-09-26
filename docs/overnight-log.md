# Overnight implementation log

## Current stopping summary

- **Passed:** Step-by-step Steps 0–4 and roadmap items 1, 2, 3, 4, 7, 9, and 11. Item 3 is gated for one bot process per local task database, with configurable in-process workers. All work is on `agentic-roadmap/full-run`; `main` was not changed.
- **In progress / next:** Roadmap item 5 — semantic tool discovery. Item 3 does not support simultaneous bot processes or hosts sharing the task database/provider.
- **Remaining in recommended order:** items 5 and 6; group 5 items 8 and 12; group 6 items 10, 13, 14, and 15.
- **Human decisions recorded:** (1) affirmative choice replies resume into planning as input-only; affirmative approvals require human grant confirmation before dispatch (fail-closed); (2) reconciliation notice delivery uses migration 008's atomic single-claim index `ux_task_reconciliation_notice_claimed_once`; (3) scheduler defaults are one worker, one active task per owner, and eight queued tasks per lane; model inference is scheduled at unit granularity; (4) children inherit a strict subset of parent tools, cannot delegate recursively, parents yield while awaiting children, and cancellation cascades. None of these decisions was reversed.
- **Verification:** Item 11 focused suite: 135 passed. Latest full isolated suite: 853 passed, 1 skipped, and the same 10 clean-database/seed-data failures (budgeting, intelligence, reconciliation, rewards, and world-model fixtures). `.env` was disabled, dummy Discord credentials were used, and the live `data/finances.db` was not accessed.
- **Latest pushed code gate:** `ae9fd15 [gate] Item 11 deterministic verification and repair gates`; Item 3 remains `83073b1` and Step 4 remains `93cd834`.
- **Human decision needed:** whether to support simultaneous bot processes/hosts sharing a task database/provider. “Leases” is underspecified (topology, expiry, renewal, fencing, and shared provider-capacity semantics). Keep distributed execution disabled; do not invent a TTL.
- **Deferred suggestions / constraints:** add an integration test proving a mocked multi-tool provider response does not dispatch later calls after an unknown receipt; add dispatcher-level persistence-failure coverage proving no tool body runs; add typed freshness/arithmetic adapters for financial tools beyond `get_current_financial_position`; and define semantic content validators for arbitrary artifacts beyond JSON syntax and CSV structure. Optional model review uses the configured advisor provider, is off by default, and never replaces deterministic checks. Distributed task leases remain disabled; do not invent expiry/fencing semantics.
- **Review first:** inspect Item 11's receipt linking, unknown-outcome stop, financial evidence, and artifact byte validation, then begin item 5.

## Progress

### Item 11 — deterministic verification and repair gates — passed

- Added a pure bounded verifier for linked receipts, per-source freshness, exact-decimal arithmetic, allowed-tool policy, required deliverables, financial numeric claims, and file identity/format evidence. Failed checks atomically transition a verifying task to partial, append a bounded task event, and create one ready manual repair step; ambiguous effects are never retried.
- Every non-control tool call in a durable task now links to a task step and receipt before dispatch. A missing durable task context stops the advisor before model/tool execution. An unknown receipt stops both the current tool batch and subsequent provider-split batches/rounds.
- Typed current-position snapshots provide the only current financial freshness/arithmetic proof today; unsupported financial sources and untyped reconciliations fail closed. JSON exports are validated from exact delivered bytes with duplicate keys and nonstandard constants rejected; CSV exports require bounded UTF-8 parsing, consistent row widths, and reject JSON documents/scalars mislabeled as CSV. Missing canonical sandbox paths fail before sending.
- Added an optional `DELILAH_TASK_MODEL_REVIEWER` pass (default off) for multi-step tasks after deterministic checks pass. It receives bounded objective/response/check summaries, runs with tools disabled, and cannot replace deterministic checks. Invalid/unavailable review fails closed.
- Verification: focused Item 11 suite: 135 passed; latest full isolated suite: 853 passed, 1 skipped, 10 known clean-seed failures. `py_compile` and `git diff --check` passed. Fresh adversarial review: no blockers.
- Deferred suggestion: add a full mocked provider-loop integration test proving no later call executes after an unknown receipt. Other deferred constraints and the clean-database failures are listed in the current stopping summary above.
- Gate commit: `[gate] Item 11 deterministic verification and repair gates` (`ae9fd15`), pushed to `agentic-roadmap/full-run`.

### Item 3 — durable background execution and completion delivery — passed for single-process deployments

- Added startup recovery and a recurring fair queue sweep for durable delegated child tasks, reusing `task_runs`/`task_steps` and existing call/receipt evidence. Added per-owner queued and active caps, configurable worker count, provider limits, pagination, and an OS-held database process lock; another bot process using the same local DB path fails closed.
- Added restart-safe child completion notices with owner-scoped destination lookup, status-only content, atomic claim/start/delivered events, and no resend after a potentially ambiguous send. Invalid scopes, resume failures, and startup recovery failures atomically park the exact child/parent pair for reconciliation. Periodic recovery does not reinterpret live running tasks as stale.
- Deployment boundary: multiple worker coroutines inside one Delilah process are supported; simultaneous processes/hosts sharing the DB/provider are not. This topology decision remains open; no TTL or fencing policy was invented.
- Verification: final full isolated suite: 721 passed, 1 skipped, and 10 known clean-database/seed-data failures. `py_compile` and `git diff --check` passed. Fresh critic found no blockers in startup recovery/pagination fixes.
- Deferred suggestion: add an integration test showing direct delegation and startup recovery contending for the same owner's active-child cap.

### Step 4 — bounded delegation as child tasks — passed

- Wired model-facing `delegate_task` and conversation-scoped `task_cancel`; parents and children are inspectable through `task_list`. Child tasks have isolated sessions, durable parent links, bounded budgets, a parent-issued capability grant, and no recursive delegation.
- Parent and child states are durably linked to ordinary `tool_calls` and `tool_receipts`. Every delegated action creates a task step. Recovery validates the parent delegate call, receipt turn/hash, child goal, tool scope, and budget before resuming; missing/mismatched evidence parks the task for reconciliation.
- Safe queued children are rehydrated on the next matching owner/session/channel/thread interaction. A CAS claim prevents duplicate resume. Recovery derives consumed steps from existing call rows, honors the original elapsed timeout, and never retries `started`/`unknown` action receipts. The parent remains `awaiting_child` until a known terminal result.
- Scheduler child slots preserve one-worker sequential behavior and cap child execution by both configured workers and provider capacity. Tests exercise one and multiple workers with provider ceilings of one and three.
- Verification: 114 focused tests passed; full isolated suite: 676 passed, 1 skipped, 10 known clean-database/seed-data failures; `py_compile` and `git diff --check` passed. Earlier adversarial blockers were fixed and re-reviewed; the final fresh review found no blockers or suggestions.
- Deferred to Item 3: run queued-child recovery from scheduler/process startup without waiting for a new interaction, and provide the general completion-delivery path.
- Gate commit: `[gate] Step 4 bounded delegation; 114 focused tests pass` (`93cd834`).

### Step 3 — scheduler and capacity controller — passed

- Built `CapacityAwareScheduler` and `ProviderLimiter` (`src/agent/scheduler.py`) enforcing hard concurrency ceilings at the provider client boundary across Ollama, OpenAI, OpenRouter, and Kev.
- Implemented fair round-robin owner rotation with dynamic arrival fairness, longest-wait lane selection between interactive and background lanes, interactive tie-breaking, and per-lane queue capacity limits preventing background backlogs from starving or crashing interactive turns.
- Enforced non-preemptive cooperative cancellation at dispatch boundaries without leaking worker slots or task counters.
- Propagated context variables (`CURRENT_TASK_ID`, `CURRENT_TURN_ID`, session tokens) across asynchronous worker tasks via `asyncio.create_task(..., context=unit.context)`.
- Routed all known model work through the scheduler at unit granularity:
  - Interactive stream inference (`stream_generator`) via `schedule_work(..., "interactive", unit_type="inference", provider=llm_provider)`.
  - Background classification (`classify_with_local_llm` and `classify_transaction_batch`).
  - Autonomous analyst (`run_autonomous_analyst`).
  - History summarization (`_summarize_history_block`).
  - Claim extraction (`auto_extract_and_persist_claims`).
  - Kev decision provider (`DecisionProvider.decide`).
  - Ollama warm/unload/preload operations in `commands.py`.
  - Batch and singular local Ollama embedding in `qdrant_client.py` gated with `provider_capacity("ollama")`.
- Real-time observability: recorded queue wait, inference duration, tool duration, cancellations, and dispatch distributions in `SchedulerMetrics`.
- Verification: 23 dedicated scheduler tests in `tests/test_scheduler.py` covering hard concurrency, owner rotation on dynamic arrival, continuous readiness arbitration, re-entrancy, waiter leak protection, cooperative cancellation, queue lane isolation, nested inference/decision turns, and embedding limiter gating. Full test suite passes: 657 passed, 1 skipped. Adversarial critic review approved with 0 blocking items.
- Deferred suggestions: wrap cloud embedding branches in `qdrant_client` with `provider_capacity(backend)`, assign explicit owner ID to tool/classifier work when triggered during an interactive turn, and persist scheduler metrics in Step 6.
- Gate commit: `[gate] Step 3 route model work through capacity-aware scheduler` (commit `c0771ec`).


### Item 1 — durable task plans and inspection — passed

- Added model-facing `task_plan` and read-only `task_list`. Plans persist ordered tool actions, descriptions, observable completion criteria, retry count, status, and next action on existing `task_steps` rows; no second execution ledger was introduced. Migration 009 adds the fields idempotently.
- Enforced plan-first behavior for detected three-or-more-action requests, artifacts, and side-effecting tools. Whole batches are rejected before dispatch if a plan is missing; `fetch_webpage(save_only=true)` is included. Planned steps must dispatch the exact next tool, link the existing tool-call and matching receipt records, and stop after a failed or ambiguous result. A plan is not authorization or approval. The policy classifier is deterministic and conservative, not a semantic guarantee for every possible paraphrase.
- `task_list` is owner/session/channel/thread scoped and bounded in both SQL and output; persisted descriptions are treated as untrusted text and task status is not evidence that an external effect succeeded.
- Verification: focused task/controller/store/catalog/recall suite: 50 passed; full suite: 600 passed, 1 skipped, 10 known clean-database/seed-data failures. `py_compile` and `git diff --check` passed. Fresh adversarial critic: no blockers.
- Deferred suggestions: add a dispatcher-level `task_list` scope/read-only test and a model-usable lookup from steps to call/receipt evidence. `retry_count` is persisted but stays zero until a separately specified safe-retry policy exists. Plan revisions are not supported. Action-bound approval for resumed side effects remains a Step 2 dependency; plans do not grant consent.

### Item 4 — durable session recall — passed

- Added bounded `search_session_history` model tool plus automatic recall cues backed by `SessionStore.search_messages`; recalled records include session/turn IDs, timestamps, freshness, and explicit untrusted-content framing. Prompt cache is scoped to exact owner/session/channel/thread and bounded to six messages/6,000 characters; returned history is bounded to ten messages/1,600 characters each.
- Gmail/mail-only turns and turns that actually invoked Gmail tools are excluded from future recall. Legacy turns are also excluded using owner-scoped durable Gmail tool-call evidence. An unlinked legacy Gmail call fails closed for that conversation because its associated turn cannot be identified. The lookup is bounded by retained messages rather than only the newest tool-call rows.
- Verification: focused session recall/store/tool catalog suite: 25 passed. Full suite in an isolated temporary working directory: 590 passed, 1 skipped, 10 failed. The same clean-database/seed-data expectation failures and pre-existing Discord budget-dashboard failure remain. `py_compile` and `git diff --check` passed. Fresh final critic: no blockers.
- Deferred critic suggestions: test Gmail exclusion through the full dispatcher rather than the marker helper; add a model-level behavioral prompt-injection evaluation; test many intervening non-Gmail calls with an older retained Gmail call; document/test that a stale unlinked Gmail record can suppress recall for the entire conversation until pruning. Intent classification for mixed mail/financial prompts remains heuristic and merits separate policy review.

### Item 9 — first-class await and resume — passed

- Added the model-facing `await_user` tool. It requires an active durable task, validates a 1–1000 character question and 2–8 distinct exact choices, persists an expiring question (5 minutes–7 days; default 24 hours), displays a task/question resume token, and terminates the current turn after the question. Mixed tool batches containing `await_user` are rejected before execution.
- Exact unique choice replies are routed to the same owner/session task. Replies arriving while the original advisor turn is finishing retain the resume match in the queue and are resumed after it drains. Expired questions are atomically cancelled; unrelated or ambiguous text remains a normal new turn. Resume prompts carry the objective and persisted step/action states.
- User answers are marked input-only, never approval. During that continuation, dispatch permits only an explicit read-only allowlist and denies unclassified tools, writes, file operations, and `fetch_webpage(save_only=true)` before dispatch.
- Verification: focused task/controller/catalog/recall/bot suite: 39 passed. Full suite in an isolated temporary working directory: 594 passed, 1 skipped, 10 known clean-database/seed-data failures. Fresh critic: no blockers.
- Deferred critic suggestions: add a dispatcher-level test proving blocked calls never reach handlers; atomically claim an answer before multiple duplicates can queue; page beyond the 500 newest waiting tasks. The explicit `!resume` prompt now carries the persisted step identity/action state.

### Item 7 — durable phase-based recovery — passed

- Added migration 010 with durable task `phase` and `phase_next_action` fields, including legacy-state backfill. Phase changes use task-version CAS, append-only `task_events`, and bounded conflict retry only when the phase itself did not change.
- Wired received/planning/executing/awaiting-user/verifying/delivering/completed/partial/failed phase updates into the runtime. Final Discord delivery exceptions are treated as ambiguous: no fallback resend/edit is attempted; task status becomes partial and restart recovery repairs status/phase split writes.
- Provider assistant tool-call batches now persist ordered IDs, tool names, and argument rows plus their manifest event atomically before dispatch. Added a bounded reconstruction API; ordinary retention preserves referenced turns and call rows.
- Restart recovery checks every unlinked persisted block before any task-step recovery branch, checks linked receipts against exact call/owner/tool/argument hash and rejects duplicate receipts, resumes only calls proven not started, and parks ambiguous or inconsistent evidence for manual reconciliation. Recovery repairs execution, delivery, terminal-status, and phase split windows.
- Verification: focused task-controller/session-store suite: 52 passed. Latest full isolated suite: 623 passed, 1 skipped, 10 known clean-database/seed-data failures. `py_compile` and `git diff --check` passed. Fresh adversarial critic: no blockers.
- Deferred critic suggestions: add an integration test proving phase/tool-call persistence precedes tool invocation; consolidate controller ownership during a later runtime extraction. `awaiting_child` is a supported phase but is not entered until Item 2 implements child execution.
- Deferred safety/product decisions: exact results from completed unlinked tool blocks are not automatically injected/replayed (fail closed and require recovery rather than risk a duplicate); retention/privacy purge policy remains an operator-level decision. Step 2 approval/grant and notice-delivery decisions remain unresolved.

### Step 2 — resumable task execution — passed

- Added `TaskController.recover_incomplete` to project linked receipt outcomes after process restart across all phases (`executing`, `planning`, `verifying`, `delivering`). Preserves `restart_after_confirmed_failure` when steps have failed, retains `monitor_readback_required`, preserves `plan_pending` wait reasons, and recovers unstarted calls to `restart_before_dispatch` with stale prepared receipt cleanup.
- Added `TaskController.validate_tool_dispatch` enforcing `TASK_NOT_DISPATCHABLE`, `MONITOR_READBACK_REQUIRED`, and `TASK_STEP_ALREADY_CONFIRMED` replay prevention, with readback exemption for `monitor_list_rules`. Delegated `llm.py` tool dispatch validation to `TaskController.validate_tool_dispatch`.
- Resumption protocol: exact choice answers via `accept_reply` resume tasks into planning phase with `reply_is_input_only=True`, denying write/mutation tools unless separately authorized.
- Proactive reconciliation surfacing: `surface_reconciliation_once` backed by migration 008's unique index `ux_task_reconciliation_notice_claimed_once` guarantees at-most-once notice delivery without race conditions.
- Verification: `tests/test_task_controller.py::test_step2_resumable_execution_full_gate` verifies read-only workflow restart without work replay, monitor insert -> restart -> readback workflow, pending-step crash recovery, unknown outcome reconciliation and replay blocking, choice reply matching/rejection, confirmed failure preservation, and plan_pending preservation across repeated restarts. All 41 task controller tests pass; full test suite passes (634 passed, 1 skipped). Adversarial critic review approved with 0 blocking items.
- Deferred suggestions: prune legacy pre-phase recovery branches in `recover_incomplete`.
- Gate commit: `[gate] Step 2 resumable task execution and workflow recovery` (commit `6e34f70`).

### Step 0 — workflow action contracts — passed

- Documented owner/effect, authorization, retry, evidence, and reconciliation contracts for Gmail search/read and monitor create/list.
- Added fail-closed receipt admission for both advisor monitor-insert tools: an owner’s unresolved `started`/`unknown` monitor receipt blocks either insert tool. Ambiguous insert/commit failures remain `unknown`; receipt finalization reads back terminal evidence after a lost commit acknowledgement.
- Verification: targeted contract/receipt/monitor tests: 32 passed. Full suite in an isolated temporary working directory: 560 passed, 1 skipped, 9 failed. The failures are legacy finance/world-model tests expecting seeded records absent from the clean test DB; the repository’s real `data/finances.db` was not used or mutated. `git diff --check` clean.
- Fresh adversarial review: no blockers. Deferred suggestions: add dispatcher-level tests for the whole model-dispatch path and exact serialized config output; route Discord `!monitor create` / `!monitor add` commands through the guarded receipt lifecycle. Those command paths are explicitly outside the Step 0 advisor-tool workflow and should be revisited before expanding monitor task recovery.
- Follow-up requirement: the current dispatcher auto-issues its internal mutation grant; it does not independently bind that grant to explicit user intent or human approval. Step 2 must enforce task/action intent binding before resumable monitor dispatch.

### Step 1 — durable task, step, and event records — passed

- Added migration 007 and owner-scoped `SessionStore` APIs for task runs, ordered/dependent steps, versioned CAS transitions, and append-only events. Steps reference existing tool-call IDs and owner/call-matched receipt IDs; queue ordering stays on task rows.
- Event payloads are restricted to task metadata/evidence references. Referenced tool calls and their turns are retained past the ordinary per-session cap; task/event deletion is restricted until an explicit purge policy exists.
- Verification: focused session-store tests: 8 passed. Full suite in isolated temporary working directory: 564 passed, 1 skipped, 9 failures from legacy finance/world-model tests requiring seed records absent from the clean DB. The live finance DB was not used or mutated. `git diff --check` clean.
- Fresh adversarial review: no blockers. Deferred note: `receipt_id` has no SQL foreign key because `ReceiptStore` installs its table independently; `SessionStore` verifies receipt ownership and matching call ID on every link/update. No receipt-deletion API currently exists.
