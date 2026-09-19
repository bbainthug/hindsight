"""单元测试：HashEmbeddingProvider 与分块（D-1，全部离线确定性）。"""

from __future__ import annotations

import pytest

from personal_brain.retrieval.embeddings import (
    HashEmbeddingProvider,
    _canonical_tokens,
    _tokens,
)
from personal_brain.retrieval.search import SearchHit, _sort_hits_hybrid
from personal_brain.retrieval.semantic_index import (
    SemanticConfig,
    chunk_text,
    index_version_of,
)


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


class TestHashEmbedding:
    def test_deterministic_and_normalized(self):
        p = HashEmbeddingProvider()
        v1, v2 = p.embed(["职业方向变化", "职业方向变化"])
        assert v1 == v2
        norm = sum(x * x for x in v1) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-9)
        assert p.dimension == 256

    def test_synonym_group_closer_than_unrelated(self):
        p = HashEmbeddingProvider()
        query, synonym_hit, unrelated = p.embed(
            ["找工作进展如何", "求职进展顺利", "系统安全很重要"]
        )
        assert _cosine(query, synonym_hit) > _cosine(query, unrelated)

    def test_latin_variant_maps_to_canonical(self):
        p = HashEmbeddingProvider()
        q, cv_text, resume_text, unrelated = p.embed(
            ["CV", "更新简历", "polish my resume", "系统安全很重要"]
        )
        # CV / 简历 / resume 共享规范成分「简历」：都比无关文本更接近查询
        assert _cosine(q, cv_text) > _cosine(q, unrelated)
        assert _cosine(q, resume_text) > _cosine(q, unrelated)

    def test_canonical_injection_contents(self):
        assert "s:求职" in _canonical_tokens("最近在找工作")
        assert "s:远程" in _canonical_tokens("支持居家办公")
        assert "s:简历" in _canonical_tokens("update my CV")
        assert _canonical_tokens("系统安全很重要") == []

    def test_tokenization(self):
        toks = _tokens("用AI工程栈")
        assert "w:ai" in toks
        assert "c:工程" in toks
        assert all(t.startswith(("w:", "c:")) for t in toks)


class TestChunking:
    def test_short_text_single_chunk(self):
        assert chunk_text("求职进展顺利", 600, 100) == [(0, 6)]

    def test_long_text_overlap_and_coverage(self):
        text = "字" * 1500
        spans = chunk_text(text, 600, 100)
        assert spans[0] == (0, 600)
        assert spans[-1][1] == 1500
        for (s1, e1), (s2, e2) in zip(spans[:-1], spans[1:], strict=True):
            assert e2 > e1  # 前进
            assert s2 < e1  # 重叠
            assert s2 - s1 == 500  # step = chars - overlap

    def test_exact_boundary_no_empty_tail(self):
        spans = chunk_text("x" * 1200, 600, 100)
        assert spans[-1][1] == 1200
        assert all(e > s for s, e in spans)


class TestIndexVersion:
    def test_binds_all_params(self):
        base = index_version_of(SemanticConfig("m", 600, 100), 512)
        assert index_version_of(SemanticConfig("m", 600, 100), 512) == base
        assert index_version_of(SemanticConfig("other", 600, 100), 512) != base
        assert index_version_of(SemanticConfig("m", 700, 100), 512) != base
        assert index_version_of(SemanticConfig("m", 600, 150), 512) != base
        assert index_version_of(SemanticConfig("m", 600, 100), 256) != base


class TestHybridRanking:
    @staticmethod
    def _hit(revision_id: str) -> SearchHit:
        return SearchHit(
            event_id=revision_id,
            revision_id=revision_id,
            is_current=True,
            conversation_id="conversation",
            speaker_id=None,
            speaker_type="owner",
            content_type="text",
            source_created_at="2026-08-01T00:00:00Z",
            time_known=True,
            score=0,
        )

    def test_rrf_combines_both_paths_without_exact_tier(self):
        hits = [self._hit("exact"), self._hit("semantic"), self._hit("both")]
        rrf = {"exact": 0.01, "semantic": 0.03, "both": 0.02}
        ranked = _sort_hits_hybrid(hits, "relevance", rrf)
        assert [h.revision_id for h in ranked] == ["semantic", "both", "exact"]
