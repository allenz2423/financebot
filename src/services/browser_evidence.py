"""Receipt-backed browser evidence classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


OBSERVATION_TOOLS = frozenset({
    "scrape_rendered_page",
    "observe_browser",
    "browser_observe",
    "read_browser_page",
    "browser_snapshot",
})
NAVIGATION_TOOLS = frozenset({
    "navigate_browser",
    "browser_navigate",
    "open_browser_page",
})
MUTATION_TOOLS = frozenset({
    "click_browser",
    "browser_click",
    "fill_browser_form",
    "submit_browser_form",
    "browser_mutate",
})


@dataclass(frozen=True)
class BrowserEvidence:
    event_type: str
    tool_name: str
    receipt_id: str | None
    complete: bool


def _confirmed(entry: Mapping[str, Any]) -> bool:
    if entry.get("ok") is not True:
        return False
    status = str(entry.get("status") or "").casefold()
    if status in {"failed", "unknown", "partial", "prepared", "started"}:
        return False
    return entry.get("complete") is not False


def classify_entry(entry: Mapping[str, Any]) -> BrowserEvidence | None:
    if not _confirmed(entry):
        return None
    name = str(entry.get("name") or entry.get("tool_name") or "").casefold()
    explicit = str(entry.get("evidence_type") or "").casefold()
    if explicit in {"observation", "navigation", "mutation", "approval_required"}:
        event_type = explicit
    elif name in OBSERVATION_TOOLS:
        event_type = "observation"
    elif name in NAVIGATION_TOOLS:
        event_type = "navigation"
    elif name in MUTATION_TOOLS:
        event_type = "mutation"
    else:
        return None
    return BrowserEvidence(
        event_type=event_type,
        tool_name=name,
        receipt_id=entry.get("receipt_id"),
        complete=entry.get("complete") is not False,
    )


def browser_evidence(trace: Iterable[Mapping[str, Any]] | None) -> tuple[BrowserEvidence, ...]:
    return tuple(
        evidence
        for entry in trace or ()
        if isinstance(entry, Mapping)
        for evidence in (classify_entry(entry),)
        if evidence is not None
    )


def observed_page(trace: Iterable[Mapping[str, Any]] | None) -> bool:
    return any(item.event_type == "observation" for item in browser_evidence(trace))


def browser_tool_ran(trace: Iterable[Mapping[str, Any]] | None) -> bool:
    return bool(browser_evidence(trace))


__all__ = [
    "BrowserEvidence",
    "browser_evidence",
    "browser_tool_ran",
    "classify_entry",
    "observed_page",
]
