"""Evidence checks for user-visible claims about tool actions.

Provider text is not execution evidence.  This module intentionally stays
pure and small so it can be used by the advisor loop, tests, and eventually a
durable receipt-backed claim service without importing the Discord/provider
stack.

The first version handles explicit action claims (for example, "I sent the
email" or "I checked your balance") and the legacy in-memory trace shape.  A
future result-contract layer can add typed fact/provenance checks without
changing the action-claim API.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ActionClaim:
    """A user-visible assertion that requires a successful tool capability."""

    text: str
    capability: str


@dataclass(frozen=True)
class ClaimDecision:
    """Result of evaluating a response against execution evidence."""

    allowed: bool
    claims: tuple[ActionClaim, ...] = ()
    unsupported: tuple[ActionClaim, ...] = ()

    @property
    def reason(self) -> str | None:
        if self.allowed:
            return None
        return "unsupported action claim without a matching successful tool receipt"


# Keep these patterns deliberately explicit.  The gate is intended to catch
# claims of completed work, not ordinary planning/help text such as "I can
# check that" or "I will send it if you approve".
_ACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"\b(?:i|we)\s+(?:have\s+)?(?:sent|posted|emailed|notified|alerted|texted|messaged)\b",
        "mutation",
    ),
    (
        r"\b(?:i|we)\s+(?:have\s+)?(?:saved|stored|remembered|added\s+to\s+memory|persisted)\b",
        "memory",
    ),
    (
        r"\b(?:i|we)\s+(?:have\s+)?(?:updated|changed|deleted|created|added|removed|transferred|scheduled|submitted|purchased|ordered)\b",
        "mutation",
    ),
    (
        r"\b(?:i|we)\s+(?:have\s+)?(?:searched|looked\s+up|looked\s+for|retrieved|read|opened|fetched|checked|reviewed|verified|confirmed|pulled|downloaded|scanned|looked\s+at|called|used|ran|executed)\b",
        "read",
    ),
    (
        r"\b(?:the|your)\s+(?:email|message|alert|file|record|transaction)\s+(?:was|has been)\s+(?:sent|posted|saved|updated|created|deleted|changed)\b",
        "mutation",
    ),
    # Coordinated claims commonly omit the repeated subject: "I checked X
    # and sent Y".  They still assert two completed actions.
    (
        r"\b(?:and|then)\s+(?:sent|posted|emailed|notified|alerted|texted|messaged|updated|changed|deleted|created|added|removed|transferred|scheduled|submitted|purchased|ordered)\b",
        "mutation",
    ),
    (
        r"\b(?:and|then)\s+(?:searched|looked\s+up|looked\s+for|retrieved|read|opened|fetched|checked|reviewed|verified|confirmed|pulled|downloaded|scanned|looked\s+at)\b",
        "read",
    ),
)

_COMPILED_ACTION_PATTERNS = tuple(
    (re.compile(pattern, re.IGNORECASE), capability)
    for pattern, capability in _ACTION_PATTERNS
)


def detect_action_claims(text: str | None) -> tuple[ActionClaim, ...]:
    """Extract explicit completed-action claims from assistant text."""

    value = str(text or "")
    claims: list[ActionClaim] = []
    seen: set[tuple[str, str]] = set()
    for pattern, capability in _COMPILED_ACTION_PATTERNS:
        for match in pattern.finditer(value):
            # A truthful refusal can quote the forbidden action: "I cannot
            # claim that I sent the alert." That is not a success claim.
            prefix = value[max(0, match.start() - 100):match.start()]
            if re.search(
                r"(?:cannot|can't|couldn't|could not|didn't|did not|never|won't|will not|wouldn't|would not|unable to|refuse to|refused to|do not|don't think)\s+(?:claim|say|state|confirm)?(?:\s+that)?\s*(?:i|we)?\s*$",
                prefix,
                flags=re.IGNORECASE,
            ):
                continue
            claim = ActionClaim(text=match.group(0), capability=capability)
            key = (claim.text.casefold(), capability)
            if key not in seen:
                claims.append(claim)
                seen.add(key)
    return tuple(claims)


def _tool_capabilities(tool_name: str) -> frozenset[str]:
    """Map a tool name to conservative evidence capabilities.

    This is intentionally name-based only as a migration adapter for the
    existing trace.  Typed registry metadata and result contracts should
    replace this mapping for high-risk tools in the next phase.
    """

    name = str(tool_name or "").casefold()
    if not name:
        return frozenset()

    capabilities = {"read"}
    mutation_tokens = (
        "send",
        "post",
        "alert",
        "notify",
        "save",
        "persist",
        "create",
        "add_",
        "update",
        "delete",
        "remove",
        "change",
        "set_",
        "schedule",
        "submit",
        "purchase",
        "order",
        "transfer",
        "move_",
        "assert_",
        "retract",
        "run_python",
        "run_shell",
        "write",
        "artifact",
        "workspace_file",
    )
    if any(token in name for token in mutation_tokens):
        capabilities.add("mutation")

    memory_tokens = ("memory", "claim", "world_model", "knowledge_base")
    if any(token in name for token in memory_tokens):
        capabilities.add("memory")

    return frozenset(capabilities)


def _receipt_is_successful(entry: Mapping[str, Any]) -> bool:
    """Accept current traces and reject explicit partial/unknown outcomes."""

    if entry.get("ok") is not True:
        return False

    status = str(entry.get("status") or "").casefold()
    if status in {"failed", "unknown", "partial", "prepared", "started"}:
        return False

    if entry.get("complete") is False:
        return False

    # Legacy turn_tool_trace entries have only ``name`` and ``ok``.  They are
    # accepted during migration, while richer receipt fields get stricter
    # semantics automatically.
    return True


def successful_capabilities(trace: Iterable[Mapping[str, Any]] | None) -> frozenset[str]:
    """Return capabilities supported by successful/confirmed trace entries."""

    capabilities: set[str] = set()
    for entry in trace or ():
        if not isinstance(entry, Mapping) or not _receipt_is_successful(entry):
            continue
        capabilities.update(_tool_capabilities(str(entry.get("name") or entry.get("tool_name") or "")))
    return frozenset(capabilities)


def evaluate_claims(
    text: str | None,
    trace: Iterable[Mapping[str, Any]] | None,
) -> ClaimDecision:
    """Determine whether completed action claims have matching evidence."""

    claims = detect_action_claims(text)
    if not claims:
        return ClaimDecision(allowed=True)

    capabilities = successful_capabilities(trace)
    unsupported = tuple(claim for claim in claims if claim.capability not in capabilities)
    return ClaimDecision(
        allowed=not unsupported,
        claims=claims,
        unsupported=unsupported,
    )


def build_claim_repair_prompt(decision: ClaimDecision) -> str:
    """Build one tool-free repair instruction for an unsupported response."""

    claims = ", ".join(repr(claim.text) for claim in decision.unsupported[:5])
    return (
        "SYSTEM EVIDENCE ENFORCEMENT: Your previous draft contained completed-action "
        "claims that are not supported by a successful tool receipt. Do not claim that "
        "those actions happened. Rewrite the final answer using only actions confirmed "
        "by the authoritative tool activity record. Do not call tools in this response. "
        "If the requested action cannot be verified, say so plainly. Unsupported claims: "
        f"{claims or 'unavailable'}"
    )


def safe_claim_fallback() -> str:
    """Return a deterministic response when repair still makes an unsupported claim."""

    return (
        "I couldn't verify that the claimed action completed from a successful tool "
        "receipt, so I won't claim that it happened."
    )


__all__ = [
    "ActionClaim",
    "ClaimDecision",
    "build_claim_repair_prompt",
    "detect_action_claims",
    "evaluate_claims",
    "safe_claim_fallback",
    "successful_capabilities",
]
