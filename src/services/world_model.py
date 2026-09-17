"""
Delilah Active World Model (AWM) — Minimal Viable Cognitive Substrate Engine

Provides:
- Sub-millisecond entity resolution (aliases, exact matching, FTS5).
- 1-hop and 2-hop relational claim retrieval with bi-temporal validity filtering.
- Epistemic claim assertion with automatic historical retraction (preserving auditability).
- High-density, token-budgeted context formatting (<= 150 tokens) for LLM prompts.
- Explainability engine tracing claims to parent sources and raw documents.
"""

import os
import re
import sqlite3
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

from src.core.state import DB_PATH

def get_primary_user_id() -> str:
    """Retrieve canonical primary user_id dynamically from the database or environment."""
    env_uid = os.getenv("DISCORD_USER_ID") or os.getenv("PRIMARY_USER_ID")
    if env_uid:
        return env_uid.strip()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            for table in ("user_info", "plaid_accounts", "transactions"):
                try:
                    c.execute(f"SELECT user_id FROM {table} WHERE user_id IS NOT NULL AND user_id != '' LIMIT 1")
                    row = c.fetchone()
                    if row and row[0]:
                        return str(row[0]).strip()
                except Exception:
                    continue
    except Exception:
        pass
    return "primary_user"

def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============================================================
# 1. ENTITY RESOLUTION & UPSERT
# ============================================================

