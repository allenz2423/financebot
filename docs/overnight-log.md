# Overnight implementation log

## Progress

### Step 2 — resumable task execution — in progress; acceptance gate blocked

- Added task-controller operations and runtime integration for the scoped Gmail and monitor workflows: durable turn/task correlation, call/receipt-linked steps, restart projection, exact-question reply matching, explicit resume, monitor read-back enforcement, reconciliation status, and once-per-task notice claims. Ambiguous side effects are never replayed. Pending approval decisions are stored with the exact action fingerprint, but affirmative approvals intentionally remain blocked because the dispatcher does not yet consume a persisted action-bound authorization grant.
- Fresh critic blockers: (1) approved tasks do not resume through a matching grant/receipt path; (2) no production flow currently creates durable task questions, so controller-only reply tests do not prove an end-to-end waiting workflow; (3) claiming a reconciliation notice before external delivery prevents duplicate alerts but can permanently lose a notice if send/crash outcome is ambiguous. The delivery guarantee is unresolved because Discord send has no exactly-once transaction with SQLite. Do not weaken the claim-before-send behavior or auto-retry an ambiguous delivery without a reviewed policy.
- Verification: task/session/receipt/auth/runtime/action-contract focused suite passed (43 tests before the final crash-boundary regression); full suite after final code: 579 passed, 1 skipped, 10 failed. Failures are the same clean-database/legacy seeded-data expectations and pre-existing `test_discord_budget_commands` failure; tests ran in isolated temporary working directories with placeholder Discord credentials. `py_compile` and `git diff --check` passed.
- Deferred critic suggestions: add full Discord restart-to-resume routing and unrelated-message integration tests; exercise competing CAS in parallel; exercise an ambiguous monitor receipt followed by an alternate-insert retry across restart; decide whether to model notification claim/delivery separately; consider CAS-atomic pending-step preparation. No Step 2 `[gate]` commit was made. Steps 3–6 remain dependency-blocked; session recall may proceed independently because `SessionStore.search_messages` already exists.

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
