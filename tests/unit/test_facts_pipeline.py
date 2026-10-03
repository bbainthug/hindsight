"""D-2 单元：提炼流水线（合成库 + 假 LLM，不出网）。

验收对应：幂等键、去重合并、关系建议不自动生效、预算门、失败不产生半批数据。
"""

from __future__ import annotations

import hashlib

import pytest
from facts_testkit import (
    FakeLLMClient,
    claim_of,
    import_conversations,
    linear_conversation,
    make_claim,
    revision_id_of,
)
from synthetic import T0

from personal_brain.facts.llm import LLMError
from personal_brain.facts.pipeline import (
    BudgetExceeded,
    FactsConfig,
    compute_extraction_key,
    estimate_extraction,
    run_extraction,
)
from personal_brain.facts.segmentation import segment_input_revision_ids
from personal_brain.facts.similarity import ClaimSimilarity


@pytest.fixture()
def facts_db(db, importer):
    """两条合成对话：owner 陈述 + assistant 补充（时间都在 T0 附近）。"""
    conv_a = linear_conversation(
        "conv-a", "方向决定",
        [
            ("a1", "user", "我决定主攻 AI 测试开发，不再并行两条线。", T0 + 60),
            ("a2", "assistant", "明白了，聚焦单一方向。", T0 + 120),
            ("a3", "user", "下周开始把简历改成 AI 测开版本。", T0 + 180),
            ("a4", "assistant", "好的。", T0 + 240),
        ],
    )
    conv_b = linear_conversation(
        "conv-b", "存储偏好",
        [
            ("b1", "user", "本地存储我还是偏好用 SQLite，简单可靠。", T0 + 300),
            ("b2", "assistant", "SQLite 够用。", T0 + 360),
        ],
    )
    import_conversations(importer, [conv_a, conv_b])
    return db


def _cfg(**over) -> FactsConfig:
    cfg = FactsConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    cfg.validate()
    return cfg


def _two_claims_response(db):
    r1 = revision_id_of(db, "我决定主攻 AI 测试开发，不再并行两条线。")
    r2 = revision_id_of(db, "下周开始把简历改成 AI 测开版本。")
    return {
        "claims": [
            claim_of("决定主攻 AI 测试开发", "decision", "intention", r1, "主攻 AI 测试开发"),
            claim_of(
                "要把简历改成 AI 测开版本", "project_fact", "intention",
                r2, "简历改成 AI 测开版本",
            ),
        ]
    }


def _window_kwargs():
    return {"since": None, "until": None, "account_namespace": None}


# ---------------------------------------------------------------------------
# 估算与预算
# ---------------------------------------------------------------------------


def test_estimate_counts_segments_and_tokens(facts_db):
    segments, est = estimate_extraction(facts_db, _cfg(), **_window_kwargs())
    assert est.segments >= 2
    assert est.owner_revisions == 3  # a1, a3, b1
    assert est.requests == est.segments + 1
    assert est.total_tokens > 0
    assert est.within_budget  # 默认预算 50 万
    segment_prompt_floor = int(sum(seg.chars for seg in segments) * 0.8)
    assert est.input_tokens > segment_prompt_floor + est.segments * 800


def test_empty_window_estimates_zero_requests(facts_db):
    _segments, est = estimate_extraction(
        facts_db, _cfg(), since="2035-01-01", until=None,
        account_namespace=None,
    )
    assert est.segments == 0
    assert est.requests == 0


def test_budget_exceeded_refuses_before_llm(facts_db):
    client = FakeLLMClient()
    with pytest.raises(BudgetExceeded):
        run_extraction(
            facts_db, _cfg(budget_tokens=10), client, **_window_kwargs()
        )
    assert client.calls == []  # 未调用模型
    rows = facts_db.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()
    assert rows[0] == 0  # 未落任何批次


# ---------------------------------------------------------------------------
# 正式提炼
# ---------------------------------------------------------------------------


