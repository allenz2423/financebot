import sqlite3
import json
import httpx
import math
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

from src.core.state import DB_PATH, OLLAMA_URL

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
            "embedding": "TEXT" # JSON array of floats
        }
        
        c.execute("PRAGMA table_info(delilah_memories)")
        existing_cols = {row[1] for row in c.fetchall()}
        
        for col, col_def in columns.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE delilah_memories ADD COLUMN {col} {col_def}")
        
        conn.commit()

async def _get_embedding(text: str) -> List[float]:
    """Get text embedding using Ollama."""
    # This assumes Ollama is available. In a production environment, 
    # we would fall back to an internal model or cloud API.
    # For now, we'll try Ollama and fallback to a dummy zero-vector 
    # if it fails (so at least keyword search can proceed in hybrid).
    try:
        model = os.getenv("WAKEUP_MODEL", "nomic-embed-text") # Standard embedding model
        # Try local Ollama
        embed_url = OLLAMA_URL.replace("/api/chat", "/api/embeddings")
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(embed_url, json={"model": model, "prompt": text})
            if res.status_code == 200:
                return res.json().get("embedding", [])
    except Exception as e:
        print(f"Embedding failed: {e}")
    
    # Fallback to pseudo-embedding (hash based) just so it doesn't crash if Ollama isn't loaded
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
    evidence_refs: List[str] = None
) -> str:
    """Saves a structured epistemic memory with provenance and confidence."""
    evidence_json = json.dumps(evidence_refs or [])
    embedding = await _get_embedding(content)
    embedding_json = json.dumps(embedding)
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO delilah_memories 
            (user_id, content, category, importance, memory_type, provenance_type, confidence, evidence_refs, embedding, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, (user_id, content, category, importance, memory_type, provenance_type, confidence, evidence_json, embedding_json))
        conn.commit()
    return "Memory successfully saved."

async def semantic_search_memory(user_id: str, query: str, top_k: int = 5, min_confidence: float = 0.0) -> str:
    """Retrieve memories using semantic similarity and strict provenance metadata."""
    query_emb = await _get_embedding(query)
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, content, memory_type, provenance_type, confidence, evidence_refs, created_at, embedding
            FROM delilah_memories
            WHERE user_id = ? AND is_active = 1 AND confidence >= ?
        """, (user_id, min_confidence))
        
        results = []
        for row in c.fetchall():
            mem_id, content, m_type, prov, conf, ev, created, emb_json = row
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
            
            results.append({
                "score": score,
                "content": content,
                "type": m_type,
                "provenance": prov,
                "confidence": conf,
                "created_at": created
            })
            
        # Sort and return top_k
        results.sort(key=lambda x: x["score"], reverse=True)
        top = results[:top_k]
        
        if not top:
            return "No relevant epistemic memories found."
            
        output = [f"Found {len(top)} memories for query: '{query}'\n"]
        for r in top:
            output.append(f"[{r['type'].upper()} | Prov: {r['provenance']} | Conf: {r['confidence']}] {r['content']} (Date: {r['created_at']})")
            
        return "\n".join(output)

# Initialize schema on load
init_epistemic_memory_schema()
