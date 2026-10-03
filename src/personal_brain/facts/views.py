"""只读视图：list / timeline / explain（任务书范围 3，用户故事 4）。

展示时对引用文本做凭据遮蔽（存储的偏移基于原文，展示用等长遮蔽
不破坏区间）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from personal_brain.retrieval.credentials import redact_known_credentials


@dataclass
class ClaimListRow:
    claim_id: str
    content: str
    memory_type: str
    assertion_kind: str
    review_status: str
    lifecycle_status: str
    asserted_at: str | None
    evidence_count: int
    superseded_by: str | None = None  # 指向替代它的主张（superseded 时展示）


@dataclass
class ExplainData:
    claim_id: str
    content: str
    memory_type: str
    assertion_kind: str
    review_status: str
    lifecycle_status: str
    verification_status: str
    revisions: list[dict] = field(default_factory=list)  # 修订历史
    evidence: list[dict] = field(default_factory=list)  # 当前修订全部证据原话
    relations: list[dict] = field(default_factory=list)


def list_claims(
    conn: sqlite3.Connection,
    *,
    memory_type: str | None = None,
    status: str | None = None,
) -> list[ClaimListRow]:
    """主张列表；status 取 review_status 或 lifecycle 值（active/pending 等）。"""
    sql = """
        SELECT c.claim_id, cr.content, c.memory_type, cr.assertion_kind,
               cr.review_status, c.lifecycle_status AS lc_status,
               cr.asserted_at,
               (SELECT COUNT(*) FROM claim_evidence ce
                WHERE ce.claim_revision_id = cr.claim_revision_id) AS ev_count
        FROM claims c
        JOIN claim_revisions cr ON cr.claim_revision_id = c.current_revision_id
        WHERE 1=1
    """
    params: list = []
    if memory_type:
        sql += " AND c.memory_type = ?"
        params.append(memory_type)
    if status:
        sql += " AND (cr.review_status = ? OR c.lifecycle_status = ?)"
        params.extend([status, status])
    sql += " ORDER BY cr.asserted_at, c.claim_id"
    rows = conn.execute(sql, params).fetchall()
    out = [
        ClaimListRow(
            claim_id=r["claim_id"],
            content=redact_known_credentials(r["content"]) or "",
            memory_type=r["memory_type"],
            assertion_kind=r["assertion_kind"],
            review_status=r["review_status"],
            lifecycle_status=r["lc_status"],
            asserted_at=r["asserted_at"],
            evidence_count=r["ev_count"],
        )
        for r in rows
    ]
    # 被替代的主张补一个指向替代者的指针（有确认的 supersedes 入边）
    for row in out:
        rel = conn.execute(
            """
            SELECT from_claim_id FROM claim_relations
            WHERE to_claim_id = ? AND relation_type = 'supersedes'
              AND status = 'confirmed' LIMIT 1
            """,
            (row.claim_id,),
        ).fetchone()
        if rel is not None:
            row.superseded_by = rel["from_claim_id"]
    return out


def decision_timeline(
    conn: sqlite3.Connection,
    *,
    memory_type: str = "decision",
    include_superseded: bool = False,
) -> list[ClaimListRow]:
    """决策时间线：active 主张按证据时间排列，每条都能点回原话。"""
    sql_status = "" if include_superseded else " AND c.lifecycle_status = 'active'"
    sql = f"""
        SELECT c.claim_id, cr.content, c.memory_type, cr.assertion_kind,
               cr.review_status, c.lifecycle_status AS lc_status, cr.asserted_at,
               (SELECT COUNT(*) FROM claim_evidence ce
                WHERE ce.claim_revision_id = cr.claim_revision_id) AS ev_count
        FROM claims c
        JOIN claim_revisions cr ON cr.claim_revision_id = c.current_revision_id
        WHERE c.memory_type = ?
          AND cr.review_status = 'approved'{sql_status}
        ORDER BY cr.asserted_at IS NULL, cr.asserted_at, c.claim_id
    """
    rows = conn.execute(sql, (memory_type,)).fetchall()
    return [
        ClaimListRow(
            claim_id=r["claim_id"],
            content=redact_known_credentials(r["content"]) or "",
            memory_type=r["memory_type"],
            assertion_kind=r["assertion_kind"],
            review_status=r["review_status"],
            lifecycle_status=r["lc_status"],
            asserted_at=r["asserted_at"],
            evidence_count=r["ev_count"],
        )
        for r in rows
    ]


def explain_claim(conn: sqlite3.Connection, claim_id: str) -> ExplainData | None:
    """一条主张的全部证据原话（用户故事 2/4 的“点回原话”）。"""
    claim = conn.execute(
        """
        SELECT c.claim_id, c.memory_type, c.lifecycle_status AS lc_status,
               cr.claim_revision_id, cr.content, cr.assertion_kind,
               cr.review_status, cr.verification_status, cr.recorded_at,
               cr.asserted_at
        FROM claims c
        JOIN claim_revisions cr ON cr.claim_revision_id = c.current_revision_id
        WHERE c.claim_id = ?
        """,
        (claim_id,),
    ).fetchone()
    if claim is None:
        return None
    data = ExplainData(
        claim_id=claim["claim_id"],
        content=redact_known_credentials(claim["content"]) or "",
        memory_type=claim["memory_type"],
        assertion_kind=claim["assertion_kind"],
        review_status=claim["review_status"],
        lifecycle_status=claim["lc_status"],
        verification_status=claim["verification_status"],
    )
    for rev in conn.execute(
        """
        SELECT claim_revision_id, content, review_status, lifecycle_status,
               recorded_at, asserted_at
        FROM claim_revisions WHERE claim_id = ?
        ORDER BY recorded_at, claim_revision_id
        """,
        (claim_id,),
    ):
        data.revisions.append(
            {
                "claim_revision_id": rev["claim_revision_id"],
                "content": redact_known_credentials(rev["content"]) or "",
                "review_status": rev["review_status"],
                "lifecycle_status": rev["lifecycle_status"],
                "recorded_at": rev["recorded_at"],
                "asserted_at": rev["asserted_at"],
                "is_current": rev["claim_revision_id"] == claim["claim_revision_id"],
            }
        )
    for ev in conn.execute(
        """
        SELECT ce.evidence_id, ce.revision_id, ce.role, ce.support_check,
               ce.span_start, ce.span_end, ce.checked_at,
               substr(er.raw_text, ce.span_start + 1,
                      ce.span_end - ce.span_start) AS quote,
               er.source_created_at, er.source_locator,
               e.conversation_id, e.speaker_type
        FROM claim_evidence ce
        JOIN event_revisions er ON er.revision_id = ce.revision_id
        JOIN events e ON e.event_id = er.event_id
        WHERE ce.claim_revision_id = ?
        ORDER BY er.source_created_at, ce.evidence_id
        """,
        (claim["claim_revision_id"],),
    ):
        data.evidence.append(
            {
                "evidence_id": ev["evidence_id"],
                "revision_id": ev["revision_id"],
                "role": ev["role"],
                "support_check": ev["support_check"],
                "span": (ev["span_start"], ev["span_end"]),
                "quote": redact_known_credentials(ev["quote"]) or "",
                "source_created_at": ev["source_created_at"],
                "source_locator": ev["source_locator"],
                "conversation_id": ev["conversation_id"],
                "speaker_type": ev["speaker_type"],
                "checked_at": ev["checked_at"],
            }
        )
    for rel in conn.execute(
        """
        SELECT r.relation_id, r.from_claim_id, r.to_claim_id, r.relation_type,
               r.status, r.origin, r.note, r.created_at, r.decided_at,
               fcr.content AS from_content, tcr.content AS to_content
        FROM claim_relations r
        JOIN claims fc ON fc.claim_id = r.from_claim_id
        JOIN claim_revisions fcr ON fcr.claim_revision_id = fc.current_revision_id
        JOIN claims tc ON tc.claim_id = r.to_claim_id
        JOIN claim_revisions tcr ON tcr.claim_revision_id = tc.current_revision_id
        WHERE r.from_claim_id = ? OR r.to_claim_id = ?
        ORDER BY r.created_at
        """,
        (claim_id, claim_id),
    ):
        data.relations.append(dict(rel))
    return data
