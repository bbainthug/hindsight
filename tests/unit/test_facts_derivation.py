"""D-2 单元：派生失效（源 revision 撤回 → 依赖主张 needs_review）。"""

from __future__ import annotations

from facts_testkit import import_conversations, linear_conversation, revision_id_of
from synthetic import T0

from personal_brain.facts.derivation import invalidate_claims_for_revisions
from personal_brain.facts.review import get_claim_view
from personal_brain.policy.withdraw import withdraw_event


def _seed_claim_on_revision(db, importer, text: str):
    """导入一条合成 owner 发言，并建立引用它的候选主张。返回 (claim_id, revision_id)。"""
    conv = linear_conversation(
        "conv-withdraw", "撤回测试",
        [("w1", "user", text, T0 + 60)],
    )
    import_conversations(importer, [conv])
    revision_id = revision_id_of(db, text)
    import uuid

    claim_id = uuid.uuid4().hex
    rev_id = uuid.uuid4().hex
    now = "2026-10-01T00:00:00.000000Z"
    db.execute(
        "INSERT INTO claims (claim_id, memory_type, lifecycle_status, created_at,"
        " updated_at) VALUES (?, 'decision', 'candidate', ?, ?)",
        (claim_id, now, now),
    )
    db.execute(
        """
        INSERT INTO claim_revisions (
            claim_revision_id, claim_id, content, memory_type, assertion_kind,
            review_status, lifecycle_status, recorded_at, rule_version
        ) VALUES (?, ?, '撤回测试主张', 'decision', 'intention',
                  'pending', 'candidate', ?, 'facts-rule-v1')
        """,
        (rev_id, claim_id, now),
    )
    db.execute(
        "UPDATE claims SET current_revision_id = ? WHERE claim_id = ?",
        (rev_id, claim_id),
    )
    # 证据边：span 指向发言全文
    db.execute(
        """
        INSERT INTO claim_evidence (
            claim_revision_id, revision_id, span_start, span_end, span_sha256,
            role, support_check, check_method, check_version, checked_at
        ) VALUES (?, ?, 0, ?, 'x', 'support', 'supported', 'verbatim_span_norm',
                  'nfkc-casefold-v2-charmap', ?)
        """,
        (rev_id, revision_id, len(text), now),
    )
    db.commit()
    return claim_id, revision_id


def test_withdraw_event_marks_claim_needs_review(db, importer):
    claim_id, revision_id = _seed_claim_on_revision(
        db, importer, "撤回这条之后主张要进复核队列"
    )
    event_id = db.execute(
        "SELECT event_id FROM event_revisions WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()["event_id"]

    withdraw_event(db, event_id)

    view = get_claim_view(db, claim_id)
    assert view.review_status == "needs_review"
    # 内容不删（派生失效≠删除，§14.1）
    assert view.content == "撤回测试主张"
    log = db.execute(
        "SELECT action, actor FROM review_log WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    assert log["action"] == "needs_review"
    assert log["actor"] == "system:withdraw"


def test_invalidation_is_idempotent(db, importer):
    claim_id, revision_id = _seed_claim_on_revision(db, importer, "幂等失效测试")
    n1 = invalidate_claims_for_revisions(db, [revision_id])
    n2 = invalidate_claims_for_revisions(db, [revision_id])
    assert n1 == 1
    assert n2 == 0  # 已是 needs_review 不重复计数/记日志
    logs = db.execute(
        "SELECT COUNT(*) FROM review_log WHERE claim_id = ?", (claim_id,)
    ).fetchone()[0]
    assert logs == 1


def test_withdraw_source_also_invalidates(db, importer):
    claim_id, _revision_id = _seed_claim_on_revision(db, importer, "整源撤回测试")
    source_id = db.execute(
        """
        SELECT e.source_id FROM events e
        JOIN event_revisions r ON r.event_id = e.event_id
        WHERE r.raw_text = '整源撤回测试'
        """
    ).fetchone()["source_id"]
    from personal_brain.policy.withdraw import withdraw_source

    withdraw_source(db, source_id)
    assert get_claim_view(db, claim_id).review_status == "needs_review"


def test_rejected_claim_not_reopened_by_invalidation(db, importer):
    claim_id, revision_id = _seed_claim_on_revision(db, importer, "已拒绝不受影响")
    db.execute(
        "UPDATE claim_revisions SET review_status='rejected' WHERE claim_id=?",
        (claim_id,),
    )
    db.commit()
    invalidate_claims_for_revisions(db, [revision_id])
    view = get_claim_view(db, claim_id)
    assert view.review_status == "rejected"
