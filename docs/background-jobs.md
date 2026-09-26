# Durable background jobs

Delilah currently uses owner-scoped `task_runs`, `task_steps`, `task_events`,
and existing tool-call/receipt evidence for detached delegated child work. The
startup recovery pass resumes only queued children whose parent, delegation
manifest, tool scope, budget, and prior receipts reconcile. A recurring sweep
drains queued work fairly across owners and delivers unclaimed terminal notices.
Restart recovery runs once before normal turns; the recurring sweep does not
reinterpret a live `running` task as stale.

This durable child-task loop is started alongside the existing monitor and
reminder services and routes model work through the shared capacity scheduler;
it does not migrate legacy reminder or monitor records into `task_runs`.

Safe defaults are one scheduler worker, one active child execution, and eight
queued background children per owner. Operators can tune these settings:

- `AGENT_MAX_WORKERS` (default `1`, maximum `64`)
- `AGENT_MAX_ACTIVE_TASKS_PER_OWNER` (default `1`)
- `AGENT_MAX_QUEUE_PER_OWNER` (default `8`)
- `BACKGROUND_RECOVERY_INTERVAL_SECONDS` (default `30`, clamped to 5–300)
- `DELEGATION_MAX_STEPS` (default `5`), `DELEGATION_MAX_TIMEOUT_SECONDS`
  (default `60`), and `DELEGATION_MAX_TOKENS` (default `4096` per generated
  response)

The provider-level concurrency setting remains an independent hard ceiling;
raising the worker count alone does not increase provider capacity. A safe
interrupted task may resume within its original time/step budget. A `started`,
`unknown`, or otherwise inconsistent side-effect receipt is never replayed
automatically. Invalid or incomplete persisted delegation manifests are
atomically parked with their exact waiting parent in `needs_reconciliation`,
so recovery does not spin on the same unsafe job. Discord completion notices
use a unique durable claim and are at-most-once: a crash or send timeout after claiming may lose the notice, but
will not cause a potentially duplicate send. Notices include status only, not
the model-generated result, because the original conversation may be shared.

## Deployment boundary

This worker configuration is for workers in one Delilah process. Scheduler
semaphores and the active-child registry are process-local. Bot startup now
acquires an OS-held exclusive lock beside the task database before starting
workers; a second bot process using that same local database path fails closed.
The OS releases the lock when the owning process exits, so no stale-lock TTL is
invented. This is host-local protection, not a renewable/fenced distributed
lease: do not share the database across hosts or filesystems that do not honor
the lock semantics, and do not assume a shared provider-capacity limiter. A
cross-host worker lease and provider-capacity boundary need an explicit
deployment decision before that mode is enabled.
