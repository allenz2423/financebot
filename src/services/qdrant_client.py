"""
Qdrant Vector Client and Sync Pipeline for Delilah Financial OS.
Provides dense vector embedding generation, collection management,
and vector indexing/search over financial snapshots, active world model claims,
and user goals.
"""

import os
import json
import time
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)

def _get_embedding_backend() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return os.getenv("EMBEDDING_BACKEND", "local").strip().lower()

def _get_embedding_vector_size() -> int:
    from dotenv import load_dotenv
    load_dotenv()
    env_size = os.getenv("EMBEDDING_VECTOR_SIZE")
    if env_size:
        return int(env_size)
    # Default to 768 for local nomic-embed-text, 1536 for cloud text-embedding-3-small
    backend = _get_embedding_backend()
    return 1536 if backend in ("cloud", "openai", "openrouter") else 768

COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "delilah_financial_memory")
VECTOR_SIZE = _get_embedding_vector_size()

# Candidate-universe filter applied at the search boundary. The vector index
# is heterogeneous by design (claims, dossiers, financial snapshots, free-form
# memory text); live adversarial-battery runs showed non-renderable domains
# crowd claims out of the top-K window and pollute the relevance band anchor
# (MRR@10 0.280 -> 0.681 and 29.3 -> 0.0 distractors in band when filtered,
# with zero recall cost vs claim-only). Env-overridable for experiments;
# set to "none" (or empty) to search the mixed universe.
_raw_retrieval_domains = os.getenv(
    "SEMANTIC_RETRIEVAL_DOMAINS",
    "world_model_claim,world_model_dossier",
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
    openai_url = os.getenv("OPENAI_URL", "https://openrouter.ai/api/v1/chat/completions")
    if "openrouter.ai" in openai_url:
        return "https://openrouter.ai/api/v1/embeddings"
    if "/chat/completions" in openai_url:
        return openai_url.replace("/chat/completions", "/embeddings")
    return "https://openrouter.ai/api/v1/embeddings"


async def get_embedding(text: str, instruction: Optional[str] = None) -> List[float]:
    """
    Generate dense embedding for text.
    Controlled by EMBEDDING_BACKEND in .env:
      - 'local': Uses Ollama (EMBEDDING_LOCAL_MODEL, default: nomic-embed-text-cpu)
      - 'cloud': Uses OpenRouter/OpenAI (EMBEDDING_CLOUD_MODEL, default: text-embedding-3-small)
      - 'auto': Attempts local first; falls back to cloud if local is unreachable

    `instruction` is the query-side task instruction for instruction-tuned
    models (Qwen3-Embedding): queries carry it, documents never do.
    """
    vector_size = _get_embedding_vector_size()
    if not text or not text.strip():
        return [0.0] * vector_size

    backend = _get_embedding_backend()
    local_model = os.getenv("EMBEDDING_LOCAL_MODEL", "nomic-embed-text-cpu").strip()
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
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    ollama_url,
                    json={"model": model_name, "input": embed_input, "keep_alive": -1},
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


async def ensure_collection(collection_name: str = COLLECTION_NAME) -> bool:
    """Ensure the target Qdrant collection exists with cosine distance."""
    base_url = _get_qdrant_url()
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Check if collection exists
        try:
            check_resp = await client.get(f"{base_url}/collections/{collection_name}")
            if check_resp.status_code == 200:
                return True
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


async def search_vectors(
    query_text: str,
    limit: int = 5,
    user_id: Optional[str] = None,
    collection_name: str = COLLECTION_NAME,
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
    if RENDERABLE_DOMAINS:
        must_filters.append({"key": "domain", "match": {"any": RENDERABLE_DOMAINS}})
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
    c.execute(
        "SELECT claim_id, subject_id, predicate, object_id, scalar_value, source_authority "
        "FROM kg_claims WHERE tx_retracted_at IS NULL AND (subject_id = ? OR subject_id LIKE ?)",
        (f"user:{uid}", f"%{uid}%")
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

    # 3. Dossiers (user-scoped: only index dossiers where primary_entity_id
    #    references the current user, to prevent cross-user data leakage)
    c.execute(
        "SELECT doc_id, primary_entity_id, title, content, tags FROM kg_dossiers "
        "WHERE primary_entity_id = ? OR primary_entity_id LIKE ?",
        (f"user:{uid}", f"%{uid}%")
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
    local_model = os.getenv("EMBEDDING_LOCAL_MODEL", "nomic-embed-text-cpu").strip()
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
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
    # Claims attributed to this user via point payload (non-user subjects like
    # "perfume:X"); they are not in the user-subject active query but must not
    # be treated as stale.
    attributed_ids = {
        p["claim_id"] for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id")
        and (p.get("user_id") or "").strip() == uid
    }

    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    c = conn.cursor()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "SELECT claim_id, subject_id, predicate, object_id, scalar_value, source_authority "
        "FROM kg_claims WHERE tx_retracted_at IS NULL AND valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to > ?) "
        "AND (subject_id = ? OR subject_id LIKE ?)",
        (now_utc, now_utc, f"user:{uid}", f"%{uid}%")
    )
    active_rows = c.fetchall()

    # Claims behind orphan points: re-index them with proper attribution if
    # they are active and not owned by a different user; otherwise delete.
    orphan_active: Dict[str, tuple] = {}
    if orphan_claim_ids:
        placeholders = ",".join("?" for _ in orphan_claim_ids)
        c.execute(
            f"SELECT claim_id, subject_id, predicate, object_id, scalar_value, source_authority "
            f"FROM kg_claims WHERE claim_id IN ({placeholders}) "
            f"AND tx_retracted_at IS NULL AND valid_from <= ? "
            f"AND (valid_to IS NULL OR valid_to > ?)",
            (*orphan_claim_ids, now_utc, now_utc),
        )
        for row in c.fetchall():
            subj = row[1]
            if subj.startswith("user:") and uid not in subj:
                continue  # another user's claim — that user's reconcile owns it
            orphan_active[row[0]] = row
    conn.close()

    active_ids = {row[0] for row in active_rows}
    stale_ids = [
        p["id"] for p in indexed_points
        if p.get("domain") == "world_model_claim" and p.get("claim_id")
        and p["claim_id"] not in active_ids
        and p["claim_id"] not in attributed_ids
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
                "SELECT DISTINCT subject_id FROM kg_claims "
                "WHERE tx_retracted_at IS NULL AND valid_from <= ? "
                "AND (valid_to IS NULL OR valid_to > ?) "
                "AND subject_id LIKE 'user:%'",
                (now_utc, now_utc),
            )
            users = [row[0].replace("user:", "", 1) for row in c.fetchall()]
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

