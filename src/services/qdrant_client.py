"""
Qdrant Vector Client and Sync Pipeline for Delilah Financial OS.
Provides dense vector embedding generation, collection management,
and vector indexing/search over financial snapshots, active world model claims,
and user goals.
"""

import os
import re
import json
import time
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)

# Shipped default for the local (Ollama) embedder. qwen3-embedding:0.6b is a
# 1024-dim, last-token-pooled instruction-tuned model; the legacy nomic-embed
# family remains supported for existing installs.
DEFAULT_LOCAL_EMBED_MODEL = "qwen3-embedding:0.6b"

def _get_embedding_backend() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return os.getenv("EMBEDDING_BACKEND", "local").strip().lower()


def _derived_embedding_vector_size(model: str, backend: str) -> Optional[int]:
    """Known model -> output dimension; unknown -> None (must be configured)."""
    if backend in ("cloud", "openai", "openrouter"):
        return 1536  # text-embedding-3-small (EMBEDDING_CLOUD_MODEL default)
    m = (model or "").lower()
    if "qwen3-embedding" in m:
        return 1024
    if "nomic-embed-text" in m:
        return 768
    return None


def _get_embedding_vector_size() -> int:
    """Embedding dimension, configured via EMBEDDING_VECTOR_SIZE (.env).

    Any positive integer is accepted — Qdrant dense vectors are created at
    exactly this size, so it must equal the chosen model's output dimension.
    When unset, the dimension is derived from the configured backend/model; a
    custom local model with an unknown dimension fails fast instead of
    silently creating a wrong-sized collection.
    """
    from dotenv import load_dotenv
    load_dotenv()
    env_size = os.getenv("EMBEDDING_VECTOR_SIZE")
    if env_size:
        try:
            size = int(str(env_size).strip())
        except (TypeError, ValueError):
            raise ValueError(
                f"EMBEDDING_VECTOR_SIZE={env_size!r} is not an integer; set it to "
                "the embedding model's output dimension (e.g. 1024 for "
                "qwen3-embedding:0.6b, 1536 for text-embedding-3-small)."
            ) from None
        if size <= 0:
            raise ValueError(f"EMBEDDING_VECTOR_SIZE={size} must be positive.")
        return size
    backend = _get_embedding_backend()
    local_model = os.getenv("EMBEDDING_LOCAL_MODEL", DEFAULT_LOCAL_EMBED_MODEL).strip()
    known = _derived_embedding_vector_size(local_model, backend)
    if known is not None:
        return known
    raise ValueError(
        f"Cannot infer the embedding dimension for local model {local_model!r}; "
        "set EMBEDDING_VECTOR_SIZE in .env to match the model's output, and "
        "ensure any existing Qdrant collection uses the same dimension."
    )

COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "delilah_financial_memory")
VECTOR_SIZE = _get_embedding_vector_size()

# Embeddings are brief bursts (~300 ms per page on GPU); never park the model
# in VRAM permanently. On an 8 GB card the advisor LLM (25B, partially
# offloaded) needs the scarce VRAM when an advisor turn runs, so the embedder
# borrows it for its burst and unloads after EMBED_KEEP_ALIVE. A short TTL
# means each research block amortizes one model reload across its batched
# embeds instead of permanently taxing the card.
EMBED_KEEP_ALIVE = os.getenv("EMBED_KEEP_ALIVE", "5m")

# Candidate-universe filter applied at the search boundary. The vector index
# is heterogeneous by design (claims, dossiers, financial snapshots, free-form
# memory text); live adversarial-battery runs showed non-renderable domains
# crowd claims out of the top-K window and pollute the relevance band anchor
# (MRR@10 0.280 -> 0.681 and 29.3 -> 0.0 distractors in band when filtered,
# with zero recall cost vs claim-only). Env-overridable for experiments;
# set to "none" (or empty) to search the mixed universe.
# web_search_result is retrievable by design: it holds only pages the advisor
# actually fetched/cited (never raw unused hits), so it complements claims
# with dated web research instead of crowding them.
_raw_retrieval_domains = os.getenv(
    "SEMANTIC_RETRIEVAL_DOMAINS",
    "world_model_claim,world_model_dossier,web_search_result",
).strip()
RENDERABLE_DOMAINS = (
    [] if _raw_retrieval_domains.lower() in ("", "none", "off", "mixed")
    else [d.strip() for d in _raw_retrieval_domains.split(",") if d.strip()]
)

# Query-side task instruction for instruction-tuned embedding models
# (Qwen3-Embedding). Applied ONLY to search queries, never to indexed
# documents, per the model's asymmetric training. Measured on this corpus
# (see /tmp/embed_bench.py): improves domain-content queries sharply while
# keeping possession-cluster retrieval usable. Env-overridable; set empty
# to disable.
QUERY_INSTRUCTION = os.getenv(
    "EMBEDDING_QUERY_INSTRUCTION",
    "Instruct: Given a user question about their life, possessions, finances "
    "and memories, retrieve the stored claims that answer it\nQuery: {q}",
).strip()


def _get_qdrant_url() -> str:
    candidates = [
        os.getenv("QDRANT_URL", "").rstrip("/"),
        "http://qdrant:6333",
        "http://localhost:6333",
        "http://172.24.0.7:6333",
        "http://127.0.0.1:6333",
    ]
    for c in candidates:
        if not c:
            continue
        try:
            with httpx.Client(timeout=0.5) as client:
                r = client.get(f"{c}/collections")
                if r.status_code == 200:
                    return c
        except Exception:
            continue
    return os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")

