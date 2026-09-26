"""Small orchestration operations over SessionStore and ReceiptStore.

The receipt remains the source of truth for execution outcome. This module
stores only task progression and links; it never invokes tools or retries them.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Mapping
from datetime import datetime, timedelta, timezone

from src.services.authorization import (
    ApprovalRequest,
    AuthorizationRequest,
    HumanApproval,
    approve_request,
)
from src.db.session_store import ConcurrentTaskUpdate, SessionStore, TaskNotFound
from src.agent.task_verifier import verify_task_contract
from src.services.tool_receipts import arguments_hash


class TaskController:
    _PHASE_TRANSITIONS = {
        "received": {"planning", "partial", "failed"},
        "planning": {"executing", "awaiting_child", "awaiting_user", "verifying", "partial", "failed"},
        "executing": {"planning", "awaiting_child", "awaiting_user", "verifying", "partial", "failed"},
        "awaiting_child": {"planning", "partial", "failed"},
        "awaiting_user": {"planning", "verifying", "partial", "failed"},
        "verifying": {"planning", "delivering", "partial", "failed"},
        "delivering": {"awaiting_user", "planning", "completed", "partial", "failed"},
        "completed": set(), "partial": set(), "failed": set(),
    }

    def __init__(self, store: SessionStore):
        self.store = store

    def transition_phase(
        self, user_id: str, task_id: str, phase: str, *, next_action: str | None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist an allowed turn phase using task-row version CAS."""
        target = str(phase).strip().casefold()
        for attempt in range(3):
            task = self.store.get_task(
                user_id, task_id, include_steps=False, include_events=False
            )
            current = str(task.get("phase") or "received")
            if target == current and task.get("phase_next_action") == next_action and not event_payload:
                return task
            if target != current and target not in self._PHASE_TRANSITIONS.get(current, set()):
                raise ValueError(f"invalid task phase transition: {current} -> {target}")
            try:
                return self.store.transition_task_phase(
                    user_id, task_id, expected_phase=current,
                    expected_version=int(task["version"]), new_phase=target,
                    next_action=next_action, event_payload=event_payload,
                )
            except ConcurrentTaskUpdate:
                latest = self.store.get_task(
                    user_id, task_id, include_steps=False, include_events=False
                )
                if str(latest.get("phase") or "received") != current or attempt == 2:
                    raise
        raise ConcurrentTaskUpdate("task phase could not be persisted after concurrent updates")

    def create_turn(
        self, user_id: str, session_id: str, turn_id: str, objective: str,
        *, channel_id: str | None = None, thread_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = f"task_{turn_id}"
        try:
            return self.store.create_task_run(
                user_id, session_id, objective[:2000], task_id=task_id,
                channel_id=channel_id, thread_id=thread_id,
            )
        except Exception:
            # Idempotent runtime-hook re-entry may encounter a task already
            # created for this stable turn identifier. Do not mask other errors.
            existing = self.store.get_task(user_id, task_id)
            if existing.get("objective") == objective[:2000]:
                return existing
            raise

    def start_step(
        self, user_id: str, task_id: str, *, tool_call_id: int,
        receipt_id: str, tool_name: str,
    ) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        order = len(task["steps"])
        return self.store.start_task_step(
            user_id, task_id, order,
            expected_task_status=task["status"],
            expected_task_version=int(task["version"]),
            tool_call_id=tool_call_id, receipt_id=receipt_id,
            next_action=f"dispatch:{tool_name}",
        )

    def create_plan(
        self,
        user_id: str,
        task_id: str,
        steps: list[Mapping[str, Any]],
        *,
        workflow_ref: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.create_task_plan(
            user_id, task_id, steps, workflow_ref=workflow_ref
        )

    def create_workflow_plan(
        self,
        user_id: str,
        task_id: str,
        *,
        registry,
        workflow_id: str,
        version: str,
        expected_digest: str,
    ) -> list[dict[str, Any]]:
        """Resolve an exact registered definition and atomically pin its plan."""
        definition = registry.get(workflow_id, version)
        digest = registry.digest(workflow_id, version)
        if digest != expected_digest:
            raise ValueError("selected workflow digest does not match the registered definition")
        return self.store.create_task_plan(
            user_id,
            task_id,
            registry.plan_steps(workflow_id, version),
            workflow_ref={
                "workflow_id": workflow_id,
                "version": version,
                "digest": digest,
            },
        )

    def validate_workflow_dispatch(
        self,
        user_id: str,
        task_id: str,
        *,
        registry,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        """Fail closed on missing/changed workflow versions or altered steps/args."""
        pin = self.store.get_task_workflow_pin(user_id, task_id)
        if pin is None:
            return
        try:
            definition = registry.get(pin["workflow_id"], pin["version"])
            actual_digest = registry.digest(pin["workflow_id"], pin["version"])
        except (KeyError, ValueError, TypeError) as exc:
            raise PermissionError(
                "WORKFLOW_PIN_UNAVAILABLE: the exact workflow version is unavailable; "
                "no action was dispatched."
            ) from exc
        if actual_digest != pin["digest"]:
            raise PermissionError(
                "WORKFLOW_PIN_MISMATCH: the pinned workflow definition changed; "
                "no action was dispatched."
            )
        task = self.store.get_task(user_id, task_id)
        plan_steps = [
            step for step in task["steps"]
            if step.get("description") is not None
        ]
        if any(step.get("status") == "running" for step in plan_steps):
            raise PermissionError(
                "WORKFLOW_STEP_IN_PROGRESS: the current workflow step must reach a "
                "known terminal state before another step can be dispatched."
            )
        if len(plan_steps) != len(definition.steps):
            raise PermissionError(
                "WORKFLOW_PLAN_MISMATCH: persisted steps do not match the pinned version."
            )
        for persisted, defined in zip(plan_steps, definition.steps):
            if (
                persisted.get("next_action") != f"dispatch:{defined.tool_name}"
                or persisted.get("description") != defined.description
                or persisted.get("completion_criteria") != defined.completion_criteria
            ):
                raise PermissionError(
                    "WORKFLOW_PLAN_MISMATCH: persisted steps do not match the pinned version."
                )
        next_index = next(
            (
                index for index, step in enumerate(plan_steps)
                if step.get("status") in {"ready", "pending"}
            ),
            None,
        )
        if next_index is None:
            raise PermissionError("WORKFLOW_PLAN_COMPLETE: no further workflow action is dispatchable.")
        expected_step = definition.steps[next_index]
        if expected_step.tool_name != tool_name:
            raise PermissionError(
                f"WORKFLOW_STEP_ORDER: pinned workflow requires {expected_step.tool_name!r} next."
            )
        registry.validate_arguments(
            pin["workflow_id"], pin["version"], next_index, arguments
        )

    def has_plan(self, user_id: str, task_id: str) -> bool:
        return self.store.task_has_plan(user_id, task_id)

    def verify_and_persist(
        self,
        user_id: str,
        task_id: str,
        contract: dict[str, Any],
        evidence: dict[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        """Evaluate supplied deterministic evidence and persist it with task CAS.

        Task lookup is owner-scoped. The store then atomically requires this
        same task to remain in ``verifying`` at ``expected_version`` while it
        appends the result; a failed result parks the task for manual repair.
        This method never executes tools or retries failed side effects.
        """
        if not isinstance(contract, dict) or not isinstance(evidence, dict):
            raise TypeError("contract and evidence must be dictionaries")
        task = self.store.get_task(
            user_id, task_id, include_steps=False, include_events=False
        )
        if str(task.get("phase") or "received") != "verifying":
            raise ConcurrentTaskUpdate("task is not in the verifying phase")
        outcome = verify_task_contract(contract, evidence)
        persisted = self.store.persist_task_verification(
            user_id,
            task_id,
            expected_version=expected_version,
            passed=bool(outcome.get("passed")),
            checks=outcome.get("checks", []),
        )
        return {**persisted, "outcome": outcome}

    def _load_parent_delegation_receipt(
        self, user_id: str, step: Mapping[str, Any], receipt_store: Any,
        *, child_task_id: str | None = None,
    ):
        call_id = step.get("tool_call_id")
        receipt_id = step.get("receipt_id")
        if call_id is None or not receipt_id:
            raise ValueError("parent delegation step is missing call or receipt linkage")
        call = self.store.get_tool_call(user_id, int(call_id))
        receipt = receipt_store.get(str(receipt_id))
        call_arguments = call.get("arguments")
        if not isinstance(call_arguments, Mapping):
            raise ValueError("parent delegation call arguments are malformed")
        if (
            call.get("tool_name") != "delegate_task"
            or receipt.call_id != call.get("call_key")
            or receipt.user_id != str(user_id)
            or receipt.tool_name != "delegate_task"
            or receipt.turn_id != call.get("turn_key")
            or receipt.arguments_hash != arguments_hash(call_arguments)
        ):
            raise ValueError("parent delegation call and receipt evidence do not match")
        if child_task_id is not None:
            spec = self.store.get_child_delegation_spec(user_id, child_task_id)
            requested_tools = call_arguments.get("allowed_tools")
            requested_budget = call_arguments.get("budget")
            child_goal = str(call_arguments.get("goal") or "").strip()
            if not isinstance(requested_tools, list) or not isinstance(requested_budget, Mapping):
                raise ValueError("parent delegation call is missing its child scope or budget")
            normalized_tools = list(dict.fromkeys(
                str(name) for name in requested_tools
                if str(name) not in {"delegate_task", "await_user", "task_cancel"}
            ))
            if (
                child_goal != str(spec["child"].get("objective") or "")
                or normalized_tools != spec["allowed_tools"]
                or any(
                    requested_budget.get(key) != spec["budget"].get(key)
                    for key in ("max_steps", "timeout_seconds", "max_tokens")
                )
            ):
                raise ValueError("parent delegation call differs from the persisted child manifest")
        return receipt

    def validate_tool_dispatch(
        self,
        user_id: str,
        task_id: str,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        """Validate that a tool dispatch is allowed by the task's state.

        Raises:
            PermissionError: If task is not dispatchable or requires monitor readback.
            ValueError: If the exact tool action was already confirmed for this task.
        """
        task = self.store.get_task(user_id, task_id)
        if task.get("phase") == "awaiting_child":
            raise PermissionError(
                "TASK_WAITING_FOR_CHILD: parent dispatch is paused until its child reaches a safe terminal state."
            )
        if task["status"] not in {"queued", "running"}:
            raise PermissionError(
                "TASK_NOT_DISPATCHABLE: task is waiting, terminal, or needs reconciliation; "
                "no tool was invoked."
            )
        if (
            self.store.task_requires_monitor_readback(user_id, task_id)
            and tool_name != "monitor_list_rules"
        ):
            raise PermissionError(
                "MONITOR_READBACK_REQUIRED: the task's confirmed monitor insert "
                "must be verified with monitor_list_rules before another action."
            )
        is_required_readback = (
            self.store.task_requires_monitor_readback(user_id, task_id)
            and tool_name == "monitor_list_rules"
        )
        if not is_required_readback and arguments is not None and self.store.has_confirmed_task_call(
            user_id, task_id, tool_name=tool_name, arguments=arguments
        ):
            raise ValueError(
                "TASK_STEP_ALREADY_CONFIRMED: this exact tool action has "
                "confirmed evidence in the resumed task; do not repeat it."
            )

    def prepare_step(self, user_id: str, task_id: str, *, tool_name: str) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task["status"] not in {"queued", "running"}:
            raise ValueError("task is not dispatchable")
        if any(step["status"] == "running" for step in task["steps"]):
            raise ValueError("an existing task step is still running")
        pending = next((step for step in task["steps"] if step["status"] == "pending"), None)
        if pending is not None:
            if pending.get("next_action") != f"dispatch:{tool_name}":
                raise ValueError("a different undispatched task step must be resumed first")
            return pending
        planned = next(
            (
                step for step in task["steps"]
                if step["status"] == "ready" and step.get("description") is not None
            ),
            None,
        )
        if planned is not None:
            if planned.get("next_action") != f"dispatch:{tool_name}":
                raise ValueError("the next planned task step requires a different tool")
            return self.store.transition_task_step(
                user_id, task_id, planned["step_id"],
                expected_status="ready", expected_version=int(planned["version"]),
                new_status="pending", next_action=planned["next_action"],
            )
        if self.store.task_has_plan(user_id, task_id):
            raise ValueError("the durable plan has no remaining dispatchable steps")
        return self.store.add_task_step(
            user_id, task_id, len(task["steps"]), status="pending",
            next_action=f"dispatch:{tool_name}",
        )

    def link_call_to_step(
        self, user_id: str, task_id: str, step_id: str, *, tool_call_id: int
    ) -> dict[str, Any]:
        step = self.store.get_task_step(user_id, task_id, step_id)
        if step.get("tool_call_id") is not None:
            previous = self.store.get_tool_call(user_id, int(step["tool_call_id"]))
            current = self.store.get_tool_call(user_id, int(tool_call_id))
            if (previous["tool_name"], previous["arguments"]) != (
                current["tool_name"], current["arguments"]
            ):
                raise ValueError("resumed step must use the exact persisted tool and arguments")
        return self.store.transition_task_step(
            user_id, task_id, step_id,
            expected_status="pending", expected_version=int(step["version"]),
            new_status="pending", tool_call_id=tool_call_id,
        )

    def claim_step(
        self, user_id: str, task_id: str, step_id: str, *, receipt_id: str
    ) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        step = self.store.get_task_step(user_id, task_id, step_id)
        return self.store.claim_task_step(
            user_id, task_id, step_id,
            expected_task_status=task["status"], expected_task_version=int(task["version"]),
            expected_step_version=int(step["version"]), receipt_id=receipt_id,
        )

    def finish_step(
        self, user_id: str, task_id: str, step_id: str, *, outcome: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record receipt-derived terminal state; unknown is never retried."""
        task = self.store.get_task(user_id, task_id)
        step = next(item for item in task["steps"] if item["step_id"] == step_id)
        step_status = "succeeded" if outcome == "confirmed" else "failed"
        task_status = task["status"]
        wait_reason = task.get("wait_reason")
        next_action = None
        event_type = None
        if outcome == "confirmed":
            pass
        elif outcome == "failed":
            if step.get("description") is not None:
                task_status = "failed"
        elif outcome == "unknown":
            step_status = "needs_reconciliation"
            task_status = "needs_reconciliation"
            wait_reason = (reason or "side_effect_outcome_unknown")[:500]
            next_action = "manual_reconciliation"
            event_type = "task.reconciliation_required"
        else:
            raise ValueError("outcome must be confirmed, failed, or unknown")
        self.store.transition_task_step_and_run(
            user_id, task_id, step_id,
            expected_step_status=step["status"], expected_step_version=int(step["version"]),
            new_step_status=step_status,
            expected_task_status=task["status"], expected_task_version=int(task["version"]),
            new_task_status=task_status, next_action=next_action, wait_reason=wait_reason,
            event_type=event_type,
            event_payload={"reason_code": wait_reason or "unknown"} if event_type else None,
        )
        return self.store.get_task(user_id, task_id)

    def _has_ambiguous_unlinked_call(
        self, user_id: str, task: Mapping[str, Any], receipt_store: Any
    ) -> bool:
        """Fail closed on any non-prepared receipt not owned by a durable step."""
        linked_call_ids: set[str] = set()
        try:
            for step in task.get("steps", []):
                if step.get("tool_call_id") is not None:
                    call = self.store.get_tool_call(user_id, int(step["tool_call_id"]))
                    linked_call_ids.add(str(call["call_key"]))
            blocks = self.store.list_assistant_tool_call_blocks(
                user_id, str(task["task_id"]), limit=500
            )
        except Exception:
            return True

        for block_index, block in enumerate(blocks):
            is_active_block = block_index == len(blocks) - 1
            for call in block["calls"]:
                if call["call_id"] in linked_call_ids:
                    continue
                try:
                    receipts = receipt_store.list_for_call(call["call_id"], user_id=user_id)
                except Exception:
                    return True
                if len(receipts) > 1:
                    return True
                if not receipts:
                    if call["status"] not in {"pending", "running"}:
                        return True
                    continue
                receipt = receipts[0]
                identity_matches = (
                    receipt.call_id == call["call_id"]
                    and receipt.user_id == user_id
                    and receipt.tool_name == call["tool_name"]
                    and receipt.arguments_hash == arguments_hash(call["arguments"])
                    and receipt.turn_id == call["turn_id"]
                )
                if not identity_matches:
                    return True
                if (
                    receipt.status == "prepared"
                    and call["status"] in {"pending", "running"}
                ):
                    continue
                if receipt.status in {"started", "unknown"}:
                    return True
                expected_status = "succeeded" if receipt.status == "confirmed" else "failed"
                if receipt.status not in {"confirmed", "failed"} or call["status"] != expected_status:
                    return True
                if task.get("phase") == "executing" and is_active_block:
                    # A tool block returned by the active provider round has
                    # not had its result fed back into model context yet.
                    return True
        return False

    def recover_incomplete(
        self,
        user_id: str,
        receipt_store: Any,
        *,
        exclude_task_ids: set[str] | frozenset[str] = frozenset(),
        only_task_ids: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Project linked receipt outcomes after a process restart, never replay."""
        recovered: list[dict[str, Any]] = []
        if only_task_ids is None:
            candidates = self.store.list_tasks(
                user_id, statuses=["running", "queued"], limit=500
            )
        else:
            candidates = []
            for requested_id in sorted(str(task_id) for task_id in only_task_ids):
                try:
                    record = self.store.get_task(user_id, requested_id)
                except TaskNotFound:
                    continue
                if record.get("status") in {"running", "queued"}:
                    candidates.append(record)
        for listed in candidates:
            task_key = str(listed["task_id"])
            if task_key in exclude_task_ids:
                continue
            if only_task_ids is not None and task_key not in only_task_ids:
                continue
            task = listed if "steps" in listed else self.store.get_task(user_id, listed["task_id"])
            if task.get("phase") == "awaiting_child":
                children = self.store.get_child_tasks(user_id, task["task_id"])
                delegation_step = None
                for candidate in task.get("steps", []):
                    if candidate.get("status") not in {"pending", "running"}:
                        continue
                    if candidate.get("tool_call_id") is None:
                        continue
                    call = self.store.get_tool_call(
                        user_id, int(candidate["tool_call_id"])
                    )
                    if call.get("tool_name") == "delegate_task":
                        delegation_step = candidate
                        break
                if any(child.get("status") in {"needs_reconciliation", "partial"} for child in children):
                    updated = self.store.transition_task_run(
                        user_id, task["task_id"], expected_status=task["status"],
                        expected_version=int(task["version"]),
                        new_status="needs_reconciliation",
                        wait_reason="child_needs_reconciliation",
                        event_type="task.child_reconciliation_required",
                        event_payload={"child_task_ids": [
                            child["task_id"] for child in children
                            if child.get("status") in {"needs_reconciliation", "partial"}
                        ]},
                    )
                    self.transition_phase(
                        user_id, task["task_id"], "partial",
                        next_action="reconcile_child_task",
                    )
                    recovered.append({"task_id": task["task_id"], "status": updated["status"]})
                    continue
                active_children = [
                    child for child in children
                    if child.get("status") in {"queued", "running", "waiting_user", "waiting_approval"}
                ]
                if active_children:
                    try:
                        if delegation_step is None:
                            raise ValueError("active child has no linked parent delegate_task receipt")
                        parent_receipt = self._load_parent_delegation_receipt(
                            user_id, delegation_step, receipt_store,
                            child_task_id=str(active_children[0]["task_id"]),
                        )
                        if parent_receipt.status not in {"started", "confirmed"}:
                            raise ValueError("parent delegate_task receipt is not safely resumable")
                    except Exception:
                        latest = self.store.get_task(
                            user_id, task["task_id"], include_steps=False, include_events=False
                        )
                        updated = self.store.transition_task_run(
                            user_id, task["task_id"], expected_status=latest["status"],
                            expected_version=int(latest["version"]),
                            new_status="needs_reconciliation",
                            wait_reason="delegation_receipt_reconciliation_required",
                            event_type="task.reconciliation_required",
                            event_payload={"reason_code": "delegation_receipt_reconciliation_required"},
                        )
                        self.transition_phase(
                            user_id, task["task_id"], "partial",
                            next_action="manual_delegation_reconciliation",
                        )
                        recovered.append({"task_id": task["task_id"], "status": updated["status"]})
                        continue
                    if task["status"] != "queued":
                        updated = self.store.transition_task_run(
                            user_id, task["task_id"], expected_status=task["status"],
                            expected_version=int(task["version"]), new_status="queued",
                            wait_reason=f"awaiting_child:{active_children[0]['task_id']}",
                            event_type="task.child_wait_recovered",
                            event_payload={"child_task_ids": [c["task_id"] for c in active_children]},
                        )
                    else:
                        updated = task
                    recovered.append({"task_id": task["task_id"], "status": updated["status"],
                                      "phase": "awaiting_child"})
                    continue
                awaited_id = str(task.get("wait_reason") or "").removeprefix("awaiting_child:")
                terminal_children = [
                    child for child in children
                    if child.get("status") in {"succeeded", "failed", "cancelled"}
                    and (not awaited_id or child["task_id"] == awaited_id)
                ]
                if terminal_children and delegation_step:
                    # The durable child row proves the internal delegation
                    # request committed. Reconcile that parent tool receipt as
                    # completed, never by redispatching it. A failed child is
                    # still a completed delegation tool call; its child status
                    # remains separate evidence.
                    try:
                        receipt = self._load_parent_delegation_receipt(
                            user_id, delegation_step, receipt_store,
                            child_task_id=str(terminal_children[0]["task_id"]),
                        )
                        if receipt.status == "started":
                            receipt_store.finish(
                                receipt.receipt_id, status="confirmed", ok=True, complete=True,
                                result_summary=(
                                    f"Delegated child {terminal_children[0]['task_id']} "
                                    f"reached terminal status {terminal_children[0]['status']}; "
                                    "inspect the durable child result."
                                ),
                            )
                        elif receipt.status != "confirmed":
                            raise ValueError("delegation receipt is not safely confirmable")
                        if delegation_step.get("status") == "running":
                            self.finish_step(
                                user_id, task["task_id"], delegation_step["step_id"],
                                outcome="confirmed",
                            )
                    except Exception:
                        latest = self.store.get_task(
                            user_id, task["task_id"], include_steps=False, include_events=False
                        )
                        if latest["status"] not in {"queued", "running"}:
                            recovered.append({"task_id": task["task_id"], "status": latest["status"]})
                            continue
                        updated = self.store.transition_task_run(
                            user_id, task["task_id"], expected_status=latest["status"],
                            expected_version=int(latest["version"]),
                            new_status="needs_reconciliation",
                            wait_reason="delegation_receipt_reconciliation_required",
                            event_type="task.reconciliation_required",
                            event_payload={"reason_code": "delegation_receipt_reconciliation_required"},
                        )
                        self.transition_phase(
                            user_id, task["task_id"], "partial",
                            next_action="manual_delegation_reconciliation",
                        )
                        recovered.append({"task_id": task["task_id"], "status": updated["status"]})
                        continue
                    task = self.store.get_task(user_id, task["task_id"])
                elif not terminal_children or not delegation_step:
                    # A persisted awaiting_child phase without a live/terminal
                    # child or linked delegation step is inconsistent with
                    # the atomic create-child transaction. Never guess whether
                    # to replay the delegate call.
                    updated = self.store.transition_task_run(
                        user_id, task["task_id"], expected_status=task["status"],
                        expected_version=int(task["version"]),
                        new_status="needs_reconciliation",
                        wait_reason=(
                            "delegation_child_missing_after_dispatch"
                            if not children else "delegation_receipt_reconciliation_required"
                        ),
                        event_type="task.reconciliation_required",
                        event_payload={
                            "reason_code": (
                                "delegation_child_missing_after_dispatch"
                                if not children else "delegation_receipt_reconciliation_required"
                            )
                        },
                    )
                    self.transition_phase(
                        user_id, task["task_id"], "partial",
                        next_action="manual_delegation_reconciliation",
                    )
                    recovered.append({"task_id": task["task_id"], "status": updated["status"]})
                    continue
                # No child was committed, or all committed children are
                # terminal. Resume parent planning only; never redispatch the
                # delegation call or infer that an external child effect ran.
                updated = self.store.transition_task_run(
                    user_id, task["task_id"], expected_status=task["status"],
                    expected_version=int(task["version"]), new_status="queued",
                    wait_reason="resume_after_child" if children else "resume_delegation_not_created",
                    event_type="task.child_wait_recovered",
                    event_payload={"terminal_child_ids": [c["task_id"] for c in children]},
                )
                self.transition_phase(
                    user_id, task["task_id"], "planning",
                    next_action="resume_after_child" if children else "retry_delegation_after_recovery",
                    event_payload={"terminal_child_ids": [c["task_id"] for c in children]},
                )
                recovered.append({"task_id": task["task_id"], "status": updated["status"],
                                  "phase": "planning"})
                continue
            if self._has_ambiguous_unlinked_call(user_id, task, receipt_store):
                updated = self.store.transition_task_run(
                    user_id, task["task_id"], expected_status=task["status"],
                    expected_version=int(task["version"]),
                    new_status="needs_reconciliation",
                    wait_reason="unlinked_tool_call_outcome_unknown",
                    event_type="task.reconciliation_required",
                    event_payload={"reason_code": "unlinked_tool_call_outcome_unknown"},
                )
                self.transition_phase(
                    user_id, task["task_id"], "partial",
                    next_action="manual_tool_call_reconciliation",
                )
                recovered.append({"task_id": task["task_id"], "status": updated["status"]})
                continue
            current_id = task.get("current_step_id")
            step = next((s for s in task["steps"] if s["step_id"] == current_id), None)
            if step is None or step["status"] not in {"pending", "running"}:
                step = next((s for s in reversed(task["steps"])
                             if s["status"] in {"pending", "running"}), None)
            if step is None:
                if task.get("phase") == "delivering":
                    # A restart can occur after Discord accepted the response
                    # but before the client observed success. Preserve that
                    # ambiguity as partial and never issue a duplicate send.
                    updated = self.store.transition_task_run(
                        user_id, task["task_id"], expected_status=task["status"],
                        expected_version=int(task["version"]), new_status="partial",
                        wait_reason="delivery_outcome_unknown",
                        event_type="task.delivery_outcome_unknown",
                        event_payload={"reason_code": "delivery_outcome_unknown"},
                    )
                    self.transition_phase(
                        user_id, task["task_id"], "partial",
                        next_action="manual_delivery_reconciliation",
                    )
                    recovered.append({"task_id": task["task_id"], "status": updated["status"]})
                    continue
                if task.get("phase") in {"executing", "planning", "verifying"}:
                    has_ready_plan_step = any(
                        s["status"] == "ready" and s.get("description") is not None
                        for s in task["steps"]
                    )
                    new_status = "queued"
                    phase_next_action = "resume_recovered_task"
                    target_phase = "planning"
                    if any(s["status"] == "failed" for s in task["steps"]):
                        wait_reason = "restart_after_confirmed_failure"
                        event_payload = {"status": "confirmed_failure"}
                        new_status = "failed"
                        target_phase = "failed"
                        phase_next_action = "report_turn_failure"
                    elif self.store.task_requires_monitor_readback(user_id, task["task_id"]):
                        wait_reason = "monitor_readback_required"
                        event_payload = {"status": "monitor_readback_required"}
                    elif has_ready_plan_step:
                        wait_reason = "plan_pending"
                        event_payload = {"status": "planned_steps_pending"}
                    elif task["steps"] and all(s["status"] == "succeeded" for s in task["steps"]):
                        wait_reason = "restart_after_confirmed_step"
                        event_payload = {"status": "confirmed_steps_only"}
                    else:
                        wait_reason = "restart_before_dispatch"
                        event_payload = {"status": "restart_before_dispatch"}
                    event_type = "task.failure_recovered" if new_status == "failed" else "task.resume_ready"
                    updated = self.store.transition_task_run(
                        user_id, task["task_id"], expected_status=task["status"],
                        expected_version=int(task["version"]), new_status=new_status,
                        wait_reason=wait_reason,
                        event_type=event_type,
                        event_payload=event_payload,
                    )
                    self.transition_phase(
                        user_id, task["task_id"], target_phase,
                        next_action=phase_next_action,
                    )
                    recovered.append({"task_id": task["task_id"], "status": updated["status"]})
                    continue
                has_ready_plan_step = any(
                    s["status"] == "ready" and s.get("description") is not None
                    for s in task["steps"]
                )
                if task["status"] == "queued" and has_ready_plan_step and task.get("wait_reason") != "plan_pending":
                    self.store.transition_task_run(
                        user_id, task["task_id"], expected_status="queued",
                        expected_version=int(task["version"]), new_status="queued",
                        wait_reason="plan_pending", event_type="task.resume_ready",
                        event_payload={"status": "planned_steps_pending"},
                    )
                    recovered.append({"task_id": task["task_id"], "status": "queued"})
                    continue
                if task["status"] == "running" and task["steps"]:
                    if any(s["status"] == "failed" for s in task["steps"]):
                        self.store.transition_task_run(
                            user_id, task["task_id"], expected_status="running",
                            expected_version=int(task["version"]), new_status="failed",
                            wait_reason="restart_after_confirmed_failure",
                        )
                        recovered.append({"task_id": task["task_id"], "status": "failed"})
                    elif has_ready_plan_step:
                        self.store.transition_task_run(
                            user_id, task["task_id"], expected_status="running",
                            expected_version=int(task["version"]), new_status="queued",
                            wait_reason="plan_pending", event_type="task.resume_ready",
                            event_payload={"status": "planned_steps_pending"},
                        )
                        recovered.append({"task_id": task["task_id"], "status": "queued"})
                    elif all(s["status"] == "succeeded" for s in task["steps"]):
                        self.store.transition_task_run(
                            user_id, task["task_id"], expected_status="running",
                            expected_version=int(task["version"]), new_status="queued",
                            wait_reason="restart_after_confirmed_step",
                            event_type="task.resume_ready",
                            event_payload={"status": "confirmed_steps_only"},
                        )
                        recovered.append({"task_id": task["task_id"], "status": "queued"})
                continue
            if task["status"] == "queued" and task.get("wait_reason") in {
                "restart_after_confirmed_step", "restart_before_dispatch"
            }:
                continue
            receipt = None
            if step.get("tool_call_id"):
                call = self.store.get_tool_call(user_id, int(step["tool_call_id"]))
                candidates = receipt_store.list_for_call(call["call_key"], user_id=user_id)
                if (
                    len(candidates) == 1
                    and (
                        step.get("receipt_id") is None
                        or candidates[0].receipt_id == step.get("receipt_id")
                    )
                    and candidates[0].call_id == call["call_key"]
                    and candidates[0].user_id == user_id
                    and candidates[0].tool_name == call["tool_name"]
                    and candidates[0].arguments_hash == arguments_hash(call["arguments"])
                    and candidates[0].turn_id == call.get("turn_key")
                ):
                    receipt = candidates[0]
                    receipt_missing = False
                else:
                    receipt_missing = True
            elif step.get("receipt_id"):
                # A receipt without its matching call link cannot prove which
                # exact action it describes; never treat it as undispatched.
                receipt_missing = True
            else:
                receipt_missing = False

            if receipt is None and not receipt_missing:
                # No receipt or only prepared receipts means execution hasn't
                # crossed ReceiptStore.start. Keep it resumable, but require an
                # explicit user resume instead of binding the next message.
                run = "queued"
                reason = "restart_before_dispatch"
                self.store.transition_task_step_and_run(
                    user_id, task["task_id"], step["step_id"],
                    expected_step_status=step["status"], expected_step_version=int(step["version"]),
                    new_step_status="pending", next_action=step.get("next_action"),
                    expected_task_status=task["status"], expected_task_version=int(task["version"]),
                    new_task_status=run, wait_reason=reason,
                    event_type="task.resume_ready",
                    event_payload={"status": "not_dispatched"},
                )
                recovered.append({"task_id": task["task_id"], "status": run})
                continue
            if receipt is None:
                self.finish_step(
                    user_id, task["task_id"], step["step_id"], outcome="unknown",
                    reason="linked_receipt_missing_after_restart",
                )
                recovered.append({"task_id": task["task_id"], "status": "needs_reconciliation"})
                continue
            if receipt.user_id != user_id:
                self.finish_step(user_id, task["task_id"], step["step_id"],
                                 outcome="unknown", reason="linked_receipt_owner_mismatch")
                recovered.append({"task_id": task["task_id"], "status": "needs_reconciliation"})
                continue
            if receipt.status == "prepared":
                # Restore the durable task to a resumable state first. If the
                # process dies after that transaction, a later restart sees
                # restart_before_dispatch and cannot mistake a finalized
                # receipt for a dispatched/failed action.
                self.store.transition_task_step_and_run(
                    user_id, task["task_id"], step["step_id"],
                    expected_step_status=step["status"], expected_step_version=int(step["version"]),
                    new_step_status="pending", next_action=step.get("next_action"),
                    expected_task_status=task["status"], expected_task_version=int(task["version"]),
                    new_task_status="queued", wait_reason="restart_before_dispatch",
                    event_type="task.resume_ready",
                    event_payload={"receipt_id": receipt.receipt_id, "status": "not_dispatched"},
                )
                try:
                    receipt_store.finish(
                        receipt.receipt_id, status="failed", ok=False, complete=False,
                        error="dispatch_not_started_recovered",
                    )
                except Exception as exc:
                    # The receipt is still only prepared, so no tool crossed
                    # the dispatch boundary. Keep the task resumable; its next
                    # dispatch replaces this stale prepared evidence link.
                    print(
                        " [TASK RECOVERY] stale prepared receipt cleanup failed "
                        f"receipt={receipt.receipt_id}: {type(exc).__name__}"
                    )
                recovered.append({"task_id": task["task_id"], "status": "queued"})
                continue
            if receipt.status == "confirmed":
                self.store.transition_task_step_and_run(
                    user_id, task["task_id"], step["step_id"],
                    expected_step_status=step["status"], expected_step_version=int(step["version"]),
                    new_step_status="succeeded", expected_task_status=task["status"],
                    expected_task_version=int(task["version"]), new_task_status="queued",
                    wait_reason="restart_after_confirmed_step",
                    event_type="task.resume_ready",
                    event_payload={"receipt_id": receipt.receipt_id, "status": receipt.status},
                )
                recovered.append({"task_id": task["task_id"], "status": "queued"})
            elif receipt.status == "failed":
                self.store.transition_task_step_and_run(
                    user_id, task["task_id"], step["step_id"],
                    expected_step_status=step["status"], expected_step_version=int(step["version"]),
                    new_step_status="failed", expected_task_status=task["status"],
                    expected_task_version=int(task["version"]), new_task_status="failed",
                    wait_reason="restart_after_confirmed_failure",
                )
                recovered.append({"task_id": task["task_id"], "status": "failed"})
            else:
                # Started and unknown are ambiguous; neither is replayed.
                self.finish_step(
                    user_id, task["task_id"], step["step_id"], outcome="unknown",
                    reason=f"receipt_{receipt.status}_after_restart",
                )
                recovered.append({"task_id": task["task_id"], "status": "needs_reconciliation"})
        status_phase = {
            "succeeded": "completed", "failed": "failed", "cancelled": "failed",
            "partial": "partial", "needs_reconciliation": "partial",
            "waiting_user": "awaiting_user", "waiting_approval": "awaiting_user",
        }
        for status, target_phase in status_phase.items():
            for listed in self.store.list_tasks(user_id, statuses=[status], limit=500):
                task = self.store.get_task(
                    user_id, listed["task_id"], include_steps=False, include_events=False
                )
                phase = str(task.get("phase") or "received")
                # Only repair a crash-window mismatch; do not rewrite an
                # already terminal phase based on an inconsistent lifecycle row.
                if phase in {"completed", "partial", "failed"} or phase == target_phase:
                    continue
                if status == "succeeded" and phase != "delivering":
                    continue
                action = {
                    "completed": None,
                    "failed": "report_turn_failure",
                    "awaiting_user": "wait_for_exact_choice",
                    "partial": (
                        "manual_delivery_reconciliation"
                        if phase == "delivering"
                        else "manual_tool_call_reconciliation"
                    ),
                }[target_phase]
                self.transition_phase(
                    user_id, task["task_id"], target_phase, next_action=action
                )
                if not any(item.get("task_id") == task["task_id"] for item in recovered):
                    recovered.append({"task_id": task["task_id"], "status": status})
        return recovered

    def resume_ready_task(self, user_id: str, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        wait_reason = str(task.get("wait_reason") or "")
        if task["status"] != "queued" or (
            wait_reason not in {
                "restart_after_confirmed_step", "restart_before_dispatch",
                "monitor_readback_required", "plan_pending",
            }
            and not wait_reason.startswith("resume_after_reply:")
        ):
            raise ValueError("task is not ready for a safe post-restart continuation")
        phase = str(task.get("phase") or "received")
        if phase == "delivering":
            raise ValueError(
                "task delivery outcome is ambiguous; reconcile the prior response before resuming"
            )
        if phase in {"partial", "failed", "completed"}:
            raise ValueError("task phase is terminal and cannot be resumed")
        self.transition_phase(
            user_id, task_id, "planning", next_action="resume_recovered_task"
        )
        task = self.store.get_task(user_id, task_id)
        return task

    def finish_turn(self, user_id: str, task_id: str, *, failed: bool = False) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task.get("phase") == "awaiting_child":
            # A completed Discord/model turn does not complete the parent task
            # while its durable child is still queued or running.
            return task
        if task["status"] in {
            "needs_reconciliation", "waiting_user", "waiting_approval",
            "succeeded", "partial", "failed", "cancelled",
        }:
            return task
        steps = task["steps"]
        if self.store.task_requires_monitor_readback(user_id, task_id):
            self.store.transition_task_run(
                user_id, task_id, expected_status=task["status"],
                expected_version=int(task["version"]), new_status="queued",
                wait_reason="monitor_readback_required",
                event_type="task.resume_ready",
                event_payload={"status": "monitor_readback_required"},
            )
            return self.store.get_task(user_id, task_id)
        active = [s for s in steps if s["status"] == "running"]
        if active:
            step = active[0]
            self.store.transition_task_step_and_run(
                user_id, task_id, step["step_id"],
                expected_step_status=step["status"], expected_step_version=int(step["version"]),
                new_step_status="needs_reconciliation", next_action="reconcile_linked_receipt",
                expected_task_status=task["status"], expected_task_version=int(task["version"]),
                new_task_status="needs_reconciliation", wait_reason="unfinished_step_at_turn_end",
            )
            return self.store.get_task(user_id, task_id)
        pending = [s for s in steps if s["status"] == "pending"]
        if pending:
            self.store.transition_task_run(
                user_id, task_id, expected_status=task["status"],
                expected_version=int(task["version"]), new_status="queued",
                wait_reason="restart_before_dispatch",
                event_type="task.resume_ready",
                event_payload={"status": "pending_step"},
            )
            return self.store.get_task(user_id, task_id)
        planned = [
            s for s in steps if s["status"] == "ready" and s.get("description") is not None
        ]
        if planned:
            self.store.transition_task_run(
                user_id, task_id, expected_status=task["status"],
                expected_version=int(task["version"]), new_status="queued",
                wait_reason="plan_pending", event_type="task.resume_ready",
                event_payload={"status": "planned_steps_pending"},
            )
            return self.store.get_task(user_id, task_id)
        terminal = "failed" if failed or any(s["status"] == "failed" for s in steps) else "succeeded"
        self.store.transition_task_run(
            user_id, task_id, expected_status=task["status"],
            expected_version=int(task["version"]), new_status=terminal,
            result_ref="turn_completed" if terminal == "succeeded" else None,
        )
        return self.store.get_task(user_id, task_id)

    def mark_delivery_unknown(self, user_id: str, task_id: str) -> dict[str, Any]:
        """Keep an ambiguous outbound send partial and prohibit automatic resend."""
        task = self.store.get_task(user_id, task_id, include_steps=False, include_events=False)
        if task["status"] in {
            "queued", "running", "waiting_user", "waiting_approval", "verifying",
        }:
            self.store.transition_task_run(
                user_id, task_id, expected_status=task["status"],
                expected_version=int(task["version"]), new_status="partial",
                wait_reason="delivery_outcome_unknown",
                event_type="task.delivery_outcome_unknown",
                event_payload={"reason_code": "delivery_outcome_unknown"},
            )
        task = self.store.get_task(user_id, task_id, include_steps=False, include_events=False)
        if task.get("phase") == "delivering":
            self.transition_phase(
                user_id, task_id, "partial",
                next_action="manual_delivery_reconciliation",
            )
        return self.store.get_task(user_id, task_id)

    def request_user(
        self, user_id: str, task_id: str, *, question_id: str, question: str,
        allowed_answers: Mapping[str, Any], expires_at: str | None = None,
        approval_request: ApprovalRequest | None = None,
        authorization_request: AuthorizationRequest | None = None,
        input_only: bool = False,
    ) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task["status"] not in {"queued", "running"}:
            raise ValueError("only active tasks can wait for a user decision")
        payload: dict[str, Any] = {
            "question_id": question_id, "question": question[:1000],
            "allowed_answers": dict(allowed_answers),
            "input_only": bool(input_only),
        }
        if expires_at:
            payload["expires_at"] = expires_at
        if (approval_request is None) != (authorization_request is None):
            raise ValueError("approval persistence requires both typed authorization objects")
        if approval_request is not None and authorization_request is not None:
            if approval_request.request_fingerprint != authorization_request.fingerprint:
                raise ValueError("approval request is not bound to the exact task action")
            if approval_request.tool_name != authorization_request.tool_name:
                raise ValueError("approval tool does not match the authorization request")
            if approval_request.operation != authorization_request.policy.operation:
                raise ValueError("approval operation does not match the authorization request")
            payload.update({
                "approval_id": approval_request.approval_id,
                "action_fingerprint": approval_request.request_fingerprint,
                "tool_name": approval_request.tool_name,
                "operation": approval_request.operation,
                "risk": approval_request.risk.value,
                "summary": approval_request.summary[:500],
                "approval_expires_at": approval_request.expires_at,
            })
        waiting_status = "waiting_approval" if approval_request is not None else "waiting_user"
        return self.store.transition_task_run(
            user_id, task_id, expected_status=task["status"],
            expected_version=int(task["version"]), new_status=waiting_status,
            wait_reason=f"question:{question_id}",
            event_type="task.question_issued", event_payload=payload,
        )

    def request_user_choice(
        self,
        user_id: str,
        task_id: str,
        *,
        question: str,
        choices: list[str],
        expires_in_minutes: int = 1440,
    ) -> dict[str, Any]:
        """Persist a bounded, expiring enum question for normal task resumption."""
        if not isinstance(question, str):
            raise ValueError("question must be text")
        prompt = question.strip()
        if not prompt or len(prompt) > 1000:
            raise ValueError("question must contain 1–1000 characters")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 8:
            raise ValueError("choices must contain 2–8 exact answer strings")
        if any(not isinstance(value, str) for value in choices):
            raise ValueError("each answer choice must be text")
        normalized = [str(value).strip() for value in choices]
        if any(not value or len(value) > 100 for value in normalized):
            raise ValueError("each answer choice must contain 1–100 characters")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("answer choices must be unique, ignoring case")
        minutes = int(expires_in_minutes)
        if not 5 <= minutes <= 10080:
            raise ValueError("expires_in_minutes must be between 5 and 10080")
        question_id = uuid.uuid4().hex
        expires_iso = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        task = self.request_user(
            user_id,
            task_id,
            question_id=question_id,
            question=prompt,
            allowed_answers={"type": "enum", "enum": normalized},
            expires_at=expires_iso,
            input_only=True,
        )
        return {
            "task": task,
            "question_id": question_id,
            "expires_at": expires_iso,
            "expires_in_minutes": minutes,
            "question": prompt,
            "choices": normalized,
        }

    def accept_reply(
        self, user_id: str, task_id: str, *, question_id: str, answer: str,
        source_message_id: str,
    ) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task["status"] not in {"waiting_user", "waiting_approval"} or task["wait_reason"] != f"question:{question_id}":
            raise ValueError("reply does not match a task waiting on this question")
        events = task["events"]
        question_events = [e for e in events if e["event_type"] == "task.question_issued"]
        if not question_events:
            raise ValueError("pending task question is missing")
        question = question_events[-1]["payload"]
        if question.get("question_id") != question_id:
            raise ValueError("reply question ID does not match the latest pending question")
        expires_at = question.get("expires_at")
        if expires_at:
            try:
                expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("persisted task question has an invalid expiry") from exc
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                changed = self.store.transition_task_run(
                    user_id, task_id, expected_status=task["status"],
                    expected_version=int(task["version"]), new_status="cancelled",
                    wait_reason=None, event_type="task.question_expired",
                    event_payload={"question_id": question_id},
                )
                return {"task": changed, "answer": "", "question": question, "approval": None}
        if any(
            e["event_type"] in {"task.reply_matched", "task.approval_recorded", "task.approval_reply_received"}
            and e["payload"].get("question_id") == question_id
            for e in events
        ):
            raise ValueError("task question has already been answered")
        answer_text = str(answer).strip()
        allowed = question.get("allowed_answers") or {}
        answer_type = allowed.get("type")
        if answer_type == "enum" and answer_text not in set(allowed.get("enum") or []):
            raise ValueError("reply is outside the allowed answer choices")
        approval: HumanApproval | None = None
        if question.get("approval_id"):
            from src.services.authorization import ApprovalStatus, RiskTier
            approved = answer_text.casefold() in {"yes", "approve", "approved", "allow"}
            approval_request = ApprovalRequest(
                approval_id=question["approval_id"],
                request_fingerprint=question["action_fingerprint"],
                tool_name=question["tool_name"],
                operation=question["operation"],
                risk=RiskTier(question["risk"]),
                summary=question.get("summary", ""),
                expires_at=float(question["approval_expires_at"]),
            )
            approval = approve_request(
                approval_request, approved=approved, approved_by=user_id
            )
        approval_pending_execution = bool(question.get("approval_id") and approval and approval.approved)
        denied_approval = bool(question.get("approval_id") and approval and not approval.approved)
        next_status = (
            "waiting_approval" if approval_pending_execution
            else "cancelled" if denied_approval
            else "queued"
        )
        reply_payload: dict[str, Any] = {
            "question_id": question_id, "source_message_id": source_message_id,
        }
        if approval is not None:
            reply_payload.update({
                "approval_id": approval.approval_id,
                "action_fingerprint": approval.request_fingerprint,
                "approval_status": approval.status.value,
                "approved_by": approval.approved_by,
                "decided_at": approval.decided_at,
            })
        changed = self.store.transition_task_run(
            user_id, task_id, expected_status=task["status"],
            expected_version=int(task["version"]), new_status=next_status,
            wait_reason=f"resume_after_reply:{question_id}" if not approval else None,
            event_type="task.approval_reply_received" if approval else "task.reply_matched",
            event_payload=reply_payload,
        )
        return {"task": changed, "answer": answer_text, "question": question, "approval": approval}

    def reply_is_input_only(self, user_id: str, task_id: str) -> bool:
        """Whether the active resumed reply supplies data but grants no action."""
        task = self.store.get_task(user_id, task_id)
        reason = str(task.get("wait_reason") or "")
        if not reason.startswith("resume_after_reply:"):
            return False
        question_id = reason.split(":", 1)[1]
        return any(
            event["event_type"] == "task.question_issued"
            and event["payload"].get("question_id") == question_id
            and event["payload"].get("input_only") is True
            for event in task["events"]
        )

    def match_waiting_reply(
        self, user_id: str, session_id: str, content: str
    ) -> dict[str, str] | None:
        """Find one exact, typed enum answer; ambiguous/free text stays a new turn."""
        matches: list[dict[str, str]] = []
        for listed in self.store.list_tasks(
            user_id, session_id=session_id, statuses=["waiting_user"], limit=500
        ):
            task = self.store.get_task(user_id, listed["task_id"])
            if task["session_key"] != session_id:
                continue
            questions = [e for e in task["events"] if e["event_type"] == "task.question_issued"]
            if not questions:
                continue
            question = questions[-1]["payload"]
            expires_at = question.get("expires_at")
            if expires_at:
                try:
                    expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                except ValueError:
                    expiry = datetime.min.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    self.store.transition_task_run(
                        user_id,
                        task["task_id"],
                        expected_status="waiting_user",
                        expected_version=int(task["version"]),
                        new_status="cancelled",
                        wait_reason=None,
                        event_type="task.question_expired",
                        event_payload={"question_id": question.get("question_id")},
                    )
                    continue
            allowed = question.get("allowed_answers") or {}
            if allowed.get("type") != "enum":
                continue
            answer = next((str(item) for item in allowed.get("enum", [])
                           if str(item).strip().casefold() == str(content).strip().casefold()), None)
            if answer is not None:
                matches.append({
                    "task_id": task["task_id"],
                    "question_id": str(question.get("question_id") or ""),
                    "answer": answer,
                    "question": str(question.get("question") or ""),
                    "objective": str(task.get("objective") or ""),
                    "step_states": [
                        {
                            "step_id": step["step_id"],
                            "step_order": step["step_order"],
                            "status": step["status"],
                            "next_action": step.get("next_action"),
                        }
                        for step in task.get("steps", [])
                    ],
                })
        return matches[0] if len(matches) == 1 and matches[0]["question_id"] else None

    def get_persisted_reply(self, user_id: str, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        replies = [e for e in task["events"] if e["event_type"] == "task.reply_matched"]
        if not replies:
            raise ValueError("task has no persisted user reply")
        event = replies[-1]
        message = self.store.get_message(
            user_id, task["session_key"], event["payload"]["source_message_id"],
            channel_id=task.get("channel_id"), thread_id=task.get("thread_id"),
        )
        question_id = event["payload"].get("question_id")
        question = next((e["payload"] for e in reversed(task["events"])
                         if e["event_type"] == "task.question_issued"
                         and e["payload"].get("question_id") == question_id), {})
        return {"answer": message["content"], "question": question, "message": message}

    def surface_reconciliation_once(self, user_id: str, task_id: str) -> bool:
        """Atomically claim the task's sole notice attempt before external delivery.

        This provides at-most-once delivery attempts. A process crash or chat API
        failure after the claim is intentionally not retried because that could
        duplicate a notice whose delivery outcome is itself ambiguous.
        """
        task = self.store.get_task(user_id, task_id)
        if task["status"] != "needs_reconciliation":
            return False
        if any(e["event_type"] in {
            "task.reconciliation_surfaced", "task.reconciliation_notice_claimed"
        } for e in task["events"]):
            return False
        try:
            self.store.append_task_event(
                user_id, task_id, event_type="task.reconciliation_notice_claimed",
                payload={"status": task["status"], "reason_code": task["wait_reason"] or "unknown"},
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def reconciliation_notice_pending(self, user_id: str, task_id: str) -> bool:
        task = self.store.get_task(user_id, task_id)
        return task["status"] == "needs_reconciliation" and not any(
            e["event_type"] in {
                "task.reconciliation_surfaced", "task.reconciliation_notice_claimed"
            } for e in task["events"]
        )

    def _project_child_cancellation(self, user_id: str, child_task: Mapping[str, Any]) -> None:
        parent_id = child_task.get("parent_task_id")
        if not parent_id:
            return
        try:
            parent = self.store.get_task(
                user_id, str(parent_id), include_steps=False, include_events=False
            )
            if parent.get("phase") != "awaiting_child" or parent["status"] not in {"queued", "running"}:
                return
            if child_task["status"] in {"needs_reconciliation", "partial"}:
                updated = self.store.transition_task_run(
                    user_id, str(parent_id), expected_status=parent["status"],
                    expected_version=int(parent["version"]),
                    new_status="needs_reconciliation",
                    wait_reason=f"child_needs_reconciliation:{child_task['task_id']}",
                    event_type="task.child_reconciliation_required",
                    event_payload={"child_task_id": child_task["task_id"]},
                )
                self.transition_phase(
                    user_id, str(parent_id), "partial",
                    next_action="reconcile_child_task",
                    event_payload={"child_task_id": child_task["task_id"]},
                )
            elif child_task["status"] == "cancelled":
                # Let recovery validate the parent's linked delegate_task
                # receipt before resuming it. Without that evidence, resuming
                # could let the model issue the delegation again.
                from src.services.tool_receipts import ReceiptStore

                self.recover_incomplete(
                    user_id, ReceiptStore(self.store.connection)
                )
        except Exception:
            # The parent remains in awaiting_child and will be reconciled by
            # recover_incomplete; do not guess around a concurrent update.
            return

    def cancel(self, user_id: str, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task["status"] in {"succeeded", "partial", "failed", "cancelled"}:
            return task

        # Stop future model work at the scheduler boundary. In-flight model
        # calls are cooperative and may finish, but validate_tool_dispatch
        # below will reject any subsequent tool action after the durable
        # cancellation transition.
        try:
            from src.agent.scheduler import get_global_scheduler
            get_global_scheduler().request_cancellation(task_id)
        except Exception:
            pass

        # Cascade cancellation to any active child tasks
        child_tasks = self.store.get_child_tasks(user_id, task_id)
        for child in child_tasks:
            if child["status"] not in {"succeeded", "partial", "failed", "cancelled"}:
                self.cancel(user_id, child["task_id"])
        task = self.store.get_task(user_id, task_id)
        if task["status"] in {"needs_reconciliation", "partial"}:
            return task
        unresolved_child_ids = [
            child["task_id"]
            for child in self.store.get_child_tasks(user_id, task_id)
            if child["status"] in {"needs_reconciliation", "partial"}
        ]
        if unresolved_child_ids:
            updated = self.store.transition_task_run(
                user_id, task_id, expected_status=task["status"],
                expected_version=int(task["version"]),
                new_status="needs_reconciliation",
                wait_reason="child_cancellation_needs_reconciliation",
                event_type="task.child_reconciliation_required",
                event_payload={"child_task_ids": unresolved_child_ids},
            )
            self.transition_phase(
                user_id, task_id, "partial", next_action="reconcile_child_task",
                event_payload={"child_task_ids": unresolved_child_ids},
            )
            return self.store.get_task(user_id, task_id)

        if any(step.get("status") == "running" for step in task.get("steps", [])):
            # A running tool step can be between receipt.start and the actual
            # external call. Cancellation cannot prove it was not dispatched;
            # keep it visibly ambiguous instead of hiding it as cancelled.
            step = next(step for step in task["steps"] if step.get("status") == "running")
            self.finish_step(
                user_id, task_id, step["step_id"], outcome="unknown",
                reason="cancel_requested_during_tool_dispatch",
            )
            self.transition_phase(
                user_id, task_id, "partial", next_action="manual_tool_reconciliation"
            )
            result = self.store.get_task(user_id, task_id)
            self._project_child_cancellation(user_id, result)
            return result

        cancelled = self.store.transition_task_run(
            user_id, task_id, expected_status=task["status"],
            expected_version=int(task["version"]), new_status="cancelled",
            cancellation_requested=True,
        )
        self.transition_phase(
            user_id, task_id, "failed", next_action="task_cancelled"
        )
        self._project_child_cancellation(user_id, cancelled)
        return self.store.get_task(user_id, task_id)


__all__ = ["TaskController"]
