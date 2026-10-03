"""人工审核（任务书范围 3 / 设计 §11.1-11.2）。

- 全部激活必须经用户确认；审核操作带 actor、时间与前后内容（review_log）。
- 关系建议（origin=llm）只是建议：supersedes / coexists 由用户裁决后
  才确认；拒绝关系不删除任何一侧主张。
- supersedes 禁止自环（schema CHECK）与环路（应用层在确认前检查）。
- 修改措辞产生新的不可变 claim_revision（§10.1），旧修订保留。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Literal

from personal_brain.facts import RELATION_TYPES
from personal_brain.history.timeutil import utc_now_iso

Actor = str
RelationDecision = Literal["supersedes", "coexists", "reject_new", "dismiss"]


@dataclass(frozen=True)
class EvidenceView:
    evidence_id: int
    revision_id: str
    role: str
    support_check: str
    span_start: int
    span_end: int
    quote: str
    source_created_at: str | None
    conversation_id: str
    speaker_type: str


@dataclass
class ClaimView:
    claim_id: str
    claim_revision_id: str
    content: str
    memory_type: str
    assertion_kind: str
    review_status: str
    lifecycle_status: str
    verification_status: str
    asserted_at: str | None
    recorded_at: str
    extraction_run_id: str | None
    evidence: list[EvidenceView] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)  # 建议与已确认关系


def _evidence_view(conn: sqlite3.Connection, claim_revision_id: str) -> list[EvidenceView]:
    rows = conn.execute(
        """
        SELECT ce.evidence_id, ce.revision_id, ce.role, ce.support_check,
               ce.span_start, ce.span_end,
               substr(er.raw_text, ce.span_start + 1, ce.span_end - ce.span_start)
                   AS quote,
               er.source_created_at, e.conversation_id, e.speaker_type
        FROM claim_evidence ce
        JOIN event_revisions er ON er.revision_id = ce.revision_id
        JOIN events e ON e.event_id = er.event_id
        WHERE ce.claim_revision_id = ?
        ORDER BY er.source_created_at, ce.evidence_id
        """,
        (claim_revision_id,),
    ).fetchall()
    return [
        EvidenceView(
            evidence_id=r["evidence_id"],
            revision_id=r["revision_id"],
            role=r["role"],
            support_check=r["support_check"],
            span_start=r["span_start"],
            span_end=r["span_end"],
            quote=r["quote"] or "",
            source_created_at=r["source_created_at"],
            conversation_id=r["conversation_id"],
            speaker_type=r["speaker_type"],
        )
        for r in rows
    ]


def _relations_view(conn: sqlite3.Connection, claim_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.relation_id, r.from_claim_id, r.to_claim_id, r.relation_type,
               r.status, r.origin, r.created_at, r.decided_at,
               ocr.content AS from_content, tcr.content AS to_content
        FROM claim_relations r
        JOIN claims oc ON oc.claim_id = r.from_claim_id
        JOIN claim_revisions ocr ON ocr.claim_revision_id = oc.current_revision_id
        JOIN claims tc ON tc.claim_id = r.to_claim_id
        JOIN claim_revisions tcr ON tcr.claim_revision_id = tc.current_revision_id
        WHERE r.from_claim_id = ? OR r.to_claim_id = ?
        ORDER BY r.created_at
        """,
        (claim_id, claim_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_claim_view(conn: sqlite3.Connection, claim_id: str) -> ClaimView | None:
    row = conn.execute(
        """
        SELECT cr.* FROM claim_revisions cr
        JOIN claims c ON c.current_revision_id = cr.claim_revision_id
        WHERE c.claim_id = ?
        """,
        (claim_id,),
    ).fetchone()
    if row is None:
        return None
    view = ClaimView(
        claim_id=row["claim_id"],
        claim_revision_id=row["claim_revision_id"],
        content=row["content"],
        memory_type=row["memory_type"],
        assertion_kind=row["assertion_kind"],
        review_status=row["review_status"],
        lifecycle_status=row["lifecycle_status"],
        verification_status=row["verification_status"],
        asserted_at=row["asserted_at"],
        recorded_at=row["recorded_at"],
        extraction_run_id=row["extraction_run_id"],
    )
    view.evidence = _evidence_view(conn, row["claim_revision_id"])
    view.relations = _relations_view(conn, row["claim_id"])
    return view


def pending_claims(
    conn: sqlite3.Connection,
    *,
    include_needs_review: bool = True,
    memory_type: str | None = None,
) -> list[ClaimView]:
    """审核队列：pending 在前、needs_review 在后（都要人看）。

    needs_review 含两种来源：待审核候选被判证据失效，以及已生效
    （active）主张的 support 证据被撤回（派生失效）——后者 lifecycle
    仍是 active，也要回到队列复核。
    """
    sql = """
        SELECT c.claim_id FROM claims c
        JOIN claim_revisions cr ON cr.claim_revision_id = c.current_revision_id
        WHERE (
            (cr.review_status = 'pending' AND c.lifecycle_status = 'candidate')
            OR (cr.review_status = 'needs_review'
                AND c.lifecycle_status IN ('candidate', 'active'))
        )
    """
    params: list = []
    if not include_needs_review:
        sql += " AND cr.review_status = 'pending'"
    if memory_type:
        sql += " AND c.memory_type = ?"
        params.append(memory_type)
    sql += (
        " ORDER BY CASE cr.review_status WHEN 'pending' THEN 0 ELSE 1 END,"
        " cr.asserted_at, c.claim_id"
    )
    return [
        v
        for (cid,) in conn.execute(sql, params).fetchall()
        if (v := get_claim_view(conn, cid)) is not None
    ]


def _log(
    conn: sqlite3.Connection,
    claim_id: str,
    claim_revision_id: str,
    action: str,
    actor: str,
    *,
    note: str | None = None,
    before: str | None = None,
    after: str | None = None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO review_log (
            claim_id, claim_revision_id, action, actor, note,
            before_content, after_content, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (claim_id, claim_revision_id, action, actor, note, before, after, now),
    )


def _current_revision(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT cr.* FROM claim_revisions cr
        JOIN claims c ON c.current_revision_id = cr.claim_revision_id
        WHERE c.claim_id = ?
        """,
        (claim_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"主张不存在: {claim_id}")
    return row


def approve_claim(
    conn: sqlite3.Connection, claim_id: str, *, actor: Actor = "user", now: str | None = None
) -> None:
    """通过：active + user_confirmed（§11.2 状态迁移带 actor/时间）。"""
    now = now or utc_now_iso()
    rev = _current_revision(conn, claim_id)
    if rev["review_status"] == "rejected":
        raise ValueError("已拒绝的候选不能直接通过（请重新提炼或手工修正）")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE claim_revisions SET
                review_status='approved', lifecycle_status='active',
                verification_status='user_confirmed'
            WHERE claim_revision_id = ?
            """,
            (rev["claim_revision_id"],),
        )
        conn.execute(
            "UPDATE claims SET lifecycle_status='active', updated_at=? WHERE claim_id=?",
            (now, claim_id),
        )
        _log(
            conn, claim_id, rev["claim_revision_id"], "approved", actor,
            before=rev["content"], after=rev["content"], now=now,
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def reject_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    actor: Actor = "user",
    note: str | None = None,
    now: str | None = None,
) -> None:
    """拒绝：候选标记 rejected（保留记录，不删除——宁留痕不静默）。"""
    now = now or utc_now_iso()
    rev = _current_revision(conn, claim_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE claim_revisions SET review_status='rejected' WHERE claim_revision_id=?",
            (rev["claim_revision_id"],),
        )
        _log(
            conn, claim_id, rev["claim_revision_id"], "rejected", actor,
            note=note, before=rev["content"], now=now,
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def edit_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    new_content: str,
    *,
    actor: Actor = "user",
    now: str | None = None,
) -> str:
    """修改措辞：新建不可变修订并直接生效（用户的措辞即确认）。"""
    new_content = new_content.strip()
    if not new_content:
        raise ValueError("修改后的内容不能为空")
    now = now or utc_now_iso()
    rev = _current_revision(conn, claim_id)
    new_revision_id = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO claim_revisions (
                claim_revision_id, claim_id, content, memory_type, assertion_kind,
                verification_status, review_status, lifecycle_status,
                subject_id, context, asserted_at, recorded_at,
                extraction_run_id, rule_version
            ) VALUES (?, ?, ?, ?, ?, 'user_confirmed', 'approved', 'active',
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                new_revision_id, claim_id, new_content, rev["memory_type"],
                rev["assertion_kind"], rev["subject_id"], rev["context"],
                rev["asserted_at"], now, rev["extraction_run_id"], rev["rule_version"],
            ),
        )
        # 证据边继承到新修订（§10.2：证据属于主张，不是属于某次措辞）
        conn.execute(
            """
            INSERT INTO claim_evidence (
                claim_revision_id, revision_id, span_start, span_end, span_sha256,
                role, support_check, check_method, check_version, checked_at
            )
            SELECT ?, revision_id, span_start, span_end, span_sha256,
                   role, support_check, check_method, check_version, ?
            FROM claim_evidence WHERE claim_revision_id = ?
            """,
            (new_revision_id, now, rev["claim_revision_id"]),
        )
        conn.execute(
            "UPDATE claims SET current_revision_id=?, updated_at=? WHERE claim_id=?",
            (new_revision_id, now, claim_id),
        )
        _log(
            conn, claim_id, new_revision_id, "edited", actor,
            before=rev["content"], after=new_content, now=now,
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return new_revision_id


def skip_claim(
    conn: sqlite3.Connection, claim_id: str, *, actor: Actor = "user", now: str | None = None
) -> None:
    """跳过：不改状态，仅记录（留在队列里下次再审）。"""
    now = now or utc_now_iso()
    rev = _current_revision(conn, claim_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _log(conn, claim_id, rev["claim_revision_id"], "skipped", actor, now=now)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _has_supersedes_path(
    conn: sqlite3.Connection, from_claim: str, to_claim: str
) -> bool:
    """确认 supersedes 前检查是否会成环（from →…→ to 已存在路径则成环）。"""
    frontier = [from_claim]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current == to_claim:
            return True
        if current in seen:
            continue
        seen.add(current)
        rows = conn.execute(
            """
            SELECT to_claim_id FROM claim_relations
            WHERE from_claim_id = ? AND relation_type = 'supersedes'
              AND status = 'confirmed'
            """,
            (current,),
        ).fetchall()
        frontier.extend(r["to_claim_id"] for r in rows)
    return False


def decide_relation(
    conn: sqlite3.Connection,
    relation_id: str,
    decision: RelationDecision,
    *,
    actor: Actor = "user",
    now: str | None = None,
) -> None:
    """裁决一条关系建议（任务书用户故事 3）。

    - supersedes：新主张生效（approved/active），旧主张标记 superseded
      （保留、不删除）；确认前检查 supersedes 不成环（§11.1）。
    - coexists：两条都保留；新主张是否同时生效由用户另行通过审核
      （此裁决只确认关系本身）。
    - reject_new：拒绝新候选，关系建议标记 dismissed。
    - dismiss：忽略建议，不做任何状态变更。
    """
    if decision not in ("supersedes", "coexists", "reject_new", "dismiss"):
        raise ValueError(f"未知裁决: {decision}")
    now = now or utc_now_iso()
    rel = conn.execute(
        "SELECT * FROM claim_relations WHERE relation_id = ?", (relation_id,)
    ).fetchone()
    if rel is None:
        raise ValueError(f"关系不存在: {relation_id}")
    if rel["relation_type"] not in RELATION_TYPES:
        raise ValueError("关系类型非法")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # review_log.claim_revision_id 有外键：先取两侧当前修订
        from_rev = _current_revision(conn, rel["from_claim_id"])
        if decision == "dismiss":
            conn.execute(
                "UPDATE claim_relations SET status='dismissed', decided_at=? "
                "WHERE relation_id=?",
                (now, relation_id),
            )
            _log(
                conn, rel["from_claim_id"], from_rev["claim_revision_id"],
                "relation_dismissed", actor, now=now,
            )
        elif decision == "coexists":
            conn.execute(
                "UPDATE claim_relations SET status='confirmed', decided_at=?, "
                "relation_type='coexists' WHERE relation_id=?",
                (now, relation_id),
            )
            _log(
                conn, rel["from_claim_id"], from_rev["claim_revision_id"],
                "relation_confirmed", actor,
                note="coexists：两条并存", now=now,
            )
        elif decision == "reject_new":
            conn.execute(
                "UPDATE claim_relations SET status='dismissed', decided_at=? "
                "WHERE relation_id=?",
                (now, relation_id),
            )
            conn.execute(
                "UPDATE claim_revisions SET review_status='rejected' "
                "WHERE claim_revision_id=?",
                (from_rev["claim_revision_id"],),
            )
            _log(
                conn, rel["from_claim_id"], from_rev["claim_revision_id"],
                "rejected", actor, note="关系裁决时拒绝新候选", now=now,
            )
        else:  # supersedes
            if _has_supersedes_path(conn, rel["to_claim_id"], rel["from_claim_id"]):
                raise ValueError(
                    "该 supersedes 会形成替代环路（§11.1 禁止）；请改用 coexists"
                )
            conn.execute(
                "UPDATE claim_relations SET status='confirmed', decided_at=?, "
                "relation_type='supersedes' WHERE relation_id=?",
                (now, relation_id),
            )
            conn.execute(
                """
                UPDATE claim_revisions SET review_status='approved',
                    lifecycle_status='active', verification_status='user_confirmed'
                WHERE claim_revision_id = ?
                """,
                (from_rev["claim_revision_id"],),
            )
            conn.execute(
                "UPDATE claims SET lifecycle_status='active', updated_at=? "
                "WHERE claim_id=?",
                (now, rel["from_claim_id"]),
            )
            old_rev = _current_revision(conn, rel["to_claim_id"])
            conn.execute(
                "UPDATE claim_revisions SET lifecycle_status='superseded', "
                "valid_to=? WHERE claim_revision_id=?",
                (now, old_rev["claim_revision_id"]),
            )
            conn.execute(
                "UPDATE claims SET lifecycle_status='superseded', updated_at=? "
                "WHERE claim_id=?",
                (now, rel["to_claim_id"]),
            )
            _log(
                conn, rel["from_claim_id"], from_rev["claim_revision_id"],
                "relation_confirmed", actor,
                note=f"supersedes {rel['to_claim_id']}", now=now,
            )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


__all__ = [
    "ClaimView",
    "EvidenceView",
    "approve_claim",
    "decide_relation",
    "edit_claim",
    "get_claim_view",
    "pending_claims",
    "reject_claim",
    "skip_claim",
]
