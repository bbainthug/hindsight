"""D-2 单元：人工审核与关系裁决（§11.1/§11.2）。"""

from __future__ import annotations

import pytest
from facts_testkit import make_claim, make_relation

from personal_brain.facts.review import (
    approve_claim,
    decide_relation,
    edit_claim,
    get_claim_view,
    pending_claims,
    reject_claim,
    skip_claim,
)


def test_approve_activates_and_confirms(db):
    claim_id = make_claim(db, "主攻 AI 测试开发", memory_type="decision")
    approve_claim(db, claim_id)
    view = get_claim_view(db, claim_id)
    assert view.review_status == "approved"
    assert view.lifecycle_status == "active"
    assert view.verification_status == "user_confirmed"
    log = db.execute(
        "SELECT action, actor FROM review_log WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    assert log["action"] == "approved"
    assert log["actor"] == "user"


def test_reject_then_approve_is_refused(db):
    claim_id = make_claim(db, "不想要的主张")
    reject_claim(db, claim_id, note="证据不足")
    with pytest.raises(ValueError):
        approve_claim(db, claim_id)
    view = get_claim_view(db, claim_id)
    assert view.review_status == "rejected"
    # 拒绝不删除：主张与日志保留
    assert db.execute(
        "SELECT COUNT(*) FROM review_log WHERE action = 'rejected'"
    ).fetchone()[0] == 1


def test_edit_creates_new_revision_and_keeps_old(db):
    claim_id = make_claim(db, "原来的措辞", memory_type="goal")
    approve_claim(db, claim_id)
    old_rev = get_claim_view(db, claim_id).claim_revision_id
    edit_claim(db, claim_id, "更准确的措辞")
    view = get_claim_view(db, claim_id)
    assert view.content == "更准确的措辞"
    assert view.claim_revision_id != old_rev
    assert view.review_status == "approved"
    # 旧修订保留（不可变历史）
    old = db.execute(
        "SELECT content, review_status FROM claim_revisions WHERE claim_revision_id = ?",
        (old_rev,),
    ).fetchone()
    assert old["content"] == "原来的措辞"
    log = db.execute(
        "SELECT action, before_content, after_content FROM review_log"
        " WHERE claim_id = ? ORDER BY review_id DESC LIMIT 1",
        (claim_id,),
    ).fetchone()
    assert log["action"] == "edited"
    assert log["before_content"] == "原来的措辞"
    assert log["after_content"] == "更准确的措辞"


def test_edit_empty_content_refused(db):
    claim_id = make_claim(db, "原措辞")
    with pytest.raises(ValueError):
        edit_claim(db, claim_id, "   ")


def test_skip_only_logs(db):
    claim_id = make_claim(db, "还没想好的主张")
    skip_claim(db, claim_id)
    view = get_claim_view(db, claim_id)
    assert view.review_status == "pending"
    assert db.execute(
        "SELECT action FROM review_log WHERE claim_id = ?", (claim_id,)
    ).fetchone()["action"] == "skipped"


def test_pending_queue_lists_pending_and_needs_review(db):
    p1 = make_claim(db, "候选一")
    n1 = make_claim(
        db, "需复核", review_status="needs_review", lifecycle_status="candidate"
    )
    make_claim(db, "已通过", review_status="approved", lifecycle_status="active")
    queue = pending_claims(db)
    ids = {v.claim_id for v in queue}
    assert ids == {p1, n1}
    # pending 排在 needs_review 之前
    assert queue[0].review_status == "pending"


def test_decide_supersedes_activates_new_and_marks_old(db):
    old_id = make_claim(
        db, "两条线并行", memory_type="decision",
        review_status="approved", lifecycle_status="active",
    )
    new_id = make_claim(db, "主攻 AI 测试开发", memory_type="decision")
    relation_id = make_relation(db, new_id, old_id, "supersedes")

    decide_relation(db, relation_id, "supersedes")

    new_view = get_claim_view(db, new_id)
    assert new_view.review_status == "approved"
    assert new_view.lifecycle_status == "active"
    old_view = get_claim_view(db, old_id)
    # 旧事实保留并标记，不删除（用户故事 3）
    assert old_view.lifecycle_status == "superseded"
    assert old_view.content == "两条线并行"
    rel = db.execute(
        "SELECT status, decided_at FROM claim_relations WHERE relation_id = ?",
        (relation_id,),
    ).fetchone()
    assert rel["status"] == "confirmed"
    assert rel["decided_at"] is not None


def test_supersedes_cycle_is_refused(db):
    a = make_claim(db, "主张A", review_status="approved", lifecycle_status="active")
    b = make_claim(db, "主张B", review_status="approved", lifecycle_status="active")
    rel_ab = make_relation(db, b, a, "supersedes")
    decide_relation(db, rel_ab, "supersedes")  # B 取代 A
    rel_ba = make_relation(db, a, b, "supersedes")
    with pytest.raises(ValueError, match="环路"):
        decide_relation(db, rel_ba, "supersedes")  # A 再取代 B → 环


def test_decide_coexists_confirms_relation_only(db):
    old_id = make_claim(
        db, "项目 A 用 SQLite", memory_type="project_fact",
        review_status="approved", lifecycle_status="active",
    )
    new_id = make_claim(db, "项目 B 用 PostgreSQL", memory_type="project_fact")
    relation_id = make_relation(db, new_id, old_id, "coexists")

    decide_relation(db, relation_id, "coexists")

    assert get_claim_view(db, new_id).review_status == "pending"  # 新候选仍待审
    assert get_claim_view(db, old_id).lifecycle_status == "active"
    rel = db.execute(
        "SELECT status FROM claim_relations WHERE relation_id = ?", (relation_id,)
    ).fetchone()
    assert rel["status"] == "confirmed"


def test_decide_reject_new_rejects_candidate(db):
    old_id = make_claim(
        db, "已有事实", review_status="approved", lifecycle_status="active"
    )
    new_id = make_claim(db, "可疑的新候选")
    relation_id = make_relation(db, new_id, old_id, "contradicts")
    decide_relation(db, relation_id, "reject_new")
    assert get_claim_view(db, new_id).review_status == "rejected"
    assert get_claim_view(db, old_id).lifecycle_status == "active"


def test_decide_dismiss_keeps_everything(db):
    old_id = make_claim(
        db, "已有事实", review_status="approved", lifecycle_status="active"
    )
    new_id = make_claim(db, "新候选")
    relation_id = make_relation(db, new_id, old_id, "refines")
    decide_relation(db, relation_id, "dismiss")
    assert get_claim_view(db, new_id).review_status == "pending"
    rel = db.execute(
        "SELECT status FROM claim_relations WHERE relation_id = ?", (relation_id,)
    ).fetchone()
    assert rel["status"] == "dismissed"
