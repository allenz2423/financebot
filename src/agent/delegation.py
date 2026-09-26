"""Bounded delegation and child task execution on CapacityAwareScheduler.

Implements Roadmap Item 2 and Step 4:
- Child tasks represented as normal durable `task_runs` with `parent_task_id`.
- Inherited owner scoping; child permissions cannot exceed parent permissions.
- Recursive delegation disabled initially (children cannot call delegate_task).
- Parent waiting in `awaiting_child` phase releases scheduler worker slot.
- Child side effects link to ordinary receipt evidence in ReceiptStore.
- Bounded budgets (max_steps, timeout_seconds) and structured results.
- Sequential execution under default 1-worker scheduler; safe concurrency under multi-worker.
- Inspection, cascading cancellation, and restart recovery.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from src.agent.runtime import (
    CURRENT_ALLOWED_TOOLS,
    CURRENT_CHANNEL_ID,
    CURRENT_DELEGATION_GRANT,
    CURRENT_DELEGATION_MAX_TOKENS,
    CURRENT_MAX_TOOL_STEPS,
    CURRENT_SESSION_KEY,
    CURRENT_TASK_ID,
    CURRENT_THREAD_ID,
    CURRENT_TURN_ID,
)
from src.agent.scheduler import CapacityAwareScheduler, get_global_scheduler
from src.agent.task_controller import TaskController
from src.db.session_store import SessionStore, TaskNotFound
from src.services.tool_receipts import ReceiptStore
from src.services.tool_grants import issue_delegation_capability_grant


def _positive_int_setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _positive_float_setting(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) and value > 0 else default
    except (TypeError, ValueError):
        return default


# Retain the bounded defaults that were already present in the Step 4 WIP
# checkpoint, while allowing self-hosters to raise/lower them explicitly.
MAX_CHILD_STEPS = _positive_int_setting("DELEGATION_MAX_STEPS", 5)
MAX_CHILD_TIMEOUT_SECONDS = _positive_float_setting("DELEGATION_MAX_TIMEOUT_SECONDS", 60.0)
MAX_CHILD_TOKENS = _positive_int_setting("DELEGATION_MAX_TOKENS", 4096)

# Process-local dispatch registry used to keep restart-only receipt recovery
# from rewriting a child that is actively executing in this process.
ACTIVE_DELEGATION_CHILDREN: dict[str, str] = {}
_OWNER_CHILD_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, int], asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _active_children_per_owner() -> int:
    try:
        return max(1, min(int(os.getenv("AGENT_MAX_ACTIVE_TASKS_PER_OWNER", "1")), 64))
    except (TypeError, ValueError):
        return 1


@asynccontextmanager
async def _owner_child_execution_slot(user_id: str):
    """Enforce the configured active-child cap across every dispatch path."""
    loop = asyncio.get_running_loop()
    owner = str(user_id)
    limit = _active_children_per_owner()
    slots = _OWNER_CHILD_SEMAPHORES.setdefault(loop, {})
    semaphore = slots.setdefault((owner, limit), asyncio.Semaphore(limit))
    async with semaphore:
        yield


@dataclass(frozen=True)
class DelegationBudget:
    """Explicit bounds; max_tokens caps each generated model response."""

    max_steps: int
    timeout_seconds: float
    max_tokens: int

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if self.max_steps > MAX_CHILD_STEPS:
            raise ValueError(f"max_steps cannot exceed configured cap {MAX_CHILD_STEPS}")
        if self.max_tokens > MAX_CHILD_TOKENS:
            raise ValueError(f"max_tokens cannot exceed configured cap {MAX_CHILD_TOKENS}")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if timeout > MAX_CHILD_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds cannot exceed configured cap {MAX_CHILD_TIMEOUT_SECONDS}"
            )
        object.__setattr__(self, "timeout_seconds", timeout)


@dataclass
class DelegationResult:
    """Structured result returned by a delegated child task."""

    task_id: str
    parent_task_id: str
    status: str  # succeeded | failed | cancelled | queued | needs_reconciliation
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    steps_executed: int = 0
    receipt_ids: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "steps_executed": self.steps_executed,
            "receipt_ids": self.receipt_ids,
            "error": self.error,
        }


class DelegationController:
    """Coordinates bounded child agent delegation over durable tasks and scheduler."""

    def __init__(
        self,
        store: SessionStore,
        task_controller: TaskController,
        scheduler: CapacityAwareScheduler | None = None,
        receipt_store: ReceiptStore | None = None,
    ) -> None:
        self.store = store
        self.task_controller = task_controller
        self._scheduler = scheduler
        self.receipt_store = receipt_store

    @property
    def scheduler(self) -> CapacityAwareScheduler:
        if self._scheduler is not None:
            return self._scheduler
        return get_global_scheduler()

    def validate_delegation_permissions(
        self,
        user_id: str,
        parent_task_id: str,
        requested_tools: Sequence[str] | set[str] | None,
        parent_allowed_tools: Sequence[str] | set[str] | None = None,
    ) -> list[str]:
        """Validate parent task eligibility and ensure child permissions never exceed parent."""
        parent = self.store.get_task(user_id, parent_task_id, include_steps=False, include_events=False)
        if not parent:
            raise TaskNotFound(f"Parent task {parent_task_id} not found for owner {user_id}")

        # Disallow recursive delegation by default
        if parent.get("parent_task_id") is not None:
            raise ValueError(
                f"Recursive delegation is disabled: task {parent_task_id} is already a child task."
            )

        if parent.get("status") in {"succeeded", "failed", "cancelled", "partial"}:
            raise ValueError(
                f"Cannot delegate from parent task {parent_task_id} with terminal status '{parent.get('status')}'"
            )

        # Build effective child tool list
        if parent_allowed_tools is None:
            raise PermissionError("parent tool permissions are required to delegate")
        parent_tools_set = {str(tool) for tool in parent_allowed_tools}
        if requested_tools is None:
            child_tools = list(parent_tools_set)
        else:
            child_tools = [str(tool) for tool in requested_tools]

        # Child cannot delegate recursively or emit an invisible user prompt.
        child_tools = [
            t for t in child_tools
            if t not in {"delegate_task", "await_user", "task_cancel"}
        ]
        child_tools = list(dict.fromkeys(child_tools))

        # Enforce that child permissions NEVER exceed the parent
        unauthorized = set(child_tools) - parent_tools_set
        if unauthorized:
            raise PermissionError(
                f"Child permissions cannot exceed parent permissions. "
                f"Unauthorized tools requested: {sorted(unauthorized)}"
            )

        return child_tools

    async def delegate(
        self,
        user_id: str,
        parent_task_id: str,
        goal: str,
        *,
        allowed_tools: Sequence[str] | set[str] | None = None,
        parent_allowed_tools: Sequence[str] | set[str] | None = None,
        budget: DelegationBudget | None = None,
        context: dict[str, Any] | None = None,
        runner_fn: Callable[[str, str, str, list[str], dict[str, Any], DelegationBudget], Awaitable[DelegationResult]] | None = None,
    ) -> DelegationResult:
        """Delegate a bounded subtask as a durable child task.

        Guarantees:
        - Parent task verified and transitioned to 'awaiting_child' phase.
        - Parent waiting releases worker slots (scheduled via background work unit).
        - Child permissions cannot exceed parent.
        - Recursive delegation disallowed.
        - Side effects link to ReceiptStore.
        - Structured DelegationResult returned upon completion.
        """
        if budget is None:
            raise ValueError("an explicit child delegation budget is required")
        if runner_fn is None:
            raise RuntimeError("child runner is not configured; refusing synthetic completion")
        sanitized_tools = self.validate_delegation_permissions(
            user_id, parent_task_id, allowed_tools, parent_allowed_tools
        )

        parent = self.store.get_task(user_id, parent_task_id, include_steps=False, include_events=False)
        parent_phase = str(parent.get("phase") or "received")

        # Atomically persist the child and parent wait state. This keeps a
        # crash from leaving an orphan child or a parent with a stale phase.
        child_task_id = f"task_child_{uuid.uuid4().hex[:12]}"
        child_session_id = f"child:{child_task_id}"
        delegation_grant = issue_delegation_capability_grant(
            user_id=user_id,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            allowed_tools=sanitized_tools,
        )
        self.store.create_child_task_and_await_parent(
            user_id,
            parent_task_id,
            child_task_id=child_task_id,
            child_session_id=child_session_id,
            objective=goal,
            expected_parent_status=parent["status"],
            expected_parent_version=int(parent["version"]),
            expected_parent_phase=parent_phase,
            next_action=f"await_child:{child_task_id}",
            allowed_tools=sanitized_tools,
            delegation_grant_id=delegation_grant.grant_id,
            budget={
                "max_steps": budget.max_steps,
                "timeout_seconds": budget.timeout_seconds,
                "max_tokens": budget.max_tokens,
            },
        )

        # Define work unit to execute child task on the scheduler
        async def _execute_child_unit() -> DelegationResult:
            # Give the child an isolated conversation and strict inherited
            # execution context. The scheduler captures these ContextVars.
            task_token = CURRENT_TASK_ID.set(child_task_id)
            turn_id = f"turn_child_{uuid.uuid4().hex}"
            turn_token = CURRENT_TURN_ID.set(turn_id)
            session_token = CURRENT_SESSION_KEY.set(child_session_id)
            channel_token = CURRENT_CHANNEL_ID.set(parent.get("channel_id"))
            thread_token = CURRENT_THREAD_ID.set(parent.get("thread_id"))
            tools_token = CURRENT_ALLOWED_TOOLS.set(frozenset(sanitized_tools))
            grant_token = CURRENT_DELEGATION_GRANT.set(delegation_grant)
            steps_token = CURRENT_MAX_TOOL_STEPS.set(budget.max_steps)
            tokens_token = CURRENT_DELEGATION_MAX_TOKENS.set(budget.max_tokens)
            try:
                self.store.begin_turn(
                    user_id, child_session_id, turn_id=turn_id,
                    metadata={"parent_task_id": parent_task_id, "child_task_id": child_task_id},
                    channel_id=parent.get("channel_id"),
                    thread_id=parent.get("thread_id"),
                )
                self.store.add_message(
                    user_id, child_session_id, role="user", content=goal,
                    message_id=f"{turn_id}:objective", turn_id=turn_id,
                    channel_id=parent.get("channel_id"),
                    thread_id=parent.get("thread_id"),
                    metadata={"parent_task_id": parent_task_id, "delegated": True},
                )
                # Update child status to running
                child_task = self.store.get_task(user_id, child_task_id, include_steps=False, include_events=False)
                self.store.transition_task_run(
                    user_id,
                    child_task_id,
                    expected_status=child_task["status"],
                    expected_version=int(child_task["version"]),
                    new_status="running",
                    event_type="task.dispatched",
                    event_payload={"lane": "background"},
                )
                self.task_controller.transition_phase(
                    user_id, child_task_id, "planning", next_action="prepare_child_execution"
                )
                self.task_controller.transition_phase(
                    user_id, child_task_id, "executing", next_action="execute_child_steps"
                )

                res = await runner_fn(
                    child_task_id, user_id, goal, sanitized_tools, context or {}, budget
                )
                if not isinstance(res, DelegationResult):
                    raise TypeError("child runner must return DelegationResult")
                if res.task_id != child_task_id or res.parent_task_id != parent_task_id:
                    raise ValueError("child runner result task identity does not match its durable task")
                if res.steps_executed < 0 or res.steps_executed > budget.max_steps:
                    raise ValueError("child runner exceeded its max_steps budget")
                supported_statuses = {
                    "succeeded", "failed", "cancelled", "queued",
                    "waiting_user", "waiting_approval", "needs_reconciliation",
                }
                if res.status not in supported_statuses:
                    raise ValueError("child runner returned an unsupported task status")
                current_child = self.store.get_task(
                    user_id, child_task_id, include_steps=False, include_events=False
                )
                if current_child["status"] in {"cancelled", "needs_reconciliation", "partial"}:
                    current_state = str(current_child["status"])
                    safe_status = (
                        "needs_reconciliation"
                        if current_state in {"needs_reconciliation", "partial"}
                        else "cancelled"
                    )
                    return DelegationResult(
                        task_id=child_task_id,
                        parent_task_id=parent_task_id,
                        status=safe_status,
                        summary=(
                            "Child action requires manual reconciliation; it was not retried."
                            if safe_status == "needs_reconciliation"
                            else "Child task was cancelled before its result could be accepted."
                        ),
                    )

                self.store.add_message(
                    user_id, child_session_id, role="assistant", content=res.summary,
                    message_id=f"{turn_id}:result", turn_id=turn_id,
                    channel_id=parent.get("channel_id"),
                    thread_id=parent.get("thread_id"),
                    metadata={"child_task_id": child_task_id, "status": res.status},
                )
                self.store.finish_turn(
                    user_id, child_session_id, turn_id,
                    status=(
                        "completed" if res.status in {"succeeded", "queued", "waiting_user", "waiting_approval", "needs_reconciliation"}
                        else res.status
                    ),
                    channel_id=parent.get("channel_id"),
                    thread_id=parent.get("thread_id"),
                )

                # Mark child task succeeded in SessionStore
                if res.status == "succeeded":
                    self.task_controller.transition_phase(
                        user_id, child_task_id, "verifying",
                        next_action="validate_child_result",
                    )
                    self.task_controller.transition_phase(
                        user_id, child_task_id, "delivering",
                        next_action="return_result_to_parent",
                    )
                child_curr = self.store.get_task(
                    user_id, child_task_id, include_steps=False, include_events=False
                )
                self.store.transition_task_run(
                    user_id,
                    child_task_id,
                    expected_status=child_curr["status"],
                    expected_version=int(child_curr["version"]),
                    new_status=res.status,
                    event_type="task.completed",
                    event_payload={
                        "status": res.status,
                        "step_count": res.steps_executed,
                    },
                )
                if res.status == "succeeded":
                    self.task_controller.transition_phase(
                        user_id, child_task_id, "completed", next_action=None,
                    )
                else:
                    target_phase = {
                        "failed": "failed", "cancelled": "failed",
                        "queued": "planning", "waiting_user": "awaiting_user",
                        "waiting_approval": "awaiting_user",
                        "needs_reconciliation": "partial",
                    }[res.status]
                    self.task_controller.transition_phase(
                        user_id, child_task_id, target_phase,
                        next_action=("manual_child_reconciliation" if res.status == "needs_reconciliation" else None),
                    )
                return res

            except asyncio.CancelledError:
                # A cancellation/timeout may arrive while a provider or tool is
                # in flight. Reconcile durable receipts before deciding the
                # child is safely cancelled; never convert unknown into failed.
                try:
                    self.task_controller.recover_incomplete(
                        user_id, self.receipt_store or ReceiptStore(self.store.connection)
                    )
                except Exception:
                    pass
                raise
            except Exception as exc:
                try:
                    self.task_controller.recover_incomplete(
                        user_id, self.receipt_store or ReceiptStore(self.store.connection)
                    )
                except Exception:
                    pass
                child_curr = self.store.get_task(user_id, child_task_id, include_steps=False, include_events=False)
                state = str(child_curr["status"])
                if state in {"running", "queued"}:
                    # If recovery proves this was before dispatch, leave a
                    # durable queued child for an explicit recovery worker.
                    result_status = "queued"
                elif state == "needs_reconciliation":
                    result_status = "needs_reconciliation"
                elif state in {"succeeded", "failed", "cancelled"}:
                    result_status = state
                else:
                    result_status = "failed"
                    if state not in {"partial", "needs_reconciliation"}:
                        self.store.transition_task_run(
                            user_id,
                            child_task_id,
                            expected_status=state,
                            expected_version=int(child_curr["version"]),
                            new_status="failed",
                            event_type="task.failed",
                            event_payload={"reason_code": "child_runner_failed"},
                        )
                        self.task_controller.transition_phase(
                            user_id, child_task_id, "failed", next_action=None
                        )
                return DelegationResult(
                    task_id=child_task_id,
                    parent_task_id=parent_task_id,
                    status=result_status,
                    summary=f"Child task execution failed: {exc}",
                    error=str(exc),
                )
            finally:
                CURRENT_DELEGATION_MAX_TOKENS.reset(tokens_token)
                CURRENT_MAX_TOOL_STEPS.reset(steps_token)
                CURRENT_ALLOWED_TOOLS.reset(tools_token)
                CURRENT_DELEGATION_GRANT.reset(grant_token)
                CURRENT_THREAD_ID.reset(thread_token)
                CURRENT_CHANNEL_ID.reset(channel_token)
                CURRENT_SESSION_KEY.reset(session_token)
                CURRENT_TURN_ID.reset(turn_token)
                CURRENT_TASK_ID.reset(task_token)

        ACTIVE_DELEGATION_CHILDREN[child_task_id] = str(user_id)
        try:
            # Do not wrap a whole child turn in a scheduler worker: its model
            # requests must enter the scheduler individually, otherwise a
            # one-worker scheduler deadlocks when the nested inference queues.
            # The separate child admission slot bounds unscheduled tool work
            # while leaving worker slots free for those nested inference units.
            async def _run_with_child_slot() -> DelegationResult:
                async with _owner_child_execution_slot(user_id):
                    async with self.scheduler.child_execution_slot():
                        return await _execute_child_unit()

            result: DelegationResult = await asyncio.wait_for(
                _run_with_child_slot(), timeout=budget.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            try:
                self.task_controller.recover_incomplete(
                    user_id, self.receipt_store or ReceiptStore(self.store.connection)
                )
            except Exception:
                pass
            child_curr = self.store.get_task(
                user_id, child_task_id, include_steps=False, include_events=False
            )
            state = str(child_curr["status"])
            if state in {"queued", "running", "waiting_user", "waiting_approval"}:
                self.task_controller.cancel(user_id, child_task_id)
                child_curr = self.store.get_task(
                    user_id, child_task_id, include_steps=False, include_events=False
                )
                state = str(child_curr["status"])
            result = DelegationResult(
                task_id=child_task_id,
                parent_task_id=parent_task_id,
                status=(
                    "needs_reconciliation" if state in {"needs_reconciliation", "partial"}
                    else state if state in {"queued", "waiting_user", "waiting_approval"}
                    else "cancelled" if state == "cancelled"
                    else "queued"
                ),
                summary="Child task timed out; any ambiguous side effect requires reconciliation and was not retried.",
                error=type(exc).__name__,
            )
        except asyncio.CancelledError:
            self.scheduler.request_cancellation(child_task_id)
            self.cancel_delegation(user_id, child_task_id)
            self.task_controller.cancel(user_id, parent_task_id)
            raise
        finally:
            ACTIVE_DELEGATION_CHILDREN.pop(child_task_id, None)
            # Resume only after a known terminal child outcome. An interrupted
            # or ambiguous child remains linked to the durable awaiting state.
            parent_curr = self.store.get_task(user_id, parent_task_id, include_steps=False, include_events=False)
            if parent_curr["status"] not in {"failed", "cancelled", "succeeded"}:
                child_status = result.status if 'result' in locals() and isinstance(result, DelegationResult) else "unknown"
                if child_status in {"succeeded", "failed", "cancelled"}:
                    self.store.transition_task_run(
                        user_id,
                        parent_task_id,
                        expected_status=parent_curr["status"],
                        expected_version=int(parent_curr["version"]),
                        new_status="queued",
                        wait_reason=f"resume_after_child:{child_task_id}",
                        event_type="task.child_completed",
                        event_payload={"child_task_id": child_task_id, "status": child_status},
                    )
                    self.task_controller.transition_phase(
                        user_id,
                        parent_task_id,
                        "planning",
                        next_action="resume_after_child",
                        event_payload={"child_task_id": child_task_id, "status": child_status},
                    )
                elif child_status == "needs_reconciliation":
                    self.store.transition_task_run(
                        user_id,
                        parent_task_id,
                        expected_status=parent_curr["status"],
                        expected_version=int(parent_curr["version"]),
                        new_status="needs_reconciliation",
                        wait_reason=f"child_needs_reconciliation:{child_task_id}",
                        event_type="task.child_reconciliation_required",
                        event_payload={"child_task_id": child_task_id},
                    )
                    self.task_controller.transition_phase(
                        user_id, parent_task_id, "partial",
                        next_action="reconcile_child_task",
                        event_payload={"child_task_id": child_task_id},
                    )

        return result

    async def resume_queued_child(
        self,
        user_id: str,
        child_task_id: str,
        *,
        runner_fn: Callable[
            [str, str, str, list[str], dict[str, Any], DelegationBudget],
            Awaitable[DelegationResult],
        ],
    ) -> DelegationResult:
        """Resume only a durable, owner-scoped child whose prior actions reconcile safely."""
        recovery_receipts = self.receipt_store or ReceiptStore(self.store.connection)
        spec = self.store.get_child_delegation_spec(user_id, child_task_id)
        child = spec["child"]
        parent = spec["parent"]
        parent_id = str(spec["parent_task_id"])
        # Validate only this parent/child evidence chain. An owner-wide restart
        # sweep here could reset a different task that is actively executing.
        self.task_controller.recover_incomplete(
            user_id,
            recovery_receipts,
            only_task_ids={parent_id, child_task_id},
        )
        spec = self.store.get_child_delegation_spec(user_id, child_task_id)
        child = spec["child"]
        parent = spec["parent"]
        if child.get("status") != "queued":
            raise ValueError("only a safely queued child task may be resumed")
        if parent.get("phase") != "awaiting_child" or parent.get("status") not in {"queued", "running"}:
            raise ValueError("child parent is not waiting for this delegated task")
        if parent.get("wait_reason") != f"awaiting_child:{child_task_id}":
            raise ValueError("parent wait state does not identify this child")

        steps = self.store.get_task(user_id, child_task_id)["steps"]
        if any(step.get("status") in {"running", "needs_reconciliation", "unknown"} for step in steps):
            raise PermissionError("child has unresolved action evidence and cannot be resumed")
        for step in steps:
            receipt_id = step.get("receipt_id")
            if not receipt_id:
                continue
            receipt = recovery_receipts.get(str(receipt_id))
            if receipt.status in {"started", "unknown"}:
                raise PermissionError("child has an ambiguous receipt and cannot be resumed")

        raw_budget = spec["budget"]
        budget = DelegationBudget(
            max_steps=raw_budget.get("max_steps"),
            timeout_seconds=raw_budget.get("timeout_seconds"),
            max_tokens=raw_budget.get("max_tokens"),
        )
        try:
            created_at = datetime.fromisoformat(str(child["created_at"]).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            remaining_timeout = budget.timeout_seconds - (
                datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
            ).total_seconds()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("child creation time is missing or malformed; refusing resume") from exc
        allowed_tools = list(spec["allowed_tools"])
        grant = issue_delegation_capability_grant(
            user_id=user_id,
            parent_task_id=parent_id,
            child_task_id=child_task_id,
            allowed_tools=allowed_tools,
            grant_id=spec["delegation_grant_id"],
        )
        used_steps = self.store.count_task_tool_calls(
            user_id,
            child_task_id,
            exclude_tool_names=(
                "end_turn", "enable_reasoning", "task_plan", "task_list",
                "task_cancel", "search_session_history", "await_user",
            ),
        )
        remaining_steps = budget.max_steps - used_steps
        if remaining_steps <= 0:
            current = self.store.get_task(user_id, child_task_id, include_steps=False, include_events=False)
            self.store.transition_task_run(
                user_id, child_task_id, expected_status="queued",
                expected_version=int(current["version"]), new_status="failed",
                wait_reason="delegation_step_budget_exhausted",
                event_type="task.failed",
                event_payload={"reason_code": "delegation_step_budget_exhausted"},
            )
            self.task_controller.transition_phase(
                user_id, child_task_id, "failed", next_action=None,
            )
            self._resume_parent_after_child(user_id, parent_id, child_task_id, "failed")
            return DelegationResult(
                child_task_id, parent_id, "failed",
                "Child task exhausted its persisted tool-step budget; no action was retried.",
                steps_executed=used_steps,
            )
        if remaining_timeout <= 0:
            current = self.store.get_task(user_id, child_task_id, include_steps=False, include_events=False)
            self.store.transition_task_run(
                user_id, child_task_id, expected_status="queued",
                expected_version=int(current["version"]), new_status="failed",
                wait_reason="delegation_timeout_budget_exhausted",
                event_type="task.failed",
                event_payload={"reason_code": "delegation_timeout_budget_exhausted"},
            )
            self.task_controller.transition_phase(
                user_id, child_task_id, "failed", next_action=None,
            )
            self._resume_parent_after_child(user_id, parent_id, child_task_id, "failed")
            return DelegationResult(
                child_task_id, parent_id, "failed",
                "Child task exhausted its persisted time budget while offline; no action was retried.",
                steps_executed=used_steps,
                error="TimeoutError",
            )

        if runner_fn is None:
            raise RuntimeError("child runner is not configured")
        turn_id = f"turn_child_resume_{uuid.uuid4().hex}"
        claimed = self.store.claim_child_task_for_resume(
            user_id, child_task_id,
            expected_child_version=int(child["version"]), turn_id=turn_id,
        )
        phase = str(claimed.get("phase") or "received")
        if phase == "received":
            self.task_controller.transition_phase(
                user_id, child_task_id, "planning", next_action="resume_child_plan"
            )
        self.task_controller.transition_phase(
            user_id, child_task_id, "executing", next_action="resume_child_execution"
        )

        child_session_id = str(child["session_key"])
        channel_id = parent.get("channel_id")
        thread_id = parent.get("thread_id")
        task_token = CURRENT_TASK_ID.set(child_task_id)
        turn_token = CURRENT_TURN_ID.set(turn_id)
        session_token = CURRENT_SESSION_KEY.set(child_session_id)
        channel_token = CURRENT_CHANNEL_ID.set(channel_id)
        thread_token = CURRENT_THREAD_ID.set(thread_id)
        tools_token = CURRENT_ALLOWED_TOOLS.set(frozenset(allowed_tools))
        grant_token = CURRENT_DELEGATION_GRANT.set(grant)
        steps_token = CURRENT_MAX_TOOL_STEPS.set(remaining_steps)
        tokens_token = CURRENT_DELEGATION_MAX_TOKENS.set(budget.max_tokens)
        ACTIVE_DELEGATION_CHILDREN[child_task_id] = str(user_id)
        try:
            self.store.begin_turn(
                user_id, child_session_id, turn_id=turn_id,
                metadata={"parent_task_id": parent_id, "child_task_id": child_task_id,
                          "recovered": True},
                channel_id=channel_id, thread_id=thread_id,
            )
            self.store.add_message(
                user_id, child_session_id, role="user",
                content=f"Continue the existing delegated objective: {child['objective']}",
                message_id=f"{turn_id}:resume", turn_id=turn_id,
                channel_id=channel_id, thread_id=thread_id,
                metadata={"parent_task_id": parent_id, "child_task_id": child_task_id,
                          "delegated": True, "recovered": True},
            )
            async with _owner_child_execution_slot(user_id):
                async with self.scheduler.child_execution_slot():
                    result = await asyncio.wait_for(
                        runner_fn(
                            child_task_id, user_id, str(child["objective"]), allowed_tools,
                            {"parent_task_id": parent_id, "recovered": True},
                            DelegationBudget(
                                max_steps=remaining_steps,
                                timeout_seconds=remaining_timeout,
                                max_tokens=budget.max_tokens,
                            ),
                        ),
                        timeout=remaining_timeout,
                    )
            if result.task_id != child_task_id or result.parent_task_id != parent_id:
                raise ValueError("recovered child runner returned mismatched task identity")
            if result.steps_executed < 0 or result.steps_executed > budget.max_steps:
                raise ValueError("recovered child runner exceeded the persisted step budget")
            if result.status not in {
                "succeeded", "failed", "cancelled", "queued", "waiting_user",
                "waiting_approval", "needs_reconciliation",
            }:
                raise ValueError("recovered child runner returned unsupported status")
            child_after = self.store.get_task(
                user_id, child_task_id, include_steps=False, include_events=False
            )
            if child_after["status"] in {"needs_reconciliation", "partial", "cancelled"}:
                final_status = (
                    "needs_reconciliation"
                    if child_after["status"] in {"needs_reconciliation", "partial"}
                    else "cancelled"
                )
            else:
                final_status = result.status
            if final_status == "succeeded":
                self.task_controller.transition_phase(
                    user_id, child_task_id, "verifying", next_action="validate_child_result"
                )
                self.task_controller.transition_phase(
                    user_id, child_task_id, "delivering", next_action="return_result_to_parent"
                )
            current = self.store.get_task(user_id, child_task_id, include_steps=False, include_events=False)
            self.store.transition_task_run(
                user_id, child_task_id,
                expected_status=current["status"], expected_version=int(current["version"]),
                new_status=final_status, event_type="task.recovered_child_completed",
                event_payload={"status": final_status, "parent_task_id": parent_id},
            )
            target_phase = {
                "succeeded": "completed", "failed": "failed", "cancelled": "failed",
                "queued": "planning", "waiting_user": "awaiting_user",
                "waiting_approval": "awaiting_user", "needs_reconciliation": "partial",
            }[final_status]
            self.task_controller.transition_phase(
                user_id, child_task_id, target_phase,
                next_action="manual_child_reconciliation" if final_status == "needs_reconciliation" else None,
            )
            if final_status in {"succeeded", "failed", "cancelled"}:
                self._resume_parent_after_child(user_id, parent_id, child_task_id, final_status)
            elif final_status == "needs_reconciliation":
                self._resume_parent_after_child(user_id, parent_id, child_task_id, final_status)
            return result
        except asyncio.TimeoutError:
            self.task_controller.recover_incomplete(user_id, recovery_receipts)
            latest = self.store.get_task(user_id, child_task_id, include_steps=False, include_events=False)
            if latest["status"] == "running":
                self.task_controller.cancel(user_id, child_task_id)
            latest = self.store.get_task(user_id, child_task_id, include_steps=False, include_events=False)
            return DelegationResult(
                child_task_id, parent_id, latest["status"],
                "Recovered child exceeded its time budget; ambiguous actions require reconciliation.",
                error="TimeoutError",
            )
        except asyncio.CancelledError:
            self.task_controller.recover_incomplete(user_id, recovery_receipts)
            raise
        finally:
            ACTIVE_DELEGATION_CHILDREN.pop(child_task_id, None)
            CURRENT_DELEGATION_MAX_TOKENS.reset(tokens_token)
            CURRENT_MAX_TOOL_STEPS.reset(steps_token)
            CURRENT_DELEGATION_GRANT.reset(grant_token)
            CURRENT_ALLOWED_TOOLS.reset(tools_token)
            CURRENT_THREAD_ID.reset(thread_token)
            CURRENT_CHANNEL_ID.reset(channel_token)
            CURRENT_SESSION_KEY.reset(session_token)
            CURRENT_TURN_ID.reset(turn_token)
            CURRENT_TASK_ID.reset(task_token)

    def _resume_parent_after_child(
        self, user_id: str, parent_task_id: str, child_task_id: str, child_status: str
    ) -> None:
        # The parent tool receipt, not the child row alone, proves the parent
        # delegation call committed. Recovery confirms that internal call and
        # fails closed if its linked evidence is missing or ambiguous.
        self.task_controller.recover_incomplete(
            user_id, self.receipt_store or ReceiptStore(self.store.connection)
        )

    def cancel_delegation(self, user_id: str, child_task_id: str) -> dict[str, Any]:
        """Cancel a delegated child task cooperatively."""
        self.scheduler.request_cancellation(child_task_id)
        return self.task_controller.cancel(user_id, child_task_id)


__all__ = [
    "DelegationBudget",
    "DelegationController",
    "DelegationResult",
]
