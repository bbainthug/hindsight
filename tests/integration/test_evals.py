"""评测工具链测试（§9）：套件校验、双开发套件全过、引用解析、失败注入。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import write_zip

from personal_brain.evals.citation import parse_citation, resolve_citation
from personal_brain.evals.runner import run_suite
from personal_brain.evals.suite import SuiteValidationError, load_suite
from personal_brain.history.db import connect
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.policy.labels import label_event, label_source
from personal_brain.policy.withdraw import withdraw_source
from personal_brain.retrieval.search import SearchFilters

SUITE_DIR = Path(__file__).resolve().parents[2] / "evals" / "suites"


@pytest.fixture()
def labeled_env(tmp_path: Path):
    """语义卷环境：全量 career + sec01 追加 secret。"""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    conn = connect(tmp_path / "brain.sqlite")
    importer = ChatGPTImporter(conn, archive_dir)
    zp = write_zip(retrieval_corpus_conversations(), tmp_path / "a.zip")
    importer.import_archive(zp, "testuser")
    sid = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
    label_source(conn, sid, scope_labels=("career",))
    sec01 = conn.execute(
        "SELECT event_id FROM events WHERE event_id LIKE '%:sec01'"
    ).fetchone()["event_id"]
    label_event(conn, sec01, scope_labels=("career",), sensitivity_labels=("secret",))
    return SimpleEnv(conn=conn, importer=importer, tmp_path=tmp_path, source_id=sid)


class SimpleEnv:
    def __init__(self, conn, importer, tmp_path, source_id):
        self.conn = conn
        self.importer = importer
        self.tmp_path = tmp_path
        self.source_id = source_id


class TestSuiteValidation:
    def _base(self, **overrides) -> dict:
        case = {
            "id": "c1", "type": "keyword", "query": "职业",
            "acceptable_event_ids": ["chatgpt:testuser:job01"],
        }
        case.update(overrides)
        return {
            "schema_version": "0.2",
            "name": "t",
            "profile": {"allow_scopes": ["*"]},
            "cases": [case],
        }

    def _run(self, tmp_path: Path, raw: dict):
        p = tmp_path / "s.json"
        p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return load_suite(p)

    def test_valid_minimal(self, tmp_path):
        suite = self._run(tmp_path, self._base())
        assert suite.profile.allow_unclassified is False  # §7.2 缺省
        assert suite.cases[0].query == "职业"

    def test_bad_schema_version(self, tmp_path):
        raw = self._base()
        raw["schema_version"] = "0.1"
        with pytest.raises(SuiteValidationError, match="schema_version"):
            self._run(tmp_path, raw)

    def test_unknown_case_type(self, tmp_path):
        with pytest.raises(SuiteValidationError, match="type"):
            self._run(tmp_path, self._base(type="vibes"))

    def test_search_case_requires_query(self, tmp_path):
        with pytest.raises(SuiteValidationError, match="query"):
            self._run(tmp_path, self._base(query=None))

    def test_get_event_requires_event_id(self, tmp_path):
        with pytest.raises(SuiteValidationError, match="event_id"):
            self._run(tmp_path, self._base(mode="get_event"))

    def test_duplicate_case_ids(self, tmp_path):
        raw = self._base()
        raw["cases"].append(dict(raw["cases"][0]))
        with pytest.raises(SuiteValidationError, match="重复"):
            self._run(tmp_path, raw)


class TestCitationResolver:
    def test_parse_roundtrip(self):
        rev = "chatgpt:testuser:job01:r9534d29922d5b5dd836e"
        assert parse_citation(f"pb:{rev}") == ("chatgpt:testuser:job01", rev)

    def test_parse_rejects_garbage(self):
        assert parse_citation("pb:nothash:r123") is None
        assert parse_citation("pb:x") is None
        assert parse_citation("other:chatgpt:a:r" + "a" * 20) is None
        assert parse_citation("pb:ev:r" + "z" * 20) is None  # 非 hex

    def test_resolve_unauthorized_returns_none(self, labeled_env):
        conn = labeled_env.conn
        rev = conn.execute(
            "SELECT revision_id FROM event_revisions WHERE event_id LIKE '%:sec01'"
        ).fetchone()["revision_id"]
        agent = SearchFilters(
            allow_scopes=("career",), deny_sensitivity=("secret",),
            allow_unclassified=False,
        )
        assert resolve_citation(conn, f"pb:{rev}", agent) is None
        local = SearchFilters()  # 本地全权
        assert resolve_citation(conn, f"pb:{rev}", local) is not None

    def test_resolve_withdrawn_returns_none(self, labeled_env):
        conn = labeled_env.conn
        rev = conn.execute(
            "SELECT revision_id FROM event_revisions WHERE event_id LIKE '%:job01'"
        ).fetchone()["revision_id"]
        withdraw_source(conn, labeled_env.source_id)
        assert resolve_citation(conn, f"pb:{rev}", SearchFilters()) is None


class TestPreconditions:
    """验收建议 2：套件环境不匹配必须 fail-fast，不能伪装成检索失败。"""

    def _bare_env(self, tmp_path: Path):
        """单来源、未标注语料。"""
        archive_dir = tmp_path / "archives"
        archive_dir.mkdir()
        conn = connect(tmp_path / "brain.sqlite")
        importer = ChatGPTImporter(conn, archive_dir)
        zp = write_zip(retrieval_corpus_conversations(), tmp_path / "a.zip")
        importer.import_archive(zp, "testuser")
        return conn, importer, tmp_path

    def test_policy_suite_on_bare_corpus_fails_fast(self, tmp_path):
        """裸库跑策略卷：前置拦截，不产生 3/6 的伪越权红色失败。"""
        conn, _, _ = self._bare_env(tmp_path)
        try:
            report = run_suite(
                conn, load_suite(SUITE_DIR / "dev-synthetic-policy.json")
            )
            assert report.passed is False
            assert report.cases == []  # 未跑任何用例
            assert report.precondition_failures
            assert any("已标注" in m for m in report.precondition_failures)
            assert any("来源" in m for m in report.precondition_failures)
        finally:
            conn.close()

    def test_semantic_suite_two_sources_fails_fast(self, labeled_env):
        """双来源库跑语义卷：前置拦截（max_sources=1）。"""
        zp = write_zip(retrieval_corpus_conversations(), labeled_env.tmp_path / "b.zip")
        labeled_env.importer.import_archive(zp, "other")
        report = run_suite(labeled_env.conn, load_suite(SUITE_DIR / "dev-synthetic.json"))
        assert report.cases == []
        assert any("≤1 个来源" in m for m in report.precondition_failures)

    def test_policy_suite_missing_second_source_fails_fast(self, labeled_env):
        """已标注但缺未标注第二来源：前置拦截（min_unlabeled_events）。"""
        report = run_suite(
            labeled_env.conn, load_suite(SUITE_DIR / "dev-synthetic-policy.json")
        )
        assert report.cases == []
        assert any("未标注事件" in m for m in report.precondition_failures)

    def test_valid_env_still_runs_cases(self, labeled_env):
        """前置满足时用例照常运行（补齐未标注第二来源后非空）。"""
        zp = write_zip(retrieval_corpus_conversations(), labeled_env.tmp_path / "b.zip")
        labeled_env.importer.import_archive(zp, "other")
        report = run_suite(
            labeled_env.conn, load_suite(SUITE_DIR / "dev-synthetic-policy.json")
        )
        assert report.precondition_failures == []
        assert len(report.cases) == 6

    def test_unknown_requires_key_rejected(self, tmp_path):
        raw = {
            "schema_version": "0.2",
            "name": "t",
            "profile": {"allow_scopes": ["*"]},
            "requires": {"no_such_key": 1},
            "cases": [{"id": "c1", "type": "keyword", "query": "职业"}],
        }
        p = tmp_path / "s.json"
        p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(SuiteValidationError, match="未知键"):
            load_suite(p)


class TestDevSuites:
    def test_semantic_suite_passes(self, labeled_env):
        suite = load_suite(SUITE_DIR / "dev-synthetic.json")
        report = run_suite(labeled_env.conn, suite)
        failures = [(c.case_id, c.failure_types) for c in report.cases if not c.passed]
        assert report.passed, failures
        gates = report.summary["gates"]
        assert gates["recall_at10"] == 1.0
        assert gates["citation_gate"] is True
        assert gates["forbidden_access_seen"] is False
        assert report.human_review  # 归属/区分语义输出供人工复核

    def test_policy_suite_passes(self, labeled_env):
        """策略卷：未标注第二来源对 agent 完全不可见（计数不泄露）。"""
        zp = write_zip(retrieval_corpus_conversations(), labeled_env.tmp_path / "b.zip")
        labeled_env.importer.import_archive(zp, "other")
        suite = load_suite(SUITE_DIR / "dev-synthetic-policy.json")
        report = run_suite(labeled_env.conn, suite)
        failures = [(c.case_id, c.failure_types, c.returned_event_ids)
                    for c in report.cases if not c.passed]
        assert report.passed, failures
        adv04 = next(c for c in report.cases if c.case_id == "adv-04")
        assert len(adv04.returned_event_ids) == 5  # 恰为 testuser 的 5 条

    def test_recall_failure_detected(self, labeled_env, tmp_path):
        """注入错误期望 → recall_fail 被捕获（防套件恒真）。"""
        raw = json.loads((SUITE_DIR / "dev-synthetic.json").read_text(encoding="utf-8"))
        raw["name"] = "injected"
        raw["profile"] = {"allow_scopes": ["*"], "allow_unclassified": True}
        raw["cases"] = [{
            "id": "bad-recall", "type": "keyword", "query": "职业",
            "acceptable_event_ids": ["chatgpt:testuser:nonexistent"],
        }]
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        report = run_suite(labeled_env.conn, load_suite(p))
        assert report.passed is False
        bad = report.cases[0]
        assert "recall_fail" in bad.failure_types
        assert bad.recall_at_k == 0.0

    def test_withdrawn_source_fails_expectations(self, labeled_env, tmp_path):
        """撤回后语义卷的可见性期望失效 → 回归门槛按失败报告（不静默通过）。"""
        withdraw_source(labeled_env.conn, labeled_env.source_id)
        suite = load_suite(SUITE_DIR / "dev-synthetic.json")
        report = run_suite(labeled_env.conn, suite)
        assert report.passed is False
