"""Kev typed-decision client and bounded policy helpers.

Kev is deliberately kept separate from the conversational provider path.  It
does not generate assistant messages or tool calls; it scores a controller-
supplied set of typed options and returns probabilities over those options.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import time
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Mapping

import httpx


class DecisionUnavailable(RuntimeError):
    """Kev could not produce a usable decision."""


class DecisionInvalidResponse(DecisionUnavailable):
    """Kev returned a response that violated the typed-decision contract."""


@dataclass(frozen=True)
class DecisionQuestion:
    type: str
    instructions: Any
    criteria: Any = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "instructions": self.instructions,
        }
        if self.criteria is not None:
            payload["criteria"] = self.criteria
        return payload


@dataclass(frozen=True)
class DecisionRequest:
    state: str | Mapping[str, Any]
    questions: Mapping[str, DecisionQuestion]
    turn_id: str = ""
    round_id: int = 0
    decision_kind: str = "general"

    def as_payload(self, model: str) -> dict[str, Any]:
        return {
            "state": self.state,
            "model": model,
            "questions": {
                name: question.as_payload()
                for name, question in self.questions.items()
            },
        }


@dataclass(frozen=True)
class DecisionAnswer:
    question_id: str
    type: str
    selected: str | bool | float | None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    confidence: float | None = None
    abstained: bool = False
    reason: str | None = None
    # Noul returns its positive probability in a scalar field rather than in
    # the categorical probability map.  Keep it explicit so callers do not
    # have to infer it from the boolean selected value.  It is appended after
    # the original fields to preserve positional-constructor compatibility.
    noul_probability: float | None = None

    @property
    def top_probability(self) -> float:
        if not self.probabilities:
            if self.noul_probability is not None:
                return max(self.noul_probability, 1.0 - self.noul_probability)
            return 0.0
        return max(float(value) for value in self.probabilities.values())

    @property
    def margin(self) -> float:
        if not self.probabilities and self.noul_probability is not None:
            return abs((2.0 * self.noul_probability) - 1.0)
        values = sorted(
            (float(value) for value in self.probabilities.values()),
            reverse=True,
        )
        return values[0] - values[1] if len(values) > 1 else values[0] if values else 0.0


@dataclass(frozen=True)
class DecisionResponse:
    provider: str
    mode: str
    model: str
    request_id: str
    answers: Mapping[str, DecisionAnswer]
    latency_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.answers)


class DecisionProvider:
    """Small HTTP client for a Jev-compatible `/v1/systemone` endpoint."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        local_url: str | None = None,
        cloud_url: str | None = None,
        cloud_api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.mode = (mode or os.getenv("KEV_MODE", "local")).strip().lower()
        if self.mode not in {"local", "cloud"}:
            raise ValueError("KEV_MODE must be 'local' or 'cloud'")
        self.local_url = (
            local_url
            or os.getenv("KEV_LOCAL_URL", "http://127.0.0.1:8009/v1/systemone")
        ).strip()
        self.cloud_url = (
            cloud_url
            or os.getenv("KEV_CLOUD_URL", "")
        ).strip()
        self.cloud_api_key = (
            cloud_api_key
            if cloud_api_key is not None
            else os.getenv("KEV_CLOUD_API_KEY", "")
        ).strip()
        # The sidecar exposes the loaded checkpoint through the stable
        # ``kev-latest`` API alias.  Keep that request alias separate from the
        # deployment/run identifier (which is pinned to Kev-4B locally).
        self.model = (model or os.getenv("KEV_MODEL", "kev-latest")).strip()
        self.timeout_seconds = max(
            0.25,
            float(timeout_seconds or os.getenv("KEV_TIMEOUT_SECONDS", "3")),
        )

    @property
    def url(self) -> str:
        if self.mode == "cloud":
            if not self.cloud_url:
                raise DecisionUnavailable("KEV_CLOUD_URL is not configured")
            return self.cloud_url
        return self.local_url

    @property
    def request_profile(self) -> str:
        return f"kev-{self.mode}"

    @property
    def models_url(self) -> str:
        """Return the sibling model-discovery endpoint for the configured URL."""
        parsed = urlsplit(self.url)
        path = parsed.path.rstrip("/")
        if path.endswith("/systemone"):
            path = path[: -len("/systemone")]
        return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/models", "", ""))

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        request_id = _request_id(request)
        started = time.perf_counter()
        headers = {"Content-Type": "application/json"}
        if self.mode == "cloud" and self.cloud_api_key:
            headers["Authorization"] = f"Bearer {self.cloud_api_key}"
        payload = request.as_payload(self.model)
        try:
            import uuid
            from src.agent.scheduler import provider_capacity, schedule_work

            async def _do_kev():
                async with provider_capacity("kev"):
                    async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                        response = await client.post(
                            self.url,
                            headers=headers,
                            json=payload,
                        )
                        response.raise_for_status()
                        return response.json()

            data = await schedule_work(
                f"kev_{uuid.uuid4().hex[:8]}", "system", "background", _do_kev,
                unit_type="inference", provider="kev"
            )
        except Exception as exc:
            raise DecisionUnavailable(
                f"Kev request failed: {type(exc).__name__}: {exc}"
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        answers = _parse_answers(data, request.questions)
        return DecisionResponse(
            provider="kev",
            mode=self.mode,
            model=str(data.get("model") or self.model),
            request_id=request_id,
            answers=answers,
            latency_ms=latency_ms,
        )

    async def health(self) -> bool:
        """Return whether the configured endpoint exposes a valid model API."""
        try:
            headers = {}
            if self.mode == "cloud" and self.cloud_api_key:
                headers["Authorization"] = f"Bearer {self.cloud_api_key}"
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(self.models_url, headers=headers)
                response.raise_for_status()
                data = response.json()
            return isinstance(data, dict) and isinstance(data.get("models"), list)
        except Exception:
            return False

    async def health_via_decision(self) -> bool:
        """Probe providers that do not implement the optional model endpoint."""
        request = DecisionRequest(
            state="health check",
            questions={
                "healthy": DecisionQuestion(
                    type="noul",
                    instructions="Is this a health check?",
                )
            },
            decision_kind="health",
        )
        try:
            response = await self.decide(request)
        except DecisionUnavailable:
            return False
        return response.ok


def _request_id(request: DecisionRequest) -> str:
    raw = json.dumps(
        {
            "turn_id": request.turn_id,
            "round_id": request.round_id,
            "decision_kind": request.decision_kind,
            "questions": sorted(request.questions),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"kev_{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _parse_answers(
    data: Any,
    questions: Mapping[str, DecisionQuestion],
) -> dict[str, DecisionAnswer]:
    if not isinstance(data, dict):
        raise DecisionInvalidResponse("Kev response must be a JSON object")
    raw_answers = data.get("answers")
    if not isinstance(raw_answers, dict):
        raise DecisionInvalidResponse("Kev response is missing answers")

    parsed: dict[str, DecisionAnswer] = {}
    for question_id, question in questions.items():
        raw = raw_answers.get(question_id)
        if not isinstance(raw, dict):
            raise DecisionInvalidResponse(
                f"Kev response is missing answer {question_id!r}"
            )
        probabilities = raw.get("probabilities") or {}
        if not isinstance(probabilities, dict):
            raise DecisionInvalidResponse(
                f"Kev probabilities for {question_id!r} are not an object"
            )
        normalized_probabilities: dict[str, float] = {}
        for key, value in probabilities.items():
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise DecisionInvalidResponse(
                    f"Kev probability {key!r} is not numeric"
                ) from exc
            if number < 0.0 or number > 1.0:
                raise DecisionInvalidResponse(
                    f"Kev probability {key!r} is outside [0, 1]"
                )
            normalized_probabilities[str(key)] = number

        selected: str | bool | float | None
        if question.type == "noul":
            try:
                noul_probability = float(raw.get("noul"))
            except (TypeError, ValueError) as exc:
                raise DecisionInvalidResponse(
                    f"Kev noul answer for {question_id!r} is not numeric"
                ) from exc
            if not math.isfinite(noul_probability) or not 0.0 <= noul_probability <= 1.0:
                raise DecisionInvalidResponse(
                    f"Kev noul answer for {question_id!r} is outside [0, 1]"
                )
            selected = bool(noul_probability >= 0.5)
        elif question.type == "score":
            selected = raw.get("score")
            try:
                selected = float(selected)
            except (TypeError, ValueError) as exc:
                raise DecisionInvalidResponse(
                    f"Kev score answer for {question_id!r} is not numeric"
                ) from exc
            if not math.isfinite(selected):
                raise DecisionInvalidResponse(
                    f"Kev score answer for {question_id!r} is not finite"
                )
            criteria = question.criteria if isinstance(question.criteria, list) else []
            if criteria and not 0.0 <= selected <= float(len(criteria) - 1):
                raise DecisionInvalidResponse(
                    f"Kev score answer for {question_id!r} is outside criteria"
                )
        else:
            selected = raw.get("choice")

        if question.type == "choice" and selected is not None:
            allowed = set((question.criteria or {}).keys())
            if allowed and str(selected) not in allowed:
                raise DecisionInvalidResponse(
                    f"Kev selected undeclared choice {selected!r}"
                )
            if allowed and probabilities and set(probabilities) != allowed:
                raise DecisionInvalidResponse(
                    f"Kev probabilities for {question_id!r} do not match criteria"
                )

        confidence = raw.get("confidence")
        try:
            confidence = None if confidence is None else float(confidence)
        except (TypeError, ValueError) as exc:
            raise DecisionInvalidResponse(
                f"Kev confidence for {question_id!r} is not numeric"
            ) from exc
        if confidence is not None and (
            not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0
        ):
            raise DecisionInvalidResponse(
                f"Kev confidence for {question_id!r} is outside [0, 1]"
            )

        parsed[question_id] = DecisionAnswer(
            question_id=question_id,
            type=question.type,
            selected=selected,
            probabilities=normalized_probabilities,
            noul_probability=(
                noul_probability if question.type == "noul" else None
            ),
            confidence=confidence,
        )
    return parsed


def threshold_for(decision_kind: str) -> tuple[float, float]:
    """Return initial conservative (top probability, margin) thresholds."""
    kind = str(decision_kind or "general").casefold()
    if kind in {"risk", "approval", "mutation_authorization"}:
        return 0.90, 0.20
    if kind in {"completion", "stop", "evidence"}:
        return 0.90, 0.20
    return 0.80, 0.15


def accepted_answer(
    answer: DecisionAnswer,
    *,
    decision_kind: str,
    allowed: set[str] | None = None,
) -> bool:
    """Apply a conservative confidence and option-set check."""
    if answer.abstained or answer.selected is None:
        return False
    if allowed is not None and str(answer.selected) not in allowed:
        return False
    minimum, margin = threshold_for(decision_kind)
    return answer.top_probability >= minimum and answer.margin >= margin


def tool_choice_request(
    *,
    state: str,
    candidates: list[str],
    turn_id: str,
    round_id: int,
    descriptions: Mapping[str, str] | None = None,
) -> DecisionRequest:
    options = list(dict.fromkeys(str(name) for name in candidates if str(name)))
    options.extend(name for name in ("none", "clarify") if name not in options)
    descriptions = descriptions or {}
    return DecisionRequest(
        state=state,
        questions={
            "tool": DecisionQuestion(
                type="choice",
                instructions="Which available capability best matches the current task?",
                criteria={
                    name: str(descriptions.get(name) or name.replace("_", " "))[:600]
                    for name in options
                },
            )
        },
        turn_id=turn_id,
        round_id=round_id,
        decision_kind="tool_routing",
    )


def kev_shadow_enabled() -> bool:
    return _truthy(os.getenv("KEV_SHADOW", "0"))


def kev_enforcing_enabled() -> bool:
    return _truthy(os.getenv("KEV_ENFORCING", "0"))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "DecisionAnswer",
    "DecisionInvalidResponse",
    "DecisionProvider",
    "DecisionQuestion",
    "DecisionRequest",
    "DecisionResponse",
    "DecisionUnavailable",
    "accepted_answer",
    "kev_enforcing_enabled",
    "kev_shadow_enabled",
    "threshold_for",
    "tool_choice_request",
]
