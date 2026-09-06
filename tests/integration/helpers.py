"""集成测试共享查询辅助。"""

from __future__ import annotations

import sqlite3


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "source_snapshots",
        "events",
        "event_revisions",
        "revision_occurrences",
        "conversation_nodes",
        "conversation_node_edges",
        "conversation_snapshots",
        "conversation_selected_path",
        "revision_policy_state",
        "revision_attachments",
    ]
    return {t: count(conn, t) for t in tables}


def event_row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()


def event_id_for(conn: sqlite3.Connection, source_message_id: str) -> str:
    row = conn.execute(
        "SELECT event_id FROM events WHERE source_message_id = ?", (source_message_id,)
    ).fetchone()
    assert row is not None, f"事件不存在: {source_message_id}"
    return row["event_id"]


def revisions_of(conn: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY revision_id",
        (event_id,),
    ).fetchall()


def snapshots_of(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_snapshots ORDER BY imported_at, snapshot_id"
    ).fetchall()
