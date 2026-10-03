"""D-2 单元：候选校验与跨度核对（§10.3 Evidence Gate 代码侧硬检查）。

验收对应：
- 跨度核对：对不上必拒；
- assistant-only 证据必拒；
- 枚举/形状非法必拒。
"""

from __future__ import annotations

import pytest
from facts_testkit import linear_conversation  # noqa: F401

from personal_brain.facts.segmentation import Segment, SegmentMessage
from personal_brain.facts.validation import (
    REJECT_ASSISTANT_ONLY,
    REJECT_CONTENT_TOO_LONG,
    REJECT_INVALID_KIND,
    REJECT_INVALID_TYPE,
    REJECT_NO_EVIDENCE,
    REJECT_SPAN_MISMATCH,
    SegmentIndex,
    find_quote_span,
    validate_candidate,
)
from personal_brain.retrieval.credentials import redact_known_credentials


def _owner_msg(rev="r-owner-1", text="我决定主攻 AI 测试开发，不再并行两条线。"):
    return SegmentMessage(
        revision_id=rev, speaker_type="owner",
        source_created_at="2026-10-01T08:00:00Z", text=text,
    )


def _assistant_msg(rev="r-asst-1", text="这是个不错的方向。"):
    return SegmentMessage(
        revision_id=rev, speaker_type="assistant",
        source_created_at="2026-10-01T08:00:30Z", text=text,
    )


def _index(*msgs: SegmentMessage) -> SegmentIndex:
    return SegmentIndex([Segment(conversation_id="c1", messages=list(msgs))])


# ---------------------------------------------------------------------------
# find_quote_span：归一化查找 → 原文偏移
# ---------------------------------------------------------------------------


def test_span_exact_substring():
    raw = "我决定主攻 AI 测试开发。"
    assert find_quote_span(raw, "主攻 AI") == (3, 8)


def test_span_normalizes_width_and_case():
    raw = "ＡＩ工程选型定了"  # 全角
    span = find_quote_span(raw, "AI工程")  # 半角查询 → NFKC 后应命中
    assert span is not None
    assert raw[span[0] : span[1]] == "ＡＩ工程"


def test_span_missing_quote_returns_none():
    assert find_quote_span("我决定主攻 AI 测试开发。", "主攻后端") is None


def test_span_on_redacted_text_keeps_offsets():
    raw = "配置好了，api_key = example-value 请查收"
    redacted = redact_known_credentials(raw)
    assert redacted != raw
    assert len(redacted) == len(raw)  # 等长遮蔽：偏移一致
    # 模型只见过遮蔽文本：它引用 █ 序列也应能在遮蔽文本中定位
    start = redacted.find("█")
    end = redacted.rfind("█") + 1
    span = find_quote_span(redacted, redacted[start:end])
    assert span == (start, end)


# ---------------------------------------------------------------------------
# validate_candidate
# ---------------------------------------------------------------------------


def _ok_claim(rev="r-owner-1", quote="主攻 AI 测试开发", **over):
    claim = {
        "content": "决定主攻 AI 测试开发",
        "memory_type": "decision",
        "assertion_kind": "intention",
        "evidence": [{"revision_id": rev, "quote": quote}],
    }
    claim.update(over)
    return claim


def test_valid_candidate_passes_with_evidence_and_time():
    index = _index(_owner_msg(), _assistant_msg())
    candidate, reason = validate_candidate(_ok_claim(), index)
    assert reason is None
    assert candidate is not None
    assert candidate.evidence[0].revision_id == "r-owner-1"
    assert candidate.asserted_at == "2026-10-01T08:00:00Z"
    # 跨度应在原文中逐字命中
    text = _owner_msg().text
    ev = candidate.evidence[0]
    assert text[ev.span_start : ev.span_end] == "主攻 AI 测试开发"


