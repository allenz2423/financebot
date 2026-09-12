"""
RAG (Retrieval-Augmented Generation) Engine for Delilah.

Implements a BM25-based document retrieval system over a curated financial
knowledge corpus. The corpus is chunked at load time into discrete "documents"
keyed by ID and tagged for fast filtering. At query time, the engine scores
every chunk against the query, applies tag-boost heuristics, and returns the
top-K results as a formatted context block the LLM can cite.

Architecture:
  1. Corpus Loading  — Markdown files in src/rag/knowledge/ are parsed into
                       structured chunks on first import (singleton).
  2. Index Building  — A tag-inverted index is built for O(1) tag lookup.
  3. Query Execution — BM25 scoring + tag-boost + dedup + top-K selection.
  4. Tool Interface  — `query_knowledge_base()` returns a formatted string
                       ready to inject into the LLM's tool response.
"""

import os
import re
import math
from collections import Counter
from pathlib import Path
from typing import List, Dict, Tuple, Optional

# ────────────────────────────────────────────────────────────
# BM25 Scorer (self-contained to avoid circular imports)
# ────────────────────────────────────────────────────────────

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "of", "for",
    "and", "or", "not", "but", "with", "as", "by", "this", "that", "from",
    "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "i", "me", "my", "you", "your", "we", "our",
    "they", "them", "their", "he", "she", "his", "her", "its", "who",
    "what", "which", "when", "where", "how", "why", "if", "then", "so",
    "no", "yes", "all", "each", "every", "both", "few", "more", "most",
    "some", "any", "other", "than", "too", "very", "just", "also",
})


def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS and len(t) > 1]