def test_extraction_creates_pending_candidates(facts_db):
    client = FakeLLMClient([_two_claims_response(facts_db)])
    report = run_extraction(facts_db, _cfg(), client, **_window_kwargs())
    assert not report.skipped_idempotent
    assert report.candidates_created == 2
    assert report.requests == 2  # 2 段；无关建议请求（无相近已有主张）
    assert report.prompt_tokens > 0
    rows = facts_db.execute(
        "SELECT content, cr.review_status, cr.lifecycle_status, c.memory_type,"
        " cr.assertion_kind, cr.asserted_at FROM claims c"
        " JOIN claim_revisions cr ON cr.claim_id = c.claim_id"
        " WHERE c.current_revision_id = cr.claim_revision_id ORDER BY content"
    ).fetchall()
    assert [r["review_status"] for r in rows] == ["pending", "pending"]
    assert [r["lifecycle_status"] for r in rows] == ["candidate", "candidate"]
    assert rows[0]["asserted_at"] is not None
    # 证据跨度可回原文
    ev = facts_db.execute(
        "SELECT ce.span_start, ce.span_end, er.raw_text FROM claim_evidence ce"
        " JOIN event_revisions er ON er.revision_id = ce.revision_id"
    ).fetchall()
    assert len(ev) == 2
    for row in ev:
        assert row["raw_text"][row["span_start"] : row["span_end"]]


def test_extraction_records_rejections(facts_db):
    r1 = revision_id_of(facts_db, "我决定主攻 AI 测试开发，不再并行两条线。")
    response = {
        "claims": [
            claim_of("决定主攻 AI 测试开发", "decision", "intention", r1, "原文里没有这句话"),
            {"content": "坏主张", "memory_type": "nonsense", "assertion_kind": "intention",
             "evidence": [{"revision_id": r1, "quote": "主攻"}]},
            claim_of("决定主攻 AI 测试开发", "decision", "intention", r1, "主攻 AI 测试开发"),
        ]
    }
    report = run_extraction(
        facts_db, _cfg(), FakeLLMClient([response]), **_window_kwargs()
    )
    assert report.candidates_created == 1
    assert report.rejections == {"span_mismatch": 1, "invalid_memory_type": 1}


def test_idempotent_rerun_skips_llm(facts_db):
    client = FakeLLMClient([_two_claims_response(facts_db)])
    first = run_extraction(facts_db, _cfg(), client, **_window_kwargs())
    assert not first.skipped_idempotent
    calls_after_first = len(client.calls)
    second = run_extraction(facts_db, _cfg(), client, **_window_kwargs())
    assert second.skipped_idempotent
    assert len(client.calls) == calls_after_first  # 未重复调用
    assert second.run_id == first.run_id


def test_idempotency_key_changes_with_model(facts_db):
    ids = ["r1", "r2"]
    k1 = compute_extraction_key(revision_ids=ids, provider_name="fake", model="m1")
    k2 = compute_extraction_key(revision_ids=ids, provider_name="fake", model="m2")
    k3 = compute_extraction_key(revision_ids=list(reversed(ids)), provider_name="fake", model="m1")
    assert k1 != k2  # 模型变化 → 新批次
    assert k1 == k3  # 输入集合顺序无关


def test_idempotency_key_covers_context_revisions_and_model_settings():
    base = {
        "base_url": "https://model-a.invalid/v1",
        "temperature": 0.2,
        "max_output_tokens": 2000,
    }
    key = compute_extraction_key(
        revision_ids=["owner-r1", "assistant-r1"], provider_name="fake",
        model="m1", model_config=base,
    )
    changed_context = compute_extraction_key(
        revision_ids=["owner-r1", "assistant-r2"], provider_name="fake",
        model="m1", model_config=base,
    )
    changed_settings = compute_extraction_key(
        revision_ids=["owner-r1", "assistant-r1"], provider_name="fake",
        model="m1", model_config={**base, "temperature": 0.7},
    )
    assert key != changed_context  # assistant 也进入模型上下文
    assert key != changed_settings  # 相同模型名、不同配置须新批次


def test_input_revision_fingerprint_includes_context(facts_db):
    from personal_brain.facts.segmentation import build_segments

    segments, _ = build_segments(
        facts_db, since=None, until=None,
    )
    fingerprint = segment_input_revision_ids(segments)
    assert len(fingerprint) == 6  # 3 条 owner + 3 条 assistant


