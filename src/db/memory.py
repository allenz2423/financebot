import sqlite3
import json
import math
import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from src.core.state import DB_PATH

def init_epistemic_memory_schema():
    """Upgrade delilah_memories to support epistemic attributes and embeddings."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        
        # Add new columns if they don't exist
        columns = {
            "memory_type": "TEXT DEFAULT 'fact'", # fact, preference, event, goal, decision, pattern, hypothesis
            "provenance_type": "TEXT DEFAULT 'llm_inferred'", # user_stated, llm_inferred, deterministic_calculation
            "confidence": "REAL DEFAULT 0.5",
            "evidence_refs": "TEXT", # JSON array of strings
            "embedding": "TEXT", # JSON array of floats
            "expires_at": "TEXT",
            "sensitivity": "TEXT",
            "dedupe_key": "TEXT",
        }
        
        c.execute("PRAGMA table_info(delilah_memories)")
        existing_cols = {row[1] for row in c.fetchall()}
        
        for col, col_def in columns.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE delilah_memories ADD COLUMN {col} {col_def}")

        # NULL dedupe keys intentionally do not participate: existing callers
        # retain append-only behavior, while keyed writes are unique per owner.
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_delilah_memories_owner_dedupe
            ON delilah_memories(user_id, dedupe_key)
            WHERE dedupe_key IS NOT NULL
        """)
        
        conn.commit()

async def _get_embedding(text: str) -> List[float]:
    """Embed using the configured provider without implicit cloud fallback.

    The shared Qdrant helper supports local-to-cloud fallback. This memory
    store deliberately does not: cloud is used only when explicitly selected
    as EMBEDDING_BACKEND. Local/auto modes make a local-only request and
    degrade to a zero vector on failure.
    """
    try:
        from src.services import qdrant_client

        backend = qdrant_client._get_embedding_backend()
        if backend in ("cloud", "openai", "openrouter"):
            # A remote embedding provider is allowed when it is an explicit
            # operator choice, and shares the app's endpoint/model config.
            return await qdrant_client.get_embedding(text)
        if backend not in ("local", "ollama", "auto"):
            raise ValueError(f"Unsupported EMBEDDING_BACKEND for memory: {backend!r}")

        import httpx
        from src.agent.scheduler import provider_capacity

        local_model = os.getenv(
            "EMBEDDING_LOCAL_MODEL", qdrant_client.DEFAULT_LOCAL_EMBED_MODEL
        ).strip()
        embed_url = qdrant_client._get_ollama_embed_url()
        async with provider_capacity("ollama"), httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                embed_url,
                json={
                    "model": local_model,
                    "input": text.strip()[:8000],
                    "keep_alive": qdrant_client.EMBED_KEEP_ALIVE,
                },
            )
        response.raise_for_status()
        vectors = response.json().get("embeddings", [])
        if not vectors:
            raise RuntimeError("Local embedding endpoint returned no vector")
        return vectors[0]
    except Exception as e:
        print(f"Embedding failed: {e}")

    # Fallback zero-vector.  Its dimension (768) mismatches the live
    # embedding dimension (1024), so _cosine_similarity returns 0.0 and
    # hybrid search degrades to keyword matching.
    return [0.0] * 768

