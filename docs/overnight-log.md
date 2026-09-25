# Overnight implementation log

## Progress

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