class _CorpusBM25:
    """
    Multi-document BM25 scorer with proper IDF computed across the full corpus.
    Unlike the single-document scorer in search.py, this uses corpus-level
    document frequency for accurate IDF weighting.
    """

    def __init__(self, documents: List[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.n_docs = len(documents)

        # Tokenize all documents
        self.doc_tokens: List[List[str]] = [_tokenize(d) for d in documents]
        self.doc_lens = [len(t) for t in self.doc_tokens]
        self.avg_dl = sum(self.doc_lens) / max(self.n_docs, 1)

        # Build term frequency maps per document
        self.tf_maps: List[Dict[str, int]] = [Counter(tokens) for tokens in self.doc_tokens]

        # Build document frequency (how many docs contain each term)
        self.df: Dict[str, int] = Counter()
        for tf_map in self.tf_maps:
            for term in tf_map:
                self.df[term] += 1

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: List[str], doc_idx: int) -> float:
        if doc_idx >= self.n_docs:
            return 0.0
        tf_map = self.tf_maps[doc_idx]
        doc_len = self.doc_lens[doc_idx]
        score = 0.0
        for term in query_tokens:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            idf = self._idf(term)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * (doc_len / self.avg_dl))
            score += idf * (numerator / denominator)
        return score

    def rank(self, query: str, top_k: int = 5) -> List[Tuple[int, float]]:
        """Return (doc_index, score) pairs sorted descending."""
        tokens = _tokenize(query)
        if not tokens:
            return []
        scored = [(i, self.score(tokens, i)) for i in range(self.n_docs)]
        scored = [(i, s) for i, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


# ────────────────────────────────────────────────────────────
# Corpus Loader
# ────────────────────────────────────────────────────────────

class KnowledgeChunk:
    __slots__ = ("id", "tags", "content", "source_file")

    def __init__(self, chunk_id: str, tags: List[str], content: str, source_file: str):
        self.id = chunk_id
        self.tags = tags
        self.content = content
        self.source_file = source_file

    def __repr__(self):
        return f"KnowledgeChunk(id={self.id!r}, tags={self.tags}, len={len(self.content)})"


def _parse_knowledge_file(filepath: str) -> List[KnowledgeChunk]:
    """
    Parses a markdown knowledge file into discrete chunks.
    Each chunk is delimited by a YAML-like frontmatter block:
        ---
        id: some_unique_id
        tags: tag1, tag2, tag3
        ---
        Content goes here...
    """
    chunks: List[KnowledgeChunk] = []
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    # Split on the --- id: pattern
    parts = re.split(r"\n---\n", text)

    current_id = None
    current_tags: List[str] = []
    current_content_parts: List[str] = []
    source = os.path.basename(filepath)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Check if this part is a frontmatter block
        id_match = re.search(r"^id:\s*(.+)$", part, re.MULTILINE)
        tags_match = re.search(r"^tags:\s*(.+)$", part, re.MULTILINE)

        if id_match:
            # Save previous chunk if exists
            if current_id and current_content_parts:
                chunks.append(KnowledgeChunk(
                    chunk_id=current_id,
                    tags=current_tags,
                    content="\n".join(current_content_parts).strip(),
                    source_file=source,
                ))

            current_id = id_match.group(1).strip()
            current_tags = [t.strip() for t in (tags_match.group(1) if tags_match else "").split(",") if t.strip()]
            # Remove the frontmatter lines from content
            remaining = re.sub(r"^id:\s*.+$", "", part, flags=re.MULTILINE)
            remaining = re.sub(r"^tags:\s*.+$", "", remaining, flags=re.MULTILINE)
            remaining = remaining.strip()
            current_content_parts = [remaining] if remaining else []
        else:
            # This is content belonging to the current chunk
            if current_id:
                current_content_parts.append(part)
            else:
                # Content before any chunk ID (e.g., file header comments)
                pass

    # Don't forget the last chunk
    if current_id and current_content_parts:
        chunks.append(KnowledgeChunk(
            chunk_id=current_id,
            tags=current_tags,
            content="\n".join(current_content_parts).strip(),
            source_file=source,
        ))

    return chunks


# ────────────────────────────────────────────────────────────
# Knowledge Base (Singleton)
# ────────────────────────────────────────────────────────────

class KnowledgeBase:
    """
    In-memory knowledge base with BM25 retrieval and tag-boosted ranking.
    Loaded once at import time from all .md files in src/rag/knowledge/.
    """

    def __init__(self):
        self.chunks: List[KnowledgeChunk] = []
        self.tag_index: Dict[str, List[int]] = {}  # tag -> list of chunk indices
        self.bm25: Optional[_CorpusBM25] = None
        self._loaded = False

    def load(self, knowledge_dir: str = None):
        if self._loaded:
            return

        if knowledge_dir is None:
            knowledge_dir = os.path.join(os.path.dirname(__file__), "knowledge")

        if not os.path.isdir(knowledge_dir):
            print(f" [RAG] Knowledge directory not found: {knowledge_dir}")
            self._loaded = True
            return

        all_chunks: List[KnowledgeChunk] = []

        for filename in sorted(os.listdir(knowledge_dir)):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(knowledge_dir, filename)
            parsed = _parse_knowledge_file(filepath)
            all_chunks.extend(parsed)
            print(f" [RAG] Loaded {len(parsed)} chunks from {filename}")

        self.chunks = all_chunks

        # Build tag inverted index
        self.tag_index = {}
        for i, chunk in enumerate(self.chunks):
            for tag in chunk.tags:
                tag_lower = tag.lower()
                if tag_lower not in self.tag_index:
                    self.tag_index[tag_lower] = []
                self.tag_index[tag_lower].append(i)

        # Build BM25 index over chunk contents
        if self.chunks:
            documents = [c.content for c in self.chunks]
            self.bm25 = _CorpusBM25(documents)

        self._loaded = True
        print(f" [RAG] Knowledge base ready: {len(self.chunks)} chunks, {len(self.tag_index)} unique tags")

    def _rebuild_index(self):
        """Hot-rebuild the BM25 index and tag index after adding new chunks."""
        self.tag_index = {}
        for i, chunk in enumerate(self.chunks):
            for tag in chunk.tags:
                tag_lower = tag.lower()
                if tag_lower not in self.tag_index:
                    self.tag_index[tag_lower] = []
                self.tag_index[tag_lower].append(i)
        if self.chunks:
            self.bm25 = _CorpusBM25([c.content for c in self.chunks])
        print(f" [RAG] Index rebuilt: {len(self.chunks)} chunks, {len(self.tag_index)} unique tags")

    def add_chunk(self, chunk: 'KnowledgeChunk', persist: bool = True) -> bool:
        """
        Add a new knowledge chunk to the live index.
        If persist=True, also append it to the learned_knowledge.md file on disk
        so it survives restarts.
        """
        if not self._loaded:
            self.load()

        # Deduplicate by ID
        for existing in self.chunks:
            if existing.id == chunk.id:
                # Update in place
                existing.content = chunk.content
                existing.tags = chunk.tags
                self._rebuild_index()
                if persist:
                    self._persist_learned_chunks()
                return True

        self.chunks.append(chunk)
        self._rebuild_index()

        if persist:
            self._persist_learned_chunks()

        return True

    def _persist_learned_chunks(self):
        """Write all learned (non-static) chunks to learned_knowledge.md."""
        knowledge_dir = os.path.join(os.path.dirname(__file__), "knowledge")
        learned_path = os.path.join(knowledge_dir, "learned_knowledge.md")

        learned_chunks = [c for c in self.chunks if c.source_file == "learned_knowledge.md"]
        if not learned_chunks:
            return

        lines = ["# Learned Knowledge (Auto-generated by Delilah's RAG Engine)\n"]
        for chunk in learned_chunks:
            lines.append("---")
            lines.append(f"id: {chunk.id}")
            lines.append(f"tags: {', '.join(chunk.tags)}")
            lines.append("---")
            lines.append(chunk.content)
            lines.append("")

        with open(learned_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def query(self, question: str, top_k: int = 5, tag_filter: str = None) -> List[Tuple[KnowledgeChunk, float]]:
        """
        Query the knowledge base. Returns a list of (chunk, score) tuples.

        If tag_filter is provided, chunks matching the tag get a 1.5x score boost
        (they are NOT excluded if they don't match — just ranked lower).
        """
        if not self._loaded:
            self.load()

        if not self.chunks or not self.bm25:
            return []

        # Get BM25 rankings
        bm25_results = self.bm25.rank(question, top_k=top_k * 3)  # Over-fetch for re-ranking

        if not bm25_results:
            return []

        # Apply tag boost
        boosted_indices = set()
        if tag_filter:
            for tag in [t.strip().lower() for t in tag_filter.split(",")]:
                boosted_indices.update(self.tag_index.get(tag, []))

        # Query tokens for additional tag matching from the query itself
        query_tokens = set(_tokenize(question))
        for tag, indices in self.tag_index.items():
            tag_tokens = set(_tokenize(tag))
            if tag_tokens & query_tokens:
                boosted_indices.update(indices)

        results: List[Tuple[KnowledgeChunk, float]] = []
        for idx, score in bm25_results:
            boost = 1.5 if idx in boosted_indices else 1.0
            results.append((self.chunks[idx], score * boost))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


# Global singleton
_kb = KnowledgeBase()


# ────────────────────────────────────────────────────────────
# Tool Interface (called by the LLM dispatcher)
# ────────────────────────────────────────────────────────────

def query_knowledge_base(question: str, tag_filter: str = "", top_k: int = 3) -> str:
    """
    LLM-facing tool. Queries the RAG knowledge base and returns formatted results.

    Args:
        question: Natural language query (e.g., "Can I chargeback a subscription?")
        tag_filter: Optional comma-separated tags to boost (e.g., "chargeback,dispute")
        top_k: Number of results to return (default 3)

    Returns:
        Formatted string with retrieved knowledge chunks and relevance scores.
    """
    _kb.load()

    results = _kb.query(question, top_k=top_k, tag_filter=tag_filter)

    if not results:
        return "No relevant knowledge found in the knowledge base for this query. Use your general knowledge or search the web."

    lines = [
        f"📚 KNOWLEDGE BASE RETRIEVAL ({len(results)} results)",
        f"Query: \"{question}\"",
        "-" * 50
    ]

    for i, (chunk, score) in enumerate(results):
        lines.append(f"\n[{i+1}] Source: {chunk.id} (Relevance: {score:.2f})")
        lines.append(f"    Tags: {', '.join(chunk.tags)}")
        lines.append(f"    ---")
        # Indent the content for readability
        for content_line in chunk.content.split("\n"):
            lines.append(f"    {content_line}")

    lines.append("\n" + "-" * 50)
    lines.append("⚠️ This knowledge is general guidance. Specific rules may vary by state, card issuer, or individual circumstances.")

    return "\n".join(lines)


def get_knowledge_base_stats() -> str:
    """Returns stats about the loaded knowledge base."""
    _kb.load()
    tags_summary = ", ".join(sorted(list(_kb.tag_index.keys()))[:20])
    learned_count = sum(1 for c in _kb.chunks if c.source_file == "learned_knowledge.md")
    static_count = len(_kb.chunks) - learned_count
    return (
        f"📚 Knowledge Base Stats:\n"
        f"Total Chunks: {len(_kb.chunks)} ({static_count} curated, {learned_count} learned)\n"
        f"Unique Tags: {len(_kb.tag_index)}\n"
        f"Sample Tags: {tags_summary}\n"
        f"Sources: {', '.join(sorted(set(c.source_file for c in _kb.chunks)))}"
    )


def save_to_knowledge_base(chunk_id: str, tags: str, content: str, source_context: str = "") -> str:
    """
    LLM-facing tool. Saves a new verified knowledge chunk into the RAG corpus.
    
    The LLM should use this after it has researched something via web search or
    synthesized a novel insight from the user's financial data. The chunk is
    immediately indexed into the live BM25 engine AND persisted to disk so it
    survives restarts.
    
    Args:
        chunk_id: Unique snake_case identifier (e.g., "amex_return_protection_2025")
        tags: Comma-separated tags for retrieval boosting
        content: The actual knowledge text to store
        source_context: Optional note about where this knowledge came from
        
    Returns:
        Confirmation string with the new corpus stats.
    """
    _kb.load()
    
    # Validate
    if not chunk_id or not chunk_id.strip():
        return "ERROR: chunk_id is required."
    if not content or not content.strip():
        return "ERROR: content is required."
    if len(content.strip()) < 50:
        return "ERROR: Content too short. Knowledge chunks should be substantive (50+ characters)."
    
    chunk_id = chunk_id.strip().lower().replace(" ", "_").replace("-", "_")
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
    
    if not tag_list:
        return "ERROR: At least one tag is required for retrieval."
    
    # Build the content with provenance
    full_content = content.strip()
    if source_context:
        full_content += f"\n\n[Source: {source_context.strip()}]"
    
    chunk = KnowledgeChunk(
        chunk_id=chunk_id,
        tags=tag_list,
        content=full_content,
        source_file="learned_knowledge.md",
    )
    
    _kb.add_chunk(chunk, persist=True)
    
    learned_count = sum(1 for c in _kb.chunks if c.source_file == "learned_knowledge.md")
    
    return (
        f"✅ Knowledge saved successfully.\n"
        f"ID: {chunk_id}\n"
        f"Tags: {', '.join(tag_list)}\n"
        f"Content Length: {len(full_content)} chars\n"
        f"Total Corpus: {len(_kb.chunks)} chunks ({learned_count} learned)\n"
        f"BM25 index rebuilt. This knowledge is now immediately queryable."
    )
