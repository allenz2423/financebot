import pytest
import os
from src.rag.engine import (
    query_knowledge_base, get_knowledge_base_stats, save_to_knowledge_base,
    _CorpusBM25, KnowledgeBase, KnowledgeChunk
)

class TestBM25Engine:
    def test_basic_ranking(self):
        docs = [
            "The cat sat on the mat",
            "Credit card chargeback dispute refund",
            "Tax deduction home office self-employed",
        ]
        bm25 = _CorpusBM25(docs)
        results = bm25.rank("chargeback dispute", top_k=3)
        assert results[0][0] == 1

    def test_empty_query(self):
        docs = ["Some document content"]
        bm25 = _CorpusBM25(docs)
        results = bm25.rank("", top_k=3)
        assert results == []

class TestKnowledgeBase:
    def test_loading(self):
        kb = KnowledgeBase()
        kb.load()
        assert len(kb.chunks) > 0
        assert len(kb.tag_index) > 0

    def test_chargeback_query(self):
        result = query_knowledge_base("Can I dispute a charge on my credit card?")
        assert "KNOWLEDGE BASE RETRIEVAL" in result

    def test_tax_query(self):
        result = query_knowledge_base("home office deduction")
        assert "KNOWLEDGE BASE RETRIEVAL" in result
        assert "home office" in result.lower()

    def test_credit_score_query(self):
        result = query_knowledge_base("How can I improve my credit score?", tag_filter="credit score")
        assert "KNOWLEDGE BASE RETRIEVAL" in result

    def test_stats(self):
        stats = get_knowledge_base_stats()
        assert "Total Chunks" in stats
        assert "curated" in stats

class TestDynamicLearning:
    def test_save_and_retrieve(self):
        # Save
        res = save_to_knowledge_base(
            chunk_id="test_dynamic_chunk_pytest",
            tags="test, pytest, dynamic",
            content="This is a test chunk saved during pytest to verify the dynamic learning pipeline works correctly end-to-end.",
            source_context="pytest"
        )
        assert "Knowledge saved successfully" in res
        assert "learned" in res

        # Retrieve
        result = query_knowledge_base("test dynamic chunk pytest")
        assert "test_dynamic_chunk_pytest" in result

    def test_save_validation_short(self):
        res = save_to_knowledge_base(chunk_id="x", tags="t", content="too short")
        assert "ERROR" in res

    def test_save_validation_no_tags(self):
        res = save_to_knowledge_base(chunk_id="x", tags="", content="A" * 60)
        assert "ERROR" in res

    def test_persistence(self):
        learned_path = os.path.join("src", "rag", "knowledge", "learned_knowledge.md")
        if os.path.exists(learned_path):
            with open(learned_path) as f:
                content = f.read()
            assert "test_dynamic_chunk_pytest" in content
