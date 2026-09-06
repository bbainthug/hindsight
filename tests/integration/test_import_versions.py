"""事件版本与更新规则测试（§5.3、Phase A 验收 2）。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from helpers import event_id_for, event_row, revisions_of, snapshots_of
from synthetic import T0, build_conversation, node, simple_conversation, text_message


def _write_zip(payload: object, path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", json.dumps(payload, ensure_ascii=False))
    return path


def _edited_conversation(new_text: str, new_update: float) -> dict:
    """同消息 ID、内容被编辑（ChatGPT 用户编辑消息产生同 ID 不同内容）。"""
    conv = simple_conversation()
    conv["mapping"]["m-0002"]["message"]["content"]["parts"] = [new_text]
    conv["mapping"]["m-0002"]["message"]["update_time"] = new_update
    return conv


class TestContentChange:
    def test_new_revision_keeps_old(self, importer, tmp_path: Path):
        z1 = _write_zip([simple_conversation()], tmp_path / "e1.zip")
        z2 = _write_zip(
            [_edited_conversation("目标是 AI 工程方向（编辑后）", T0 + 9999)],
            tmp_path / "e2.zip",
        )

        importer.import_archive(z1, "testuser")
        eid = event_id_for(importer.conn, "m-0002")
        importer.import_archive(z2, "testuser")

        revs = revisions_of(importer.conn, eid)
        assert len(revs) == 2  # 旧版本保留，不原地覆盖
        event = event_row(importer.conn, eid)
        assert event["revision_resolution"] == "resolved"
        current = importer.conn.execute(
            "SELECT raw_text FROM event_revisions WHERE revision_id=?",
            (event["current_revision_id"],),
        ).fetchone()
        assert current["raw_text"] == "目标是 AI 工程方向（编辑后）"

    def test_out_of_order_older_export_does_not_demote_current(self, importer, tmp_path: Path):
        """新旧导出乱序：先新后旧，current 保持最新版本（Phase A 验收）。"""
        z_old = _write_zip([simple_conversation()], tmp_path / "old.zip")
        z_new = _write_zip(
            [_edited_conversation("最新编辑内容", T0 + 99999)], tmp_path / "new.zip"
        )

        importer.import_archive(z_new, "testuser")
        eid = event_id_for(importer.conn, "m-0002")
        importer.import_archive(z_old, "testuser")

        event = event_row(importer.conn, eid)
        revs = revisions_of(importer.conn, eid)
        assert len(revs) == 2
        assert event["revision_resolution"] == "resolved"
        current = importer.conn.execute(
            "SELECT raw_text FROM event_revisions WHERE revision_id=?",
            (event["current_revision_id"],),
        ).fetchone()
        assert current["raw_text"] == "最新编辑内容"

    def test_equal_update_time_different_content_is_conflict(self, importer, tmp_path: Path):
        """版本先后不明：保留冲突，current 置空，禁止后续自动激活的前提状态。"""
        z1 = _write_zip([simple_conversation()], tmp_path / "e1.zip")
        z2 = _write_zip(
            [_edited_conversation("内容甲", T0 + 600)], tmp_path / "e2.zip"
        )
        z3 = _write_zip(
            [_edited_conversation("内容乙", T0 + 600)], tmp_path / "e3.zip"
        )
        importer.import_archive(z1, "testuser")
        eid = event_id_for(importer.conn, "m-0002")
        importer.import_archive(z2, "testuser")
        importer.import_archive(z3, "testuser")

        event = event_row(importer.conn, eid)
        assert event["current_revision_id"] is None
        assert event["revision_resolution"] == "unresolved"
        assert len(revisions_of(importer.conn, eid)) == 3  # 全部保留

    def test_conflict_resolved_by_strictly_newer(self, importer, tmp_path: Path):
        z1 = _write_zip([simple_conversation()], tmp_path / "e1.zip")
        z2 = _write_zip([_edited_conversation("内容甲", T0 + 600)], tmp_path / "e2.zip")
        z3 = _write_zip([_edited_conversation("内容乙", T0 + 600)], tmp_path / "e3.zip")
        z4 = _write_zip([_edited_conversation("内容丙", T0 + 700)], tmp_path / "e4.zip")

        importer.import_archive(z1, "testuser")
        eid = event_id_for(importer.conn, "m-0002")
        importer.import_archive(z2, "testuser")
        importer.import_archive(z3, "testuser")
        importer.import_archive(z4, "testuser")

        event = event_row(importer.conn, eid)
        assert event["revision_resolution"] == "resolved"
        current = importer.conn.execute(
            "SELECT raw_text FROM event_revisions WHERE revision_id=?",
            (event["current_revision_id"],),
        ).fetchone()
        assert current["raw_text"] == "内容丙"


class TestDisappearance:
    def test_missing_message_not_deleted(self, importer, tmp_path: Path):
        """旧消息在新导出中消失：不推断删除，仅该快照不包含（§5.3）。"""
        z1 = _write_zip([simple_conversation()], tmp_path / "e1.zip")
        z2 = _write_zip([_only_first_message()], tmp_path / "e2.zip")

        importer.import_archive(z1, "testuser")
        eid_gone = event_id_for(importer.conn, "a-0002")
        importer.import_archive(z2, "testuser")

        # 事件与版本仍在
        assert event_row(importer.conn, eid_gone) is not None
        assert len(revisions_of(importer.conn, eid_gone)) == 1
        # 出现位置只指向第一个快照
        occ_snapshots = importer.conn.execute(
            """
            SELECT s.archive_sha256 FROM revision_occurrences o
            JOIN source_snapshots s ON s.snapshot_id = o.snapshot_id
            WHERE o.revision_id = (
                SELECT revision_id FROM event_revisions WHERE event_id = ?
            )
            """,
            (eid_gone,),
        ).fetchall()
        all_snapshots = snapshots_of(importer.conn)
        assert len(all_snapshots) == 2
        assert len(occ_snapshots) == 1  # 第二个快照未包含

    def test_disappearance_keeps_visibility(self, importer, tmp_path: Path):
        """消失消息的 availability 不变（删除是显式操作，不是导入推断）。"""
        z1 = _write_zip([simple_conversation()], tmp_path / "e1.zip")
        z2 = _write_zip([_only_first_message()], tmp_path / "e2.zip")
        importer.import_archive(z1, "testuser")
        importer.import_archive(z2, "testuser")
        states = importer.conn.execute(
            "SELECT DISTINCT availability FROM revision_policy_state"
        ).fetchall()
        assert [s["availability"] for s in states] == ["available"]


class TestTimeUnknownVersions:
    def test_undated_conflicting_content_is_unresolved(self, importer, tmp_path: Path):
        """双方都无时间且内容不同 → 先后不明，保留冲突。"""
        z1 = _write_zip([_undated("版本一")], tmp_path / "e1.zip")
        z2 = _write_zip([_undated("版本二")], tmp_path / "e2.zip")
        importer.import_archive(z1, "testuser")
        eid = event_id_for(importer.conn, "u-0001")
        importer.import_archive(z2, "testuser")
        event = event_row(importer.conn, eid)
        assert event["revision_resolution"] == "unresolved"
        assert event["current_revision_id"] is None
        assert len(revisions_of(importer.conn, eid)) == 2

    def test_undated_reimport_is_stable(self, importer, tmp_path: Path):
        z1 = _write_zip([_undated("版本一")], tmp_path / "e1.zip")
        importer.import_archive(z1, "testuser")
        eid = event_id_for(importer.conn, "u-0001")
        before = event_row(importer.conn, eid)
        z2 = _write_zip([_undated("版本一")], tmp_path / "e2.zip")
        importer.import_archive(z2, "testuser")
        after = event_row(importer.conn, eid)
        assert after["revision_resolution"] == "resolved"
        assert after["current_revision_id"] == before["current_revision_id"]


def _only_first_message() -> dict:
    """仅包含第一轮（m-0001/a-0001）的对话。"""
    m1 = text_message("m-0001", "user", "我最近正在考虑职业方向变化", T0)
    a1 = text_message("a-0001", "assistant", "可以从目标行业和现有技能入手。", T0 + 60)
    mapping = dict(
        [
            node("root", None, None, ["m-0001"]),
            node("m-0001", m1, "root", ["a-0001"]),
            node("a-0001", a1, "m-0001", []),
        ]
    )
    return build_conversation("conv-1", "职业讨论", T0, mapping, "a-0001")


def _undated(text: str) -> dict:
    m = text_message("u-0001", "user", text, None)
    mapping = dict([node("root", None, None, ["u-0001"]), node("u-0001", m, "root", [])])
    return build_conversation("conv-undated", "无时间", None, mapping, "u-0001")
