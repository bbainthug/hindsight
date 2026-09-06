"""可恢复导入与原子发布测试（§5.2、Phase A 验收 5 之导入恢复部分）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import table_counts
from synthetic import simple_conversation, write_zip

import personal_brain.importers.importer as importer_mod


class TestInterruptionBeforePublish:
    def test_failure_after_staging_leaves_no_visible_state(
        self, importer, archive_dir: Path, tmp_path: Path, monkeypatch
    ):
        zp = write_zip([simple_conversation()], tmp_path / "e.zip")

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash after staging")

        monkeypatch.setattr(importer_mod, "publish_snapshot", boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            importer.import_archive(zp, "testuser")

        # 失败不发布成功状态：无快照、无事件、无部分行
        assert importer.conn.execute(
            "SELECT COUNT(*) FROM source_snapshots"
        ).fetchone()[0] == 0
        assert importer.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert importer.conn.execute(
            "SELECT COUNT(*) FROM event_revisions"
        ).fetchone()[0] == 0
        failed_jobs = importer.conn.execute(
            "SELECT status FROM import_jobs WHERE status='failed'"
        ).fetchall()
        assert len(failed_jobs) == 1
        # 原始归档已暂存（L0 保留），无临时残留
        staged = list((archive_dir / "chatgpt/testuser").iterdir())
        assert len(staged) == 1

    def test_reimport_after_failure_recovers_completely(
        self, importer, tmp_path: Path, monkeypatch
    ):
        zp = write_zip([simple_conversation()], tmp_path / "e.zip")

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(importer_mod, "publish_snapshot", boom)
        with pytest.raises(RuntimeError):
            importer.import_archive(zp, "testuser")

        monkeypatch.undo()
        result = importer.import_archive(zp, "testuser")
        assert result.job_kind == "published"
        assert result.events_new == 4
        assert result.revisions_new == 4
        assert importer.conn.execute(
            "SELECT COUNT(*) FROM revision_occurrences"
        ).fetchone()[0] == 4

    def test_failure_inside_publish_transaction_rolls_back(
        self, db, archive_dir: Path, tmp_path: Path, monkeypatch
    ):
        """发布事务中途失败 → 整体回滚，无部分可见状态（§5.2 可见性边界）。"""
        zp = write_zip([simple_conversation()], tmp_path / "e.zip")
        imp = importer_mod.ChatGPTImporter(db, archive_dir)

        real_publish = importer_mod.publish_snapshot
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("crash inside transaction window")
            return real_publish(*args, **kwargs)

        monkeypatch.setattr(importer_mod, "publish_snapshot", flaky)
        with pytest.raises(RuntimeError):
            imp.import_archive(zp, "testuser")
        monkeypatch.undo()
        imp.import_archive(zp, "testuser")

        counts = table_counts(db)
        assert counts["events"] == 4
        assert counts["conversation_nodes"] == 5
        assert counts["conversation_selected_path"] == 5


class TestAtomicityUnderCorruptInput:
    def test_no_conversation_data_fails_without_snapshot(
        self, importer, tmp_path: Path
    ):
        zp = tmp_path / "empty.zip"
        import zipfile

        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("readme.txt", "not conversation data")
        with pytest.raises(ValueError, match="NO_CONVERSATION_DATA"):
            importer.import_archive(zp, "testuser")
        assert importer.conn.execute(
            "SELECT COUNT(*) FROM source_snapshots"
        ).fetchone()[0] == 0
        failed = importer.conn.execute(
            "SELECT COUNT(*) FROM import_jobs WHERE status='failed'"
        ).fetchone()[0]
        assert failed == 1


class TestPartialParse:
    def test_partial_conversation_publishes_with_status(
        self, importer, tmp_path: Path
    ):
        """单对话解析失败：其余照常发布，快照标 partial，不冒充完整（§18）。"""
        good = simple_conversation()
        bad = {"conversation_id": "conv-bad", "mapping": None}  # mapping 非对象
        zp = write_zip([good, bad], tmp_path / "p.zip")
        result = importer.import_archive(zp, "testuser")
        assert result.parse_status == "partial"
        snap = importer.conn.execute(
            "SELECT parse_status, coverage_notes FROM source_snapshots"
        ).fetchone()
        assert snap["parse_status"] == "partial"
        assert snap["coverage_notes"]
        # 好对话完整入库
        assert result.events_new == 4
        errors = importer.conn.execute(
            "SELECT error_code FROM import_job_errors"
        ).fetchall()
        assert any(e["error_code"] == "CONVERSATION_PARSE_FAILED" for e in errors)
