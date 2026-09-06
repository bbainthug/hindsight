"""逻辑撤回（§14.1 withdraw / §14.2 步骤 2）。

- withdraw：从可服务的数据中撤回——**所有普通查询不可返回**
  （检索、最近列表在 SQL 层一律过滤 availability）。
- 撤回是版本化状态变更：关闭当前 policy_state 行、写入 withdrawn 行，
  不删除内容 revision，不触发原始数据改动。
- 单事务完成（§5.2/§14.2：撤回与派生失效同一事务）；撤回期间崩溃
  回滚后保持原状，不产生"半撤回"可见状态。
- purge（§14.1）不属本阶段；恢复可见性亦不提供（失败恢复另行处理）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from personal_brain.history.timeutil import utc_now_iso


@dataclass
class WithdrawSummary:
    target: str
    target_id: str
    revisions_withdrawn: int = 0
    events_affected: int = 0
    applied_at: str = ""


def _withdraw_revisions(
    conn: sqlite3.Connection, revision_ids: list[str], now: str
) -> int:
    count = 0
    for rid in revision_ids:
        old_rows = conn.execute(
            """
            SELECT state_id, policy_version, classification_status
            FROM revision_policy_state
            WHERE revision_id = ? AND valid_to IS NULL AND availability = 'available'
            """,
            (rid,),
        ).fetchall()
        for old in old_rows:
            conn.execute(
                "UPDATE revision_policy_state SET valid_to = ? WHERE state_id = ?",
                (now, old["state_id"]),
            )
            conn.execute(
                """
                INSERT INTO revision_policy_state (
                    revision_id, policy_version, availability,
                    classification_status, valid_from
                ) VALUES (?, ?, 'withdrawn', ?, ?)
                """,
                (rid, old["policy_version"], old["classification_status"], now),
            )
            count += 1
    return count


def withdraw_source(
    conn: sqlite3.Connection, source_id: str, *, now: str | None = None
) -> WithdrawSummary:
    """撤回整个来源：全部事件的所有可用版本 + 来源状态（单事务）。"""
    now = now or utc_now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        src = conn.execute(
            "SELECT status FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        if src is None:
            raise ValueError(f"来源不存在: {source_id}")
        if src["status"] == "withdrawn":
            return WithdrawSummary(
                target="source", target_id=source_id, applied_at=now
            )
        rows = conn.execute(
            """
            SELECT DISTINCT r.revision_id, r.event_id FROM event_revisions r
            JOIN events e ON e.event_id = r.event_id
            WHERE e.source_id = ?
            """,
            (source_id,),
        ).fetchall()
        count = _withdraw_revisions(
            conn, [r["revision_id"] for r in rows], now
        )
        conn.execute(
            "UPDATE sources SET status = 'withdrawn' WHERE source_id = ?",
            (source_id,),
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return WithdrawSummary(
        target="source",
        target_id=source_id,
        revisions_withdrawn=count,
        events_affected=len(rows),
        applied_at=now,
    )


def withdraw_event(
    conn: sqlite3.Connection, event_id: str, *, now: str | None = None
) -> WithdrawSummary:
    """撤回单事件（可用版本），不改变来源状态（单事务）。"""
    now = now or utc_now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        ev = conn.execute(
            "SELECT event_id FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if ev is None:
            raise ValueError(f"事件不存在: {event_id}")
        rows = conn.execute(
            "SELECT revision_id FROM event_revisions WHERE event_id = ?",
            (event_id,),
        ).fetchall()
        count = _withdraw_revisions(
            conn, [r["revision_id"] for r in rows], now
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return WithdrawSummary(
        target="event",
        target_id=event_id,
        revisions_withdrawn=count,
        events_affected=1,
        applied_at=now,
    )
