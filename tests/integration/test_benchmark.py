"""基准与合成生成器测试（§9.2 门槛 7）：小规模冒烟 + 确定性 + 真实导入路径。"""

from __future__ import annotations

import json
from pathlib import Path

from personal_brain.evals.benchmark import (
    _percentile,
    collect_environment,
    run_benchmark,
)
from personal_brain.evals.synthgen import build_benchmark_archive, generate_conversations
from personal_brain.history.db import connect
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.policy.labels import label_source
from personal_brain.policy.profiles import AccessProfile


def _profile(labeled: bool) -> AccessProfile:
    return AccessProfile(
        name="bench",
        allow_scopes=("bench",) if labeled else ("*",),
        deny_sensitivity=(),
        allow_unclassified=not labeled,
        tools=("search_history", "get_recent_events", "get_event"),
        delivery_boundary="local_only",
    )


class TestMigrationUpgrade:
    def test_v3_to_v4_preserves_fts_and_search(self, tmp_path: Path):
        """迁移 4 把 FTS 表 rowid 化后，旧库（schema 3）升级后行数与检索不变。"""
        import hashlib
        import sqlite3

        from personal_brain.history.schema import MIGRATIONS

        db = tmp_path / "old.sqlite"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, sql in MIGRATIONS:  # 只应用迁移 1-3（旧库形态）
            if version >= 4:
                break
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 't')",
                (version,),
            )

        def rid(rev: str) -> int:
            return int.from_bytes(
                hashlib.sha256(rev.encode()).digest()[:8], "big"
            ) & 0x7FFFFFFFFFFFFFFF

        conn.create_function("fts_rowid", 1, rid)
        conn.execute(
            "INSERT INTO event_revisions_fts (revision_id, bigram_text) "
            "VALUES ('ev:r11111111111111111111', '职业')"
        )
        conn.commit()
        conn.close()

        upgraded = connect(db)  # 重开走完整迁移（应用迁移 4）
        try:
            version = upgraded.execute(
                "SELECT MAX(version) v FROM schema_migrations"
            ).fetchone()["v"]
            assert version == 5
            assert upgraded.execute(
                "SELECT COUNT(*) c FROM event_revisions_fts"
            ).fetchone()["c"] == 1
            rows = upgraded.execute(
                "SELECT revision_id FROM event_revisions_fts WHERE "
                "event_revisions_fts MATCH '职业'"
            ).fetchall()
            assert rows[0]["revision_id"] == "ev:r11111111111111111111"
        finally:
            upgraded.close()


class TestSynthgen:
    def test_deterministic_generation(self, tmp_path: Path):
        a = generate_conversations(3, 5, seed=42)
        b = generate_conversations(3, 5, seed=42)
        assert a == b  # 种子确定

    def test_unique_tokens(self):
        convs = generate_conversations(2, 10, seed=1)
        texts = [
            p
            for c in convs
            for n in c["mapping"].values()
            if n["message"]
            for p in n["message"]["content"]["parts"]
        ]
        assert len(texts) == 20
        assert any("主题0号第3条" in t for t in texts)
        assert any("编号U1M7" in t for t in texts)

    def test_imports_via_real_importer(self, tmp_path: Path):
        """生成物必须通过真实导入器发布（格式契约）。"""
        zp = build_benchmark_archive(tmp_path / "s.zip", 2, 4)
        conn = connect(tmp_path / "b.sqlite")
        try:
            importer = ChatGPTImporter(conn, tmp_path / "archives")
            result = importer.import_archive(zp, "benchuser")
            assert result.events_new == 8
        finally:
            conn.close()


class TestBenchmarkSmoke:
    def test_small_scale_benchmark_passes(self, tmp_path: Path):
        """小规模（2×25）冒烟：报告结构、预热 P95 远低于门槛。"""
        zp = build_benchmark_archive(tmp_path / "s.zip", 2, 25)
        conn = connect(tmp_path / "b.sqlite")
        try:
            importer = ChatGPTImporter(conn, tmp_path / "archives")
            importer.import_archive(zp, "benchuser")
            sid = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
            label_source(conn, sid, scope_labels=("bench",))
            sample = conn.execute("SELECT event_id FROM events LIMIT 1").fetchone()["event_id"]
            report = run_benchmark(
                conn, _profile(True), sample, db_path=tmp_path / "b.sqlite", repeats=5
            )
        finally:
            conn.close()
        names = {r.name for r in report.results}
        assert "search_2char_cjk_职业" in names
        assert any(r.disclosed_separately for r in report.results)  # 短词单独披露
        env = collect_environment(tmp_path / "b.sqlite")
        assert env["sqlite"]
        assert env["platform"]
        payload = json.loads(report.to_json())
        assert payload["passed"] is True
        assert payload["rows"]["event_revisions"] == 50

    def test_percentile(self):
        # 策略：round(q*(n-1)) 取最近索引（Python 银行家舍入）
        assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 3.0  # round(1.5)=2 → 第 3 个
        assert _percentile([1.0, 2.0, 3.0], 0.5) == 2.0  # round(1.0)=1
        assert _percentile([5.0], 0.95) == 5.0
