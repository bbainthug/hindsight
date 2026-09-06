"""对话树与分支测试（§4.3、Phase A 验收 2）。"""

from __future__ import annotations

from pathlib import Path

from synthetic import branched_conversation, simple_conversation, write_zip


def _conv_snapshot_row(conn, conv_id: str):
    return conn.execute(
        """
        SELECT cs.* FROM conversation_snapshots cs
        WHERE cs.conversation_id = ?
        """,
        (conv_id,),
    ).fetchone()


def _path_node_ids(conn, conv_snapshot_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT cn.node_id FROM conversation_selected_path sp
        JOIN conversation_nodes cn ON cn.node_key = sp.node_key
        WHERE sp.conv_snapshot_id = ? ORDER BY sp.depth
        """,
        (conv_snapshot_id,),
    ).fetchall()
    return [r["node_id"] for r in rows]


class TestSharedBranchAncestors:
    def test_graph_stored_once_with_both_branches(self, importer, tmp_path: Path):
        zp = write_zip([branched_conversation()], tmp_path / "b.zip")
        importer.import_archive(zp, "testuser")

        # 共享祖先 b-0002 只有一份节点记录，两条分支都在图中
        edges = importer.conn.execute(
            """
            SELECT p.node_id AS parent, c.node_id AS child
            FROM conversation_node_edges e
            JOIN conversation_nodes p ON p.node_key = e.parent_node_key
            JOIN conversation_nodes c ON c.node_key = e.child_node_key
            WHERE p.node_id = 'b-0002'
            ORDER BY e.child_order
            """
        ).fetchall()
        assert [(e["parent"], e["child"]) for e in edges] == [
            ("b-0002", "b-0003"),
            ("b-0002", "b-0004"),
        ]
        # 不存在 branch_id 列：分支成员关系由图和路径推导（§4.3）
        cols = {
            r["name"]
            for r in importer.conn.execute("PRAGMA table_info(conversation_snapshots)")
        }
        assert "branch_id" not in cols

    def test_selected_path_follows_current_branch(self, importer, tmp_path: Path):
        zp = write_zip([branched_conversation()], tmp_path / "b.zip")
        importer.import_archive(zp, "testuser")
        cs = _conv_snapshot_row(importer.conn, "conv-branch")
        assert cs["active_path_known"] == 1
        path = _path_node_ids(importer.conn, cs["conv_snapshot_id"])
        assert path == ["root", "b-0001", "b-0002", "b-0004", "b-0005"]

    def test_shared_ancestor_appears_once_in_selected_path(
        self, importer, tmp_path: Path
    ):
        zp = write_zip([branched_conversation()], tmp_path / "b.zip")
        importer.import_archive(zp, "testuser")
        cs = _conv_snapshot_row(importer.conn, "conv-branch")
        path = _path_node_ids(importer.conn, cs["conv_snapshot_id"])
        assert len(path) == len(set(path))
        assert path.count("b-0002") == 1

    def test_sibling_branch_still_queryable_via_graph(self, importer, tmp_path: Path):
        """左支不在所选路径中，但完整保留在图内（历史检索 all 模式的前提）。"""
        zp = write_zip([branched_conversation()], tmp_path / "b.zip")
        importer.import_archive(zp, "testuser")
        cs = _conv_snapshot_row(importer.conn, "conv-branch")
        members = importer.conn.execute(
            """
            SELECT cn.node_id FROM conversation_snapshot_nodes sn
            JOIN conversation_nodes cn ON cn.node_key = sn.node_key
            WHERE sn.conv_snapshot_id = ?
            """,
            (cs["conv_snapshot_id"],),
        ).fetchall()
        ids = {m["node_id"] for m in members}
        assert "b-0003" in ids  # 左支节点
        assert "b-0003" not in _path_node_ids(importer.conn, cs["conv_snapshot_id"])


class TestUnknownActivePath:
    def test_dangling_parent_chain_marks_unknown(self, importer, tmp_path: Path):
        """回归（验收建议采纳）：父链悬空（父节点在 mapping 外）时
        不能把孤立节点当根 → active_path_unknown，不写猜测路径。"""
        conv = branched_conversation()
        # 断开 b-0001 与其父 root：root 节点从 mapping 移除，父链悬空
        del conv["mapping"]["root"]
        zp = write_zip([conv], tmp_path / "dp.zip")
        importer.import_archive(zp, "testuser")
        cs = _conv_snapshot_row(importer.conn, "conv-branch")
        assert cs["active_path_known"] == 0
        n_paths = importer.conn.execute(
            "SELECT COUNT(*) FROM conversation_selected_path WHERE conv_snapshot_id=?",
            (cs["conv_snapshot_id"],),
        ).fetchone()[0]
        assert n_paths == 0

    def test_missing_current_node_marks_unknown(self, importer, tmp_path: Path):
        """无法确定所选路径 → active_path_unknown，不暗中猜测（§4.3）。"""
        conv = branched_conversation()
        conv["current_node"] = None
        zp = write_zip([conv], tmp_path / "u.zip")
        importer.import_archive(zp, "testuser")
        cs = _conv_snapshot_row(importer.conn, "conv-branch")
        assert cs["active_path_known"] == 0
        assert cs["current_node_id"] is None
        n_paths = importer.conn.execute(
            "SELECT COUNT(*) FROM conversation_selected_path WHERE conv_snapshot_id=?",
            (cs["conv_snapshot_id"],),
        ).fetchone()[0]
        assert n_paths == 0

    def test_dangling_current_node_marks_unknown(self, importer, tmp_path: Path):
        conv = branched_conversation()
        conv["current_node"] = "ghost-node"
        zp = write_zip([conv], tmp_path / "d.zip")
        importer.import_archive(zp, "testuser")
        cs = _conv_snapshot_row(importer.conn, "conv-branch")
        assert cs["active_path_known"] == 0


class TestLinearGraph:
    def test_simple_conversation_path(self, importer, tmp_path: Path):
        zp = write_zip([simple_conversation()], tmp_path / "s.zip")
        importer.import_archive(zp, "testuser")
        cs = _conv_snapshot_row(importer.conn, "conv-1")
        assert cs["active_path_known"] == 1
        assert _path_node_ids(importer.conn, cs["conv_snapshot_id"]) == [
            "root",
            "m-0001",
            "a-0001",
            "m-0002",
            "a-0002",
        ]

    def test_node_event_linkage(self, importer, tmp_path: Path):
        zp = write_zip([simple_conversation()], tmp_path / "s.zip")
        importer.import_archive(zp, "testuser")
        row = importer.conn.execute(
            """
            SELECT e.event_id FROM conversation_nodes n
            JOIN events e ON e.event_id = n.event_id
            WHERE n.node_id = 'm-0001'
            """
        ).fetchone()
        assert row is not None
        assert row["event_id"] == "chatgpt:testuser:m-0001"
