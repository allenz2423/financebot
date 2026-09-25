"""Deterministic intent-to-required-tool rules for explicit user actions."""

from __future__ import annotations

import re


_PLAID_SYNC_REQUEST = re.compile(
    r"\b(?:"
    r"(?:run|start|perform|do|execute|initiate|trigger|launch|refresh|update|pull)"
    r"\s+(?:a\s+)?plaid\s+sync"
    r"|sync\s+(?:my\s+)?plaid"
    r"|plaid\s+sync\s+(?:now|please|today)?"
    r")\b",
    re.IGNORECASE,
)
_NEGATED_PLAID_SYNC = re.compile(
    r"\b(?:don't|do not|never|without)\b[^.!?\n]{0,48}\b(?:plaid\s+sync|sync\s+plaid)\b",
    re.IGNORECASE,
)


def infer_required_tools(prompt: str) -> frozenset[str]:
    """Return tools that an explicit affirmative user action requires."""

    text = str(prompt or "").strip()
    if not text or _NEGATED_PLAID_SYNC.search(text):
        return frozenset()
    if _PLAID_SYNC_REQUEST.search(text):
        return frozenset({"sync_plaid_accounting"})
    return frozenset()


__all__ = ["infer_required_tools"]
