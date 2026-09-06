"""幂等导入测试（§5.3、Phase A 验收 1）。"""

from __future__ import annotations

from pathlib import Path

from helpers import table_counts
from synthetic import simple_conversation, write_directory, write_zip, write_zip_bytes_varied


class TestSameArchiveReimport:
    def test_reimport_is_full_noop(self, importer, tmp_path: Path):
        zp = write_zip([simple_conversation()], tmp_path / "exp.zip")

        first = importer.import_archive(zp, "testuser")
        assert first.job_kind == "published"
        assert first.events_new == 4
        assert first.revisions_new == 4
        snapshot_id = first.snapshot_id

        counts_after_first = table_counts(importer.conn)

        second = importer.import_archive(zp, "testuser")
        assert second.job_kind == "duplicate"
        assert second.snapshot_id == snapshot_id
        assert second.events_new == 0
        assert second.revisions_new == 0
        assert second.occurrences_new == 0
        assert second.nodes_new == 0

        assert table_counts(importer.conn) == counts_after_first

    def test_reimport_does_not_duplicate_jobs_side_effects(
        self, importer, tmp_path: Path
    ):
        zp = write_zip([simple_conversation()], tmp_path / "exp.zip")
        importer.import_archive(zp, "testuser")
        importer.import_archive(zp, "testuser")
        jobs = importer.conn.execute(
            "SELECT conversations_seen, events_new, revisions_new FROM import_jobs"
        ).fetchall()
        # 首次发布 + 一次去重记录；去重作业全零（不重复触发后续提炼类动作）
        assert len(jobs) == 2
        assert all(j["events_new"] == 0 and j["revisions_new"] == 0 for j in jobs[1:])
        assert jobs[0]["events_new"] == 4


class TestSameContentDifferentArchive:
    def test_new_snapshot_adds_occurrences_not_events(self, importer, tmp_path: Path):
        """新导出重复包含旧消息：增加出现位置，事件身份不重复（§5.3）。"""
        payload = [simple_conversation()]
        z1 = write_zip(payload, tmp_path / "e1.zip")
        z2 = write_zip_bytes_varied(payload, tmp_path / "e2.zip")  # 同内容不同字节

        r1 = importer.import_archive(z1, "testuser")
        r2 = importer.import_archive(z2, "testuser")

        assert r1.job_kind == "published"
        assert r2.job_kind == "published"
        assert r2.snapshot_id != r1.snapshot_id  # 不同归档 → 不同快照（可并存）
        assert r2.events_new == 0
        assert r2.revisions_new == 0
        assert r2.occurrences_new == 4  # 每条消息在新快照中各出现一次

        assert importer.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4
        assert importer.conn.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0] == 4
        assert importer.conn.execute(
            "SELECT COUNT(DISTINCT snapshot_id) FROM revision_occurrences"
        ).fetchone()[0] == 2

    def test_directory_and_zip_same_content_shared_source(self, importer, tmp_path: Path):
        d1 = write_directory([simple_conversation()], tmp_path / "d1")
        z1 = write_zip([simple_conversation()], tmp_path / "z1.zip")
        r1 = importer.import_archive(d1, "testuser")
        r2 = importer.import_archive(z1, "testuser")
        assert r1.source_id == r2.source_id
        assert r2.events_new == 0


class TestSourceIdentity:
    def test_same_namespace_reuses_source(self, importer, tmp_path: Path):
        z1 = write_zip([simple_conversation()], tmp_path / "a.zip")
        z2 = write_zip([simple_conversation()], tmp_path / "b.zip")
        r1 = importer.import_archive(z1, "testuser")
        r2 = importer.import_archive(z2, "testuser")
        assert r1.source_id == r2.source_id
        assert importer.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1

    def test_namespace_scopes_event_identity(self, importer, tmp_path: Path):
        """同一内容导入到两个账号命名空间 → 两套事件身份。"""
        zp = write_zip([simple_conversation()], tmp_path / "a.zip")
        importer.import_archive(zp, "alice")
        r2 = importer.import_archive(zp, "bob")
        assert r2.source_id != importer.conn.execute(
            "SELECT source_id FROM sources WHERE account_namespace='alice'"
        ).fetchone()[0]
        assert importer.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 8

    def test_invalid_namespace_rejected(self, importer, tmp_path: Path):
        zp = write_zip([simple_conversation()], tmp_path / "a.zip")
        for bad in ["", "带空格 ns", "a" * 65, "../escape"]:
            try:
                importer.import_archive(zp, bad)
            except ValueError:
                continue
            raise AssertionError(f"应拒绝非法别名: {bad!r}")