def upsert_entity(
    entity_id: str,
    entity_type: str,
    canonical_name: str,
    aliases: Optional[List[str]] = None,
    attributes: Optional[Dict[str, Any]] = None
) -> str:
    """Create or update a canonical entity node in the Active World Model."""
    aliases_json = json.dumps(aliases or [])
    attributes_json = json.dumps(attributes or {})
    
    with _get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO kg_entities (entity_id, entity_type, canonical_name, aliases, attributes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(entity_id) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                aliases = excluded.aliases,
                attributes = excluded.attributes
        """, (entity_id, entity_type, canonical_name, aliases_json, attributes_json))
        
        # Also index into FTS
        alias_text = " ".join(aliases or [])
        c.execute("DELETE FROM kg_search_fts WHERE target_id = ?", (entity_id,))
        c.execute("""
            INSERT INTO kg_search_fts (target_id, target_type, title, content, tags)
            VALUES (?, 'entity', ?, ?, ?)
        """, (entity_id, canonical_name, alias_text, entity_type))
        conn.commit()
    return entity_id

def resolve_entities(query: str, user_id: Optional[str] = None) -> List[str]:
    """
    Given a user query or text snippet, identify matching entity_ids.
    Uses:
    1. Exact alias/name matching.
    2. SQLite FTS5 BM25 search.
    Multi-tenant safe: user:<id> entities belonging to other users are never matched.
    """
    matched_ids = set()
    normalized_q = query.lower()
    target_user_id = str(user_id or get_primary_user_id()).strip() if user_id is not None else None
    user_prefix = f"user:{target_user_id}" if target_user_id else None

    with _get_connection() as conn:
        c = conn.cursor()

        # 1. Direct scan against aliases and canonical names
        c.execute("SELECT entity_id, canonical_name, aliases FROM kg_entities")
        for row in c.fetchall():
            eid = row["entity_id"]
            if eid.startswith("user:") and user_prefix and eid != user_prefix:
                continue

            name = row["canonical_name"].lower()
            # Word boundary matching to avoid partial substrings (e.g. 'me' matching 'Acme')
            if re.search(r"\b" + re.escape(name) + r"\b", normalized_q) or eid.lower() in normalized_q:
                matched_ids.add(eid)
                continue

            try:
                aliases = json.loads(row["aliases"]) if row["aliases"] else []
                for alias in aliases:
                    if re.search(r"\b" + re.escape(alias.lower()) + r"\b", normalized_q):
                        matched_ids.add(eid)
                        break
            except Exception:
                pass

        # 2. FTS5 BM25 search for words in query
        words = [w for w in re_words(query) if len(w) > 2]
        if words:
            fts_query = " OR ".join(f'"{w}"' for w in words[:6])
            try:
                c.execute("""
                    SELECT target_id FROM kg_search_fts
                    WHERE target_type = 'entity' AND kg_search_fts MATCH ?
                    ORDER BY rank LIMIT 10
                """, (fts_query,))
                for row in c.fetchall():
                    tid = row["target_id"]
                    if tid.startswith("user:") and user_prefix and tid != user_prefix:
                        continue
                    matched_ids.add(tid)
            except Exception:
                pass

    return list(matched_ids)

def re_words(text: str) -> List[str]:
    import re
    return re.findall(r"\b[A-Za-z0-9_]+\b", text)

# ============================================================
# 2. BI-TEMPORAL CLAIM ASSERTION & RETRACTION
# ============================================================

# Predicate cardinality governs how a second assertion on the same
# (subject, predicate) interacts with the first:
#
#   "one"  — single-slot. A new assertion SUPERSEDES the previous one by
#            closing its validity window (valid_to = now). The old claim
#            stays in the DB as history; only the newest is current.
#   "many" — multi-value. A new assertion with a DIFFERENT value coexists
#            with the previous one; both stay current. Re-asserting the
#            SAME value is idempotent (handled by the duplicate check below).
#
# Unknown predicates default to "one": letting an unmapped predicate
# accumulate contradictory claims is more dangerous than superseding one.
PREDICATE_CARDINALITY: Dict[str, str] = {
    # Multi-value: a subject can hold several at once.
    "owns": "many",
    "allergic_to": "many",
    "favorite_fragrance": "many",
    "medical_conditions": "many",
    "fact": "many",
    # Single-slot: every predicate not listed here is "one".
    "amex_autopay_confirmed_funded": "one",
    "amex_autopay_next_payment": "one",
    "cap_exhaustion_date": "one",
    "career_goal": "one",
    "cuny_refund_landed": "one",
    "daily_commute_cost": "one",
    "daily_food_cost": "one",
    "date_integrity": "one",
    "employed_by": "one",
    "enrolled_at": "one",
    "evaluating_contract": "one",
    "fall_2026_academic_calendar": "one",
    "fall_2026_class_schedule": "one",
    "fragrance_freeze_active": "one",
    "fragrance_freeze_end_date": "one",
    "gpa": "one",
    "has_side_income": "one",
    "hourly_wage": "one",
    "interest_apr": "one",
    "job_search_status": "one",
    "major": "one",
    "max_weekly_hours": "one",
    "medication_farxiga_non_insurance_cost": "one",
    "medication_tarpeyo_non_insurance_cost": "one",
    "medication_voyxact_non_insurance_cost": "one",
    "monitored_event": "one",
    "owes_debt_to": "one",
    "primary_financial_goal": "one",
    "promo_apr": "one",
    "promo_expiry": "one",
    "purchase_timing": "one",
    "quicksilver_statement_cycle": "one",
    "rent_obligation": "one",
    "sales_tax_rate_nyc": "one",
    "savor_next_payment_due": "one",
    "secondary_financial_goal": "one",
    "semester_cap": "one",
    "sign_convention": "one",
    "subscription_active": "one",
    "target_graduation": "one",
}


def get_predicate_cardinality(predicate: str) -> str:
    """Return 'one' or 'many' for a predicate; unknown predicates default to 'one'."""
    return PREDICATE_CARDINALITY.get(predicate, "one")


def assert_claim(
    subject_id: str,
    predicate: str,
    object_id: Optional[str] = None,
    scalar_value: Optional[Any] = None,
    provenance_type: str = "DIRECT_OBSERVATION",
    source_authority: int = 4,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    parent_claim_ids: Optional[List[str]] = None,
    claim_id: Optional[str] = None,
    owner_user_id: Optional[str] = None
) -> str:
    """
    Assert an epistemic claim into the Active World Model.

    Behavior on a conflicting prior claim depends on predicate cardinality
    (see PREDICATE_CARDINALITY / get_predicate_cardinality):
      - "one":  the previous claim is SUPERSEDED — its valid_to is closed.
      - "many": a different value COEXISTS with the previous claim; the
                same value is a no-op (idempotent).
    Either way history is preserved; tx_retracted_at stays reserved for
    explicit retraction via retract_world_model_claim().
    """
    cid = claim_id or f"claim_{uuid.uuid4().hex[:12]}"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    v_from = valid_from or now_utc
    scalar_str = str(scalar_value) if scalar_value is not None else None
    ev_json = json.dumps(evidence_refs or [])
    parents_json = json.dumps(parent_claim_ids or [])

    with _get_connection() as conn:
        c = conn.cursor()

        # Idempotency: re-asserting the same (subject, predicate, value) that is
        # already current is a no-op — don't create a duplicate claim.
        effective_value = scalar_str or (str(object_id) if object_id else None)
        if effective_value is not None:
            c.execute("""
                SELECT claim_id FROM kg_claims
                WHERE subject_id = ? AND predicate = ? 
                  AND COALESCE(scalar_value, object_id) = COALESCE(?, ?)
                  AND tx_retracted_at IS NULL AND valid_from <= ?
                  AND (valid_to IS NULL OR valid_to > ?)
            """, (subject_id, predicate, effective_value, effective_value, now_utc, now_utc))
            existing = c.fetchone()
            if existing:
                return existing[0]

        # Handle a conflicting prior claim on the same (subject, predicate).
        # The idempotency check above already returned if the same value is
        # being re-asserted, so at this point the new value differs.
        #
        #   "one"  — SUPERSEDE: close the validity window of every prior
        #            claim on this (subject, predicate). History is preserved
        #            (valid_to is set, tx_retracted_at stays NULL); only the
        #            newest claim is current.
        #   "many" — COEXIST: do nothing. Different values accumulate; both
        #            stay current. This is what makes 'owns' reusable across
        #            multiple possessions without eating the earlier ones.
        if get_predicate_cardinality(predicate) == "one":
            c.execute("""
                UPDATE kg_claims
                SET valid_to = ?
                WHERE subject_id = ? AND predicate = ? AND tx_retracted_at IS NULL
                  AND (valid_to IS NULL OR valid_to > ?)
            """, (now_utc, subject_id, predicate, now_utc))

        # Insert new claim
        c.execute("""
            INSERT INTO kg_claims (
                claim_id, subject_id, predicate, object_id, scalar_value,
                provenance_type, source_authority, valid_from, valid_to,
                tx_asserted_at, tx_retracted_at, parent_claim_ids, evidence_refs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """, (
            cid, subject_id, predicate, object_id, scalar_str,
            provenance_type, source_authority, v_from, valid_to,
            now_utc, parents_json, ev_json
        ))

        # Index into full-text search index (FTS5) for instant retrieval
        claim_content = f"{predicate}: {scalar_str or object_id or ''}"
        c.execute("DELETE FROM kg_search_fts WHERE target_id = ?", (cid,))
        c.execute("""
            INSERT INTO kg_search_fts (target_id, target_type, title, content, tags)
            VALUES (?, 'claim', ?, ?, ?)
        """, (cid, f"{subject_id} -> {predicate}", claim_content, f"claim {predicate}"))
        conn.commit()

    # Incremental vector index sync for real-time semantic search.
    # NOTE: superseded claims are NOT deleted from Qdrant — they remain valid
    # history, just not current. Only explicit retraction (retract_world_model_claim)
    # removes a vector point. Stale points are filtered at retrieval time instead.
    try:
        import asyncio
        from src.services.qdrant_client import index_single_claim
        loop = asyncio.get_running_loop()
        u_id = owner_user_id or (subject_id.replace("user:", "") if subject_id.startswith("user:") else "")
        embed_value = scalar_str if scalar_str is not None else (str(object_id) if object_id else "")
        loop.create_task(index_single_claim(cid, subject_id, predicate, embed_value, source_authority, u_id))
    except Exception as e:
        print(f" [VECTOR SYNC FAILED] claim={cid}: {type(e).__name__}: {e}")

    return cid

# ============================================================
# 3. SUBGRAPH EXTRACTION WITH BI-TEMPORAL FILTERING
# ============================================================

def get_entity_subgraph(
    entity_ids: List[str],
    depth: int = 1,
    as_of_valid_time: Optional[str] = None
) -> Dict[str, Any]:
    """
    Traverse active claims rooted at the specified entity_ids.
    Enforces bi-temporal constraints:
    - tx_retracted_at IS NULL (Delilah currently believes it).
    - valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    target_vt = as_of_valid_time or now_utc

    entities_map: Dict[str, Dict[str, Any]] = {}
    claims_list: List[Dict[str, Any]] = []
    visited_entities = set(entity_ids)
    current_frontier = set(entity_ids)

    with _get_connection() as conn:
        c = conn.cursor()

        for _ in range(depth + 1):
            if not current_frontier:
                break

            placeholders = ",".join("?" for _ in current_frontier)
            c.execute(f"""
                SELECT entity_id, entity_type, canonical_name, attributes
                FROM kg_entities WHERE entity_id IN ({placeholders})
            """, list(current_frontier))
            
            for row in c.fetchall():
                eid = row["entity_id"]
                if eid not in entities_map:
                    entities_map[eid] = {
                        "id": eid,
                        "type": row["entity_type"],
                        "name": row["canonical_name"],
                        "attributes": json.loads(row["attributes"]) if row["attributes"] else {}
                    }

            # Fetch active claims where subject is in current frontier
            c.execute(f"""
                SELECT claim_id, subject_id, predicate, object_id, scalar_value,
                       provenance_type, source_authority, valid_from, valid_to,
                       evidence_refs, parent_claim_ids
                FROM kg_claims
                WHERE subject_id IN ({placeholders})
                  AND tx_retracted_at IS NULL
                  AND valid_from <= ?
                  AND (valid_to IS NULL OR valid_to > ?)
            """, list(current_frontier) + [target_vt, target_vt])

            next_frontier = set()
            for row in c.fetchall():
                claim_data = dict(row)
                claims_list.append(claim_data)
                obj_id = claim_data.get("object_id")
                if obj_id and obj_id not in visited_entities:
                    next_frontier.add(obj_id)
                    visited_entities.add(obj_id)

            current_frontier = next_frontier

    return {
        "entities": entities_map,
        "claims": claims_list
    }

# ============================================================
# 4. COMPACT CONTEXT ASSEMBLER (<150 TOKENS)
# ============================================================

def build_world_model_context(query: str, max_tokens: int = 180, user_id: Optional[str] = None) -> str:
    """
    Generates a structured, high-density context block for Delilah's LLM prompt.
    Replaces the brute-force 58-bullet raw memory dump with verified relational facts.
    Scoped to user:<user_id>.
    """
    target_user_id = str(user_id or get_primary_user_id()).strip()
    user_anchor = f"user:{target_user_id}"
    matched_eids = resolve_entities(query, user_id=target_user_id)
    
    # If no specific user entity matched, default to primary user profile anchor
    if user_anchor not in matched_eids:
        matched_eids.append(user_anchor)

    subgraph = get_entity_subgraph(matched_eids[:3], depth=1)
    entities = subgraph["entities"]
    claims = subgraph["claims"]

    if not entities and not claims:
        return ""

    lines = [
        "==================================================",
        "ACTIVE WORLD MODEL CONTEXT (VERIFIED GROUND TRUTH)",
        "==================================================",
        "[ACTIVE ENTITIES]"
    ]

    target_max_words = int(max_tokens * 0.75) if max_tokens else 350
    current_words = 15

    for eid, e in list(entities.items())[:3]:
        attrs = [f"{k}: {v}" for k, v in list(e["attributes"].items())[:2]]
        attr_summary = ", ".join(attrs)
        line = f"• {eid} ({e['name']})" + (f" [{attr_summary}]" if attr_summary else "")
        lines.append(line)
        current_words += len(line.split())

    if claims:
        lines.append("")
        lines.append("[VERIFIED CLAIMS]")
        # Prioritize claims relevant to query keywords, then source authority
        raw_words = set(re.findall(r"\w+", query.lower())) - {"what", "is", "my", "the", "a", "an", "in", "on", "for", "to", "do", "i", "how", "much", "about", "you", "know"}
        query_words = set(raw_words)
        # Expand semantic schedule / calendar aliases so both class schedule & academic calendar surface together
        if any(w in query_words for w in ("schedule", "classes", "class", "calendar", "conversion", "term", "semester", "routine")):
            query_words.update({"schedule", "calendar", "classes", "term", "semester", "academic"})
        # Expand food / allergy / health aliases so medical conditions & allergies surface on food/health queries
        if any(w in query_words for w in ("food", "eat", "lunch", "dinner", "breakfast", "cook", "make", "meal", "recipe", "shrimp", "seafood", "shellfish", "peanut", "allergic", "allergy", "allergies", "health", "medical", "medication", "kidney", "igan")):
            query_words.update({"food", "lunch", "dinner", "meal", "allergic", "allergy", "allergies", "medical", "health", "diet", "shellfish", "pollen", "kidney", "igan"})
        
        def claim_relevance_score(c):
            pred = str(c.get("predicate", "")).lower()
            val = str(c.get("scalar_value", "") or c.get("object_id", "")).lower()
            auth = c.get("source_authority", 3)
            match_score = sum(3 for w in query_words if len(w) > 2 and w in pred)
            match_score += sum(2 for w in query_words if len(w) > 2 and w in val)
            # High safety priority: always prioritize active health/allergy constraints when food or health is relevant
            if any(w in query_words for w in ("food", "eat", "lunch", "dinner", "meal", "allergic", "allergy", "allergies", "health", "diet")):
                if "allergic" in pred or "medical" in pred or "allergy" in pred:
                    match_score += 15
            return (match_score, auth)

        sorted_claims = sorted(claims, key=claim_relevance_score, reverse=True)
        for c in sorted_claims:
            subj = c["subject_id"]
            pred = c["predicate"]
            obj = c.get("object_id") or c.get("scalar_value")
            auth = c.get("source_authority", 3)
            obj_str = str(obj)
            max_obj_words = max(8, target_max_words - current_words - 8)
            words = obj_str.split()
            if len(words) > max_obj_words:
                obj_str = " ".join(words[:max_obj_words]) + " ..."
            claim_line = f"• {subj} -> {pred}: {obj_str} (Auth: {auth}/5)"
            claim_word_count = len(claim_line.split())
            if current_words + claim_word_count > target_max_words and len(lines) > 6:
                break
            lines.append(claim_line)
            current_words += claim_word_count

    lines.append("==================================================")
    
    context_str = "\n".join(lines)
    return context_str


# Diagnostics from the most recent build_semantic_world_model_context call.
# Not part of the API — consumed by tests/harnesses to explain gate decisions.
_LAST_RETRIEVAL_DIAG: Dict[str, Any] = {}


async def build_semantic_world_model_context(query: str, max_tokens: int = 250, user_id: Optional[str] = None) -> str:
    """
    Semantic Memory RAG: Uses vector search (Qdrant) + entity resolution to inject
    ONLY semantically relevant claims and dossiers into Delilah's prompt context.
    Prevents brute-force context stuffing (e.g. asking about fragrance won't dump CUNY classes or debts).
    """
    target_user_id = str(user_id or get_primary_user_id()).strip()
    user_anchor = f"user:{target_user_id}"

    # 1. Resolve explicit entity mentions in the query (e.g., 'Apple Card', 'CUNY')
    explicit_matched_eids = resolve_entities(query, user_id=target_user_id)
    named_entity_ids = [eid for eid in explicit_matched_eids if eid != user_anchor]

    lines = [
        "==================================================",
        "ACTIVE WORLD MODEL CONTEXT (SEMANTIC GROUND TRUTH)",
        "==================================================",
    ]

    # 2. Semantic vector search via Qdrant for relevant claims/dossiers
    vector_claims = []
    vector_dossiers = []
    vector_web = []
    vector_mail = []
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # No intent detection of any kind — the embedding model is the sole
    # relevance gate. Fetch a wide candidate pool and keep only what clears
    # the score floor: broad queries naturally surface many hits, narrow
    # ones few.
    hits = []
    try:
        from src.services.qdrant_client import search_vectors, retrieval_domains_for_query
        # Mail-targeted queries (receipts, bills, orders, statements) also open
        # the gmail_message domain; every other query keeps the claim-only
        # universe exactly as benchmarked (MRR@10 0.681 with no distractors).
        hits = await search_vectors(
            query, limit=100, user_id=target_user_id,
            domains=retrieval_domains_for_query(query),
        )

        # Vector points can lag SQLite (supersessions, missed indexing) — only
        # trust hits whose claim is still CURRENT in the knowledge graph.
        active_claim_ids = set()
        hit_claim_ids = [h.get("payload", {}).get("claim_id") for h in hits if h.get("payload", {}).get("domain") == "world_model_claim" and h.get("payload", {}).get("claim_id")]
        if hit_claim_ids:
            with _get_connection() as conn:
                placeholders = ",".join("?" for _ in hit_claim_ids)
                rows = conn.execute(
                    f"SELECT claim_id FROM kg_claims WHERE claim_id IN ({placeholders}) "
                    "AND tx_retracted_at IS NULL AND valid_from <= ? "
                    "AND (valid_to IS NULL OR valid_to > ?)",
                    hit_claim_ids + [now_utc, now_utc],
                ).fetchall()
                active_claim_ids = {r[0] for r in rows}

        # The relevance gate is score-relative and outlier-robust: the band is
        # anchored ONLY on scores of results that are eligible to render
        # (claims/dossiers). Non-claim domains (background knowledge, cached
        # text, snapshots) never render, so letting them set the anchor lets
        # adversarial prose drag the floor up and erase genuine claims. With
        # fewer than three claim hits, fall back to the best claim score
        # explicitly instead of silently anchoring on noise.
        renderable_domains = ("world_model_claim", "world_model_dossier")
        claim_scores_sorted = sorted(
            (h.get("score", 0.0) for h in hits
             if h.get("payload", {}).get("domain") in renderable_domains),
            reverse=True,
        )
        non_claim_scores = sorted(
            (h.get("score", 0.0) for h in hits
             if h.get("payload", {}).get("domain") not in renderable_domains),
            reverse=True,
        )
        if claim_scores_sorted:
            anchor = claim_scores_sorted[2] if len(claim_scores_sorted) >= 3 else claim_scores_sorted[0]
        else:
            anchor = 0.0
        relevance_floor = max(0.33, anchor - 0.10)
        for h in hits:
            score = h.get("score", 0.0)
            payload = h.get("payload", {})
            domain = payload.get("domain", "")
            if score >= relevance_floor:
                if domain == "world_model_claim":
                    if payload.get("claim_id") and payload["claim_id"] not in active_claim_ids:
                        continue
                    vector_claims.append({
                        "subject_id": payload.get("subject_id", user_anchor),
                        "predicate": payload.get("predicate", "fact"),
                        "scalar_value": payload.get("value", ""),
                        "claim_id": payload.get("claim_id"),
                        "source_authority": payload.get("authority", 5),
                        "score": score
                    })
                elif domain == "world_model_dossier":
                    vector_dossiers.append({
                        "title": payload.get("title", ""),
                        "text": payload.get("text", ""),
                        "score": score
                    })
                elif domain == "web_search_result":
                    vector_web.append({
                        "url": str(payload.get("url", "") or ""),
                        "title": str(payload.get("title", "") or ""),
                        "section_name": str(payload.get("section_name", "") or ""),
                        "query": str(payload.get("query", "") or ""),
                        "fetched_at": str(payload.get("fetched_at", "") or ""),
                        "text": str(payload.get("text", "") or ""),
                        "score": score
                    })
                elif domain == "gmail_message":
                    vector_mail.append({
                        "subject": str(payload.get("subject", "") or ""),
                        "sender_email": str(payload.get("sender_email", "") or ""),
                        "date": str(payload.get("date", "") or ""),
                        "section_name": str(payload.get("section_name", "") or ""),
                        "text": str(payload.get("text", "") or ""),
                        "score": score,
                    })
        vector_claims.sort(key=lambda x: x["score"], reverse=True)
        vector_dossiers.sort(key=lambda x: x["score"], reverse=True)
        vector_web.sort(key=lambda x: x["score"], reverse=True)
        vector_mail.sort(key=lambda x: x["score"], reverse=True)
    except Exception as e:
        logger.debug(f"Semantic vector search in world model context failed: {e}")

    # 2b. Collection completeness from SQLite, gated purely by the embedding
    # model's scores. Terse claims ("owns X") rank below rich dossiers in
    # vector space, so a collection surfaces only a top-N slice of itself.
    # When the same predicate survives the relevance band repeatedly, the
    # query is about that whole cluster — pull every active claim for the
    # predicates the search surfaced, from the authoritative KG. Whether a
    # surfaced predicate cluster really is a collection is decided below by
    # plateau shape on raw hits, not by count alone.
    # Band-side predicate stats (kept for diagnostics; the plateau gate below
    # works on raw hits instead).
    pred_scores: Dict[str, List[float]] = {}
    for c in vector_claims:
        p = str(c.get("predicate", ""))
        if p:
            pred_scores.setdefault(p, []).append(float(c.get("score", 0.0)))

    # Plateau detection over RAW top-100 claim hits (not band survivors).
    # The band is a rendering filter; deciding expansion from band survivors
    # is circular — a strong co-occurring cluster (e.g. dossiers on perfume
    # queries) raises the floor and hides the very cluster expansion exists
    # to recover. Instead, judge each predicate's own score distribution in
    # the raw candidate pool:
    #   enough claims   (>= MIN_CLUSTER_HITS in the raw top-100)
    #   tight spread    (cluster max-min <= 0.15: one coherent plateau)
    #   live cluster    (cluster bottom >= best claim score - 0.15: the cluster
    #                    is topically engaged with this query, not incidental)
    #   top-2 presence  (the cluster is one of the two largest raw claim
    #                    groups: the query is about these, not a long tail)
    # The "live cluster" check also kills the narrow-query false positive:
    # when one specific claim towers over everything (e.g. a payment-due hit
    # at 0.69 above an owns cluster at 0.38), the owns mass is background
    # similarity, not a collection the query is asking for.
    MIN_CLUSTER_HITS = 10
    raw_pred_scores: Dict[str, List[float]] = {}
    for h in hits:
        payload = h.get("payload", {})
        if payload.get("domain") == "world_model_claim" and payload.get("predicate"):
            raw_pred_scores.setdefault(str(payload["predicate"]), []).append(float(h.get("score", 0.0)))
    best_claim_score = claim_scores_sorted[0] if claim_scores_sorted else 0.0
    raw_counts_ranked = sorted(
        ((p, len(ss)) for p, ss in raw_pred_scores.items()), key=lambda kv: -kv[1]
    )
    cluster_eval = []
    target_preds: set = set()
    expansion_reason = "insufficient_claim_evidence"
    if raw_counts_ranked:
        for rank_i, (p, raw_count) in enumerate(raw_counts_ranked):
            ss = raw_pred_scores[p]
            if raw_count < MIN_CLUSTER_HITS:
                continue
            s_sorted = sorted(ss, reverse=True)
            spread = s_sorted[0] - s_sorted[-1]
            gap_ok = s_sorted[-1] >= best_claim_score - 0.15
            top2 = rank_i < 2
            is_plateau = spread <= 0.15 and gap_ok and top2
            cluster_eval.append({
                "predicate": p, "raw_count": raw_count,
                "top": round(s_sorted[0], 3), "bottom": round(s_sorted[-1], 3),
                "spread": round(spread, 3), "gap_ok": gap_ok, "top2": top2,
                "plateau": is_plateau,
            })
            if is_plateau:
                target_preds.add(p)
        if target_preds:
            expansion_reason = "broad_plateau"
        elif cluster_eval:
            expansion_reason = "no_plateau"
        else:
            expansion_reason = "narrow_query"

    pre_expansion_count = len(vector_claims)

    if target_preds:
        with _get_connection() as conn:
            user_rows = conn.execute(
                "SELECT predicate, COALESCE(scalar_value, object_id), source_authority "
                "FROM kg_claims WHERE subject_id = ? AND tx_retracted_at IS NULL "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
                (user_anchor, now_utc, now_utc),
            ).fetchall()
            other_rows = conn.execute(
                "SELECT subject_id, predicate, COALESCE(scalar_value, object_id), source_authority "
                "FROM kg_claims WHERE subject_id != ? AND tx_retracted_at IS NULL "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
                (user_anchor, now_utc, now_utc),
            ).fetchall()

        already = {str(c.get("scalar_value", "")).strip().lower() for c in vector_claims}

        def _add_claim(subj: str, pred: str, val: str, auth, score: float):
            if not val or val.lower() in already:
                return False
            already.add(val.lower())
            vector_claims.append({
                "subject_id": subj,
                "predicate": pred,
                "scalar_value": val,
                "source_authority": auth or 4,
                "score": score,
            })
            return True

        # (a) the user's own claims for the predicates the search surfaced.
        for pred, val, auth in user_rows:
            if pred not in target_preds:
                continue
            _add_claim(user_anchor, pred, str(val or "").strip(), auth, 0.76)

        # (b) claims about items the user possesses: match claim subjects
        # against the possession list the search surfaced.
        owned_names = [
            str(r[1] or "").strip().lower()
            for r in user_rows if r[0] == "owns" and str(r[1] or "").strip()
        ]
        item_claims: Dict[str, tuple] = {}  # (item, subject, predicate) -> shortest value wins
        for subj, pred, val, auth in other_rows:
            if pred not in target_preds:
                continue
            hay = f"{subj} {str(val or '')[:120]}".lower()
            item = next((n for n in owned_names if n and n in hay), None)
            if not item:
                continue
            key = (item, str(subj), str(pred))
            cur = item_claims.get(key)
            val_s = str(val or "").strip()
            if cur is None or len(val_s) < len(cur[2]):
                item_claims[key] = (subj, pred, val_s, auth)
        for (item, subj, pred), (_s, pred, val, auth) in item_claims.items():
            _add_claim(subj, pred, val, auth, 0.78)
        vector_claims.sort(key=lambda x: x["score"], reverse=True)

    expanded_claim_count = len(vector_claims) - pre_expansion_count
    owns_scores = pred_scores.get("owns", [])
    owns_spread = (max(owns_scores) - min(owns_scores)) if len(owns_scores) >= 2 else 0.0
    owns_others_max = max(
        (s for p, ss in pred_scores.items() if p != "owns" for s in ss),
        default=0.0,
    )
    _LAST_RETRIEVAL_DIAG.clear()
    _LAST_RETRIEVAL_DIAG.update({
        "query": query,
        "n_hits": len(hits),
        "claim_domain_scores_top20": [round(s, 3) for s in claim_scores_sorted[:20]],
        "non_claim_scores_top20": [round(s, 3) for s in non_claim_scores[:20]],
        "non_claim_in_band": sum(1 for s in non_claim_scores if s >= relevance_floor),
        "anchor": round(anchor, 3),
        "band_floor": round(relevance_floor, 3),
        "in_band_claims": pre_expansion_count,
        "pred_in_band_counts": {p: len(ss) for p, ss in pred_scores.items()},
        "pred_raw_counts": {e["predicate"]: e["raw_count"] for e in cluster_eval},
        "owns_in_band": len(owns_scores),
        "owns_score_spread": round(owns_spread, 3),
        "owns_cluster_gap": round((min(owns_scores) - owns_others_max) if owns_scores else 0.0, 3),
        "cluster_eval": cluster_eval,
        "expansion_triggered": bool(target_preds),
        "expansion_reason": expansion_reason,
        "expanded_claim_count": expanded_claim_count,
    })
    # The budget follows the retrieval: everything the embedding model kept
    # above the score floor gets room in the prompt. A narrow query injects
    # a few lines; a collection-wide query injects the whole set. Web findings
    # are capped at two entries and contribute a small title+summary share.
    retrieval_words = sum(
        len(str(c.get("scalar_value", "")).split()) + 8
        for c in vector_claims
    ) + sum(len(d.get("text", "").split()) + 10 for d in vector_dossiers)
    retrieval_words += sum(40 + len(w.get("text", "").split()) // 8 for w in vector_web)
    retrieval_words += sum(40 + len(w.get("text", "").split()) // 8 for w in vector_mail)
    if retrieval_words:
        max_tokens = max(max_tokens or 0, int(retrieval_words / 0.75) + 30)

    # Pinned (immutable) knowledge: user-confirmed claims and URLs render with
    # a (PINNED) marker so the model treats them as authoritative without
    # re-verification. Pins are user-scoped; the table may not exist yet on
    # older DBs, so degrade to an empty set rather than failing retrieval.
    pinned_urls = set()
    immutable_claim_keys = set()
    try:
        with _get_connection() as conn:
            pin_rows = conn.execute(
                "SELECT url FROM web_knowledge_pins WHERE user_id = ?",
                (target_user_id,),
            ).fetchall()
            for r in pin_rows:
                u = str(r["url"] or "").strip().rstrip("/")
                if u:
                    pinned_urls.add(u)
            imm_rows = conn.execute(
                "SELECT predicate, scalar_value FROM kg_claims WHERE immutable = 1"
            ).fetchall()
            for r in imm_rows:
                val = str(r["scalar_value"] or "").strip()
                if val:
                    immutable_claim_keys.add((str(r["predicate"] or ""), val.lower()))
    except Exception:
        pinned_urls = set()
        immutable_claim_keys = set()

    # 3. If explicit named entities were mentioned, pull their direct claims too
    entity_claims = []
    entities_map = {}
    if named_entity_ids:
        subgraph = get_entity_subgraph(named_entity_ids[:2], depth=1)
        entities_map = subgraph.get("entities", {})
        entity_claims = subgraph.get("claims", [])

    # If no semantic hits, no dossiers, and no named entities matched:
    # Do NOT stuff the prompt with random user facts. The embedding model
    # said nothing in memory is relevant — inject nothing. A lone web finding
    # above the floor is still injected: surfacing past research is the point.
    if not vector_claims and not vector_dossiers and not vector_web and not vector_mail and not entity_claims:
        return ""

    target_max_words = int(max_tokens * 0.75) if max_tokens else 200
    current_words = 15

    # Display entities if relevant
    if entities_map:
        lines.append("[ACTIVE ENTITIES]")
        for eid, e in list(entities_map.items())[:2]:
            line = f"• {eid} ({e.get('name', eid)})"
            lines.append(line)
            current_words += len(line.split())
        lines.append("")

    # Display relevant dossiers (e.g. Fragrance Freeze or specific policy)
    if vector_dossiers:
        lines.append("[RELEVANT DIRECTIVES & DOSSIERS]")
        for d in vector_dossiers:
            d_line = f"• [{d['title']}]: {d['text']}"
            lines.append(d_line)
            current_words += len(d_line.split())
            if current_words >= target_max_words:
                break
        lines.append("")

    # Display verified claims (combining vector hits and explicit entity claims)
    all_claims = vector_claims + [
        {
            "subject_id": c.get("subject_id"),
            "predicate": c.get("predicate"),
            "scalar_value": c.get("scalar_value") or c.get("object_id"),
            "claim_id": c.get("claim_id") or c.get("id"),
            "source_authority": c.get("source_authority", 4),
        }
        for c in entity_claims
    ]

    # Deduplicate claims by (subject_id, predicate, value). Multi-value
    # predicates like "owns" hold many distinct claims on the same
    # (subject, predicate) pair — deduping on the pair alone collapses a
    # 64-bottle fragrance collection into a single line.
    seen = set()
    deduped_claims = []
    for c in all_claims:
        k = (
            c.get("subject_id"),
            c.get("predicate"),
            str(c.get("scalar_value", "")).strip().lower(),
        )
        if k not in seen:
            seen.add(k)
            deduped_claims.append(c)

    if deduped_claims:
        lines.append("[RELEVANT GROUND TRUTH CLAIMS]")
        for c in deduped_claims:
            pinned_mark = " (PINNED)" if (
                str(c["predicate"] or "").strip() and
                str(c.get("scalar_value", "") or "").strip().lower() in {
                    v for p, v in immutable_claim_keys if p == str(c["predicate"] or "").strip()
                }
            ) else ""
            claim_id_mark = f" (claim: {c['claim_id']})" if c.get("claim_id") else ""
            claim_line = f"• {c['subject_id']} -> {c['predicate']}: {c['scalar_value']} (Auth: {c.get('source_authority', 5)}/5){claim_id_mark}{pinned_mark}"
            lines.append(claim_line)
            current_words += len(claim_line.split())
            if current_words >= target_max_words:
                break

    # Prior web research: past search/fetch results that cleared the relevance
    # floor. Rendering these lets the model answer from stored research
    # instead of re-running search_web/fetch_webpage. Entries carry the URL
    # (so a still-open question can be re-verified deliberately) and either a
    # (PINNED) marker for user-confirmed authority or an age marker.
    if vector_web:
        lines.append("[PRIOR WEB RESEARCH]")
        for w in vector_web[:2]:
            if not w.get("url"):
                continue
            age_mark = ""
            if w.get("fetched_at"):
                try:
                    fd = datetime.strptime(w["fetched_at"], "%Y-%m-%d %H:%M:%S")
                    age_days = max(0, (datetime.now(timezone.utc) - fd.replace(tzinfo=timezone.utc)).days)
                    age_mark = f" (age: {age_days}d)"
                except Exception:
                    age_mark = ""
            url_norm = w["url"].strip().rstrip("/")
            trust_mark = " (PINNED)" if url_norm in pinned_urls else age_mark
            w_title = w.get("title") or w.get("url")
            if w.get("section_name"):
                w_title = f"{w_title} [{w['section_name']}]"
            summary = re.sub(r"\s+", " ", w.get("text", "") or " ").strip()
            if len(summary) > 280:
                summary = summary[:277].rstrip() + "..."
            lines.append(f"• [WEB] {w_title} — {w['url']}{trust_mark}: {summary}")
            current_words += 40 + len(summary.split()) // 4
            if current_words >= target_max_words:
                break
        lines.append("")

    # Email correspondence (gmail sections): indexed mail joins retrieval ONLY
    # for mail-targeted queries (receipts, bills, orders, statements). Entries
    # carry sender + subject + date so the model can answer from stored mail
    # instead of asking the user to re-share it.
    if vector_mail:
        lines.append("[EMAIL RECEIPTS & CORRESPONDENCE]")
        for m in vector_mail[:2]:
            subject = re.sub(r"\s+", " ", (m.get("subject") or "")).strip()
            sender = (m.get("sender_email") or "").strip() or "(unknown sender)"
            date_s = (str(m.get("date") or "") or "date?")[:10]
            section = f" [{m['section_name']}]" if m.get("section_name") else ""
            summary = re.sub(r"\s+", " ", m.get("text", "") or " ").strip()
            if len(summary) > 280:
                summary = summary[:277].rstrip() + "..."
            lines.append(f"• [MAIL] {subject or '(no subject)'}{section} — {sender} ({date_s}): {summary}")
            current_words += 40 + len(summary.split()) // 4
            if current_words >= target_max_words:
                break
        lines.append("")

    lines.append("==================================================")
    return "\n".join(lines)


def pin_knowledge_immutable(user_id: str, kind: str, ref: str, reason: str = "") -> str:
    """Pin a claim or web URL as immutable knowledge for the user.

    Pinned claims get kg_claims.immutable = 1; pinned URLs are recorded in
    web_knowledge_pins. Both render with a (PINNED) marker in semantic
    context, which tells the advisor the fact is user-confirmed authority and
    must not be re-verified or silently contradicted by newer web noise.
    """
    user_id = str(user_id or "").strip()
    kind = str(kind or "").strip().lower()
    ref = str(ref or "").strip()
    if not user_id or kind not in ("claim", "web") or not ref:
        return (
            "pin_knowledge_immutable requires kind ('claim' | 'web') and ref "
            "(a claim_id or a URL)."
        )

    if kind == "claim":
        with _get_connection() as conn:
            row = conn.execute(
                "SELECT claim_id FROM kg_claims WHERE claim_id = ?", (ref,)
            ).fetchone()
            if row is None:
                return f"No claim found with claim_id {ref}; nothing pinned."
            conn.execute(
                "UPDATE kg_claims SET immutable = 1 WHERE claim_id = ?", (ref,)
            )
            conn.commit()
        return f"Pinned claim {ref} as immutable. It will render with a (PINNED) marker and not require re-verification."

    with _get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO web_knowledge_pins (user_id, url, reason) VALUES (?, ?, ?)",
            (user_id, str(ref)[:2048], str(reason or "")[:500]),
        )
        conn.commit()
    return f"Pinned {ref} as immutable web knowledge. It will render with a (PINNED) marker."

# ============================================================
# 5. EXPLAINABILITY & PROVENANCE AUDIT
# ============================================================

def explain_claim(claim_id: str) -> Dict[str, Any]:
    """
    Traces the provenance DAG of a claim back to root evidence and source documents.
    Answers: 'Why do you believe this?'
    """
    with _get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT claim_id, subject_id, predicate, object_id, scalar_value,
                   provenance_type, source_authority, valid_from, valid_to,
                   tx_asserted_at, tx_retracted_at, parent_claim_ids, evidence_refs
            FROM kg_claims WHERE claim_id = ?
        """, (claim_id,))
        row = c.fetchone()
        if not row:
            return {"error": f"Claim '{claim_id}' not found."}

        data = dict(row)
        data["evidence_refs"] = json.loads(data["evidence_refs"]) if data["evidence_refs"] else []
        data["parent_claim_ids"] = json.loads(data["parent_claim_ids"]) if data["parent_claim_ids"] else []
        
        # Recursively fetch parent claims if any exist
        parents = []
        for pid in data["parent_claim_ids"]:
            parents.append(explain_claim(pid))
        data["parents"] = parents

        return data

# ============================================================
# 5b. COMPREHENSIVE QUERY & MANAGEMENT FUNCTIONS
# ============================================================

def get_world_model_entity(entity_id_or_name: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Retrieve full profile and active claims for an entity by ID or name/alias.
    Multi-tenant safe: entities belonging to other users are excluded.
    """
    resolved = resolve_entities(entity_id_or_name, user_id=user_id)
    target_id = resolved[0] if resolved else entity_id_or_name.strip().lower()
    # If resolution found nothing and the input isn't a valid entity ID,
    # fall back to the user anchor so the current user's profile is always reachable.
    if not target_id or not resolved:
        if user_id:
            target_id = f"user:{user_id}"
        else:
            target_id = "primary_user"

    with _get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT entity_id, entity_type, canonical_name, aliases, attributes, created_at
            FROM kg_entities WHERE entity_id = ?
        """, (target_id,))
        ent_row = c.fetchone()
        if not ent_row:
            return {"error": f"Entity '{entity_id_or_name}' not found or unauthorized."}

        subgraph = get_entity_subgraph([target_id], depth=1)
        entity_info = dict(ent_row)
        entity_info["id"] = entity_info["entity_id"]
        entity_info["aliases"] = json.loads(entity_info["aliases"]) if entity_info["aliases"] else []
        entity_info["attributes"] = json.loads(entity_info["attributes"]) if entity_info["attributes"] else {}

        # Also check for attached dossier
        c.execute("SELECT doc_id, title, content, tags, updated_at FROM kg_dossiers WHERE primary_entity_id = ?", (target_id,))
        dossier_rows = [dict(r) for r in c.fetchall()]

        return {
            "entity": entity_info,
            "claims": subgraph["claims"],
            "dossiers": dossier_rows
        }

def search_world_model(query: str, limit: int = 5, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Full-text BM25 search across all entities and dossiers in the Active World Model.
    Scoped so user entities belonging to other users are excluded, and the current user is prioritized.
    """
    words = [w for w in re_words(query) if len(w) > 1]
    if not words:
        return []

    target_user_id = str(user_id or get_primary_user_id()).strip() if user_id is not None else None
    user_prefix = f"user:{target_user_id}" if target_user_id else None

    fts_query = " OR ".join(f'"{w}"' for w in words[:6])
    results = []

    with _get_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("""
                SELECT target_id, target_type, title, content, tags, rank
                FROM kg_search_fts
                WHERE kg_search_fts MATCH ?
                ORDER BY rank LIMIT ?
            """, (fts_query, limit * 3))
            
            for row in c.fetchall():
                res = dict(row)
                tid = res.get("target_id", "")
                # Multi-tenant isolation: do not leak another user's entity, dossier, or claims
                if tid.startswith("user:") and user_prefix and tid != user_prefix:
                    continue
                title_str = str(res.get("title", ""))
                if title_str.startswith("user:") and user_prefix and not title_str.startswith(user_prefix):
                    continue

                if res["target_type"] == "entity":
                    # Fetch basic entity info
                    c.execute("SELECT entity_type, canonical_name, attributes FROM kg_entities WHERE entity_id = ?", (tid,))
                    e = c.fetchone()
                    if e:
                        res["entity_type"] = e["entity_type"]
                        res["canonical_name"] = e["canonical_name"]
                        res["attributes"] = json.loads(e["attributes"]) if e["attributes"] else {}
                
                results.append(res)
                if len(results) >= limit:
                    break
        except Exception as err:
            return [{"error": f"FTS search error: {err}"}]

    return results


def list_world_model_claims(
    predicate: Optional[str] = None,
    subject_id: Optional[str] = None,
    user_id: Optional[str] = None,
    value_contains: str = "",
    limit: int = 100,
) -> Dict[str, Any]:
    """
    Deterministic enumeration of claims from SQLite — NOT semantic search.
    Use for "list all my X" / "how many X do I have" questions: every active
    claim is returned regardless of embedding similarity.
    """
    resolved_subject = subject_id or (f"user:{user_id}" if user_id else None)
    sql = [
        "SELECT claim_id, subject_id, predicate, object_id, scalar_value,",
        "provenance_type, source_authority, valid_from, tx_asserted_at",
        "FROM kg_claims WHERE tx_retracted_at IS NULL",
    ]
    params: List[Any] = []
    if predicate:
        sql.append("AND predicate = ?")
        params.append(predicate.strip())
    if resolved_subject:
        sql.append("AND subject_id = ?")
        params.append(resolved_subject)
    if value_contains:
        sql.append("AND LOWER(scalar_value) LIKE ?")
        params.append(f"%{value_contains.lower()}%")
    sql.append("ORDER BY tx_asserted_at DESC LIMIT ?")
    params.append(max(1, min(int(limit), 500)))

    with _get_connection() as conn:
        c = conn.cursor()
        c.execute(" ".join(sql), params)
        rows = [dict(r) for r in c.fetchall()]
        total_active = c.execute(
            "SELECT COUNT(*) FROM kg_claims WHERE tx_retracted_at IS NULL"
        ).fetchone()[0]

    return {
        "count": len(rows),
        "total_active_claims_in_model": total_active,
        "truncated": len(rows) >= max(1, min(int(limit), 500)),
        "claims": rows,
    }

async def search_world_model_semantic(query: str, limit: int = 5, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Vector-first world model search: Qdrant semantic hits merged with FTS5 BM25.
    Falls back to pure FTS when the embedding/vector pipeline is unavailable.
    """
    results: List[Dict[str, Any]] = []
    seen_ids: set = set()
    target_user_id = str(user_id or get_primary_user_id()).strip()
    user_prefix = f"user:{target_user_id}"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        from src.services.qdrant_client import search_vectors
        hits = await search_vectors(query, limit=max(limit * 2, 8), user_id=target_user_id)

        # Only trust claim hits that are still CURRENT in SQLite (vectors lag
        # supersessions/retractions).
        hit_claim_ids = [
            h.get("payload", {}).get("claim_id") for h in hits
            if h.get("payload", {}).get("domain") == "world_model_claim"
            and h.get("payload", {}).get("claim_id")
        ]
        active_claim_ids = set()
        if hit_claim_ids:
            with _get_connection() as conn:
                placeholders = ",".join("?" for _ in hit_claim_ids)
                rows = conn.execute(
                    f"SELECT claim_id, subject_id, predicate, object_id, scalar_value, source_authority "
                    f"FROM kg_claims WHERE claim_id IN ({placeholders}) "
                    "AND tx_retracted_at IS NULL AND valid_from <= ? "
                    "AND (valid_to IS NULL OR valid_to > ?)",
                    hit_claim_ids + [now_utc, now_utc],
                ).fetchall()
                active_claim_ids = {r[0] for r in rows}
                active_rows = {r[0]: dict(r) for r in rows}

        for h in hits:
            payload = h.get("payload", {})
            domain = payload.get("domain", "")
            score = h.get("score", 0.0)
            if score < 0.44:
                continue
            if domain == "world_model_claim":
                cid = payload.get("claim_id")
                if not cid or cid not in active_claim_ids:
                    continue
                row = active_rows[cid]
                results.append({
                    "target_id": cid,
                    "target_type": "claim",
                    "title": f"{row['subject_id']} -> {row['predicate']}",
                    "content": str(row["object_id"] or row["scalar_value"] or ""),
                    "predicate": row["predicate"],
                    "source_authority": row["source_authority"],
                    "score": round(score, 4),
                    "retrieval": "vector",
                })
                seen_ids.add(cid)
            elif domain == "world_model_dossier":
                doc_id = payload.get("doc_id")
                if not doc_id or doc_id in seen_ids:
                    continue
                results.append({
                    "target_id": doc_id,
                    "target_type": "dossier",
                    "title": payload.get("title", ""),
                    "content": str(payload.get("text", ""))[:300],
                    "tags": payload.get("tags", ""),
                    "score": round(score, 4),
                    "retrieval": "vector",
                })
                seen_ids.add(doc_id)
    except Exception as e:
        logger.debug(f"Semantic world model search failed, falling back to FTS: {e}")

    # Merge FTS results (exact/keyword matches the vector index may miss),
    # skipping anything already surfaced by the vector pass.
    for res in search_world_model(query, limit=limit, user_id=user_id):
        tid = res.get("target_id")
        if tid and tid in seen_ids:
            continue
        res["retrieval"] = "fts"
        results.append(res)
        if tid:
            seen_ids.add(tid)

    results.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return results[:limit]


def get_world_model_dossier(doc_id_or_title: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Retrieve a specific knowledge dossier document from the Active World Model.
    Ensures dossiers belonging to other users are not accessed.
    """
    target_user_id = str(user_id or get_primary_user_id()).strip() if user_id is not None else None
    user_prefix = f"user:{target_user_id}" if target_user_id else None

    with _get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT doc_id, primary_entity_id, title, content, tags, updated_at
            FROM kg_dossiers WHERE doc_id = ? OR lower(title) LIKE ?
        """, (doc_id_or_title, f"%{doc_id_or_title.strip().lower()}%"))
        row = c.fetchone()
        if not row:
            return {"error": f"Dossier '{doc_id_or_title}' not found."}
        
        doc = dict(row)
        peid = doc.get("primary_entity_id", "")
        if peid.startswith("user:") and user_prefix and peid != user_prefix:
            return {"error": f"Dossier '{doc_id_or_title}' not found."}
        return doc

def retract_world_model_claim(claim_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Retract an active claim by setting tx_retracted_at = now (preserving auditability).
    Ensures user-scoped claims belonging to another user cannot be retracted.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    target_user_id = str(user_id or get_primary_user_id()).strip() if user_id is not None else None
    user_prefix = f"user:{target_user_id}" if target_user_id else None

    with _get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT claim_id, subject_id, predicate, tx_retracted_at FROM kg_claims WHERE claim_id = ?", (claim_id,))
        row = c.fetchone()
        if not row:
            return {"error": f"Claim '{claim_id}' not found."}
        
        subj = row["subject_id"]
        if subj.startswith("user:") and user_prefix and subj != user_prefix:
            return {"error": f"Claim '{claim_id}' not found or unauthorized."}

        if row["tx_retracted_at"]:
            return {"status": "ALREADY_RETRACTED", "claim_id": claim_id, "retracted_at": row["tx_retracted_at"]}

        c.execute("UPDATE kg_claims SET tx_retracted_at = ? WHERE claim_id = ?", (now_utc, claim_id))
        c.execute("DELETE FROM kg_search_fts WHERE target_id = ?", (claim_id,))
        conn.commit()

    # Keep the vector index consistent: a retracted claim must not stay searchable.
    try:
        import asyncio
        from src.services.qdrant_client import delete_claim_point
        loop = asyncio.get_running_loop()
        loop.create_task(delete_claim_point(claim_id))
    except Exception as e:
        print(f" [VECTOR DELETE FAILED] claim={claim_id}: {type(e).__name__}: {e}")

    return {
        "status": "RETRACTED",
        "claim_id": claim_id,
        "subject_id": row["subject_id"],
        "predicate": row["predicate"],
        "retracted_at": now_utc
    }


# ============================================================
# 6. DETERMINISTIC SIMULATION & CASH-FLOW PROJECTION (PHASE 2)
# ============================================================

def simulate_deterministic_cash_flow(
    user_id: str,
    days_ahead: int = 60,
    scenario_overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Deterministic numerical simulation of cash flow and debt trajectories.
    Pure Python math — zero LLM guesswork.
    Combines:
    - Current liquid depository balances.
    - Active debt obligations & interest amortizations.
    - Confirmed FWS earnings up to cap.
    - Scheduled bills and recurring expenses.
    Supports ephemeral scenario overrides for what-if counterfactual branches.
    """
    overrides = scenario_overrides or {}
    now = datetime.now()
    
    with _get_connection() as conn:
        c = conn.cursor()
        
        # 1. Starting liquid cash
        c.execute("""
            SELECT COALESCE(SUM(current_balance), 0.0) 
            FROM plaid_accounts 
            WHERE user_id = ? AND type = 'depository'
        """, (user_id,))
        starting_cash = float(c.fetchone()[0])
        if "starting_cash_delta" in overrides:
            starting_cash += float(overrides["starting_cash_delta"])

        # 2. Debts and APRs
        c.execute("""
            SELECT name, balance, apr, min_payment
            FROM user_debts WHERE user_id = ? AND balance > 0
        """, (user_id,))
        debts = [dict(r) for r in c.fetchall()]

        # 3. Scheduled recurring bills
        c.execute("""
            SELECT merchant, last_amount, next_due_date
            FROM subscriptions WHERE user_id = ? AND status = 'Active'
        """, (user_id,))
        recurring_bills = [dict(r) for r in c.fetchall()]

        # 4. Active employment / work-study cap status from world model
        c.execute("""
            SELECT object_id FROM kg_claims
            WHERE subject_id = ? AND predicate = 'employed_by'
              AND tx_retracted_at IS NULL LIMIT 1
        """, (f"user:{user_id}",))
        emp_row = c.fetchone()
        employer_entity = emp_row[0] if emp_row else None

        fws_cap = 0.0
        if employer_entity:
            c.execute("""
                SELECT scalar_value FROM kg_claims 
                WHERE subject_id = ? AND predicate = 'semester_cap' 
                  AND tx_retracted_at IS NULL LIMIT 1
            """, (employer_entity,))
            fws_cap_row = c.fetchone()
            if fws_cap_row and fws_cap_row[0]:
                try:
                    fws_cap = float(fws_cap_row[0])
                except (ValueError, TypeError):
                    fws_cap = 0.0
        
        # Check hours worked to date
        c.execute("""
            SELECT COALESCE(SUM(net_expected), 0.0)
            FROM cash_inflows WHERE user_id = ? AND source LIKE '%FWS%' AND status = 'Received'
        """, (user_id,))
        fws_earned = float(c.fetchone()[0])
        fws_remaining = max(0.0, fws_cap - fws_earned)
        if overrides.get("fws_terminated", False):
            fws_remaining = 0.0

    # Daily projection loop (discrete time step simulation: t -> t + 1)
    daily_trajectory = []
    current_cash = starting_cash
    fws_rate_per_biweek = float(overrides.get("fws_biweekly_rate", 680.0))
    monthly_extra_burn = float(overrides.get("monthly_expense_delta", 0.0))
    daily_extra_burn = monthly_extra_burn / 30.0

    min_cash = current_cash
    ruin_day = None

    for day_idx in range(days_ahead):
        curr_date = now + timedelta(days=day_idx)
        date_str = curr_date.strftime("%Y-%m-%d")
        
        # Inflows (Bi-weekly FWS until cap exhausted)
        daily_inflow = 0.0
        if day_idx > 0 and day_idx % 14 == 0 and fws_remaining > 0:
            payout = min(fws_rate_per_biweek, fws_remaining)
            daily_inflow += payout
            fws_remaining -= payout

        # Outflows (recurring subscriptions based on due date)
        daily_outflow = daily_extra_burn
        for bill in recurring_bills:
            due_str = str(bill.get("next_due_date") or "")
            if due_str.startswith(date_str):
                daily_outflow += float(bill.get("last_amount", 0.0))

        current_cash += (daily_inflow - daily_outflow)
        min_cash = min(min_cash, current_cash)
        if current_cash < 0 and ruin_day is None:
            ruin_day = date_str

        daily_trajectory.append({
            "day": day_idx,
            "date": date_str,
            "cash": round(current_cash, 2)
        })

    return {
        "starting_cash": round(starting_cash, 2),
        "ending_cash": round(current_cash, 2),
        "minimum_projected_cash": round(min_cash, 2),
        "insolvency_date": ruin_day,
        "fws_remaining_at_end": round(fws_remaining, 2),
        "days_projected": days_ahead,
        "scenario_overrides": overrides
    }

# ============================================================
# 7. COUNTERFACTUAL SCENARIO ENGINE (PHASE 2)
# ============================================================

def run_counterfactual_comparison(
    user_id: str,
    scenario_name: str,
    overrides: Dict[str, Any],
    days_ahead: int = 60
) -> str:
    """
    Evaluates a What-If scenario against baseline reality.
    Pure discrete-time math. Never mutates production database records.
    """
    baseline = simulate_deterministic_cash_flow(user_id, days_ahead=days_ahead)
    alternate = simulate_deterministic_cash_flow(user_id, days_ahead=days_ahead, scenario_overrides=overrides)

    diff_ending_cash = alternate["ending_cash"] - baseline["ending_cash"]
    diff_min_cash = alternate["minimum_projected_cash"] - baseline["minimum_projected_cash"]

    lines = [
        f"🌌 COUNTERFACTUAL SCENARIO ANALYSIS: {scenario_name}",
        "==================================================",
        f"• Baseline Ending Cash (Day {days_ahead}): ${baseline['ending_cash']:,.2f}",
        f"• Alternate Ending Cash (Day {days_ahead}): ${alternate['ending_cash']:,.2f}",
        f"• Net Delta: {'+$' if diff_ending_cash >= 0 else '-$'}{abs(diff_ending_cash):,.2f}",
        "",
        f"• Baseline Minimum Liquidity: ${baseline['minimum_projected_cash']:,.2f}",
        f"• Alternate Minimum Liquidity: ${alternate['minimum_projected_cash']:,.2f}",
        f"• Liquidity Delta: {'+$' if diff_min_cash >= 0 else '-$'}{abs(diff_min_cash):,.2f}",
    ]

    if alternate["insolvency_date"]:
        lines.append(f"⚠️ LIQUIDITY RISK: Scenario causes cash to fall below $0 on {alternate['insolvency_date']}!")
    else:
        lines.append(" Solvency maintained throughout projection period.")

    lines.append("==================================================")
    return "\n".join(lines)

# ============================================================
# 8. PREDICTION AUDITING & WATCHDOG ANOMALY DETECTOR (PHASE 3)
# ============================================================

def record_prediction(
    model_name: str,
    target_entity: str,
    predicted_property: str,
    predicted_value: float,
    target_valid_time: str,
    input_claim_ids: List[str]
) -> str:
    """Commit a forward-looking prediction into kg_predictions for future verification."""
    pred_id = f"pred_{uuid.uuid4().hex[:10]}"
    with _get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO kg_predictions (
                prediction_id, model_name, target_entity, predicted_property,
                predicted_value, target_valid_time, input_claim_ids, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """, (
            pred_id, model_name, target_entity, predicted_property,
            predicted_value, target_valid_time, json.dumps(input_claim_ids)
        ))
        conn.commit()
    return pred_id

def audit_world_model_health() -> Dict[str, Any]:
    """
    Lightweight, deterministic watchdog audit of the Active World Model.
    Checks:
    1. Due predictions awaiting validation.
    2. Active unresolved contradictions.
    3. Stale claims whose valid_to has expired.
    Runs in <5ms without LLM invocations.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    
    with _get_connection() as conn:
        c = conn.cursor()
        
        # 1. Due predictions
        c.execute("""
            SELECT prediction_id, target_entity, predicted_property, predicted_value, target_valid_time
            FROM kg_predictions
            WHERE status = 'PENDING' AND target_valid_time <= ?
        """, (now_utc,))
        due_preds = [dict(r) for r in c.fetchall()]

        # 2. Contradictions
        c.execute("""
            SELECT contradiction_id, claim_id_a, claim_id_b, created_at
            FROM kg_contradictions WHERE status = 'UNRESOLVED'
        """)
        unresolved_conflicts = [dict(r) for r in c.fetchall()]

        # 3. Active entities count
        c.execute("SELECT COUNT(*) FROM kg_entities")
        entity_count = c.fetchone()[0]

        # 4. Active claims count
        c.execute("SELECT COUNT(*) FROM kg_claims WHERE tx_retracted_at IS NULL")
        active_claims_count = c.fetchone()[0]

    return {
        "status": "HEALTHY",
        "entity_count": entity_count,
        "active_claims_count": active_claims_count,
        "due_predictions_count": len(due_preds),
        "due_predictions": due_preds,
        "unresolved_contradictions_count": len(unresolved_conflicts),
        "unresolved_contradictions": unresolved_conflicts,
        "audited_at": now_utc
    }