def test_assistant_only_evidence_rejected():
    index = _index(_owner_msg(), _assistant_msg())
    claim = _ok_claim(rev="r-asst-1", quote="不错的方向")
    candidate, reason = validate_candidate(claim, index)
    assert candidate is None
    assert reason == REJECT_ASSISTANT_ONLY


def test_unknown_revision_citation_rejected():
    index = _index(_owner_msg())
    candidate, reason = validate_candidate(_ok_claim(rev="r-fabricated"), index)
    assert candidate is None
    assert reason == REJECT_NO_EVIDENCE


def test_span_mismatch_rejected():
    index = _index(_owner_msg())
    candidate, reason = validate_candidate(
        _ok_claim(quote="模型改写的句子，原文没有"), index
    )
    assert candidate is None
    assert reason == REJECT_SPAN_MISMATCH


def test_context_role_owner_evidence_rejected():
    # 重叠携带的 owner 上下文（role=context）不能再次作证据
    ctx = SegmentMessage(
        revision_id="r-owner-ctx", speaker_type="owner",
        source_created_at="2026-10-01T07:00:00Z",
        text="之前说过主攻 AI 测试开发", role="context",
    )
    index = _index(ctx)
    candidate, reason = validate_candidate(
        _ok_claim(rev="r-owner-ctx", quote="主攻 AI 测试开发"), index
    )
    assert candidate is None
    assert reason == REJECT_NO_EVIDENCE


def test_invalid_memory_type_rejected():
    index = _index(_owner_msg())
    candidate, reason = validate_candidate(_ok_claim(memory_type="behavior_pattern"), index)
    assert candidate is None
    assert reason == REJECT_INVALID_TYPE


def test_invalid_assertion_kind_rejected():
    index = _index(_owner_msg())
    candidate, reason = validate_candidate(_ok_claim(assertion_kind="vibes"), index)
    assert candidate is None
    assert reason == REJECT_INVALID_KIND


def test_overlong_content_rejected():
    index = _index(_owner_msg())
    candidate, reason = validate_candidate(_ok_claim(content="长" * 301), index)
    assert candidate is None
    assert reason == REJECT_CONTENT_TOO_LONG


def test_quote_longer_than_cap_is_not_silently_truncated():
    index = _index(_owner_msg(text="abcdefghij" * 20))
    long_quote = "abcdefghij" * 20  # 超过 MAX_QUOTE_CHARS
    candidate, reason = validate_candidate(
        _ok_claim(quote=long_quote), index
    )
    assert candidate is None
    assert reason == REJECT_SPAN_MISMATCH


def test_empty_or_malformed_shapes_rejected():
    index = _index(_owner_msg())
    for bad in [
        {},
        {"content": "", "memory_type": "goal", "assertion_kind": "intention",
         "evidence": []},
        {"content": "x", "memory_type": "goal", "assertion_kind": "intention"},
        {"content": "x", "memory_type": "goal", "assertion_kind": "intention",
         "evidence": "not-a-list"},
    ]:
        candidate, reason = validate_candidate(bad, index)
        assert candidate is None
        assert reason is not None


def test_segment_index_prefers_support_over_context():
    # 同一 revision 在重叠处出现两次时，support 资格以 support 出现为准
    support = _owner_msg(rev="r-dup", text="边界处的原话")
    carried = SegmentMessage(
        revision_id="r-dup", speaker_type="owner",
        source_created_at=support.source_created_at, text=support.text, role="context",
    )
    index = SegmentIndex([
        Segment(conversation_id="c1", messages=[support]),
        Segment(conversation_id="c1", messages=[carried]),
    ])
    candidate, reason = validate_candidate(_ok_claim(rev="r-dup", quote="边界处的原话"), index)
    assert reason is None
    assert candidate is not None


@pytest.mark.parametrize(
    "quote,expected",
    [("决定", (1, 3)), ("测试", (9, 11)), ("不再并行两条线", (14, 21))],
)
def test_span_offsets_parametrized(quote, expected):
    raw = _owner_msg().text
    assert find_quote_span(raw, quote) == expected
