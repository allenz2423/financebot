"""Explicit prompt-context blocks with bounded metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ContextBlock:
    """One independently auditable piece of model context."""

    name: str
    content: str
    tier: str = "volatile"
    source: str = "runtime"
    authority: str = "derived"
    sensitivity: str = "internal"
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    token_cost: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tier not in {"stable", "context", "volatile"}:
            raise ValueError("ContextBlock.tier must be stable, context, or volatile")
        if not str(self.name or "").strip():
            raise ValueError("ContextBlock.name is required")
        if not isinstance(self.content, str):
            raise TypeError("ContextBlock.content must be text")


@dataclass(frozen=True)
class ContextBundle:
    """Ordered, bounded context collection used by a prompt builder."""

    blocks: tuple[ContextBlock, ...] = ()
    max_chars: int = 120_000

    @classmethod
    def from_blocks(
        cls, blocks: Iterable[ContextBlock], *, max_chars: int = 120_000
    ) -> "ContextBundle":
        return cls(tuple(blocks), max_chars=max(1, int(max_chars)))

    def for_tier(self, tier: str) -> tuple[ContextBlock, ...]:
        return tuple(block for block in self.blocks if block.tier == tier)

    def render(self) -> str:
        """Render blocks without exceeding the bundle character budget."""

        sections: list[str] = []
        remaining = self.max_chars
        # Stable context first, then durable context, then volatile facts.
        for tier in ("stable", "context", "volatile"):
            for block in self.for_tier(tier):
                header = f"[{block.tier.upper()}:{block.name}]"
                body = f"{header}\n{block.content.strip()}".strip()
                if not body:
                    continue
                separator_cost = 2 if sections else 0
                available = max(0, remaining - separator_cost)
                if len(body) > available:
                    marker = "\n[context truncated]"
                    if available > len(marker):
                        body = body[: available - len(marker)].rstrip() + marker
                    else:
                        body = body[:available]
                sections.append(body)
                remaining -= separator_cost + len(body)
                if remaining <= 0:
                    return "\n\n".join(sections)
        return "\n\n".join(sections)


__all__ = ["ContextBlock", "ContextBundle"]
