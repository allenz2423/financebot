"""Compact typed result envelopes for advisor tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolResultEnvelope:
    schema_version: int
    tool_name: str
    call_id: str
    receipt_id: str | None
    ok: bool
    complete: bool
    status: str
    summary: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    side_effect: str = "none"
    evidence_refs: tuple[str, ...] = ()
    # Internal compatibility bridge; never included in model-facing or trace
    # serialization. Legacy callers can retain rich payloads while the
    # runtime standardizes status/facts/provenance around them.
    raw_result: Any = None

    @classmethod
    def from_runtime(
        cls,
        *,
        tool_name: str,
        call_id: str,
        receipt_id: str | None,
        ok: bool,
        complete: bool,
        status: str,
        summary: str,
        error: str = "",
        facts: Mapping[str, Any] | None = None,
        side_effect: str = "none",
        raw_result: Any = None,
    ) -> "ToolResultEnvelope":
        status = str(status or "failed").casefold()
        if status not in {"confirmed", "partial", "failed", "unknown"}:
            status = "failed" if not ok else "confirmed"
        return cls(
            schema_version=1,
            tool_name=str(tool_name or "unknown_tool"),
            call_id=str(call_id or ""),
            receipt_id=receipt_id,
            ok=bool(ok),
            complete=bool(complete),
            status=status,
            summary=str(summary or "")[:4000],
            facts=dict(facts or {}),
            provenance={
                "source": "tool_execution",
                "receipt_id": receipt_id,
            },
            error=str(error or "")[:1000],
            side_effect=str(side_effect or "none"),
            evidence_refs=tuple(
                ref for ref in (receipt_id,) if ref
            ),
            raw_result=raw_result,
        )

    def model_content(self) -> str:
        """Return compact model-facing text without dumping the envelope."""

        if self.status == "unknown":
            return f"UNKNOWN: {self.summary}".strip()
        if self.status == "failed":
            return self.error or self.summary or "Tool execution failed."
        return self.summary

    def trace_fields(self) -> dict[str, Any]:
        """Return bounded metadata suitable for turn_tool_trace."""

        return {
            "result_status": self.status,
            "complete": self.complete,
            "facts": dict(self.facts),
            "provenance": dict(self.provenance),
            "evidence_refs": list(self.evidence_refs),
            "side_effect": self.side_effect,
        }


__all__ = ["ToolResultEnvelope"]