def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm_a = math.sqrt(sum(a * a for a in vec1))
    norm_b = math.sqrt(sum(b * b for b in vec2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

async def save_epistemic_memory(
    user_id: str,
    content: str,
    memory_type: str = "fact",
    category: str = "general",
    importance: str = "normal",
    provenance_type: str = "llm_inferred",
    confidence: float = 0.5,
    evidence_refs: List[str] = None,
    expires_at: Optional[str] = None,
    sensitivity: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> str:
    """Saves a structured epistemic memory with provenance and confidence."""
    if dedupe_key is not None and not user_id:
        raise ValueError("dedupe_key requires a non-empty owner user_id")
    normalized_expiry = _normalize_expiry(expires_at)

    # Avoid unnecessary embedding work for the common restart/repeat case.
    # The partial unique index and ON CONFLICT clause below remain the
    # concurrency-safe source of truth for simultaneous writers.
    if dedupe_key is not None:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            existing = conn.execute(
                "SELECT 1 FROM delilah_memories WHERE user_id = ? AND dedupe_key = ? LIMIT 1",
                (user_id, dedupe_key),
            ).fetchone()
        if existing:
            return "Memory successfully saved."

    evidence_json = json.dumps(evidence_refs or [])
    embedding = await _get_embedding(content)
    embedding_json = json.dumps(embedding)
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO delilah_memories 
            (user_id, content, category, importance, memory_type, provenance_type, confidence, evidence_refs, embedding, expires_at, sensitivity, dedupe_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(user_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
        """, (
            user_id, content, category, importance, memory_type, provenance_type,
            confidence, evidence_json, embedding_json, normalized_expiry,
            sensitivity, dedupe_key,
        ))
        conn.commit()
    return "Memory successfully saved."


def _normalize_expiry(expires_at: Optional[str]) -> Optional[str]:
    """Normalize ISO expiry timestamps to comparable UTC SQLite text."""
    if expires_at is None:
        return None
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise ValueError("expires_at must be an ISO-8601 timestamp or None")
    try:
        parsed = datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("expires_at must be an ISO-8601 timestamp or None") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(sep=" ")

async def semantic_search_memory(
    user_id: str,
    query: str,
    top_k: int = 5,
    min_confidence: float = 0.0,
    *,
    memory_type: Optional[str | List[str] | tuple[str, ...]] = None,
) -> str:
    """Retrieve memories using semantic similarity and strict provenance metadata."""
    query_emb = await _get_embedding(query)
    type_clause = ""
    parameters: list[Any] = [user_id, min_confidence]
    if memory_type is not None:
        if isinstance(memory_type, str):
            raw_types = [memory_type]
        elif isinstance(memory_type, (list, tuple)):
            raw_types = list(memory_type)
        else:
            raise ValueError("memory_type must be a string or a list/tuple of strings")
        normalized_types = [str(item).strip().lower() for item in raw_types]
        if (
            not 1 <= len(normalized_types) <= 8
            or any(not item or len(item) > 64 for item in normalized_types)
            or len(set(normalized_types)) != len(normalized_types)
        ):
            raise ValueError("memory_type must contain 1–8 distinct non-empty types")
        placeholders = ", ".join("?" for _ in normalized_types)
        type_clause = f" AND LOWER(memory_type) IN ({placeholders})"
        parameters.extend(normalized_types)
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, content, memory_type, provenance_type, confidence, evidence_refs, created_at, embedding, sensitivity
            FROM delilah_memories
            WHERE user_id = ? AND is_active = 1 AND confidence >= ?
              AND (expires_at IS NULL OR (
                  julianday(expires_at) IS NOT NULL AND julianday(expires_at) > julianday('now')
              ))
        """ + type_clause, parameters)
        
        results = []
        for row in c.fetchall():
            mem_id, content, m_type, prov, conf, ev, created, emb_json, sensitivity = row
            try:
                emb = json.loads(emb_json) if emb_json else []
                sim = _cosine_similarity(query_emb, emb) if emb else 0.0
            except:
                sim = 0.0
                
            # If embedding failed or missing, fallback to basic keyword match score
            if sim == 0.0:
                words = set(query.lower().split())
                c_words = set(content.lower().split())
                overlap = len(words & c_words)
                sim = overlap * 0.1 # Very basic fallback
                
            # Temporal decay (optional boost for recent facts)
            # Not fully implemented here, keeping it pure semantic + confidence
            score = sim * 0.7 + conf * 0.3 # Weigh similarity and epistemic confidence
            try:
                parsed_evidence = json.loads(ev) if isinstance(ev, str) and ev else ev
            except (TypeError, ValueError):
                parsed_evidence = []
            if not isinstance(parsed_evidence, list):
                parsed_evidence = []

            results.append({
                "score": score,
                "content": content,
                "type": m_type,
                "provenance": prov,
                "confidence": conf,
                "sensitivity": sensitivity,
                "evidence_refs": parsed_evidence,
                "created_at": created
            })
            
        # Sort and return top_k
        results.sort(key=lambda x: x["score"], reverse=True)
        top = results[:top_k]
        
        if not top:
            return "No relevant epistemic memories found."
            
        output = [f"Found {len(top)} memories for query: '{query}'\n"]
        for r in top:
            sensitivity_label = f" | Sensitivity: {r['sensitivity']}" if r["sensitivity"] else ""
            refs = r.get("evidence_refs")
            refs = [str(ref)[:2048] for ref in refs if isinstance(ref, str)][:10] if isinstance(refs, list) else []
            evidence_label = f" | Evidence: {json.dumps(refs, ensure_ascii=False)}" if refs else ""
            output.append(f"[{r['type'].upper()} | Prov: {r['provenance']} | Conf: {r['confidence']}{sensitivity_label}{evidence_label}] {r['content']} (Date: {r['created_at']})")
            
        return "\n".join(output)

# Initialize schema on load
init_epistemic_memory_schema()
