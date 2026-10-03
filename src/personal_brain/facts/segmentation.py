"""提炼输入分段（设计 §10.4 分段与幂等）。

- 只取 owner 发言作为 support 证据候选；assistant 消息只作段内上下文。
- 按对话分组，段内按时间排序；时间间隔超过 ``max_gap_seconds`` 或
  段字符数超过 ``max_segment_chars`` 时切分，切分处携带有限上下文
  （重叠消息标记 role='context'，防止边界处重复提炼同一事实）。
- 所选路径（§4.3）：路径已知的对话只取所选路径上的消息；路径未知
  的对话整段跳过并计数报告（不暗中猜测路径）。
- 出站前对每条消息文本做凭据遮蔽（复用检索同一实现，等长替换 █，
  原文偏移在遮蔽后仍然有效）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from personal_brain.retrieval.credentials import redact_known_credentials
from personal_brain.retrieval.search import _selected_path_events

SEGMENTATION_VERSION = "facts-seg-v1"


@dataclass(frozen=True)
class SegmentMessage:
    """段内一条消息（文本已遮蔽；owner 消息才允许作 support 证据）。"""

    revision_id: str
    speaker_type: str
    source_created_at: str | None
    text: str  # 已做凭据遮蔽（等长替换，偏移与原文一致）
    role: str = "support"  # support | context（重叠携带的上下文）


@dataclass
class Segment:
    """一个提炼批次：同一对话内一段连续发言 + 有限上下文。"""

    conversation_id: str
    messages: list[SegmentMessage] = field(default_factory=list)

    @property
    def owner_revision_ids(self) -> list[str]:
        return [m.revision_id for m in self.messages if m.speaker_type == "owner"]

    @property
    def chars(self) -> int:
        return sum(len(m.text) for m in self.messages)


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    since: str | None,
    until: str | None,
    account_namespace: str | None,
) -> list[sqlite3.Row]:
    """窗口内可服务的 revisions（owner + assistant，供上下文）。

    授权过滤与检索同一口径：来源未撤回 + 当前 policy 状态 available
    （§7.3：出站数据必须过同一套过滤）。
    """
    sql = """
        SELECT r.revision_id, r.event_id, r.raw_text, r.source_created_at,
               e.conversation_id, e.speaker_type
        FROM event_revisions r
        JOIN events e ON e.event_id = r.event_id
        JOIN sources s ON s.source_id = e.source_id
        WHERE s.status != 'withdrawn'
          AND EXISTS (
            SELECT 1 FROM revision_policy_state ps
            WHERE ps.revision_id = r.revision_id
              AND ps.valid_to IS NULL AND ps.availability = 'available'
          )
          AND r.raw_text IS NOT NULL
    """
    params: list = []
    if since is not None:
        sql += " AND r.source_created_at IS NOT NULL AND r.source_created_at >= ?"
        params.append(since)
    if until is not None:
        sql += " AND r.source_created_at IS NOT NULL AND r.source_created_at < ?"
        params.append(until)
    if account_namespace is not None:
        sql += " AND s.account_namespace = ?"
        params.append(account_namespace)
    sql += " ORDER BY e.conversation_id, r.source_created_at, r.revision_id"
    return conn.execute(sql, params).fetchall()


def build_segments(
    conn: sqlite3.Connection,
    *,
    since: str | None,
    until: str | None,
    account_namespace: str | None = None,
    max_segment_chars: int = 6000,
    max_gap_seconds: int = 3600,
    context_messages: int = 4,
    max_owner_chars: int | None = None,
    include_assistant_context: bool = True,
) -> tuple[list[Segment], int]:
    """构造提炼分段；返回 (分段列表, 路径未知被跳过的对话数)。

    ``max_owner_chars``：跳过超过该长度的 owner 发言（粘贴的长日志/JD 等，
    D-12 预算收窄用）；assistant 上下文不受影响。None 表示不过滤。
    ``include_assistant_context``：False 时 assistant 消息整体不进分段
    （只提炼 owner 原话；预算收窄用，默认 True 保持上下文）。
    """
    rows = _candidate_rows(
        conn, since=since, until=until, account_namespace=account_namespace
    )
    # 所选路径（§4.3）：与检索 selected 模式同一实现；路径未知对话跳过。
    selected_events, path_unknown = _selected_path_events(conn)

    by_conv: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["event_id"] not in selected_events:
            # 只取所选路径上的消息（§10.3 选中路径；路径未知对话整段跳过）
            continue
        if not include_assistant_context and row["speaker_type"] == "assistant":
            continue
        if (
            max_owner_chars is not None
            and row["speaker_type"] == "owner"
            and len(row["raw_text"]) > max_owner_chars
        ):
            # 超长粘贴文本不作为提炼输入（含其 support 资格）
            continue
        by_conv.setdefault(row["conversation_id"], []).append(row)

    segments: list[Segment] = []
    for conversation_id, conv_rows in sorted(by_conv.items()):
        messages = [
            SegmentMessage(
                revision_id=row["revision_id"],
                speaker_type=row["speaker_type"],
                source_created_at=row["source_created_at"],
                text=redact_known_credentials(row["raw_text"]) or "",
            )
            for row in conv_rows
        ]
        conv_segments = _split_conversation(
            conversation_id,
            messages,
            max_segment_chars=max_segment_chars,
            max_gap_seconds=max_gap_seconds,
            context_messages=context_messages,
        )
        # Assistant-only windows are neither evidence input nor useful context
        # without an owner utterance to anchor them.
        segments.extend(
            seg
            for seg in conv_segments
            if any(m.speaker_type == "owner" and m.role == "support" for m in seg.messages)
        )
    return segments, len(path_unknown)


def _ts(value: str | None) -> float:
    """ISO 时间戳 → 排序秒；未知时间为 0（不触发间隔切分）。"""
    if not value:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _split_conversation(
    conversation_id: str,
    messages: list[SegmentMessage],
    *,
    max_segment_chars: int,
    max_gap_seconds: int,
    context_messages: int,
) -> list[Segment]:
    """单对话内切分：时间间隔 / 字符上限；切分处携带有限上下文。

    重叠消息在下一段中标记 role='context'（§10.4：上下文重叠部分
    标记为 context，防止重复提炼）；owner 消息在重叠里仍保持可引用
    （跨段边界的事实不应因为切分丢失证据），同一事实被两段重复提炼
    时由去重合并兜底（§10.3 duplicate check）。
    """
    segments: list[Segment] = []
    current: list[SegmentMessage] = []
    current_chars = 0

    def close() -> None:
        nonlocal current, current_chars
        if current:
            segments.append(
                Segment(conversation_id=conversation_id, messages=list(current))
            )
        overlap = current[-context_messages:] if context_messages else []
        carried = [
            SegmentMessage(
                revision_id=m.revision_id,
                speaker_type=m.speaker_type,
                source_created_at=m.source_created_at,
                text=m.text,
                role="context",
            )
            for m in overlap
        ]
        current = carried
        current_chars = sum(len(m.text) for m in current)

    for msg in messages:
        if current:
            prev = current[-1]
            gap = _ts(msg.source_created_at) - _ts(prev.source_created_at)
            if gap > max_gap_seconds:
                close()
        if current and current_chars + len(msg.text) > max_segment_chars:
            close()
        if any(m.revision_id == msg.revision_id for m in current):
            continue  # 上一段携带的重叠消息不重复追加
        current.append(msg)
        current_chars += len(msg.text)
    if current:
        segments.append(Segment(conversation_id=conversation_id, messages=current))
    return segments


def segment_fingerprint_ids(segments: list[Segment]) -> list[str]:
    """owner revision（去重、排序），用于估算 owner 发言数量。"""
    ids: set[str] = set()
    for seg in segments:
        ids.update(seg.owner_revision_ids)
    return sorted(ids)


def segment_input_revision_ids(segments: list[Segment]) -> list[str]:
    """全部实际发送给模型的 revision（包含 assistant/context），去重排序。"""
    return sorted({msg.revision_id for seg in segments for msg in seg.messages})


__all__ = [
    "SEGMENTATION_VERSION",
    "Segment",
    "SegmentMessage",
    "build_segments",
    "segment_fingerprint_ids",
    "segment_input_revision_ids",
]