def test_llm_failure_marks_run_failed_without_claims(facts_db):
    class ExplodingClient(FakeLLMClient):
        def complete_json(self, system, user):
            raise LLMError("boom")

    with pytest.raises(LLMError):
        run_extraction(facts_db, _cfg(), ExplodingClient(), **_window_kwargs())
    row = facts_db.execute(
        "SELECT status FROM extraction_runs"
    ).fetchone()
    assert row is not None and row["status"] == "failed"
    assert facts_db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_failed_run_can_be_retried_without_duplicate_run_row(facts_db):
    class ExplodingClient(FakeLLMClient):
        def complete_json(self, system, user):
            raise LLMError("temporary failure")

    with pytest.raises(LLMError):
        run_extraction(facts_db, _cfg(), ExplodingClient(), **_window_kwargs())
    failed_id = facts_db.execute(
        "SELECT run_id FROM extraction_runs"
    ).fetchone()["run_id"]

    report = run_extraction(
        facts_db, _cfg(), FakeLLMClient([_two_claims_response(facts_db)]),
        **_window_kwargs(),
    )
    row = facts_db.execute(
        "SELECT run_id, status FROM extraction_runs"
    ).fetchone()
    assert row["run_id"] == failed_id
    assert row["status"] == "completed"
    assert report.run_id == failed_id
    assert facts_db.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 1


def test_span_hash_uses_original_text_not_redacted_prompt_text(db, importer):
    raw = "api_key = example-value； 我偏好使用 SQLite。"
    conv = linear_conversation(
        "conv-span-hash", "跨度摘要", [("span1", "user", raw, T0 + 60)]
    )
    import_conversations(importer, [conv])
    revision_id = revision_id_of(db, raw)
    client = FakeLLMClient([{
        "claims": [claim_of(
            "偏好使用 SQLite", "preference", "self_report",
            revision_id, "我偏好使用 SQLite",
        )]
    }])
    report = run_extraction(db, _cfg(), client, **_window_kwargs())
    assert report.candidates_created == 1, report.rejections
    evidence = db.execute(
        """SELECT ce.span_start, ce.span_end, ce.span_sha256, er.raw_text
           FROM claim_evidence ce JOIN event_revisions er USING (revision_id)"""
    ).fetchone()
    assert evidence["raw_text"][evidence["span_start"]:evidence["span_end"]] == (
        "我偏好使用 SQLite"
    )
    assert evidence["span_sha256"] == hashlib.sha256(
        "我偏好使用 SQLite".encode()
    ).hexdigest()


def test_dedup_merges_same_content_into_existing_claim(facts_db):
    r1 = revision_id_of(facts_db, "我决定主攻 AI 测试开发，不再并行两条线。")
    r2 = revision_id_of(facts_db, "本地存储我还是偏好用 SQLite，简单可靠。")
    # 两段返回同一内容主张（不同证据）
    response_a = {"claims": [
        claim_of("偏好用 SQLite 做本地存储", "preference", "self_report", r1, "决定主攻"),
    ]}
    response_b = {"claims": [
        claim_of("偏好用 SQLite 做本地存储", "preference", "self_report", r2, "偏好用 SQLite"),
    ]}
    client = FakeLLMClient([response_a, response_b])
    report = run_extraction(facts_db, _cfg(), client, **_window_kwargs())
    assert report.candidates_created == 1
    assert report.candidates_merged == 1
    evidence = facts_db.execute(
        "SELECT COUNT(*) FROM claim_evidence"
    ).fetchone()[0]
    assert evidence == 2  # 两条证据边挂在同一主张上


