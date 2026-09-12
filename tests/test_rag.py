import pytest
from src.rag.engine import query_knowledge_base, get_knowledge_base_stats, _CorpusBM25, _parse_knowledge_file, KnowledgeBase

class TestBM25Engine:
    def test_basic_ranking(self):
        docs = [
            "The cat sat on the mat",
            "Credit card chargeback dispute refund",
            "Tax deduction home office self-employed",
        ]
        bm25 = _CorpusBM25(docs)
        results = bm25.rank("chargeback dispute", top_k=3)
        assert results[0][0] == 1  # Credit card doc should rank first

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
        assert "chargeback" in result.lower() or "dispute" in result.lower()

    def test_tax_query(self):
        result = query_knowledge_base("home office deduction")
        assert "KNOWLEDGE BASE RETRIEVAL" in result
        assert "home office" in result.lower()

    def test_credit_score_query(self):
        result = query_knowledge_base("How can I improve my credit score?", tag_filter="credit score")
        assert "KNOWLEDGE BASE RETRIEVAL" in result
        assert "utilization" in result.lower()

    def test_empty_result(self):
        result = query_knowledge_base("quantum physics black holes spacetime")
        # Should still return something or the "no results" message
        assert isinstance(result, str)

    def test_stats(self):
        stats = get_knowledge_base_stats()
        assert "Total Chunks" in stats
        assert "Unique Tags" in stats
