"""候选主张校验（设计 §10.3 Evidence Gate 的代码侧硬检查）。

提示词是建议，代码才是边界（§7.6 历史提示注入：模型输出不可信）：
- 跨度核对：quote 必须在指认 revision 的（遮蔽后）原文中连续出现；
  用与检索 snippet 同一 NFKC+casefold 偏移映射还原原文区间。
- 证据性质：只有 owner 且 role=support 的消息可作 support 证据；
  assistant-only 的候选直接拒绝（不能成为用户事实）。
- 枚举与形状：memory_type / assertion_kind 必须在 v0 枚举内；
  内容非空、长度受限。
- revision 必须是本次分段实际提供过的（模型不得编造出处）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from personal_brain.facts import ASSERTION_KINDS, MEMORY_TYPES
from personal_brain.facts.segmentation import Segment, SegmentMessage
from personal_brain.history.normalization import normalize_with_map, raw_span

# 主张内容与引用跨度的长度上限（防提醒注入式长文与滥用）
MAX_CONTENT_CHARS = 300
MAX_QUOTE_CHARS = 120

# 拒绝原因（进 extraction_runs.notes / 统计口径）
REJECT_BAD_SHAPE = "bad_shape"
REJECT_INVALID_TYPE = "invalid_memory_type"
REJECT_INVALID_KIND = "invalid_assertion_kind"
REJECT_NO_EVIDENCE = "no_evidence"
REJECT_UNKNOWN_REVISION = "unknown_revision"
REJECT_CONTEXT_ONLY_EVIDENCE = "context_only_evidence"
REJECT_ASSISTANT_ONLY = "assistant_only_evidence"
REJECT_SPAN_MISMATCH = "span_mismatch"
REJECT_CONTENT_TOO_LONG = "content_too_long"


@dataclass(frozen=True)
class ValidatedEvidence:
    revision_id: str
    span_start: int  # 原文偏移（与遮蔽后文本等长，偏移一致）
    span_end: int
    quote: str  # 遮蔽后文本（展示时仍是脱敏文本）


@dataclass
class ValidatedCandidate:
    content: str
    memory_type: str
    assertion_kind: str
    asserted_at: str | None  # support 证据的最早发言时间
    evidence: list[ValidatedEvidence] = field(default_factory=list)


class SegmentIndex:
    """本次提炼提供的消息索引：revision_id → SegmentMessage。"""

    def __init__(self, segments: list[Segment]) -> None:
        self._by_revision: dict[str, SegmentMessage] = {}
        for seg in segments:
            for msg in seg.messages:
                # 同一 revision 在重叠处出现两次（support + context）时
                # 保留 support 角色：证据资格以 support 出现为准。
                existing = self._by_revision.get(msg.revision_id)
                if existing is None or (
                    existing.role != "support" and msg.role == "support"
                ):
                    self._by_revision[msg.revision_id] = msg

    def get(self, revision_id: str) -> SegmentMessage | None:
        return self._by_revision.get(revision_id)

    def __contains__(self, revision_id: str) -> bool:
        return revision_id in self._by_revision


def find_quote_span(raw_text: str, quote: str) -> tuple[int, int] | None:
    """quote 在原文中的区间（归一化查找 → 原文偏移映射）。

    raw_text 为遮蔽后文本（与原文等长）：quote 含 █ 时同样能在
    遮蔽文本中定位；返回偏移对原文与遮蔽文本同样有效。
    找不到（模型改写/拼接/复述）返回 None。
    """
    if not quote or not raw_text:
        return None
    norm_quote, _ = normalize_with_map(quote)
    if not norm_quote:
        return None
    norm_raw, offsets = normalize_with_map(raw_text)
    idx = norm_raw.find(norm_quote)
    if idx < 0:
        return None
    return raw_span(norm_raw, offsets, idx, idx + len(norm_quote))


def validate_candidate(
    raw: dict,
    index: SegmentIndex,
) -> tuple[ValidatedCandidate | None, str | None]:
    """校验单个 LLM 候选；返回 (候选, None) 或 (None, 拒绝原因)。"""
    content = raw.get("content")
    memory_type = raw.get("memory_type")
    assertion_kind = raw.get("assertion_kind")
    evidence_raw = raw.get("evidence")

    if not isinstance(content, str) or not content.strip():
        return None, REJECT_BAD_SHAPE
    if len(content) > MAX_CONTENT_CHARS:
        return None, REJECT_CONTENT_TOO_LONG
    if memory_type not in MEMORY_TYPES:
        return None, REJECT_INVALID_TYPE
    if assertion_kind not in ASSERTION_KINDS:
        return None, REJECT_INVALID_KIND
    if not isinstance(evidence_raw, list) or not evidence_raw:
        return None, REJECT_NO_EVIDENCE

    evidence: list[ValidatedEvidence] = []
    seen_spans: set[tuple[str, int, int]] = set()
    had_owner = False
    had_assistant_citation = False
    for item in evidence_raw:
        if not isinstance(item, dict):
            continue
        revision_id = item.get("revision_id")
        quote = item.get("quote")
        if not isinstance(revision_id, str) or revision_id not in index:
            continue  # 编造的出处：直接丢弃该引用
        if not isinstance(quote, str) or not quote.strip():
            continue
        msg = index.get(revision_id)
        assert msg is not None
        if msg.speaker_type != "owner":
            # assistant（或非 owner）消息不能成为用户事实的证据（§10.3）
            had_assistant_citation = True
            continue
        if msg.role != "support":
            continue  # 重叠上下文：该消息在原段中已是 support，不重复提炼
        had_owner = True
        if len(quote) > MAX_QUOTE_CHARS:
            continue  # 不截断模型引用；跨度必须对应完整引用文本
        span = find_quote_span(msg.text, quote)
        if span is None:
            continue  # 跨度对不上：丢弃该证据边（§10.3 对不上的候选拒绝）
        key = (revision_id, span[0], span[1])
        if key in seen_spans:
            continue
        seen_spans.add(key)
        evidence.append(
            ValidatedEvidence(
                revision_id=revision_id,
                span_start=span[0],
                span_end=span[1],
                quote=msg.text[span[0] : span[1]],
            )
        )

    if not had_owner:
        # 引用全是 assistant（或没有任何合法 owner 引用）：不能成为用户事实
        reason: str = REJECT_ASSISTANT_ONLY if had_assistant_citation else REJECT_NO_EVIDENCE
        return None, reason
    if not evidence:
        return None, REJECT_SPAN_MISMATCH

    timestamps = [
        msg.source_created_at
        for msg in (index.get(e.revision_id) for e in evidence)
        if msg is not None and msg.source_created_at is not None
    ]
    asserted_at = min(timestamps) if timestamps else None
    return (
        ValidatedCandidate(
            content=content.strip(),
            memory_type=memory_type,
            assertion_kind=assertion_kind,
            asserted_at=asserted_at,
            evidence=evidence,
        ),
        None
    )


def existing_claim_contents(
    conn: sqlite3.Connection,
    *,
    include_statuses: tuple[str, ...] = ("candidate", "active"),
) -> list[tuple[str, str, str]]:
    """已有主张 (claim_id, content, memory_type)，用于去重与冲突检测。"""
    rows = conn.execute(
        """
        SELECT c.claim_id, c.memory_type, cr.content
        FROM claims c
        JOIN claim_revisions cr ON cr.claim_revision_id = c.current_revision_id
        WHERE c.lifecycle_status IN ('candidate', 'active')
          AND cr.review_status IN ('pending', 'approved')
        """
    ).fetchall()
    return [(r["claim_id"], r["content"], r["memory_type"]) for r in rows]
