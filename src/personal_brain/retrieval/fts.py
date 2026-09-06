"""FTS5 派生索引维护（L3，可从 L1 完整重建，§3.2/§6.2）。

- 发布事务内同步写入（与 snapshot 同一事务，§5.2 可见性边界）。
- 撤回内容仍留在索引中，但所有查询必须联接 availability 过滤——
  索引本身不是访问边界（§7.3 过滤发生在排序/返回之前）。
- ``rebuild_fts`` 提供解析器/归一化版本升级后的显式重建路径。
"""

from __future__ import annotations

import sqlite3

from personal_brain.history.db import fts_rowid
from personal_brain.history.normalization import normalize_search_text
from personal_brain.retrieval.bigram import to_index_text


def index_revision(conn: sqlite3.Connection, revision_id: str, raw_text: str | None) -> None:
    """在发布事务内为内容版本建立索引行。

    幂等：rowid = fts_rowid(revision_id) 为主键，INSERT OR REPLACE 按 rowid
    替换（O(log n)）；不做按 revision_id 列的 DELETE（会全扫索引 → O(n²)）。
    """
    bigram_text = to_index_text(normalize_search_text(raw_text)) if raw_text else ""
    conn.execute(
        "INSERT OR REPLACE INTO event_revisions_fts (rowid, bigram_text, revision_id)"
        " VALUES (?, ?, ?)",
        (fts_rowid(revision_id), bigram_text, revision_id),
    )


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """从 event_revisions 全量重建索引（L3 重建，不触碰 L1）；返回行数。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM event_revisions_fts")
        rows = conn.execute(
            "SELECT revision_id, raw_text FROM event_revisions"
        ).fetchall()
        for row in rows:
            bigram_text = (
                to_index_text(normalize_search_text(row["raw_text"]))
                if row["raw_text"]
                else ""
            )
            conn.execute(
                "INSERT OR REPLACE INTO event_revisions_fts (rowid, bigram_text, revision_id)"
                " VALUES (?, ?, ?)",
                (fts_rowid(row["revision_id"]), bigram_text, row["revision_id"]),
            )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return len(rows)


def fts_match(conn: sqlite3.Connection, match_expression: str) -> set[str]:
    """受控 FTS 查询：仅返回候选 revision_id 集合（后续必须逐条验证）。"""
    rows = conn.execute(
        "SELECT revision_id FROM event_revisions_fts WHERE event_revisions_fts MATCH ?",
        (match_expression,),
    ).fetchall()
    return {r["revision_id"] for r in rows}


def fts_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM event_revisions_fts"
    ).fetchone()[0]
