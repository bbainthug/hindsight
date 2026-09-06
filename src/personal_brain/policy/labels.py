"""访问标签：本地显式分类（§7.2）。

- 标签作用于**内容版本**的版本化策略状态：修改标签不改写内容
  revision，也不重算 revision_hash（§4.2）。
- 变更 = 关闭当前状态行（valid_to）+ 写入新状态行，历史可追溯。
- ``label_source`` 把来源下全部事件的所有可用版本归入 scope；
  混合话题需要事件级标签（``label_event``），由 CLI 显示该限制。
- 本模块只做存储与查询过滤支撑；MCP 端策略执行属 Phase B。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from personal_brain.history.timeutil import utc_now_iso

_VALID_CLASSIFICATION = {"unclassified", "rule_classified", "human_reviewed"}


@dataclass
class LabelSummary:
    target: str
    target_id: str
    revisions_relabeled: int = 0
    scope_labels: tuple[str, ...] = ()
    sensitivity_labels: tuple[str, ...] = ()
    classification_status: str = "human_reviewed"
    policy_version: str = "v0-local"
    applied_at: str = ""
    warnings: list[str] = field(default_factory=list)


def _close_and_relabel(
    conn: sqlite3.Connection,
    *,
    revision_ids: list[str],
    scope_labels: tuple[str, ...],
    sensitivity_labels: tuple[str, ...],
    classification_status: str,
    policy_version: str,
    now: str,
) -> int:
    if classification_status not in _VALID_CLASSIFICATION:
        raise ValueError(f"非法 classification_status: {classification_status}")
    count = 0
    for rid in revision_ids:
        old_rows = conn.execute(
            """
            SELECT state_id, classification_status FROM revision_policy_state
            WHERE revision_id = ? AND valid_to IS NULL
            """,
            (rid,),
        ).fetchall()
        for old in old_rows:
            # 继承未显式给出的标签？不：显式标注是权威覆盖（本地人工输入）
            conn.execute(
                "UPDATE revision_policy_state SET valid_to = ? WHERE state_id = ?",
                (now, old["state_id"]),
            )
            cur = conn.execute(
                """
                INSERT INTO revision_policy_state (
                    revision_id, policy_version, availability,
                    classification_status, valid_from
                ) VALUES (?, ?, 'available', ?, ?)
                """,
                (rid, policy_version, classification_status, now),
            )
            state_id = cur.lastrowid
            for label in scope_labels:
                conn.execute(
                    "INSERT OR IGNORE INTO revision_scope_labels (state_id, label) VALUES (?, ?)",
                    (state_id, label),
                )
            for label in sensitivity_labels:
                conn.execute(
                    "INSERT OR IGNORE INTO revision_sensitivity_labels"
                    " (state_id, label) VALUES (?, ?)",
                    (state_id, label),
                )
            count += 1
    return count


def label_source(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    scope_labels: tuple[str, ...] = (),
    sensitivity_labels: tuple[str, ...] = (),
    classification_status: str = "human_reviewed",
    policy_version: str = "v0-local",
    now: str | None = None,
) -> LabelSummary:
    """把来源下全部事件的**可用**内容版本归入给定标签（单事务）。"""
    now = now or utc_now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        src = conn.execute(
            "SELECT source_id, account_namespace FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if src is None:
            raise ValueError(f"来源不存在: {source_id}")
        revision_ids = [
            r["revision_id"]
            for r in conn.execute(
                """
                SELECT DISTINCT r.revision_id FROM event_revisions r
                JOIN events e ON e.event_id = r.event_id
                WHERE e.source_id = ?
                """,
                (source_id,),
            )
        ]
        count = _close_and_relabel(
            conn,
            revision_ids=revision_ids,
            scope_labels=scope_labels,
            sensitivity_labels=sensitivity_labels,
            classification_status=classification_status,
            policy_version=policy_version,
            now=now,
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return LabelSummary(
        target="source",
        target_id=source_id,
        revisions_relabeled=count,
        scope_labels=scope_labels,
        sensitivity_labels=sensitivity_labels,
        classification_status=classification_status,
        policy_version=policy_version,
        applied_at=now,
        warnings=[
            "来源级标签覆盖该来源全部对话；混合话题请使用事件级标签 "
            "(label-event)，不要仅凭来源开放（§7.2）"
        ],
    )


def label_event(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    scope_labels: tuple[str, ...] = (),
    sensitivity_labels: tuple[str, ...] = (),
    classification_status: str = "human_reviewed",
    policy_version: str = "v0-local",
    now: str | None = None,
) -> LabelSummary:
    """把单事件全部可用内容版本归入给定标签（单事务）。"""
    now = now or utc_now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        ev = conn.execute(
            "SELECT event_id FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if ev is None:
            raise ValueError(f"事件不存在: {event_id}")
        revision_ids = [
            r["revision_id"]
            for r in conn.execute(
                "SELECT revision_id FROM event_revisions WHERE event_id = ?",
                (event_id,),
            )
        ]
        count = _close_and_relabel(
            conn,
            revision_ids=revision_ids,
            scope_labels=scope_labels,
            sensitivity_labels=sensitivity_labels,
            classification_status=classification_status,
            policy_version=policy_version,
            now=now,
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return LabelSummary(
        target="event",
        target_id=event_id,
        revisions_relabeled=count,
        scope_labels=scope_labels,
        sensitivity_labels=sensitivity_labels,
        classification_status=classification_status,
        policy_version=policy_version,
        applied_at=now,
    )