def test_dedup_can_use_vector_similarity(facts_db):
    class SameVectorEmbedder:
        model_id = "synthetic-embedder"
        dimension = 2

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    old_claim = make_claim(
        facts_db, "本地数据库偏好 SQLite", memory_type="preference",
        review_status="approved", lifecycle_status="active",
    )
    r1 = revision_id_of(facts_db, "本地存储我还是偏好用 SQLite，简单可靠。")
    response = {"claims": [
        claim_of(
            "我倾向于使用 SQLite 作为本地数据库", "preference", "self_report",
            r1, "偏好用 SQLite",
        )
    ]}
    report = run_extraction(
        facts_db, _cfg(), FakeLLMClient([{"claims": []}, response]),
        similarity=ClaimSimilarity(SameVectorEmbedder()), **_window_kwargs(),
    )
    assert report.candidates_created == 0
    assert report.candidates_merged == 1
    assert facts_db.execute(
        "SELECT COUNT(*) FROM claim_evidence ce JOIN claims c"
        " ON c.current_revision_id=ce.claim_revision_id WHERE c.claim_id=?",
        (old_claim,),
    ).fetchone()[0] == 1


class _FixedSimilarity(ClaimSimilarity):
    """固定相近度（确定性测试关系分带）。"""

    def __init__(self, score: float) -> None:
        super().__init__()
        self._score = score

    def similarity(self, a: str, b: str) -> float:
        return self._score


def test_relation_suggestion_recorded_but_not_applied(facts_db):
    # 已有 active 主张（旧方向），新候选与之相近（固定 0.6，落在冲突带）
    old_claim = make_claim(
        facts_db, "两条线并行推进", memory_type="decision",
        review_status="approved", lifecycle_status="active",
    )
    r1 = revision_id_of(facts_db, "我决定主攻 AI 测试开发，不再并行两条线。")
    response = {"claims": [
        claim_of("决定主攻 AI 测试开发", "decision", "intention", r1, "主攻 AI 测试开发"),
    ]}
    # 提炼请求 + 关系建议请求（两类请求分开排队）
    client = FakeLLMClient(
        [response],
        relation_responses=[{"relations": [{"index": 0, "relation_type": "supersedes"}]}],
    )
    report = run_extraction(
        facts_db, _cfg(), client, similarity=_FixedSimilarity(0.6), **_window_kwargs()
    )
    assert report.relations_suggested == 1
    rel = facts_db.execute(
        "SELECT relation_type, status, origin, from_claim_id, to_claim_id"
        " FROM claim_relations"
    ).fetchone()
    assert rel["relation_type"] == "supersedes"
    assert rel["status"] == "suggested"  # 只是建议
    assert rel["origin"] == "llm"
    assert rel["to_claim_id"] == old_claim
    # 旧主张未被悄悄改状态（§11.1 冲突不替用户判）
    old = facts_db.execute(
        "SELECT lifecycle_status FROM claims WHERE claim_id = ?", (old_claim,)
    ).fetchone()
    assert old["lifecycle_status"] == "active"
    # 新候选仍是待审核
    new_status = facts_db.execute(
        "SELECT cr.review_status FROM claim_revisions cr"
        " JOIN claims c ON c.current_revision_id = cr.claim_revision_id"
        " WHERE cr.content = '决定主攻 AI 测试开发'"
    ).fetchone()
    assert new_status["review_status"] == "pending"


def test_relation_failure_does_not_block_candidates(facts_db):
    class RelationBoom(FakeLLMClient):
        def complete_json(self, system, user):
            if "配对" in user:  # 关系建议请求（见 prompting.build_relation_prompt）
                raise LLMError("relation service down")
            return super().complete_json(system, user)

    # 相近但不同文（固定 0.6 落在冲突带）的已有 active 主张
    make_claim(
        facts_db, "打算主攻 AI 测试开发这条路", memory_type="decision",
        review_status="approved", lifecycle_status="active",
    )
    r1 = revision_id_of(facts_db, "我决定主攻 AI 测试开发，不再并行两条线。")
    response = {"claims": [
        claim_of("决定主攻 AI 测试开发", "decision", "intention", r1, "主攻 AI 测试开发"),
    ]}
    client = RelationBoom([response])
    report = run_extraction(
        facts_db, _cfg(), client,
        similarity=_FixedSimilarity(0.6), **_window_kwargs(),
    )
    assert report.candidates_created == 1
    assert "关系建议" in report.notes  # 记录了关系建议失败
    assert facts_db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
