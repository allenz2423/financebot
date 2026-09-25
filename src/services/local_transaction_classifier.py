"""Closed-taxonomy local-LLM adjudication for uncertain transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Mapping, Sequence

import httpx

from src.core.state import ADVISOR_NUM_CTX, CLASSIFIER_MODEL, MODEL_KEEP_ALIVE, OLLAMA_URL
from src.services.transaction_taxonomy import TRANSACTION_CATEGORIES, category_is_valid


@dataclass(frozen=True)
class LocalClassification:
    transaction_row_id: str
    category: str | None
    status: str
    confidence: float = 0.0
    margin: float = 0.0
    merchant_identity_confidence: float = 0.0
    evidence_sufficient: bool = False
    reason: str | None = None
    source_ids: tuple[str, ...] = ()
    model: str | None = None
    error: str | None = None


def _bounded_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _prompt(items: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    system = """You are Delilah's transaction adjudicator. Return only valid JSON.
Merchant identity and category are separate. Search evidence may be weak or
irrelevant; never infer merchant identity from a payment processor token.
Choose only one of the supplied category_path values. If identity evidence is
insufficient, conflicting, or the category is not supported, abstain.
Required result format:
{"items":[{"transaction_row_id":"...","decision":"accept|abstain",
"category_path":"... or null","confidence":0.0,"margin":0.0,
"merchant_identity_confidence":0.0,"evidence_sufficient":false,
"source_ids":[],"reason":"..."}]}
"""
    payload = []
    for item in items:
        payload.append({
            "transaction_row_id": str(item.get("id") or item.get("transaction_row_id") or ""),
            "merchant": item.get("merchant"),
            "normalized_merchant": item.get("clean_merchant"),
            "amount": item.get("amount"),
            "date": item.get("date"),
            "account": item.get("account_used"),
            "kev_signals": item.get("kev_signals") or {},
            "identity_status": item.get("identity_status") or "NO_IDENTITY",
            "search_evidence": item.get("search_evidence") or [],
            "allowed_category_paths": item.get("allowed_category_paths") or list(TRANSACTION_CATEGORIES),
        })
    return system, json.dumps({"transactions": payload}, ensure_ascii=False, separators=(",", ":"))


def _parse(raw: str, items: Sequence[Mapping[str, Any]], model: str | None) -> list[LocalClassification]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [LocalClassification(str(item.get("id")), None, "invalid_response", model=model, error=str(exc)) for item in items]
    entries = parsed.get("items") if isinstance(parsed, Mapping) else None
    by_id = {str(entry.get("transaction_row_id")): entry for entry in entries or [] if isinstance(entry, Mapping)}
    output: list[LocalClassification] = []
    for item in items:
        row_id = str(item.get("id") or item.get("transaction_row_id") or "")
        entry = by_id.get(row_id)
        if not entry:
            output.append(LocalClassification(row_id, None, "invalid_response", model=model, error="missing transaction result"))
            continue
        category = entry.get("category_path")
        allowed = set(item.get("allowed_category_paths") or TRANSACTION_CATEGORIES)
        decision = str(entry.get("decision") or "abstain").lower()
        valid_category = isinstance(category, str) and category in allowed and category_is_valid(category)
        confidence = _bounded_float(entry.get("confidence"))
        margin = _bounded_float(entry.get("margin"))
        identity_confidence = _bounded_float(entry.get("merchant_identity_confidence"))
        evidence_sufficient = bool(entry.get("evidence_sufficient"))
        external_identity_ok = str(item.get("identity_status") or "NO_IDENTITY") in {"STRONG_IDENTITY", "CONFIRMED_IDENTITY"}
        accepted = (
            decision == "accept"
            and valid_category
            and evidence_sufficient
            and external_identity_ok
            and identity_confidence >= 0.80
        )
        if not external_identity_ok and decision == "accept":
            reason = "external merchant identity evidence is insufficient"
        else:
            reason = str(entry.get("reason") or "local model did not establish sufficient evidence")
        output.append(LocalClassification(
            row_id,
            category if valid_category else None,
            "accepted" if accepted else "abstained",
            confidence=confidence,
            margin=margin,
            merchant_identity_confidence=identity_confidence,
            evidence_sufficient=evidence_sufficient,
            reason=reason,
            source_ids=tuple(str(source) for source in entry.get("source_ids") or []),
            model=model,
        ))
    return output


async def classify_with_local_llm(
    items: Sequence[Mapping[str, Any]],
    *,
    timeout_seconds: float = 120.0,
    url: str | None = None,
    model: str | None = None,
) -> list[LocalClassification]:
    if not items:
        return []
    effective_url = url or os.getenv("OLLAMA_URL", OLLAMA_URL)
    effective_model = model or os.getenv("CLASSIFIER_MODEL", CLASSIFIER_MODEL)
    system, user = _prompt(items)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                effective_url,
                json={
                    "model": effective_model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "format": "json",
                    "stream": False,
                    "keep_alive": os.getenv("MODEL_KEEP_ALIVE", MODEL_KEEP_ALIVE),
                    "options": {"temperature": float(os.getenv("CLASSIFY_TEMPERATURE", "0.1")), "num_ctx": ADVISOR_NUM_CTX},
                    "think": False,
                },
            )
            response.raise_for_status()
            raw = str(response.json().get("message", {}).get("content", ""))
            return _parse(raw, items, effective_model)
    except Exception as exc:
        return [LocalClassification(str(item.get("id") or item.get("transaction_row_id") or ""), None, "error", model=effective_model, error=f"{type(exc).__name__}: {exc}") for item in items]


__all__ = ["LocalClassification", "classify_with_local_llm"]
