"""派生失效（设计 Phase D / §14：撤回传播到派生数据）。

源 revision 撤回（withdraw-event / withdraw-source）时，依赖它的
support 证据失效：相关主张标记 review_status='needs_review'（不删除、
不改写内容），并写 review_log（actor=system:withdraw）。必须在撤回
同一事务内调用（§5.2/§14.2：状态变更与派生失效同事务）。
"""

from __future__ import annotations

import sqlite3

from personal_brain.history.timeutil import utc_now_iso


def invalidate_claims_for_revisions(
    conn: sqlite3.Connection,
    revision_ids: list[str],
    *,
    now: str | None = None,
    actor: str = "system:withdraw",
) -> int:
    """把 support 证据指向这些 revision 的主张标记 needs_review。

    返回受影响的主张修订数。重复调用幂等（已是 needs_review 的不再
    重复计数/记日志）。
    """
    if not revision_ids:
        return 0
    now = now or utc_now_iso()
    placeholders = ",".join("?" for _ in revision_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT cr.claim_revision_id, cr.claim_id
        FROM claim_revisions cr
        JOIN claim_evidence ce ON ce.claim_revision_id = cr.claim_revision_id
        WHERE ce.role = 'support'
          AND cr.review_status IN ('pending', 'approved')
          AND ce.revision_id IN ({placeholders})
        """,
        revision_ids,
    ).fetchall()
    if not rows:
        return 0
    for row in rows:
        conn.execute(
            "UPDATE claim_revisions SET review_status='needs_review' "
            "WHERE claim_revision_id = ?",
            (row["claim_revision_id"],),
        )
        conn.execute(
            """
            INSERT INTO review_log (
                claim_id, claim_revision_id, action, actor, note, decided_at
            ) VALUES (?, ?, 'needs_review', ?, ?, ?)
            """,
            (
                row["claim_id"],
                row["claim_revision_id"],
                actor,
                "support 证据的源 revision 被撤回，主张需人工复核（派生失效）",
                now,
            ),
        )
    return len(rows)
