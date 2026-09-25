"""Evidence and provenance primitives for transaction classification.

This module deliberately contains no model or database writes.  It converts a
statement descriptor into a shared identity representation and scores search
results for merchant identity.  Category classifiers may consume the result,
but a processor token can never establish merchant identity by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


PROCESSOR_TOKENS = frozenset({
    "aplpay", "applepay", "paypal", "square", "stripe", "venmo",
    "cashapp", "cash", "clover", "tst", "sq", "sp", "pos", "payment",
    "pmt", "debit", "credit", "googlepay", "adyen", "shopify",
})

GENERIC_IDENTITY_TOKENS = frozenset({
    "the", "and", "are", "we", "store", "shop", "official", "website",
    "online", "market", "retail", "purchase", "transaction", "inc", "llc",
    "corp", "company", "brooklyn", "ny", "nyc", "usa", "us",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True)
class MerchantDescriptor:
    raw: str
    processor_tokens: tuple[str, ...]
    merchant_candidate: str
    merchant_tokens: tuple[str, ...]
    location_tokens: tuple[str, ...] = ()
    reference_tokens: tuple[str, ...] = ()
    normalization_version: str = "merchant-v2"


@dataclass(frozen=True)
class SearchEvidence:
    identity: str
    accepted: bool
    reasons: tuple[str, ...] = ()
    results: tuple[Mapping[str, Any], ...] = ()
    identity_tokens: tuple[str, ...] = ()
    processor_tokens: tuple[str, ...] = ()
    score: float = 0.0

    @property
    def usable_for_identity(self) -> bool:
        return self.identity in {"STRONG_IDENTITY", "CONFIRMED_IDENTITY"} and self.accepted


def _tokens(value: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(str(value or ""))]


def parse_merchant_descriptor(raw: str) -> MerchantDescriptor:
    # Normalize punctuation to token boundaries. This prevents a rail marker
    # such as ``AplPay`` or ``SQ*`` from being glued to a real merchant token,
    # while preserving the original descriptor for audit/debug purposes.
    raw_text = re.sub(r"\s+", " ", str(raw or "")).strip()
    tokens = _tokens(raw_text)
    processors: list[str] = []
    remaining = tokens[:]
    while remaining:
        marker = remaining[0]
        # These words are legitimate merchant identity tokens by themselves
        # (Google One, Apple Store). Only the anchored payment-rail phrase is
        # processor metadata.
        if marker in {"google", "apple"} and len(remaining) > 1 and remaining[1] == "pay":
            processors.extend((remaining.pop(0), remaining.pop(0)))
            continue
        if marker in PROCESSOR_TOKENS:
            processors.append(remaining.pop(0))
            continue
        break
    # A processor marker may be attached to the first token, e.g. SQ*FOO.
    if not processors and remaining and remaining[0].startswith("sq") and len(remaining[0]) > 2:
        processors.append("sq")
        remaining[0] = remaining[0][2:]
    merchant_tokens = [
        token for token in remaining
        if token not in PROCESSOR_TOKENS and token not in {"brooklyn", "manhattan", "nyc", "ny"}
    ]
    candidate = " ".join(merchant_tokens).strip()
    return MerchantDescriptor(
        raw=raw_text,
        processor_tokens=tuple(processors),
        merchant_candidate=candidate,
        merchant_tokens=tuple(merchant_tokens),
        location_tokens=tuple(token for token in remaining if token in {"brooklyn", "manhattan", "nyc", "ny"}),
        reference_tokens=tuple(token for token in remaining if token.isdigit()),
    )


def merchant_identity_tokens(descriptor: MerchantDescriptor) -> tuple[str, ...]:
    return tuple(
        token for token in descriptor.merchant_tokens
        if len(token) >= 3 and token not in GENERIC_IDENTITY_TOKENS
    )


def build_identity_queries(descriptor: MerchantDescriptor) -> tuple[dict[str, Any], ...]:
    candidate = descriptor.merchant_candidate.strip()
    if not candidate:
        return ()
    return (
        {"query": f'"{candidate}"', "kind": "exact_identity"},
        {"query": candidate, "kind": "candidate_identity"},
    )


def _evidence_text(result: Mapping[str, Any]) -> str:
    return " ".join(
        str(result.get(key) or "") for key in ("title", "snippet", "url", "content")
    ).lower()


def _normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _first_party_match(descriptor: MerchantDescriptor, result: Mapping[str, Any]) -> bool:
    candidate_tokens = set(merchant_identity_tokens(descriptor))
    host = (urlparse(str(result.get("url") or "")).hostname or "").lower()
    if not candidate_tokens or not host:
        return False
    host_text = re.sub(r"[^a-z0-9]+", " ", host)
    return len(candidate_tokens & set(host_text.split())) >= max(1, min(2, len(candidate_tokens)))


def evaluate_merchant_search(
    descriptor: MerchantDescriptor,
    results: Sequence[Mapping[str, Any]] | None,
) -> SearchEvidence:
    identity_tokens = merchant_identity_tokens(descriptor)
    processor_tokens = tuple(descriptor.processor_tokens)
    if not identity_tokens:
        return SearchEvidence(
            identity="NO_IDENTITY",
            accepted=False,
            reasons=("generic_or_processor_only_candidate",),
            identity_tokens=identity_tokens,
            processor_tokens=processor_tokens,
        )
    usable: list[Mapping[str, Any]] = []
    reasons: list[str] = []
    best_score = 0.0
    candidate_phrase = _normalized_text(descriptor.merchant_candidate)
    for result in results or ():
        text = _normalized_text(_evidence_text(result))
        title = _normalized_text(str(result.get("title") or ""))
        url = _normalized_text(str(result.get("url") or ""))
        token_hits = sum(1 for token in identity_tokens if re.search(rf"\b{re.escape(token)}\b", text))
        phrase_hit = bool(candidate_phrase and candidate_phrase in text)
        title_phrase = bool(candidate_phrase and candidate_phrase in title)
        first_party = _first_party_match(descriptor, result)
        processor_only = bool(processor_tokens) and token_hits == 0 and any(
            re.search(rf"\b{re.escape(token)}\b", text) for token in processor_tokens
        )
        score = (10.0 if title_phrase else 0.0) + (8.0 if phrase_hit else 0.0)
        score += min(5.0, float(max(0, token_hits - 1) * 2))
        score += 10.0 if first_party else 0.0
        if processor_only:
            score -= 20.0
        if score >= 8.0 and not processor_only:
            usable.append(result)
            best_score = max(best_score, score)

    if not usable:
        if results:
            if processor_tokens and all(
                any(re.search(rf"\b{re.escape(token)}\b", _normalized_text(_evidence_text(result))) for token in processor_tokens)
                for result in results
            ):
                reasons.append("processor_only_match")
            else:
                reasons.append("no_identity_phrase_or_strong_token_match")
        else:
            reasons.append("no_results")
        return SearchEvidence(
            identity="NO_IDENTITY",
            accepted=False,
            reasons=tuple(reasons),
            identity_tokens=identity_tokens,
            processor_tokens=processor_tokens,
        )

    identity = "STRONG_IDENTITY" if len(usable) == 1 else "STRONG_IDENTITY"
    return SearchEvidence(
        identity=identity,
        accepted=True,
        reasons=("merchant_identity_match",),
        results=tuple(usable),
        identity_tokens=identity_tokens,
        processor_tokens=processor_tokens,
        score=best_score,
    )


def registry_trust_tier(source: str | None, matched_by: str | None = None) -> str:
    value = str(source or "").strip().lower()
    if value in {"human_confirmed", "user_alias", "curated", "user"}:
        return "TRUSTED"
    if value in {"web_research", "research", "model_proposed"}:
        return "SUGGESTED"
    if value in {"auto_cache", "imported", "model", "heuristic_cleaner"}:
        return "UNTRUSTED"
    if str(matched_by or "").startswith("heuristic"):
        return "UNTRUSTED"
    return "UNTRUSTED"


__all__ = [
    "MerchantDescriptor", "SearchEvidence", "PROCESSOR_TOKENS",
    "parse_merchant_descriptor", "merchant_identity_tokens",
    "build_identity_queries", "evaluate_merchant_search", "registry_trust_tier",
]
