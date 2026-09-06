"""时间语义与数据模型约束测试（§4.4 / §4.2）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from helpers import event_id_for
from synthetic import (
    multimodal_conversation,
    naive_time_conversation,
    simple_conversation,
    write_zip,
)


class TestTimeSemantics:
    def test_utc_storage_and_original_value(self, importer, tmp_path: Path):
        zp = write_zip([simple_conversation()], tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        eid = event_id_for(importer.conn, "m-0001")
        rev = importer.conn.execute(
            "SELECT * FROM event_revisions WHERE event_id=?", (eid,)
        ).fetchone()
        assert rev["source_created_at"] == "2026-08-01T08:00:00.000000Z"
        assert rev["source_updated_at"] == "2026-08-01T08:00:00.000000Z"
        assert rev["original_time_value"] == "1785571200.0"
        assert rev["time_precision"] == "second"
        assert rev["timezone_status"] == "utc"

    def test_unknown_time_not_fabricated(self, importer, tmp_path: Path):
        """无法确定的时间不以 imported_at 代替（§4.4）。"""
        zp = write_zip([naive_time_conversation()], tmp_path / "t.zip")
        result = importer.import_archive(zp, "testuser")
        eid = event_id_for(importer.conn, "t-0001")
        rev = importer.conn.execute(
            "SELECT * FROM event_revisions WHERE event_id=?", (eid,)
        ).fetchone()
        assert rev["source_created_at"] is None
        assert rev["time_precision"] == "unknown"
        snap = importer.conn.execute(
            "SELECT coverage_start, coverage_end FROM source_snapshots"
        ).fetchone()
        assert snap["coverage_start"] is None
        assert snap["coverage_end"] is None
        assert result.parse_status == "complete"

    def test_coverage_only_spans_known_times(self, importer, tmp_path: Path):
        payload = [simple_conversation(), naive_time_conversation()]
        zp = write_zip(payload, tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        snap = importer.conn.execute(
            "SELECT coverage_start, coverage_end FROM source_snapshots"
        ).fetchone()
        assert snap["coverage_start"] == "2026-08-01T08:00:00.000000Z"
        assert snap["coverage_end"] == "2026-08-01T08:03:00.000000Z"


class TestPolicyStateInitialization:
    def test_initial_state_available_unclassified(self, importer, tmp_path: Path):
        """每个内容版本初始化独立策略状态：available + unclassified（§4.2/§7.2）。"""
        zp = write_zip([simple_conversation()], tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        rows = importer.conn.execute(
            "SELECT availability, classification_status, policy_version, valid_to "
            "FROM revision_policy_state"
        ).fetchall()
        assert len(rows) == 4
        assert all(
            r["availability"] == "available"
            and r["classification_status"] == "unclassified"
            and r["policy_version"] == "v0"
            and r["valid_to"] is None
            for r in rows
        )


class TestSearchTextGeneration:
    def test_search_text_normalized(self, importer, tmp_path: Path):
        zp = write_zip([simple_conversation()], tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        eid = event_id_for(importer.conn, "m-0001")
        rev = importer.conn.execute(
            "SELECT search_text, search_text_norm_version FROM event_revisions WHERE event_id=?",
            (eid,),
        ).fetchone()
        assert rev["search_text"] == "我最近正在考虑职业方向变化"
        assert rev["search_text_norm_version"] == "nfkc-casefold-v1"


class TestSourceAssetsManifest:
    def test_member_manifest_persisted(self, importer, tmp_path: Path):
        """§5.2 步骤 1：归档 manifest 落库为成员级清单（验收建议采纳）。"""
        zp = write_zip([simple_conversation()], tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        rows = importer.conn.execute(
            "SELECT member_path, asset_kind, size_bytes, length(asset_sha256) AS h "
            "FROM source_assets"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["member_path"] == "conversations.json"
        assert rows[0]["asset_kind"] == "conversation_data"
        assert rows[0]["size_bytes"] > 0
        assert rows[0]["h"] == 64  # sha256 hex

    def test_manifest_not_duplicated_on_reimport(self, importer, tmp_path: Path):
        zp = write_zip([simple_conversation()], tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        importer.import_archive(zp, "testuser")
        assert importer.conn.execute(
            "SELECT COUNT(*) FROM source_assets"
        ).fetchone()[0] == 1


class TestUnsupportedCountScope:
    def test_counts_unsupported_content(self, importer, tmp_path: Path):
        """unsupported_count 统一覆盖 UNSUPPORTED_* 口径。"""
        zp = write_zip([multimodal_conversation()], tmp_path / "t.zip")
        result = importer.import_archive(zp, "testuser")
        # 多模态对话含 1 个不支持的 part（数字 12345）
        assert result.unsupported_count == 1
        job = importer.conn.execute(
            "SELECT unsupported_count FROM import_jobs WHERE status='published'"
        ).fetchone()
        assert job["unsupported_count"] == 1


class TestAttachments:
    def test_attachment_refs_persisted(self, importer, tmp_path: Path):
        zp = write_zip([multimodal_conversation()], tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        rows = importer.conn.execute(
            "SELECT asset_pointer, content_type FROM revision_attachments"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["asset_pointer"] == "file-service://synthetic-image-1"
        assert rows[0]["content_type"] == "image_asset_pointer"


class TestSchemaConstraints:
    def test_foreign_keys_enforced(self, importer):
        assert importer.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            importer.conn.execute(
                "INSERT INTO events (event_id, source_id, conversation_id, "
                "source_message_id, revision_resolution, speaker_type) "
                "VALUES ('x', 'no-such-source', 'c', 'm', 'resolved', 'owner')"
            )

    def test_enum_check_on_events(self, importer):
        with pytest.raises(sqlite3.IntegrityError):
            importer.conn.execute(
                "INSERT INTO events (event_id, source_id, conversation_id, "
                "source_message_id, revision_resolution, speaker_type) "
                "VALUES ('x', 's', 'c', 'm', 'bogus', 'owner')"
            )

    def test_unique_event_revision_hash(self, importer, tmp_path: Path):
        import pytest

        zp = write_zip([simple_conversation()], tmp_path / "t.zip")
        importer.import_archive(zp, "testuser")
        eid = event_id_for(importer.conn, "m-0001")
        rev = importer.conn.execute(
            "SELECT revision_hash FROM event_revisions WHERE event_id=?", (eid,)
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            importer.conn.execute(
                "INSERT INTO event_revisions (revision_id, event_id, revision_hash, "
                "content_type, recorded_at, source_locator) "
                "VALUES ('dup', ?, ?, 'text', '2026-01-01T00:00:00Z', 'x')",
                (eid, rev["revision_hash"]),
            )

    def test_snapshot_source_unique(self, importer, tmp_path: Path):
        """同一来源同一归档摘要唯一：数据库层兜底幂等。"""
        z1 = write_zip([simple_conversation()], tmp_path / "a.zip")
        z2 = write_zip([simple_conversation()], tmp_path / "b.zip")
        r1 = importer.import_archive(z1, "testuser")
        r2 = importer.import_archive(z2, "testuser")
        assert r1.snapshot_id == r2.snapshot_id
        assert importer.conn.execute(
            "SELECT COUNT(*) FROM source_snapshots"
        ).fetchone()[0] == 1