def _get_ollama_embed_url() -> str:
    candidates = [
        os.getenv("OLLAMA_EMBED_URL", "").rstrip("/"),
        "http://ollama:11434/api/embed",
        "http://localhost:11434/api/embed",
        "http://127.0.0.1:11434/api/embed",
    ]
    for c in candidates:
        if not c:
            continue
        try:
            with httpx.Client(timeout=0.5) as client:
                base = c.replace("/api/embed", "/api/tags")
                r = client.get(base)
                if r.status_code == 200:
                    return c
        except Exception:
            continue
    return "http://ollama:11434/api/embed"

def _get_openai_api_key() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return os.getenv("OPENAI_API_KEY", "").strip()

def _get_embeddings_endpoint() -> str:
    """Resolve the cloud embeddings HTTP endpoint.

    EMBEDDING_CLOUD_URL always wins. Otherwise it is derived from OPENAI_URL:
    OpenRouter and the native OpenAI host are recognized explicitly; any other
    OpenAI-compatible gateway has /chat/completions swapped for /embeddings (or
    the path is used as-is when it already ends in /embeddings).
    """
    override = os.getenv("EMBEDDING_CLOUD_URL", "").strip()
    if override:
        return override.rstrip("/")
    openai_url = os.getenv(
        "OPENAI_URL", "https://openrouter.ai/api/v1/chat/completions"
    ).strip()
    low = openai_url.lower()
    if "openrouter.ai" in low:
        return "https://openrouter.ai/api/v1/embeddings"
    if "api.openai.com" in low:
        return "https://api.openai.com/v1/embeddings"
    if "/chat/completions" in low:
        return openai_url.replace("/chat/completions", "/embeddings")
    return openai_url if low.endswith("/embeddings") else f"{openai_url.rstrip('/')}/embeddings"


async def get_embedding(text: str, instruction: Optional[str] = None) -> List[float]:
    """
    Generate dense embedding for text.
    Controlled by EMBEDDING_BACKEND in .env:
      - 'local' | 'ollama': Ollama (EMBEDDING_LOCAL_MODEL, default: qwen3-embedding:0.6b)
      - 'cloud' | 'openai' | 'openrouter': HTTP embeddings API
        (EMBEDDING_CLOUD_MODEL, default: text-embedding-3-small; endpoint from
        OPENAI_URL or EMBEDDING_CLOUD_URL)
      - 'auto': local first, cloud fallback when local is unreachable

    The vector dimension is EMBEDDING_VECTOR_SIZE (or derived from the model
    when unset) and must match the Qdrant collection.

    `instruction` is the query-side task instruction for instruction-tuned
    models (Qwen3-Embedding): queries carry it, documents never do.
    """
    vector_size = _get_embedding_vector_size()
    if not text or not text.strip():
        return [0.0] * vector_size

    backend = _get_embedding_backend()
    local_model = os.getenv("EMBEDDING_LOCAL_MODEL", DEFAULT_LOCAL_EMBED_MODEL).strip()
    cloud_model = os.getenv("EMBEDDING_CLOUD_MODEL", "text-embedding-3-small").strip()

    # Cloud explicitly requested
    if backend in ("cloud", "openai", "openrouter"):
        api_key = _get_openai_api_key()
        endpoint = _get_embeddings_endpoint()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": cloud_model,
            "input": text.strip()[:8000],
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Cloud Embedding API failed ({resp.status_code}): {resp.text}")
            data = resp.json()
            return data["data"][0]["embedding"]

    # Local Ollama requested (or auto fallback)
    ollama_url = _get_ollama_embed_url()
    embed_input = f"{instruction.format(q=text.strip()[:8000])}" if instruction else text.strip()[:8000]
    candidate_models = [local_model]
    if local_model == "nomic-embed-text-cpu":
        candidate_models.append("nomic-embed-text")

    for model_name in candidate_models:
        try:
            from src.agent.scheduler import provider_capacity

            async with provider_capacity("ollama"), httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    ollama_url,
                    json={"model": model_name, "input": embed_input, "keep_alive": EMBED_KEEP_ALIVE},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    embs = data.get("embeddings", [])
                    if embs:
                        return embs[0]
        except Exception as e:
            logger.debug(f"Local embedding via {model_name} failed: {e}")

    # If backend was 'local' and failed, attempt cloud fallback with warning
    if backend == "auto" or backend == "local":
        logger.warning("Local Ollama embedding failed; falling back to cloud endpoint")
        api_key = _get_openai_api_key()
        endpoint = _get_embeddings_endpoint()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": cloud_model,
            "input": text.strip()[:8000],
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data["data"][0]["embedding"]
            raise RuntimeError(f"Both local and cloud embedding generation failed: {resp.text}")

    return [0.0] * vector_size


