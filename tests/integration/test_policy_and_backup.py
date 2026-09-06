"""标签、撤回、备份恢复校验集成测试（§7.2/§14.1/§14.3）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import T0, build_conversation, node, text_message, write_zip

from personal_brain.backup import backup, restore_check
from personal_brain.history.db import connect
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.policy.labels import label_event, label_source
from personal_brain.policy.withdraw import withdraw_event, withdraw_source
from personal_brain.retrieval.search import SearchFilters, search_history


@pytest.fixture()
def corpus(tmp_path: Path):
    """自包含语料环境：暴露 conn / db_path / archive_dir（备份测试需要路径）。"""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    db_path = tmp_path / "brain.sqlite"
    conn = connect(db_path)
    importer = ChatGPTImporter(conn, archive_dir)
    zp = write_zip(retrieval_corpus_conversations(), tmp_path / "corpus.zip")
    importer.import_archive(zp, "testuser")
    yield SimpleNamespace(
        conn=conn, db_path=db_path, archive_dir=archive_dir, importer=importer
    )
    conn.close()


def _hit_events(conn, query, **kw):
    r = search_history(conn, query, match_mode="literal", **kw)
    return {h.event_id.rsplit(":", 1)[-1] for h in r.hits}


class TestLabels:
    def test_label_source_covers_all_revisions(self, corpus):
        conn = corpus.conn
        source_id = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
        summary = label_source(
            conn, source_id, scope_labels=("career",), sensitivity_labels=("private",)
        )
        n_revisions = conn.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0]
        assert summary.revisions_relabeled == n_revisions
        rows = conn.execute(
            """
            SELECT ps.classification_status, sl.label FROM revision_policy_state ps
            JOIN revision_scope_labels sl ON sl.state_id = ps.state_id
            WHERE ps.valid_to IS NULL
            """
        ).fetchall()
        assert len(rows) == n_revisions
        assert all(r["classification_status"] == "human_reviewed" for r in rows)
        assert all(r["label"] == "career" for r in rows)

    def test_label_versioning_keeps_history(self, corpus):
        conn = corpus.conn
        ev = conn.execute("SELECT event_id FROM events LIMIT 1").fetchone()["event_id"]
        label_event(conn, ev, scope_labels=("v1",))
        label_event(conn, ev, scope_labels=("v2",))
        states = conn.execute(
            """
            SELECT availability, valid_to IS NULL AS open, sl.label
            FROM revision_policy_state ps
            LEFT JOIN revision_scope_labels sl ON sl.state_id = ps.state_id
            WHERE revision_id = (SELECT revision_id FROM event_revisions WHERE event_id = ?)
            ORDER BY ps.state_id
            """,
            (ev,),
        ).fetchall()
        assert [s["label"] for s in states if s["open"]] == ["v2"]
        assert len([s for s in states if not s["open"]]) >= 1  # 旧状态行被关闭

    def test_scope_filter(self, corpus):
        conn = corpus.conn
        ev = conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE '%:seek01'"
        ).fetchone()["event_id"]
        label_event(conn, ev, scope_labels=("job",))
        f = SearchFilters(allow_scopes=("job",))
        assert _hit_events(conn, "求职", filters=f) == {"seek01"}
        # 未标注内容被过滤掉
        f_all = SearchFilters(allow_scopes=("job",))
        assert "job01" not in _hit_events(conn, "职业", filters=f_all)

    def test_sensitivity_deny_filter(self, corpus):
        conn = corpus.conn
        ev = conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE '%:sec01'"
        ).fetchone()["event_id"]
        label_event(conn, ev, sensitivity_labels=("secret",))
        f = SearchFilters(deny_sensitivity=("secret",))
        assert "sec01" not in _hit_events(conn, "安全", filters=f)
        assert "sec01" in _hit_events(conn, "安全")  # 无过滤时仍可见

    def test_unclassified_filter(self, corpus):
        conn = corpus.conn
        ev = conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE '%:job01'"
        ).fetchone()["event_id"]
        label_event(conn, ev, scope_labels=("x",))
        f = SearchFilters(allow_unclassified=False)
        hits = _hit_events(conn, "职业", filters=f)
        assert hits == {"job01"}

    def test_unknown_source_rejected(self, corpus):
        with pytest.raises(ValueError, match="来源不存在"):
            label_source(corpus.conn, "nope", scope_labels=("x",))


class TestWithdrawSource:
    def test_withdraw_excludes_all_queries(self, corpus):
        conn = corpus.conn
        source_id = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
        summary = withdraw_source(conn, source_id)
        assert summary.revisions_withdrawn > 0
        assert conn.execute("SELECT status FROM sources").fetchone()["status"] == "withdrawn"
        # 检索排除全部
        for q in ("职业", "求职", "安全", "安"):
            assert _hit_events(conn, q) == set()
        # 最近列表排除
        from datetime import UTC, datetime

        from personal_brain.retrieval.search import recent_events
        r = recent_events(conn, days=7, limit=50, now=datetime(2026, 8, 2, tzinfo=UTC))
        assert r.hits == []
        # 撤回是版本化状态：内容 revision 仍在，FTS 行仍在（仅过滤）
        assert conn.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM event_revisions_fts").fetchone()[0] > 0
        # 当前状态行全部为 withdrawn
        avail = {
            r["availability"]
            for r in conn.execute(
                "SELECT DISTINCT availability FROM revision_policy_state WHERE valid_to IS NULL"
            )
        }
        assert avail == {"withdrawn"}

    def test_withdraw_single_event_only(self, corpus):
        conn = corpus.conn
        ev = conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE '%:seek01'"
        ).fetchone()["event_id"]
        withdraw_event(conn, ev)
        assert _hit_events(conn, "求职") == {"both"}  # 仅撤回 seek01，both 仍在
        assert "job01" in _hit_events(conn, "职业")  # 其他事件不受影响

    def test_withdraw_unknown_rejected(self, corpus):
        with pytest.raises(ValueError, match="来源不存在"):
            withdraw_source(corpus.conn, "nope")

    def test_reimport_after_withdraw_stays_withdrawn(self, corpus, tmp_path):
        """撤回登记在数据库内：重复导入同一归档不复活内容（§14.3）。"""
        conn = corpus.conn
        source_id = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
        withdraw_source(conn, source_id)
        zp = write_zip(retrieval_corpus_conversations(), tmp_path / "again.zip")
        corpus.importer.import_archive(zp, "testuser")  # 重复导入（dup fast-path）
        assert _hit_events(conn, "职业") == set()


class TestBackupRestore:
    def test_backup_and_check(self, corpus, tmp_path):
        conn = corpus.conn
        db_path = corpus.db_path
        conn.close()
        result = backup(db_path, tmp_path / "backups", archive_dir=corpus.archive_dir)
        assert (result.backup_dir / "manifest.json").exists()
        assert (result.backup_dir / "archives").exists()
        assert result.integrity == "ok"
        check = restore_check(result.backup_dir)
        assert check.integrity == "ok"
        assert check.foreign_key_violations == 0
        assert check.schema_version == 2
        assert check.manifest_ok is True
        assert check.counts["events"] > 0

    def test_restored_backup_keeps_withdrawal(self, corpus, tmp_path):
        """撤回后备份：恢复出的库同样排除已撤回内容（§14.3 存储层保证）。"""
        conn = corpus.conn
        source_id = conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]
        withdraw_source(conn, source_id)
        db_path = corpus.db_path
        conn.close()
        result = backup(db_path, tmp_path / "backups")
        # 直接在恢复快照上验证检索为空（恢复后行为一致）
        restored = sqlite3.connect(str(result.snapshot_path))
        restored.row_factory = sqlite3.Row
        avail = {
            r["availability"]
            for r in restored.execute(
                "SELECT DISTINCT availability FROM revision_policy_state WHERE valid_to IS NULL"
            )
        }
        restored.close()
        assert avail == {"withdrawn"}
        check = restore_check(result.backup_dir)
        assert check.integrity == "ok" and check.manifest_ok is True

    def test_manifest_detects_tampering(self, corpus, tmp_path):
        conn = corpus.conn
        db_path = corpus.db_path
        conn.close()
        result = backup(db_path, tmp_path / "backups")
        snap = result.backup_dir / "snapshot.sqlite"
        data = bytearray(snap.read_bytes())
        data[-100] ^= 0xFF  # 翻转尾部字节（页面空闲区，尽量不破坏结构）
        snap.write_bytes(bytes(data))
        check = restore_check(result.backup_dir)
        assert check.manifest_ok is False


class TestWithdrawLifecycleRegression:
    """验收缺陷 A/B 回归：撤回不可被标注或重复导入隐式解除（§14.1/§14.2.3）。"""

    def _source_id(self, conn) -> str:
        return conn.execute("SELECT source_id FROM sources").fetchone()["source_id"]

    def _event_id(self, conn, tail: str) -> str:
        return conn.execute(
            "SELECT event_id FROM events WHERE event_id LIKE ?", (f"%:{tail}",)
        ).fetchone()["event_id"]

    def test_label_source_after_withdraw_rejected_not_revive(self, corpus):
        """缺陷 A：撤回来源后标注操作被拒绝，且不复活内容。"""
        conn = corpus.conn
        source_id = self._source_id(conn)
        withdraw_source(conn, source_id)
        assert _hit_events(conn, "职业") == set()
        with pytest.raises(ValueError, match="撤回状态"):
            label_source(conn, source_id, scope_labels=("career",))
        assert _hit_events(conn, "职业") == set()  # 仍不可检索

    def test_label_source_after_withdraw_does_not_revive_even_if_allowed(self, corpus):
        """缺陷 A 核心断言（独立于拒绝路径）：即使来源未整体撤回，
        事件级撤回的版本也不得被来源级标注复活。"""
        conn = corpus.conn
        source_id = self._source_id(conn)
        withdraw_event(conn, self._event_id(conn, "seek01"))
        summary = label_source(conn, source_id, scope_labels=("career",))
        assert summary.skipped_withdrawn >= 1  # 撤回版本被跳过并报告
        assert _hit_events(conn, "求职") == {"both"}  # seek01 仍不可检索
        assert "job01" in _hit_events(conn, "职业")  # 其余标注正常生效

    def test_label_event_fully_withdrawn_rejected(self, corpus):
        conn = corpus.conn
        ev = self._event_id(conn, "seek01")
        withdraw_event(conn, ev)
        with pytest.raises(ValueError, match="撤回状态"):
            label_event(conn, ev, scope_labels=("x",))

    def test_reimport_edited_export_after_withdraw_stays_withdrawn(self, corpus, tmp_path):
        """缺陷 B：另一份导出含同事件身份的编辑内容，新版本继承撤回状态。

        §14.2.3：显式"忘记此内容"应覆盖重复来源，不能靠另一份导出保活。
        """
        conn = corpus.conn
        source_id = self._source_id(conn)
        withdraw_source(conn, source_id)
        assert _hit_events(conn, "职业") == set()

        # 构造"另一份导出"：同消息 ID（同事件身份）、编辑后的文本、更晚时间
        m = text_message("job01", "user", "职业方向变化后的全新思考", T0 + 9999)
        mapping = dict(
            [
                node("root3", None, None, ["job01"]),
                node("job01", m, "root3", []),
            ]
        )
        conv = build_conversation("conv-edited", "编辑导出", T0, mapping, "job01")
        zp = write_zip([conv], tmp_path / "edited.zip")
        corpus.importer.import_archive(zp, "testuser")

        # 新版本已入库（证据链保留），但 policy 继承 withdrawn
        rows = conn.execute(
            """
            SELECT ps.availability FROM event_revisions r
            JOIN revision_policy_state ps
              ON ps.revision_id = r.revision_id AND ps.valid_to IS NULL
            WHERE r.event_id LIKE '%:job01' AND r.raw_text LIKE '%全新思考%'
            """
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["availability"] == "withdrawn"
        # 检索不可见：新文本与旧关键词都无命中
        assert _hit_events(conn, "全新思考") == set()
        assert _hit_events(conn, "职业") == set()
        # current 已指向新版本（时间更晚），检索仍排除——撤回状态优先
        cur = conn.execute(
            "SELECT current_revision_id FROM events WHERE event_id LIKE '%:job01'"
        ).fetchone()["current_revision_id"]
        new_rev = conn.execute(
            "SELECT revision_id FROM event_revisions WHERE raw_text LIKE '%全新思考%'"
        ).fetchone()["revision_id"]
        assert cur == new_rev

    def test_event_level_withdraw_blocks_edited_reimport(self, corpus, tmp_path):
        """缺陷 B 事件级：撤回单事件后，编辑导出的新版本同样继承撤回。"""
        conn = corpus.conn
        withdraw_event(conn, self._event_id(conn, "seek01"))
        m = text_message("seek01", "assistant", "求职进展顺利（修订版）", T0 + 8888)
        mapping = dict(
            [
                node("root4", None, None, ["seek01"]),
                node("seek01", m, "root4", []),
            ]
        )
        conv = build_conversation("conv-edited2", "编辑导出二", T0, mapping, "seek01")
        zp = write_zip([conv], tmp_path / "edited2.zip")
        corpus.importer.import_archive(zp, "testuser")
        assert _hit_events(conn, "修订版") == set()
        assert "job01" in _hit_events(conn, "职业")  # 其余内容不受影响

    def test_search_defends_against_status_drift(self, corpus):
        """防御层：policy 行被手工改回 available 后，withdrawn 来源仍不可检索。"""
        conn = corpus.conn
        source_id = self._source_id(conn)
        withdraw_source(conn, source_id)
        conn.execute(
            "UPDATE revision_policy_state SET availability='available' WHERE valid_to IS NULL"
        )
        conn.commit()
        assert _hit_events(conn, "职业") == set()  # sources.status 过滤兜底
        assert _hit_events(conn, "求职") == set()
