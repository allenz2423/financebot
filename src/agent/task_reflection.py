"""Bounded reflection choices derived from verified task evidence.

The model may select among server-built, receipt-bound procedure patterns. It
cannot author lesson text, facts, decisions, permissions, or evidence strings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from typing import Any


MAX_STEPS = 20
MAX_TOOL_NAME_CHARS = 120
MAX_CHECKS = 50
MAX_EVIDENCE_REF_CHARS = 256
MAX_CONTEXT_CHARS = 12_000
MAX_CANDIDATES = 5
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,119}\Z")


def _payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _passed_checks(task: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Read the persisted aggregate proof, whose writer validates every check."""
    events = task.get("events")
    if not isinstance(events, (list, tuple)):
        return None
    for event in events:
        if not isinstance(event, Mapping) or event.get("event_type") != "task.verification_passed":
            continue
        proof = _payload(event.get("payload_json", event.get("payload")))
        # SessionStore only persists this event with passed=true after checking
        # all individual outcomes. It intentionally omits their bools in storage.
        if proof is None or proof.get("passed") is not True:
            continue
        checks = proof.get("checks")
        if not isinstance(checks, list) or not checks or len(checks) > MAX_CHECKS:
            continue
        valid: list[dict[str, Any]] = []
        for check in checks:
            if not isinstance(check, Mapping):
                valid = []
                break
            check_id = check.get("check_id")
            refs = check.get("evidence_refs", [])
            if (
                not isinstance(check_id, str) or not 1 <= len(check_id) <= 256
                or check_id.strip() != check_id
                or check.get("passed") is False
                or not isinstance(refs, (list, tuple)) or len(refs) > 20
                or any(
                    not isinstance(ref, str) or not ref
                    or ref.strip() != ref or len(ref) > MAX_EVIDENCE_REF_CHARS
                    for ref in refs
                )
            ):
                valid = []
                break
            valid.append({"check_id": check_id, "evidence_refs": list(refs)})
        if valid:
            return valid
    return None


def _verified_choices(
    task: Mapping[str, Any], registered_tools: Collection[str]
) -> list[dict[str, str]] | None:
    if not isinstance(task, Mapping) or task.get("status") != "succeeded":
        return None
    checks = _passed_checks(task)
    steps = task.get("steps")
    if checks is None or not isinstance(steps, (list, tuple)):
        return None

    choices: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for step in steps[:MAX_STEPS]:
        if not isinstance(step, Mapping) or step.get("status") != "succeeded":
            continue
        step_id = step.get("step_id")
        receipt_id = step.get("receipt_id")
        tool_name = step.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            action = step.get("next_action")
            tool_name = action[len("dispatch:"):] if isinstance(action, str) and action.startswith("dispatch:") else ""
        if (
            not isinstance(step_id, str) or not step_id
            or not isinstance(receipt_id, str) or not receipt_id
            or not isinstance(tool_name, str) or _TOOL_NAME.fullmatch(tool_name) is None
            or tool_name not in registered_tools
        ):
            continue
        for check in checks:
            if receipt_id not in check["evidence_refs"]:
                continue
            key = (step_id, check["check_id"])
            if key in seen:
                continue
            seen.add(key)
            choices.append({
                "choice_id": f"choice_{len(choices) + 1}",
                "step_id": step_id,
                "tool_name": tool_name,
                "check_id": check["check_id"],
                "receipt_id": receipt_id,
            })
            if len(choices) >= MAX_CANDIDATES:
                break
        if len(choices) >= MAX_CANDIDATES:
            break
    if not choices:
        return None
    return choices


def build_reflection_context(
    task: Mapping[str, Any], *, registered_tools: Collection[str]
) -> dict[str, Any] | None:
    """Build opaque selection choices; exclude all user/model-authored prose."""
    choices = _verified_choices(task, registered_tools)
    if choices is None:
        return None
    # Never send persisted identifiers to the model: APIs may allow callers to
    # choose IDs, and opaque host-generated labels are sufficient for selection.
    context = {"verified_procedure_choices": [
        {"choice_id": choice["choice_id"], "tool_name": choice["tool_name"]}
        for choice in choices
    ]}
    if len(json.dumps(context, separators=(",", ":"))) > MAX_CONTEXT_CHARS:
        return None
    return context


def parse_lesson_candidates(
    task: Mapping[str, Any], model_text: str, *,
    registered_tools: Collection[str],
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Validate model selections and construct text from a fixed safe template."""
    context = build_reflection_context(task, registered_tools=registered_tools)
    if context is None or not isinstance(model_text, str):
        return []
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool) or max_candidates < 1:
        return []
    try:
        result = json.loads(model_text)
    except (TypeError, ValueError):
        return []
    if not isinstance(result, dict) or set(result) != {"lessons"}:
        return []
    raw = result["lessons"]
    if not isinstance(raw, list) or len(raw) > min(max_candidates, MAX_CANDIDATES):
        return []

    trusted_choices = _verified_choices(task, registered_tools)
    allowed = {choice["choice_id"]: choice for choice in trusted_choices or []}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_tools: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"pattern", "choice_id"}:
            return []
        choice_id = item.get("choice_id")
        choice = allowed.get(choice_id)
        if item.get("pattern") != "verify_receipt_before_reporting" or choice is None or choice_id in seen:
            return []
        seen.add(choice_id)
        if choice["tool_name"] in seen_tools:
            continue
        seen_tools.add(choice["tool_name"])
        # All interpolated values are server-loaded IDs or canonical tool names;
        # no model-authored content can become durable memory text.
        selected.append({
            "kind": "procedure",
            "lesson": (
                f"After using {choice['tool_name']}, confirm its persisted "
                "completion evidence before reporting the task as complete."
            ),
            "evidence_refs": [choice["receipt_id"]],
            "confidence": 0.9,
        })
    return selected