# Section-aware indexing sends an array of section texts in ONE embed call
# (Ollama /api/embed accepts input arrays); measured flat ~35 ms/section on
# GPU versus ~20 ms + fixed overhead per separate call, and per-page bursts of
# 10-24 sections amortize the one-time model (re)load within EMBED_KEEP_ALIVE.
async def get_embeddings(
    texts: List[str], instruction: Optional[str] = None
) -> List[List[float]]:
    """Batch-embed many texts in one provider call; output order matches input.

    Sections are documents, so `instruction` is normally None (the query-side
    task instruction is never applied to indexed content). Falls back to
    sequential single embeds if the batch path fails or returns a mismatched
    count, so indexing never hard-fails on a provider quirk.
    """
    vector_size = _get_embedding_vector_size()
    out: List[List[float]] = [[0.0] * vector_size for _ in texts]
    todo: List[int] = [i for i, t in enumerate(texts) if t and t.strip()]
    if not todo:
        return out

    inputs = [texts[i].strip()[:8000] for i in todo]
    backend = _get_embedding_backend()
    local_model = os.getenv("EMBEDDING_LOCAL_MODEL", DEFAULT_LOCAL_EMBED_MODEL).strip()
    cloud_model = os.getenv("EMBEDDING_CLOUD_MODEL", "text-embedding-3-small").strip()

    if backend in ("cloud", "openai", "openrouter"):
        try:
            api_key = _get_openai_api_key()
            endpoint = _get_embeddings_endpoint()
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    endpoint,
                    json={"model": cloud_model, "input": inputs},
                    headers=headers,
                )
                if resp.status_code == 200:
                    rows = sorted(
                        resp.json().get("data", []), key=lambda d: d.get("index", 0)
                    )
                    if len(rows) == len(inputs):
                        for i, row in zip(todo, rows):
                            out[i] = row["embedding"]
                        return out
        except Exception as e:
            logger.debug(f"Batch cloud embedding failed: {e}")

    ollama_url = _get_ollama_embed_url()
    candidate_models = [local_model]
    if local_model == "nomic-embed-text-cpu":
        candidate_models.append("nomic-embed-text")
    for model_name in candidate_models:
        try:
            payload_input = inputs
            if instruction:
                payload_input = [instruction.format(q=t) for t in inputs]
            from src.agent.scheduler import provider_capacity

            async with provider_capacity("ollama"), httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    ollama_url,
                    json={
                        "model": model_name,
                        "input": payload_input,
                        "keep_alive": EMBED_KEEP_ALIVE,
                    },
                )
                if resp.status_code == 200:
                    rows = resp.json().get("embeddings", [])
                    if len(rows) == len(inputs):
                        for i, row in zip(todo, rows):
                            out[i] = row
                        return out
        except Exception as e:
            logger.debug(f"Local batch embedding via {model_name} failed: {e}")

    for i in todo:
        out[i] = await get_embedding(texts[i], instruction=instruction)
    return out


async def ensure_collection(collection_name: str = COLLECTION_NAME) -> bool:
    """Ensure the target Qdrant collection exists with cosine distance.

    Embeds/upserts/search all funnel through here, so an existing collection
    whose vector dimension differs from EMBEDDING_VECTOR_SIZE raises a
    RuntimeError with a fix hint: dense dimensions cannot be resized in place,
    and upserting wrong-sized vectors only fails later with opaque errors.
    The collection is created at VECTOR_SIZE when missing.
    """
    base_url = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Check if collection exists
        try:
            check_resp = await client.get(f"{base_url}/collections/{collection_name}")
            if check_resp.status_code == 200:
                cfg = (
                    check_resp.json()
                    .get("result", {})
                    .get("config", {})
                    .get("params", {})
                    or {}
                )
                vectors = cfg.get("vectors") or {}
                existing_size = vectors.get("size") if isinstance(vectors, dict) else None
                if existing_size is not None and int(existing_size) != VECTOR_SIZE:
                    raise RuntimeError(
                        f"Qdrant collection '{collection_name}' already exists with "
                        f"vector dimension {existing_size}, but "
                        f"EMBEDDING_VECTOR_SIZE={VECTOR_SIZE}. Dense vector dimensions "
                        "cannot be resized in place: either align EMBEDDING_VECTOR_SIZE "
                        "with the collection, or (after backing up) recreate the "
                        "collection at the intended dimension."
                    )
                return True
        except RuntimeError:
            raise
        except Exception:
            pass

        # Create collection
        create_payload = {
            "vectors": {
                "size": VECTOR_SIZE,
                "distance": "Cosine",
            }
        }
        create_resp = await client.put(f"{base_url}/collections/{collection_name}", json=create_payload)
        if create_resp.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create Qdrant collection {collection_name}: {create_resp.text}")
        return True


def _claim_embed_text(subject_id: str, predicate: str, value: Any) -> str:
    """
    Content-first embedding text for a KG claim. The old format embedded the
    schema string verbatim ("user:342385... owns: X (Authority 4/5)"), so every
    claim vector was dominated by the internal user-ID prefix and authority
    boilerplate and all predicates clustered together. Embed the semantic
    content instead: subject (namespace word kept — it is part of the data),
    predicate in plain words, then the value.
    """
    subj = str(subject_id or "")
    if subj.startswith("user:"):
        subj_part = ""  # internal anchor: the raw user ID carries no semantics
    else:
        subj_part = subj.replace(":", " ")
    parts = [
        p.strip()
        for p in (subj_part, str(predicate or "").replace("_", " "), str(value or ""))
        if p.strip()
    ]
    return " ".join(parts) if parts else str(value or "")


