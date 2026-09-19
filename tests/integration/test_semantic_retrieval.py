"""集成测试：本地语义索引与 hybrid 检索（D-1）。

安全断言不可妥协（任务书「权限与边界」）：
- 向量候选与 exact 走同一套过滤（语义命中 ⊆ 同过滤全量池）
- hybrid 返回 ⊆ profile 授权集合，且 exact ⊆ hybrid
- 撤回/策略不可用与向量行清理同事务
- 全部用 HashEmbeddingProvider，CI 离线、不下载模型
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import build_conversation, node, text_message, write_zip

from personal_brain.history.db import connect
from personal_brain.history.normalization import normalize_with_map
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.policy.labels import label_event
from personal_brain.policy.withdraw import withdraw_event, withdraw_source
from personal_brain.retrieval.embeddings import HashEmbeddingProvider
from personal_brain.retrieval.search import (
    NOTE_SEMANTIC_INFERRED,
    SearchFilters,
    search_history,
)
from personal_brain.retrieval.semantic_index import (
    SemanticConfig,
    reindex_semantic,
    semantic_meta,
    semantic_unavailable_reason,
)

pytest.importorskip("sqlite_vec", reason="需要 sqlite-vec 扩展（可选依赖 semantic）")


def _import_corpus(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "brain.sqlite")
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    zp = write_zip(retrieval_corpus_conversations(), tmp_path / "a.zip")
    ChatGPTImporter(conn, archive_dir).import_archive(zp, "testuser")
    return conn


@pytest.fixture()
def indexed_env(tmp_path: Path):
    conn = _import_corpus(tmp_path)
    provider = HashEmbeddingProvider()
    stats = reindex_semantic(conn, provider)
    yield conn, provider, stats
    conn.close()


class TestReindex:
    def test_index_built_and_idempotent(self, indexed_env):
        conn, provider, stats = indexed_env
        assert stats["chunks_new"] > 0
        meta = semantic_meta(conn)
        assert meta["model_id"] == "hash-embedding-test"
        assert meta["dimension"] == 256
        # 幂等：第二次运行 0 新增
        again = reindex_semantic(conn, provider)
        assert again["revisions_embedded"] == 0
        assert again["chunks_new"] == 0

    def test_full_rebuild_recreates(self, indexed_env):
        conn, provider, _ = indexed_env
        full = reindex_semantic(conn, provider, full=True)
        assert full["mode"] == "full"
        assert full["revisions_embedded"] > 0
        # 全量后内容一致
        again = reindex_semantic(conn, provider)
        assert again["revisions_embedded"] == 0

    def test_param_change_invalidates_all(self, indexed_env):
        conn, provider, _ = indexed_env
        before_version = semantic_meta(conn)["index_version"]
        other = reindex_semantic(conn, provider, chunk_chars=300)
        assert other["mode"] == "full"
        assert other["index_version"] != before_version
        meta = semantic_meta(conn)
        assert meta["chunk_chars"] == 300

    def test_credential_revisions_not_indexed(self, tmp_path):
        conn = _import_corpus(tmp_path)
        # 追加一条含凭据形态的消息
        m = text_message("cred-01", "user", "我的 API_KEY=sk-ant-1234567890abcdef", None)
        mapping = dict(
            [
                node("root3", None, None, ["cred-01"]),
                node("cred-01", m, "root3", []),
            ]
        )
        conv = build_conversation("conv-cred", "凭据对话", 1754035200.0, mapping, "cred-01")
        zp = write_zip([conv], tmp_path / "b.zip")
        ChatGPTImporter(conn, tmp_path / "archives").import_archive(zp, "testuser")
        provider = HashEmbeddingProvider()
        reindex_semantic(conn, provider)
        row = conn.execute(
            "SELECT COUNT(*) c FROM revision_chunks WHERE revision_id LIKE '%:cred-01'"
        ).fetchone()
        assert row["c"] == 0
        conn.close()

    def test_provider_receives_normalized_text_and_offsets(self, tmp_path):
        conn = _import_corpus(tmp_path)

        class RecordingProvider:
            model_id = "recording"
            dimension = 256

            def __init__(self):
                self.seen = []
                self.delegate = HashEmbeddingProvider()

            def embed(self, texts):
                self.seen.extend(texts)
                return self.delegate.embed(texts)

        provider = RecordingProvider()
        reindex_semantic(conn, provider)
        row = conn.execute(
            """
            SELECT r.raw_text, r.search_text, c.text_start, c.text_end
            FROM event_revisions r
            JOIN events e ON e.event_id = r.event_id
            JOIN revision_chunks c ON c.revision_id = r.revision_id
            WHERE e.event_id LIKE '%:wide'
            """
        ).fetchone()
        assert row is not None
        normalized, _ = normalize_with_map(row["raw_text"])
        assert normalized == row["search_text"]
        assert normalized in provider.seen
        assert normalized[row["text_start"] : row["text_end"]] == normalized
        conn.close()


class TestCleanup:
    def test_withdraw_event_purges_same_transaction(self, indexed_env):
        conn, provider, _ = indexed_env
        rid = conn.execute(
            "SELECT revision_id FROM revision_chunks LIMIT 1"
        ).fetchone()["revision_id"]
        event_id = conn.execute(
            "SELECT event_id FROM event_revisions WHERE revision_id = ?",
            (rid,),
        ).fetchone()["event_id"]
        chunk_ids = [
            r["chunk_id"]
            for r in conn.execute(
                "SELECT chunk_id FROM revision_chunks WHERE revision_id = ?", (rid,)
            )
        ]
        assert chunk_ids
        withdraw_event(conn, event_id)
        assert conn.execute(
            "SELECT COUNT(*) c FROM revision_chunks WHERE revision_id = ?", (rid,)
        ).fetchone()["c"] == 0
        # 向量行也已清除（扩展可用时同步删除）
        assert conn.execute(
            "SELECT COUNT(*) c FROM revision_chunk_vectors WHERE chunk_id IN ("
            + ",".join("?" for _ in chunk_ids) + ")",
            chunk_ids,
        ).fetchone()["c"] == 0

    def test_withdraw_source_purges(self, indexed_env):
        conn, provider, _ = indexed_env
        sid = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
        before = conn.execute("SELECT COUNT(*) c FROM revision_chunks").fetchone()["c"]
        assert before > 0
        withdraw_source(conn, sid)
        assert conn.execute("SELECT COUNT(*) c FROM revision_chunks").fetchone()["c"] == 0

    def test_policy_unavailable_cleaned_on_incremental(self, indexed_env):
        """availability 翻转 → 下一次增量重建把该 revision 的 chunk/向量清掉。"""
        conn, provider, _ = indexed_env
        rid = conn.execute(
            "SELECT revision_id FROM revision_chunks LIMIT 1"
        ).fetchone()["revision_id"]
        conn.execute(
            "UPDATE revision_policy_state SET availability = 'withdrawn'"
            " WHERE revision_id = ? AND valid_to IS NULL",
            (rid,),
        )
        stats = reindex_semantic(conn, provider)
        assert stats["mode"] == "incremental"
        assert conn.execute(
            "SELECT COUNT(*) c FROM revision_chunks WHERE revision_id = ?", (rid,)
        ).fetchone()["c"] == 0
        # 恢复可用 → 增量重建补回
        conn.execute(
            "UPDATE revision_policy_state SET availability = 'available'"
            " WHERE revision_id = ? AND valid_to IS NULL",
            (rid,),
        )
        reindex_semantic(conn, provider)
        assert conn.execute(
            "SELECT COUNT(*) c FROM revision_chunks WHERE revision_id = ?", (rid,)
        ).fetchone()["c"] > 0


class TestHybridSearch:
    def test_semantic_only_hit_and_notes(self, indexed_env):
        conn, provider, _ = indexed_env
        res = search_history(
            conn, "找工作", mode="hybrid", order="relevance",
            embedding_provider=provider,
        )
        ids = [h.event_id for h in res.hits]
        assert "chatgpt:testuser:seek01" in ids
        sem_hits = [h for h in res.hits if h.match_kind == "semantic"]
        both_hits = [h for h in res.hits if h.match_kind == "both"]
        # 词面「找工作」不匹配任何原文 → seek01 只能是语义或 both
        assert sem_hits or both_hits
        target = [h for h in res.hits if h.event_id == "chatgpt:testuser:seek01"][0]
        assert target.match_kind in ("semantic", "both")
        assert target.semantic_score is not None
        assert target.snippet  # chunk 窗口 snippet
        assert target.match_span is None or target.match_kind == "both"
        assert NOTE_SEMANTIC_INFERRED in res.notes
        plan = [p for p in res.query_plans if p.get("path") == "semantic"]
        assert plan and plan[0]["model"] == "hash-embedding-test"

    def test_exact_subset_of_hybrid(self, indexed_env):
        conn, provider, _ = indexed_env
        # 用大 limit 取完整结果集（分页会截断融合列表，集合关系只对全集成立）
        exact = search_history(
            conn, "职业", mode="exact", order="chronological", limit=50
        )
        hybrid = search_history(
            conn, "职业", mode="hybrid", order="chronological", limit=50,
            embedding_provider=provider,
        )
        exact_ids = {h.event_id for h in exact.hits}
        hybrid_ids = {h.event_id for h in hybrid.hits}
        assert exact_ids <= hybrid_ids
        assert not hybrid.truncated

    def test_hybrid_within_authorized_set(self, indexed_env):
        """不可妥协：hybrid 返回 ⊆ 同一过滤条件的授权全量池。"""
        from personal_brain.retrieval.search import _filter_sql

        conn, provider, _ = indexed_env
        sec01 = conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE '%:sec01'"
        ).fetchone()["event_id"]
        label_event(conn, sec01, sensitivity_labels=("secret",))
        filters = SearchFilters(deny_sensitivity=("secret",))
        res = search_history(
            conn, "远程办公 设备", mode="hybrid", order="relevance",
            filters=filters, embedding_provider=provider,
        )
        params: list = []
        pool_sql = _filter_sql(filters, params)
        authorized = {
            r["revision_id"] for r in conn.execute(pool_sql, params).fetchall()
        }
        assert authorized  # 池非空，断言有意义
        assert {h.revision_id for h in res.hits} <= authorized
        # sec01 被拒绝：即使语义相近也不出现
        assert all(h.event_id != "chatgpt:testuser:sec01" for h in res.hits)

    def test_semantic_candidates_within_same_filter_pool(self, indexed_env):
        """语义候选 ⊆ 同一 _filter_sql 全量池（与 exact 同一套过滤）。"""
        conn, provider, _ = indexed_env
        rid = conn.execute(
            "SELECT revision_id FROM revision_chunks LIMIT 1"
        ).fetchone()["revision_id"]
        event_id = conn.execute(
            "SELECT event_id FROM event_revisions WHERE revision_id = ?",
            (rid,),
        ).fetchone()["event_id"]
        withdraw_event(conn, event_id)
        res = search_history(
            conn, "求职", mode="hybrid", order="relevance",
            embedding_provider=provider, limit=50,
        )
        assert all(h.revision_id != rid for h in res.hits)

    def test_remote_synonym_recall(self, indexed_env):
        conn, provider, _ = indexed_env
        res = search_history(
            conn, "在家办公", mode="hybrid", order="relevance",
            embedding_provider=provider,
        )
        ids = [h.event_id for h in res.hits]
        assert "chatgpt:testuser:remote01" in ids

    def test_exact_mode_unchanged_note(self, indexed_env):
        conn, provider, _ = indexed_env
        res = search_history(conn, "职业", mode="exact")
        assert "语义扩展未实现" in res.notes[0]
        assert all(h.match_kind == "exact" for h in res.hits)


class TestDegradation:
    """缺依赖/无索引/模型不一致 → 退化为 exact + note，不报错。"""

    def test_index_not_built(self, tmp_path):
        conn = _import_corpus(tmp_path)
        try:
            res = search_history(conn, "职业", mode="hybrid", order="relevance")
            base = search_history(conn, "职业", mode="exact", order="relevance")
            assert res.notes[0].startswith("semantic_unavailable: index_not_built")
            assert [h.revision_id for h in res.hits] == [
                h.revision_id for h in base.hits
            ]
        finally:
            conn.close()

    def test_extension_unavailable(self, indexed_env, monkeypatch):
        conn, provider, _ = indexed_env
        import personal_brain.retrieval.semantic_index as si

        monkeypatch.setattr(si, "load_vec_extension", lambda _c: False)
        res = search_history(
            conn, "找工作", mode="hybrid", embedding_provider=provider
        )
        assert res.notes[0] == (
            "semantic_unavailable: vec_extension_unavailable"
        )
        assert res.query_plans[-1].get("path") != "semantic"

    def test_model_mismatch(self, indexed_env):
        conn, provider, _ = indexed_env
        res = search_history(
            conn, "职业", mode="hybrid",
            semantic_config=SemanticConfig("BAAI/bge-small-zh-v1.5", 600, 100),
        )
        assert res.notes[0].startswith("semantic_unavailable: index_version_mismatch")

    def test_sqlite_vec_not_installed(self, indexed_env, monkeypatch):
        conn, provider, _ = indexed_env
        import sys

        monkeypatch.setitem(sys.modules, "sqlite_vec", None)
        res = search_history(
            conn, "职业", mode="hybrid", embedding_provider=provider
        )
        assert res.notes[0] == "semantic_unavailable: sqlite_vec_not_installed"

    def test_unavailable_reason_matches_notes(self, indexed_env):
        conn, provider, _ = indexed_env
        assert semantic_unavailable_reason(
            conn, SemanticConfig(provider.model_id, 600, 100)
        ) is None


class TestModeValidation:
    def test_unknown_mode_raises(self, tmp_path):
        conn = _import_corpus(tmp_path)
        try:
            with pytest.raises(ValueError):
                search_history(conn, "职业", mode="vector")
        finally:
            conn.close()

    def test_meta_serializable(self, indexed_env):
        conn, provider, _ = indexed_env
        row = semantic_meta(conn)
        payload = dict(row)
        assert json.dumps(payload)


class TestMCPIntegration:
    def _profile(self):
        from personal_brain.policy.profiles import AccessProfile

        return AccessProfile(
            name="t", allow_scopes=("*",), deny_sensitivity=(),
            allow_unclassified=True,
            tools=("search_history", "get_recent_events", "get_event"),
            delivery_boundary="local_only",
        )

    def test_tool_search_history_hybrid(self, indexed_env):
        from personal_brain.mcp_server.service import tool_search_history
        from personal_brain.retrieval.search import SearchLimits

        conn, provider, _ = indexed_env
        payload = tool_search_history(
            conn, self._profile(),
            {"query": "找工作", "mode": "hybrid"}, SearchLimits(), "UTC",
            embedding_provider=provider,
            semantic_config=SemanticConfig(provider.model_id),
        )
        hits = payload["results"]
        assert hits
        kinds = {h["match_kind"] for h in hits}
        assert kinds <= {"exact", "semantic", "both"}
        assert any(w.startswith("semantic_hits_are_inferred") for w in payload["warnings"])
        assert all(h["citation_id"].startswith("pb:") for h in hits)

    def test_tool_search_history_default_exact(self, indexed_env):
        from personal_brain.mcp_server.service import tool_search_history
        from personal_brain.retrieval.search import SearchLimits

        conn, provider, _ = indexed_env
        payload = tool_search_history(
            conn, self._profile(), {"query": "职业"}, SearchLimits(), "UTC",
        )
        assert all(h["match_kind"] == "exact" for h in payload["results"])
        assert payload["warnings"] == [
            "语义扩展未实现：同义表达（如“求职/找工作”）需分别检索（§6.3 已知能力边界）"
        ]

    def test_tool_search_history_invalid_mode(self, indexed_env):
        from personal_brain.mcp_server.service import ToolError, tool_search_history
        from personal_brain.retrieval.search import SearchLimits

        conn, _, _ = indexed_env
        with pytest.raises(ToolError) as exc:
            tool_search_history(
                conn, self._profile(), {"query": "职业", "mode": "vector"},
                SearchLimits(), "UTC",
            )
        assert exc.value.code == "INVALID_PARAMS"


class TestEvalRegressionGate:
    """回归门：hybrid 在原用例上 Recall@10 ≥ exact；semantic 用例只 hybrid 评分。"""

    SUITE = Path(__file__).resolve().parents[2] / "evals" / "suites" / "dev-synthetic.json"

    @staticmethod
    def _recall(report, types: tuple[str, ...]) -> float:
        vals = [
            r.recall_at_k for r in report.cases
            if r.case_type in types and r.recall_at_k is not None
        ]
        return sum(vals) / len(vals)

    def test_gate(self, tmp_path):
        from personal_brain.evals.runner import run_suite
        from personal_brain.evals.suite import load_suite
        from personal_brain.retrieval.search import SearchLimits

        conn = _import_corpus(tmp_path)
        try:
            suite = load_suite(self.SUITE)
            exact = run_suite(conn, suite, limits=SearchLimits(), mode="exact")
            hybrid = run_suite(conn, suite, limits=SearchLimits(), mode="hybrid")
            original = ("keyword", "multi_temporal", "insufficiency", "adversarial")
            assert exact.summary["skipped_semantic_cases"] == 3
            assert hybrid.summary["skipped_semantic_cases"] == 0
            assert self._recall(hybrid, original) >= self._recall(exact, original) - 1e-9
            # hybrid 门槛整体通过（含 semantic 用例）
            assert hybrid.passed, [
                (r.case_id, r.failure_types or r.error) for r in hybrid.cases if not r.passed
            ]
            # semantic 用例在 hybrid 下确实通过
            sem = [r for r in hybrid.cases if r.case_type == "semantic"]
            assert sem and all(r.passed for r in sem)
        finally:
            conn.close()
