"""Progress-based safeguards for repeated or deadlocked tool work."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque


@dataclass(frozen=True)
class ProgressDecision:
    action: str
    reason: str


@dataclass
class ProgressPolicy:
    repeat_threshold: int = 3
    unknown_threshold: int = 2
    history_size: int = 12
    _fingerprints: deque[str] = field(init=False, repr=False)
    _unknown_effects: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._fingerprints = deque(maxlen=max(1, int(self.history_size)))

    def observe(
        self,
        *,
        fingerprint: str,
        progressed: bool,
        status: str = "confirmed",
    ) -> ProgressDecision:
        status = str(status).casefold()
        if status == "unknown":
            self._unknown_effects += 1
        elif progressed:
            self._unknown_effects = 0

        repeated = self._fingerprints.count(str(fingerprint)) + 1
        self._fingerprints.append(str(fingerprint))
        if self._unknown_effects >= self.unknown_threshold:
            return ProgressDecision("stop", "repeated unknown external outcomes")
        if repeated >= self.repeat_threshold and not progressed:
            return ProgressDecision("repair", "repeated identical work without progress")
        return ProgressDecision("continue", "progress policy permits another step")


__all__ = ["ProgressDecision", "ProgressPolicy"]