async def upsert_points(points: List[Dict[str, Any]], collection_name: str = COLLECTION_NAME) -> Dict[str, Any]:
    """Upsert vectors and payload into Qdrant."""
    await ensure_collection(collection_name)
    base_url = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.put(
            f"{base_url}/collections/{collection_name}/points?wait=true",
            json={"points": points},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to upsert points into Qdrant: {resp.text}")
        return resp.json()


_MAIL_CUE_RE = re.compile(
    r"\b(invoice|invoices|receipt|receipts|bill|billing|billed|order|orders|purchase|purchases|"
    r"subscription|subscriptions|refund|refunds|statement|statements|payment|payments|paid|"
    r"charge|charged|charges|transaction|transactions|shipping|delivery|shipped|renewal|renew|"
    r"cancellation|canceled|cancelled|overdraft|overdrawn|confirmed|confirmation|email|e-mail|"
    r"mail|gmail|inbox|newsletter|sender|spent|spend|spending|withdraw|deposit|fee|fees)\b",
    re.IGNORECASE,
)


def query_targets_mail(query: str) -> bool:
    """Deterministic gate: does this query ask about things email correspondence knows?

    Receipts, bills, orders, invoices, statements, renewals and payment
    confirmations live in mail, not in the claim KG — opening the gmail domain
    for those queries is what turns indexed mail into usable memory. The list
    is deliberately conservative: the claim-only universe stays untouched for
    every non-mail query (the benchmarked MRR@10 0.681 path).
    """
    return bool(_MAIL_CUE_RE.search(query or ""))


def retrieval_domains_for_query(query: str) -> Optional[List[str]]:
    """Candidate domain universe for one retrieval query.

    Returns None (use the default RENDERABLE_DOMAINS filter) except for
    mail-targeted queries, which also open ``gmail_message`` so indexed email
    sections join the candidate pool. GMAIL_RETRIEVAL_ENABLED=0 restores the
    claim-only universe for every query.
    """
    if os.getenv("GMAIL_RETRIEVAL_ENABLED", "1") == "0" or not query_targets_mail(query):
        return None
    domains = list(RENDERABLE_DOMAINS)
    domains.append("gmail_message")
    return domains


async def search_vectors(
    query_text: str,
    limit: int = 5,
    user_id: Optional[str] = None,
    collection_name: str = COLLECTION_NAME,
    domains: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Search Qdrant for semantic neighbors of query_text."""
    await ensure_collection(collection_name)
    base_url = _get_qdrant_url()
    query_vector = await get_embedding(query_text, instruction=QUERY_INSTRUCTION or None)

    search_payload: Dict[str, Any] = {
        "vector": query_vector,
        "limit": limit,
        "with_payload": True,
    }
    must_filters: List[Dict[str, Any]] = []
    if user_id:
        must_filters.append({"key": "user_id", "match": {"value": str(user_id)}})
    effective_domains = RENDERABLE_DOMAINS if domains is None else domains
    if effective_domains:
        must_filters.append({"key": "domain", "match": {"any": effective_domains}})
    if must_filters:
        search_payload["filter"] = {"must": must_filters}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{base_url}/collections/{collection_name}/points/search",
            json=search_payload,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Qdrant search failed: {resp.text}")
        results = resp.json().get("result", [])
        return [
            {
                "score": r.get("score"),
                "id": r.get("id"),
                "payload": r.get("payload"),
            }
            for r in results
        ]


async def index_user_financial_profile(user_id: str) -> Dict[str, Any]:
    """
    Extracts all verified Active World Model claims, financial position snapshots,
    goals, and health context for user_id and embeds them into Qdrant.
    """
    import sqlite3
    import uuid
    from src.core.state import DB_PATH
    from src.db.queries import get_current_financial_position, get_savings_buckets

    uid = str(user_id).strip()
    points = []

    # 1. Financial Snapshot Point
    position = get_current_financial_position(user_id=uid)
    buckets = get_savings_buckets(user_id=uid)
    snapshot_summary = (
        f"User {uid} Financial Position: Liquid checking/cash: ${position.get('total_liquid', 0):,.2f}. "
        f"Net Cash: {position.get('net_cash')}. Accounts: {position.get('all_balances')}. "
        f"Savings Buckets: {[b['name'] + ' target $' + str(b['target']) for b in buckets]}."
    )
    snap_emb = await get_embedding(snapshot_summary)
    snap_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{uid}_financial_snapshot"))
    points.append({
        "id": snap_id,
        "vector": snap_emb,
        "payload": {
            "user_id": uid,
            "domain": "financial_position",
            "text": snapshot_summary,
            "liquid_checking": position.get("total_liquid"),
            "net_cash": str(position.get("net_cash")),
            "type": "snapshot",
        }
    })

    # 2. Knowledge Graph Claims Points
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    c = conn.cursor()
    from src.services.world_model import (
        _claim_visibility_clause,
        _dossier_visibility_clause,
    )
    claim_visibility_sql, claim_visibility_params = _claim_visibility_clause(uid)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "SELECT claim_id, subject_id, predicate, object_id, scalar_value, source_authority "
        "FROM kg_claims WHERE tx_retracted_at IS NULL AND valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to > ?) AND "
        f"{claim_visibility_sql}",
        (now_utc, now_utc, *claim_visibility_params),
    )
    claims = c.fetchall()

    for row in claims:
        cid, subj, pred, obj_id, s_val, auth = row
        val = s_val or obj_id
        claim_text = _claim_embed_text(subj, pred, val)
        emb = await get_embedding(claim_text)
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"claim_{cid}"))
        points.append({
            "id": point_id,
            "vector": emb,
            "payload": {
                "user_id": uid,
                "claim_id": cid,
                "subject_id": subj,
                "predicate": pred,
                "value": str(val),
                "authority": auth,
                "domain": "world_model_claim",
                "text": claim_text,
            }
        })

    # 3. Dossiers follow the same owner/visibility policy as user-facing
    #    World Model reads; SQLite, not the vector payload, is authoritative.
    dossier_visibility_sql, dossier_visibility_params = _dossier_visibility_clause(uid)
    c.execute(
        "SELECT doc_id, primary_entity_id, title, content, tags FROM kg_dossiers "
        f"WHERE {dossier_visibility_sql}",
        dossier_visibility_params,
    )
    dossiers = c.fetchall()
    for row in dossiers:
        doc_id, ent_id, title, content, tags = row
        dossier_text = f"Dossier {title} [{tags}]: {content[:1000]}"
        emb = await get_embedding(dossier_text)
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"dossier_{doc_id}"))
        points.append({
            "id": point_id,
            "vector": emb,
            "payload": {
                "user_id": uid,
                "doc_id": doc_id,
                "title": title,
                "tags": tags,
                "domain": "world_model_dossier",
                "text": dossier_text,
            }
        })

    conn.close()

    if points:
        await upsert_points(points)

    return {
        "status": "INDEXED",
        "collection": COLLECTION_NAME,
        "points_indexed": len(points),
        "user_id": uid,
    }

async def index_single_claim(claim_id: str, subject_id: str, predicate: str, value: str, authority: int, user_id: str):
    """Real-time incremental vector indexing of a newly asserted claim."""
    try:
        claim_text = _claim_embed_text(subject_id, predicate, value)
        emb = await get_embedding(claim_text)
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"claim_{claim_id}"))
        await upsert_points([{
            "id": point_id,
            "vector": emb,
            "payload": {
                "user_id": str(user_id),
                "claim_id": str(claim_id),
                "subject_id": str(subject_id),
                "predicate": str(predicate),
                "value": str(value),
                "authority": authority,
                "domain": "world_model_claim",
                "text": claim_text,
            }
        }])
    except Exception as e:
        logger.warning(f"Failed to index single claim into Qdrant: {e}")


def _claim_point_id(claim_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"claim_{claim_id}"))


def _gmail_section_point_id(user_id: str, message_id: str, idx: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gmail_{user_id}_{message_id}_sec_{idx}"))


def _split_email_sections(
    subject: str,
    sender: str,
    date: str,
    body: str,
    max_chars: int = 1600,
) -> List[tuple[str, str]]:
    """Split an email into (section_name, section_text) pairs.

    The header (sender/date/subject) is its own section — subject text is the
    highest-signal part of most mail and deserves a dedicated vector. The body
    is then chunked on paragraph boundaries into ~max_chars pieces so one flat
    pooled vector never averages a long threaded/forwarded body into mush.
    """
    sections: List[tuple[str, str]] = []
    header = (
        f"Email from {sender or '?'} on {date or '?'}\n"
        f"Subject: {subject or '(no subject)'}"
    )
    sections.append(("Header", header))

    body = re.sub(r"\s+", " ", (body or "")).strip()
    if not body:
        return sections

    paras = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", body) if p.strip()]
    if not paras:
        paras = [body]

    part = 1
    cur: List[str] = []
    cur_len = 0
    for p in paras:
        # Hard-split any single over-long paragraph first.
        while len(p) > max_chars:
            if cur:
                sections.append((f"Body {part}", " ".join(cur)))
                part += 1
                cur = []
                cur_len = 0
            sections.append((f"Body {part}", p[:max_chars]))
            part += 1
            p = p[max_chars:]
        if cur_len and cur_len + len(p) > max_chars:
            sections.append((f"Body {part}", " ".join(cur)))
            part += 1
            cur = []
            cur_len = 0
        cur.append(p)
        cur_len += len(p)
    if cur:
        sections.append((f"Body {part}", " ".join(cur)))
    return sections


async def _delete_gmail_points_for_message(
    user_id: str, message_id: str, collection_name: str = COLLECTION_NAME
) -> int:
    """Delete every indexed point for one message (a prior flat vector and/or
    a section family) so a re-embed converges instead of stacking."""
    base_url = _get_qdrant_url()
    point_ids: List[str] = []
    offset: Optional[str] = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            payload: Dict[str, Any] = {
                "limit": 256,
                "with_payload": False,
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": str(user_id)}},
                        {"key": "message_id", "match": {"value": str(message_id)}},
                    ]
                },
            }
            if offset:
                payload["offset"] = offset
            resp = await client.post(
                f"{base_url}/collections/{collection_name}/points/scroll", json=payload
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Qdrant scroll failed ({resp.status_code}): {resp.text}")
            data = resp.json().get("result", {})
            point_ids += [p["id"] for p in data.get("points", [])]
            offset = data.get("next_page_offset")
            if offset is None:
                break
    return await _delete_point_ids(point_ids, collection_name)


async def index_gmail_message(
    user_id: str,
    message_id: str,
    subject: str,
    sender: str,
    date: str,
    body: str,
) -> bool:
    """Embed and index one archived email into the vector store.

    One vector per email section (header + body chunks) instead of a single
    flat whole-message vector, so a long threaded/forwarded mail retrieves on
    the exact part that matches a query rather than one pooled mush vector.
    Emails get domain "gmail_message", which is NOT in SEMANTIC_RETRIEVAL_DOMAINS
    by default — they are indexed (searchable, diag-nosed, future-proof) but do
    not crowd claim retrieval with marketing noise. Mail-targeted queries
    (receipts/bills/orders/statements) open the domain via the deterministic
    query gate in retrieval_domains_for_query(); set GMAIL_RETRIEVAL_ENABLED=0
    to keep it closed for everything.
    """
    try:
        sections = _split_email_sections(subject or "", sender or "", date or "", body or "")
        if not sections:
            return False
        embeds = await get_embeddings([t for _, t in sections])
        if len(embeds) != len(sections):
            embeds = [await get_embedding(t) for _, t in sections]

        points: List[Dict[str, Any]] = []
        for i, ((name, text), vec) in enumerate(zip(sections, embeds)):
            points.append({
                "id": _gmail_section_point_id(str(user_id), str(message_id), i),
                "vector": vec,
                "payload": {
                    "user_id": str(user_id),
                    "domain": "gmail_message",
                    "message_id": str(message_id),
                    "sender_email": (sender or "")[:500],
                    "subject": (subject or "")[:500],
                    "date": date or "",
                    "section_name": name,
                    "text": text[:12000],
                },
            })

        await _delete_gmail_points_for_message(str(user_id), str(message_id))
        await upsert_points(points)
        return True
    except Exception as e:
        logger.warning(f"Failed to index gmail message into Qdrant: {e}")
        return False


async def index_web_search_result(
    user_id: str,
    url: str,
    title: str = "",
    text: str = "",
    query: str = "",
) -> bool:
    """Embed a web page the advisor surfaced (search hit or fetched page) into
    the vector store.

    Domain "web_search_result" IS in SEMANTIC_RETRIEVAL_DOMAINS by default, so
    past research auto-injects into retrieval alongside world-model claims.
    Callers feed either top search hits (title+snippet, from search_web) or
    pages the advisor actually fetched — the point id is URL-keyed so a hit and
    a later fetch of the same page converge on one point (fetch text is richer
    and last-write-wins), and repeated searches never stack near-duplicates.
    """
    try:
        text = str(text or "")[:12000]
        if not text.strip():
            return False
        emb = await get_embedding(text)
        point_id = _web_result_point_id(str(url))
        await upsert_points([{
            "id": point_id,
            "vector": emb,
            "payload": {
                "user_id": str(user_id),
                "domain": "web_search_result",
                "url": str(url)[:2048],
                "title": (title or "")[:500],
                "query": (query or "")[:500],
                "text": text,
                "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }])
        return True
    except Exception as e:
        logger.warning(f"Failed to index web search result into Qdrant: {e}")
        return False


def _web_result_point_id(url: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"web_{url}"))


def _web_section_point_id(url: str, idx: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"web_{url}_sec_{idx}"))


async def _delete_web_points_for_url(
    user_id: str, url: str, collection_name: str = COLLECTION_NAME
) -> int:
    """Delete every indexed point for one URL (a flat whole-page vector and/or
    a prior section family) so a re-fetch converges instead of stacking."""
    base_url = _get_qdrant_url()
    point_ids: List[str] = []
    offset: Optional[str] = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            payload: Dict[str, Any] = {
                "limit": 256,
                "with_payload": False,
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": str(user_id)}},
                        {"key": "url", "match": {"value": str(url)}},
                    ]
                },
            }
            if offset:
                payload["offset"] = offset
            resp = await client.post(
                f"{base_url}/collections/{collection_name}/points/scroll", json=payload
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Qdrant scroll failed ({resp.status_code}): {resp.text}")
            data = resp.json().get("result", {})
            point_ids += [p["id"] for p in data.get("points", [])]
            offset = data.get("next_page_offset")
            if offset is None:
                break
    return await _delete_point_ids(point_ids, collection_name)


async def index_web_sections(
    user_id: str,
    url: str,
    title: str = "",
    sections: Optional[List[Dict[str, str]]] = None,
    query: str = "",
) -> int:
    """Section-aware index of a fetched web page (doc-side mush fix).

    Replaces the single flat whole-page vector with one vector per section.
    Every section point carries the same URL payload — all of them "point to
    the same doc" — and section_name records which meaning matched, so
    retrieval lands on the exact part of the page (drug Interactions vs
    Dosage) instead of one pooled mush vector. Any existing points for the
    URL (flat or stale section family) are deleted first. Embeddings are
    batched into a single provider call.
    """
    try:
        clean: List[tuple[str, str]] = []
        for sec in (sections or []):
            name = re.sub(r"\s+", " ", str(sec.get("name", "") or "")).strip()[:200]
            text = str(sec.get("text", "") or "").strip()[:12000]
            if not name or not text:
                continue
            clean.append((name, text))
        if not clean:
            return 0

        embeds = await get_embeddings([t for _, t in clean])
        if len(embeds) != len(clean):
            embeds = [await get_embedding(t) for _, t in clean]

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        points: List[Dict[str, Any]] = []
        for i, ((name, text), vec) in enumerate(zip(clean, embeds)):
            points.append({
                "id": _web_section_point_id(str(url), i),
                "vector": vec,
                "payload": {
                    "user_id": str(user_id),
                    "domain": "web_search_result",
                    "url": str(url)[:2048],
                    "title": (title or "")[:500],
                    "section_name": name,
                    "query": (query or "")[:500],
                    "text": text,
                    "fetched_at": now,
                },
            })

        await _delete_web_points_for_url(str(user_id), str(url))
        await upsert_points(points)
        logger.info(f"Indexed {len(points)} sections for web page {url}")
        return len(points)
    except Exception as e:
        logger.warning(f"Failed to section-index web page into Qdrant: {e}")
        return 0


async def delete_claim_point(claim_id: str, collection_name: str = COLLECTION_NAME) -> bool:
    """Remove a retracted claim's vector point so it stays consistent with SQLite."""
    point_id = _claim_point_id(claim_id)
    base_url = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{base_url}/collections/{collection_name}/points/{point_id}?wait=true"
        )
        if resp.status_code not in (200, 404):
            raise RuntimeError(f"Qdrant point delete failed ({resp.status_code}): {resp.text}")
        return resp.status_code == 200


async def _scroll_user_point_ids(user_id: str, collection_name: str = COLLECTION_NAME) -> List[Dict[str, Any]]:
    """Scroll all points for a user, returning [{id, claim_id, domain}]."""
    base_url = _get_qdrant_url()
    points: List[Dict[str, Any]] = []
    offset: Optional[str] = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            payload: Dict[str, Any] = {
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
                # Second branch picks up orphan claim points (empty payload.user_id)
                # left by the pre-owner-attribution indexing bug, so drift/reconcile
                # can repair or delete them instead of ignoring them forever.
                "filter": {
                    "should": [
                        {"key": "user_id", "match": {"value": str(user_id)}},
                        {
                            "must": [
                                {"key": "user_id", "match": {"value": ""}},
                                {"key": "domain", "match": {"value": "world_model_claim"}},
                            ]
                        },
                    ],
                },
            }
            if offset:
                payload["offset"] = offset
            resp = await client.post(f"{base_url}/collections/{collection_name}/points/scroll", json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Qdrant scroll failed ({resp.status_code}): {resp.text}")
            data = resp.json().get("result", {})
            for p in data.get("points", []):
                pl = p.get("payload") or {}
                points.append({
                    "id": p.get("id"),
                    "claim_id": pl.get("claim_id"),
                    "domain": pl.get("domain"),
                    "user_id": pl.get("user_id"),
                })
            offset = data.get("next_page_offset")
            if offset is None:
                break
    return points


async def count_user_points(user_id: str, collection_name: str = COLLECTION_NAME) -> int:
    """Count Qdrant points indexed for a user (for drift diagnostics)."""
    base_url = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{base_url}/collections/{collection_name}/points/count",
            json={
                "exact": True,
                "filter": {"must": [{"key": "user_id", "match": {"value": str(user_id)}}]},
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Qdrant count failed ({resp.status_code}): {resp.text}")
        return int(resp.json().get("result", {}).get("count", 0))


async def collection_status(collection_name: str = COLLECTION_NAME) -> Dict[str, Any]:
    """Collection health snapshot: exists, point count, vector size, distance."""
    base_url = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{base_url}/collections/{collection_name}")
        if resp.status_code != 200:
            return {"exists": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json().get("result", {})
        vectors = (data.get("config", {}).get("params", {}) or {}).get("vectors", {}) or {}
        return {
            "exists": True,
            "points_count": data.get("points_count"),
            "vector_size": vectors.get("size"),
            "distance": vectors.get("distance"),
        }


async def embedding_status(probe_text: str = "delilah vector probe") -> Dict[str, Any]:
    """Probe the embedding pipeline end to end: backend, model, latency, dimension sanity."""
    backend = _get_embedding_backend()
    local_model = os.getenv("EMBEDDING_LOCAL_MODEL", DEFAULT_LOCAL_EMBED_MODEL).strip()
    started = time.monotonic()
    try:
        vec = await get_embedding(probe_text)
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": True,
            "backend": backend,
            "local_model": local_model,
            "expected_size": VECTOR_SIZE,
            "returned_size": len(vec),
            "zero_vector": all(v == 0.0 for v in vec),
            "latency_ms": latency_ms,
        }
    except Exception as e:
        return {
            "ok": False,
            "backend": backend,
            "local_model": local_model,
            "expected_size": VECTOR_SIZE,
            "error": f"{type(e).__name__}: {e}",
        }


async def user_vector_drift(user_id: str, collection_name: str = COLLECTION_NAME) -> Dict[str, Any]:
    """
    Compare SQLite active claims against Qdrant indexed points for one user.
    missing = active in SQLite but not vector-indexed; stale = indexed but retracted.
    Claims whose subject_id does not reference the user (e.g. "perfume:X...")
    are attributed via the indexed point's payload.user_id (set by assert_claim's
    owner_user_id), so they count as active for the owning user.
    """
    import sqlite3
    from src.core.state import DB_PATH

    uid = str(user_id).strip()
    indexed_points = await _scroll_user_point_ids(uid, collection_name)
    # Only properly attributed points count as "indexed"; orphan points
    # (empty payload.user_id from the pre-owner-attribution bug) are invisible
    # to user-scoped search, so their claims must surface as missing.
    indexed_map = {
        p["claim_id"]: p["id"]
        for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id")
        and (p.get("user_id") or "").strip() == uid
    }
    # Points with an empty payload.user_id are orphaned (pre-owner-attribution
    # indexing bug); they are invisible to user-scoped search — treat them as missing.
    orphan_point_ids = [
        p["id"] for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id") and not (p.get("user_id") or "").strip()
    ]

    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    c = conn.cursor()
    c.execute(
        "SELECT claim_id FROM kg_claims "
        "WHERE tx_retracted_at IS NULL AND valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to > ?) "
        "AND (subject_id = ? OR subject_id LIKE ?)",
        (now_utc, now_utc, f"user:{uid}", f"%{uid}%"),
    )
    active_ids = {row[0] for row in c.fetchall()}

    # Claims attributed to this user via their point payload (non-user subjects).
    attributed_ids = {
        p["claim_id"] for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id")
        and (p.get("user_id") or "").strip() == uid
    }
    conn.close()

    return {
        "user_id": uid,
        "active_claims": len(active_ids),
        "indexed_claims": len(indexed_map),
        "other_points": sum(1 for p in indexed_points if p.get("domain") != "world_model_claim"),
        "orphaned_points": orphan_point_ids,
        "missing": sorted(active_ids - set(indexed_map)),
        "stale": sorted(set(indexed_map) - active_ids - attributed_ids),
    }


async def _delete_point_ids(point_ids: List[str], collection_name: str = COLLECTION_NAME) -> int:
    """Batch-delete point ids; returns how many were requested."""
    if not point_ids:
        return 0
    base_url = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/collections/{collection_name}/points/delete?wait=true",
            json={"points": point_ids},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Qdrant batch delete failed ({resp.status_code}): {resp.text}")
    return len(point_ids)


async def reconcile_user_vectors(user_id: str, collection_name: str = COLLECTION_NAME) -> Dict[str, Any]:
    """
    Converge Qdrant with SQLite ground truth for one user:
    - Delete vector points for claims that are no longer active.
    - Re-index active claims that are missing from the vector store.
    The financial snapshot point is left alone; index_user_financial_profile
    refreshes it deterministically on demand.
    """
    import sqlite3
    from src.core.state import DB_PATH

    uid = str(user_id).strip()
    indexed_points = await _scroll_user_point_ids(uid, collection_name)
    indexed_claims = {
        p["claim_id"] for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id")
    }
    orphan_claim_ids = {
        p["claim_id"] for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id")
        and not (p.get("user_id") or "").strip()
    }
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    c = conn.cursor()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    from src.services.world_model import _claim_visibility_clause
    claim_visibility_sql, claim_visibility_params = _claim_visibility_clause(uid)
    c.execute(
        "SELECT claim_id, subject_id, predicate, object_id, scalar_value, source_authority "
        "FROM kg_claims WHERE tx_retracted_at IS NULL AND valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to > ?) "
        f"AND {claim_visibility_sql}",
        (now_utc, now_utc, *claim_visibility_params),
    )
    active_rows = c.fetchall()

    # Adopt legacy orphan points only if the authoritative row is visible to
    # this owner under the exact same policy as normal reads.
    orphan_active: Dict[str, tuple] = {}
    if orphan_claim_ids:
        placeholders = ",".join("?" for _ in orphan_claim_ids)
        c.execute(
            f"SELECT claim_id, subject_id, predicate, object_id, scalar_value, source_authority "
            f"FROM kg_claims WHERE claim_id IN ({placeholders}) "
            f"AND tx_retracted_at IS NULL AND valid_from <= ? "
            f"AND (valid_to IS NULL OR valid_to > ?) AND {claim_visibility_sql}",
            (*orphan_claim_ids, now_utc, now_utc, *claim_visibility_params),
        )
        for row in c.fetchall():
            orphan_active[row[0]] = row
    conn.close()

    active_ids = {row[0] for row in active_rows}
    stale_ids = [
        p["id"] for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id")
        and p["claim_id"] not in active_ids
        and p["claim_id"] not in orphan_active
    ]

    deleted = 0
    if stale_ids:
        deleted = await _delete_point_ids(stale_ids, collection_name)

    reindexed = 0
    for claim_id, subj, pred, obj_id, s_val, auth in active_rows:
        if claim_id in indexed_claims and claim_id not in orphan_claim_ids:
            continue
        await index_single_claim(claim_id, subj, pred, str(s_val or obj_id or ""), auth, uid)
        reindexed += 1

    # Orphan points on active non-user-subject claims: overwrite with correct
    # owner attribution and content-first embedding text.
    for claim_id, (cid, subj, pred, obj_id, s_val, auth) in orphan_active.items():
        if cid in active_ids:
            continue  # already re-indexed by the user-subject loop above
        await index_single_claim(cid, subj, pred, str(s_val or obj_id or ""), auth, uid)
        reindexed += 1

    return {
        "status": "RECONCILED",
        "user_id": uid,
        "active_claims": len(active_ids),
        "indexed_before": len(indexed_claims),
        "stale_deleted": deleted,
        "reindexed": reindexed,
    }


async def vector_reconcile_loop(interval_seconds: Optional[int] = None):
    """
    Periodic reconciliation across every user with active claims.
    Runs once immediately, then on VECTOR_RECONCILE_INTERVAL_SECONDS (default 6h).
    """
    import asyncio
    import sqlite3
    from src.core.state import DB_PATH

    if interval_seconds is None:
        try:
            interval_seconds = int(os.getenv("VECTOR_RECONCILE_INTERVAL_SECONDS", "21600"))
        except ValueError:
            interval_seconds = 21600

    # Give Ollama/Qdrant time to finish booting before the first pass.
    await asyncio.sleep(45)

    while True:
        try:
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(DB_PATH, timeout=10.0)
            c = conn.cursor()
            c.execute(
                "SELECT DISTINCT owner_user_id FROM kg_claims "
                "WHERE tx_retracted_at IS NULL AND valid_from <= ? "
                "AND (valid_to IS NULL OR valid_to > ?) AND owner_user_id IS NOT NULL "
                "AND visibility = 'private' "
                "UNION SELECT DISTINCT substr(subject_id, 6) FROM kg_claims "
                "WHERE tx_retracted_at IS NULL AND valid_from <= ? "
                "AND (valid_to IS NULL OR valid_to > ?) AND owner_user_id IS NULL "
                "AND visibility = 'legacy_private' AND subject_id LIKE 'user:%'",
                (now_utc, now_utc, now_utc, now_utc),
            )
            users = [str(row[0]).strip() for row in c.fetchall() if row[0] and str(row[0]).strip()]
            conn.close()
            for uid in users:
                try:
                    result = await reconcile_user_vectors(uid)
                    if result.get("stale_deleted") or result.get("reindexed"):
                        print(
                            f" [VECTOR RECONCILE] uid={uid} deleted={result['stale_deleted']} "
                            f"reindexed={result['reindexed']} active={result['active_claims']}"
                        )
                except Exception as e:
                    print(f" [VECTOR RECONCILE FAILED] uid={uid}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f" [VECTOR RECONCILE FAILED] user discovery error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)
