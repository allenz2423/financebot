# Deterministic task verification

Delilah's final-response boundary now runs deterministic task checks after the
claim-evidence guard and before memory extraction or delivery. The default
contract verifies every persisted task-step receipt against the owner, call ID,
tool name, turn, argument hash, successful call state, and confirmed/complete
receipt state, plus a non-empty final response. Delegated child tasks also
verify observed tools against their inherited allowlist. A receipt or model
statement is not proof of fresh data, arithmetic correctness, or a requested
artifact's contents.

`src/agent/task_verifier.py` exposes `verify_task_contract(contract, evidence)`
for trusted workflow controllers. Its bounded typed checks cover required
receipts, timestamp freshness, exact decimal-string arithmetic, allowed-tool
policy, required deliverable references, and artifact identity/delivery
(owner/call/receipt, canonical relative path, exact-byte SHA-256, byte size,
and Discord message ID). A requested JSON export additionally requires a
`.json` filename and successful JSON parsing of the exact bytes read for
delivery (bounded to 2 MB). This validates format, not whether the file
contains every semantically requested record; duplicate object keys and
nonstandard `NaN`/`Infinity` constants are rejected. Requested CSV exports
also require bounded UTF-8 CSV parsing with consistent row widths and reject
JSON documents/scalars misnamed as CSV. Financial numeric claims are
checked against typed USD evidence; unsupported currency symbols/codes,
over-precision values, and labeled bare-number claims fail closed. Missing,
malformed, stale, future, ambiguous, or mismatched evidence fails closed. The
verifier does not query the database, call tools, invoke a model, or perform
repairs.

TaskController persists bounded check IDs, reason codes, and evidence
references in the existing append-only `task_events` trail. It does not copy
receipt bodies or financial values into another ledger. A failed contract
atomically marks the task partial and creates one ready `repair_failed_checks`
step containing only the failed check IDs. That step is a durable repair plan,
not permission to repeat an operation; an ambiguous or non-idempotent side
effect remains manual-reconciliation-only. An optional read-only reviewer pass
is controlled by `DELILAH_TASK_MODEL_REVIEWER` (default `0`). When enabled, it
runs only after deterministic checks pass and for tasks with at least two
linked tool steps. It receives bounded objective/response/check summaries,
has tools disabled, and must return strict JSON. An unavailable or malformed
review fails closed into the same repair flow. It uses the configured advisor
provider and may incur inference cost; deterministic checks remain enabled
regardless of this setting.

Every non-control tool call in a durable task is linked to a task step and
receipt before dispatch, including newly added tools. The legacy advisor loop
refuses to enter model/tool execution when durable task storage did not provide
a task context. Once a receipt becomes `unknown`, the current model batch and
the turn stop immediately; recovery requires reconciliation and never replays
that side effect.

The final-turn hook does not infer freshness or arithmetic from prose or
untyped summaries. It consumes typed evidence from `get_current_financial_position`:
the source sync timestamp is checked against that query's existing 12-hour
staleness policy, and a nearby owner-scoped `financial_snapshots` row proves
`checking_balance - total_credit_debt = net_cash` within one cent. Invalid
timestamps fail closed. Other financial reads and auto-applying
reconciliation tools currently lack equivalent typed source evidence and
therefore park summaries/reconciliations as partial with a repair step rather
than claim success. The objective/tool classifier is a conservative bridge
until task plans persist an explicit typed completion contract.

A requested generated file/report is not complete merely because the model
says it exists or because a receipt says “sent.” The final gate validates the
owner-scoped task receipt and server-reported typed evidence for the exact
owner-relative path, hash and byte size of the bytes read for Discord delivery,
plus the returned Discord message ID. If the objective names a filename, that
filename must match. Explicit export formats must match the delivered
filename; JSON files are parsed from the exact delivered bytes. This proves
artifact identity, delivery, and syntax validation—not semantic correctness or
completeness of arbitrary file contents. Other format-specific requirements
need typed validators.
Missing identity/validation evidence fails to a repair step, without retrying
the send.
