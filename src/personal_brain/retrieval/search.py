"""历史检索核心（§6.1/§6.3）。

语义：
- ``literal``：归一化文本的连续子串匹配（整串为一个查询词）。
- ``all_terms``：空白分隔的查询项全部出现；每个中文查询项保持连续子串语义。
- 时间/来源/对话/说话者/路径过滤；区间 [date_from, date_to)。
- 候选路径：FTS 预筛选（仅当超集保证成立，见 bigram 模块证明）+
  ``instr`` 逐条验证；不可安全预筛选的词回退参数化扫描。
  FTS 路径与基线路径的匹配集合必须一致（差分验证由测试保证）。
- 空 query 不触发全库导出；最近事件走 ``recent_events`` 专用接口。
- 回退扫描超资源上限抛 QUERY_TOO_BROAD，不把超限当作没有结果。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from personal_brain.history.normalization import normalize_with_map, raw_span
from personal_brain.retrieval.bigram import TermPlan, build_fts_query, plan_term
from personal_brain.retrieval.fts import fts_match


class QueryTooBroad(Exception):
    """回退扫描超过资源限制（§6.3），应提示缩小时间或来源。"""


@dataclass(frozen=True)
class SearchFilters:
    """检索过滤条件。"""

    date_from: str | None = None  # UTC ISO（含边界），调用方按时区换算
    date_to: str | None = None  # UTC ISO（不含）
    source_id: str | None = None
    account_namespace: str | None = None
    conversation_id: str | None = None
    speaker_type: str | None = None
    path_mode: str = "all"  # selected | all（§4.3）
    # 基础标签过滤（可选；None = 本地全权不做标签限制；策略执行 Phase B 收紧）
    allow_scopes: tuple[str, ...] | None = None
    deny_sensitivity: tuple[str, ...] | None = None
    allow_unclassified: bool = True


@dataclass
class SearchLimits:
    """§6.3 限额：可配置，硬上限由调用方约束。"""

    default_limit: int = 10
    max_limit: int = 50
    snippet_chars: int = 400
    max_text_chars: int = 8000
    max_scan: int = 50_000

    def __post_init__(self) -> None:
        # 硬上限（§6.3）：配置只能下调，不得超过规格数值
        self.default_limit = max(1, min(self.default_limit, 10))
        self.max_limit = max(self.default_limit, min(self.max_limit, 50))
        self.snippet_chars = max(1, min(self.snippet_chars, 400))
        self.max_text_chars = max(1, min(self.max_text_chars, 8000))
        self.max_scan = max(1, self.max_scan)


@dataclass
class SearchHit:
    event_id: str
    revision_id: str
    is_current: bool
    conversation_id: str
    speaker_id: str | None
    speaker_type: str
    content_type: str
    source_created_at: str | None
    time_known: bool
    score: int
    raw_text: str | None = None
    snippet: str | None = None
    source_locator: str = ""
    occurrences: list[dict[str, str]] = field(default_factory=list)
    match_span: tuple[int, int] | None = None  # 归一化坐标，内部用于 snippet 还原


@dataclass
class SearchResult:
    hits: list[SearchHit]
    total_matched: int
    truncated: bool
    next_cursor: str | None
    undated_excluded: int  # 时间过滤生效时被排除的未知时间记录数
    path_unknown_conversations: list[str]  # selected 模式下路径未知的对话
    interpreted: dict[str, str | None]  # 解释后的查询区间（§4.4）
    query_plans: list[dict[str, object]]  # 每词检索路径（可验证性）
    notes: tuple[str, ...] = (
        "语义扩展未实现：同义表达（如“求职/找工作”）需分别检索（§6.3 已知能力边界）",
    )


_BASE_SQL = """
SELECT r.revision_id, r.event_id, r.raw_text, r.search_text, r.content_type,
       r.source_created_at, r.source_locator,
       e.conversation_id, e.speaker_id, e.speaker_type,
       e.current_revision_id, s.account_namespace, s.source_id
FROM event_revisions r
JOIN events e ON e.event_id = r.event_id
JOIN sources s ON s.source_id = e.source_id
WHERE s.status != 'withdrawn'
  AND EXISTS (
    SELECT 1 FROM revision_policy_state ps
    WHERE ps.revision_id = r.revision_id
      AND ps.valid_to IS NULL AND ps.availability = 'available'
)
"""


def _filter_sql(filters: SearchFilters, params: list) -> str:
    sql = _BASE_SQL
    if filters.date_from is not None:
        sql += " AND r.source_created_at IS NOT NULL AND r.source_created_at >= ?"
        params.append(filters.date_from)
    if filters.date_to is not None:
        sql += " AND r.source_created_at IS NOT NULL AND r.source_created_at < ?"
        params.append(filters.date_to)
    if filters.source_id is not None:
        sql += " AND e.source_id = ?"
        params.append(filters.source_id)
    if filters.account_namespace is not None:
        sql += " AND s.account_namespace = ?"
        params.append(filters.account_namespace)
    if filters.conversation_id is not None:
        sql += " AND e.conversation_id = ?"
        params.append(filters.conversation_id)
    if filters.speaker_type is not None:
        sql += " AND e.speaker_type = ?"
        params.append(filters.speaker_type)
    if not filters.allow_unclassified:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps2"
            " WHERE ps2.revision_id = r.revision_id AND ps2.valid_to IS NULL"
            " AND ps2.classification_status != 'unclassified')"
        )
    if filters.allow_scopes is not None:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps3"
            " JOIN revision_scope_labels sl ON sl.state_id = ps3.state_id"
            " WHERE ps3.revision_id = r.revision_id AND ps3.valid_to IS NULL"
            " AND sl.label IN (" + ",".join("?" for _ in filters.allow_scopes) + "))"
        )
        params.extend(filters.allow_scopes)
    if filters.deny_sensitivity is not None:
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM revision_policy_state ps4"
            " JOIN revision_sensitivity_labels se ON se.state_id = ps4.state_id"
            " WHERE ps4.revision_id = r.revision_id AND ps4.valid_to IS NULL"
            " AND se.label IN (" + ",".join("?" for _ in filters.deny_sensitivity) + "))"
        )
        params.extend(filters.deny_sensitivity)
    return sql


# ---------------------------------------------------------------------------
# 日期边界
# ---------------------------------------------------------------------------


def parse_date_bound(value: str, timezone_name: str, *, end_of_day: bool) -> str:
    """自然日期 + 配置时区 → 明确 UTC 边界（§4.4；区间 [from, to)）。"""
    from datetime import date as _date

    day = _date.fromisoformat(value)
    dt = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(timezone_name))
    if end_of_day:
        dt += timedelta(days=1)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# selected 路径（§4.3）
# ---------------------------------------------------------------------------


def _selected_path_events(conn: sqlite3.Connection) -> tuple[set[str], list[str]]:
    """selected 模式：每对话取最近发布快照的所选路径（决策 18）。

    返回 (路径上事件 ID 集合, active_path_unknown 的对话列表)。
    """
    events: set[str] = set()
    unknown: list[str] = []
    snaps = conn.execute(
        """
        SELECT conv_snapshot_id, conversation_id, active_path_known
        FROM conversation_snapshots cs
        WHERE conv_snapshot_id = (
            SELECT cs2.conv_snapshot_id FROM conversation_snapshots cs2
            WHERE cs2.conversation_id = cs.conversation_id
            ORDER BY cs2.rowid DESC LIMIT 1
        )
        """
    ).fetchall()
    for snap in snaps:
        if not snap["active_path_known"]:
            unknown.append(snap["conversation_id"])
            continue
        rows = conn.execute(
            """
            SELECT event_id FROM conversation_nodes
            WHERE node_key IN (
                SELECT node_key FROM conversation_selected_path
                WHERE conv_snapshot_id = ?
            ) AND event_id IS NOT NULL
            """,
            (snap["conv_snapshot_id"],),
        ).fetchall()
        events.update(r["event_id"] for r in rows)
    return events, unknown


# ---------------------------------------------------------------------------
# 验证与 snippet
# ---------------------------------------------------------------------------


def _verify(
    search_text: str | None, plans: list[TermPlan]
) -> tuple[bool, tuple[int, int] | None, int]:
    """原始语义验证：每个词必须在 search_text 中连续出现。

    返回 (匹配, 首个命中区间[归一化坐标], 得分=全部词出现次数和)。
    """
    if search_text is None:
        return False, None, 0
    first_span: tuple[int, int] | None = None
    score = 0
    for plan in plans:
        idx = search_text.find(plan.term)
        if idx < 0:
            return False, None, 0
        count = search_text.count(plan.term)
        if first_span is None:
            first_span = (idx, idx + len(plan.term))
        score += count
    return True, first_span, score


def _snippet(raw_text: str, search_text: str, span: tuple[int, int], width: int) -> str:
    """匹配区间 → 原文片段（偏移映射，§6.2 不用归一化索引切原文）。"""
    norm, offsets = normalize_with_map(raw_text)
    if norm != search_text:
        # 理论不可达：索引与映射同实现；防御性回退为原文开头
        return raw_text[:width]
    raw_start, raw_end = raw_span(norm, offsets, span[0], span[1])
    ctx = max(0, (width - (raw_end - raw_start)) // 2)
    left = max(0, raw_start - ctx)
    right = min(len(raw_text), raw_end + ctx)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(raw_text) else ""
    return f"{prefix}{raw_text[left:right]}{suffix}"


# ---------------------------------------------------------------------------
# 排序与游标（游标键只需唯一+稳定，按位置定位，无需序敏感）
# ---------------------------------------------------------------------------


def _sort_key(hit: SearchHit, order: str) -> tuple:
    if order == "relevance":
        # 相关性只用于排序（§6.1）；确定性：得分降序 → 时间升序 → revision_id
        return (
            -hit.score,
            0 if hit.time_known else 1,
            hit.source_created_at or "",
            hit.revision_id,
        )
    if order == "reverse_chronological":
        # 已知时间降序，未知时间排最后；同时间戳 revision_id 升序（决策 19）
        return (
            0 if hit.time_known else 1,
            hit.source_created_at or "",
            hit.revision_id,
            -hit.score,
        )
    return (0 if hit.time_known else 1, hit.source_created_at or "", hit.revision_id, -hit.score)


def _sort_hits(hits: list[SearchHit], order: str) -> list[SearchHit]:
    if order == "reverse_chronological":
        known = sorted(
            (h for h in hits if h.time_known),
            key=lambda h: (h.source_created_at or "", h.revision_id),
            reverse=True,
        )
        unknown = sorted(
            (h for h in hits if not h.time_known), key=lambda h: h.revision_id
        )
        return known + unknown
    return sorted(hits, key=lambda h: _sort_key(h, order))


def _encode_cursor(hit: SearchHit, order: str) -> str:
    payload = {"k": [str(x) for x in _sort_key(hit, order)]}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _cursor_start(hits: list[SearchHit], cursor: str, order: str) -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        key = [str(x) for x in data["k"]]
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无效游标: {exc}") from exc
    for i, hit in enumerate(hits):
        if [str(x) for x in _sort_key(hit, order)] == key:
            return i + 1
    raise ValueError("游标不匹配当前结果集（结果可能已变化）")


def _apply_text_budget(page: list[SearchHit], limits: SearchLimits) -> None:
    """单次正文总量硬上限（§6.3：8000 字符），超出部分截断/置空。"""
    total = 0
    for hit in page:
        text = hit.raw_text or ""
        remain = max(0, limits.max_text_chars - total)
        if len(text) > remain:
            hit.raw_text = text[:remain]
        total += min(len(text), remain)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def search_history(
    conn: sqlite3.Connection,
    query: str,
    *,
    match_mode: str = "literal",
    filters: SearchFilters | None = None,
    order: str = "chronological",
    limit: int | None = None,
    cursor: str | None = None,
    limits: SearchLimits | None = None,
    use_fts: bool = True,
) -> SearchResult:
    """历史检索主入口。

    ``use_fts=False`` 强制全量基线扫描（差分验证用；两者匹配集合
    必须一致，§6.2 候选优化不得改变匹配集合）。
    """
    limits = limits or SearchLimits()
    filters = filters or SearchFilters()
    if match_mode not in ("literal", "all_terms"):
        raise ValueError(f"未知 match_mode: {match_mode}")
    if order not in ("chronological", "reverse_chronological", "relevance"):
        raise ValueError(f"未知 order: {order}")
    if filters.path_mode not in ("all", "selected"):
        raise ValueError(f"未知 path_mode: {filters.path_mode}")

    norm_query, _ = normalize_with_map(query)
    if not norm_query.strip():
        raise ValueError("空查询：历史检索不接受空 query（不触发全库导出）")
    terms = [norm_query] if match_mode == "literal" else norm_query.split()
    plans = [plan_term(t) for t in terms]
    if not use_fts:
        plans = [
            TermPlan(term=p.term, fts_tokens=()) for p in plans
        ]

    eff_limit = min(limit if limit is not None else limits.default_limit, limits.max_limit)
    if eff_limit < 1:
        raise ValueError("limit 必须 ≥ 1")

    params: list = []
    base_sql = _filter_sql(filters, params)
    rows = conn.execute(base_sql, params).fetchall()
    by_rid = {r["revision_id"]: r for r in rows}

    # 候选缩小：可安全预筛选的词用 FTS（超集），其余词回退全过滤池。
    # 任一回退词存在且过滤池超出 max_scan → QUERY_TOO_BROAD（§6.3）。
    needs_fallback = any(p.needs_fallback for p in plans)
    if needs_fallback and len(rows) > limits.max_scan:
        raise QueryTooBroad(
            f"回退扫描范围 {len(rows)} 条超出资源上限 {limits.max_scan}；"
            "请缩小时间范围或指定来源（QUERY_TOO_BROAD）"
        )
    candidates: set[str] | None = None
    for plan in plans:
        if plan.needs_fallback:
            term_set = set(by_rid)
        else:
            fts_ids = fts_match(conn, build_fts_query(plan.fts_tokens))
            term_set = set(by_rid) & fts_ids
        candidates = term_set if candidates is None else (candidates & term_set)
        if not candidates:
            candidates = set()
            break

    selected_events: set[str] | None = None
    path_unknown: list[str] = []
    if filters.path_mode == "selected":
        selected_events, path_unknown = _selected_path_events(conn)

    hits: list[SearchHit] = []
    for rid in candidates or set():
        row = by_rid[rid]
        if selected_events is not None and row["event_id"] not in selected_events:
            continue
        ok, span, score = _verify(row["search_text"], plans)
        if not ok or span is None:
            continue
        hits.append(
            SearchHit(
                event_id=row["event_id"],
                revision_id=rid,
                is_current=row["current_revision_id"] == rid,
                conversation_id=row["conversation_id"],
                speaker_id=row["speaker_id"],
                speaker_type=row["speaker_type"],
                content_type=row["content_type"],
                source_created_at=row["source_created_at"],
                time_known=row["source_created_at"] is not None,
                score=score,
                raw_text=row["raw_text"],
                source_locator=row["source_locator"],
                match_span=span,
            )
        )

    total_matched = len(hits)
    hits = _sort_hits(hits, order)
    start_index = _cursor_start(hits, cursor, order) if cursor else 0
    page = hits[start_index : start_index + eff_limit]
    truncated = (start_index + eff_limit) < total_matched

    # 页内富化：snippet（原文偏移映射，锚定命中处）+ 出现位置（来源追溯）
    for hit in page:
        row = by_rid[hit.revision_id]
        if hit.raw_text and row["search_text"] and hit.match_span is not None:
            hit.snippet = _snippet(
                hit.raw_text, row["search_text"], hit.match_span, limits.snippet_chars
            )
        hit.occurrences = [
            {
                "account_namespace": o["account_namespace"],
                "archive_relative_path": o["archive_relative_path"],
                "source_locator": o["source_locator"],
            }
            for o in conn.execute(
                """
                SELECT src.account_namespace, s.archive_relative_path, o.source_locator
                FROM revision_occurrences o
                JOIN source_snapshots s ON s.snapshot_id = o.snapshot_id
                JOIN sources src ON src.source_id = s.source_id
                WHERE o.revision_id = ?
                ORDER BY s.imported_at
                """,
                (hit.revision_id,),
            ).fetchall()
        ]
    _apply_text_budget(page, limits)

    return SearchResult(
        hits=page,
        total_matched=total_matched,
        truncated=truncated,
        next_cursor=_encode_cursor(page[-1], order) if truncated and page else None,
        undated_excluded=_count_undated(conn, filters),
        path_unknown_conversations=path_unknown,
        interpreted={"date_from": filters.date_from, "date_to": filters.date_to},
        query_plans=[
            {
                "term": p.term,
                "fts_tokens": list(p.fts_tokens),
                "fallback": p.needs_fallback,
            }
            for p in plans
        ],
    )


def _count_undated(conn: sqlite3.Connection, filters: SearchFilters) -> int:
    """时间过滤生效时，被排除的未知时间记录数（§4.4 报告存在性）。"""
    if filters.date_from is None and filters.date_to is None:
        return 0
    relaxed = SearchFilters(
        source_id=filters.source_id,
        account_namespace=filters.account_namespace,
        conversation_id=filters.conversation_id,
        speaker_type=filters.speaker_type,
        path_mode="all",
        allow_scopes=filters.allow_scopes,
        deny_sensitivity=filters.deny_sensitivity,
        allow_unclassified=filters.allow_unclassified,
    )
    params: list = []
    sql = _filter_sql(relaxed, params) + " AND r.source_created_at IS NULL"
    row = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# recent：最近事件专用接口（空 query 不触发全库导出，§6.3）
# ---------------------------------------------------------------------------


def recent_events(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    limit: int | None = None,
    limits: SearchLimits | None = None,
    now: datetime | None = None,
) -> SearchResult:
    limits = limits or SearchLimits()
    eff_limit = min(limit if limit is not None else limits.default_limit, limits.max_limit)
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    window_start = (ref - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    filters = SearchFilters(date_from=window_start + "Z")
    params: list = []
    sql = _filter_sql(filters, params)
    rows = conn.execute(sql, params).fetchall()
    hits = [
        SearchHit(
            event_id=r["event_id"],
            revision_id=r["revision_id"],
            is_current=r["current_revision_id"] == r["revision_id"],
            conversation_id=r["conversation_id"],
            speaker_id=r["speaker_id"],
            speaker_type=r["speaker_type"],
            content_type=r["content_type"],
            source_created_at=r["source_created_at"],
            time_known=r["source_created_at"] is not None,
            score=0,
            raw_text=r["raw_text"],
            source_locator=r["source_locator"],
        )
        for r in rows
    ]
    hits.sort(key=lambda h: (h.source_created_at or "", h.revision_id), reverse=True)
    total = len(hits)
    page = hits[:eff_limit]
    for hit in page:
        hit.occurrences = [
            {
                "account_namespace": o["account_namespace"],
                "archive_relative_path": o["archive_relative_path"],
                "source_locator": o["source_locator"],
            }
            for o in conn.execute(
                """
                SELECT src.account_namespace, s.archive_relative_path, o.source_locator
                FROM revision_occurrences o
                JOIN source_snapshots s ON s.snapshot_id = o.snapshot_id
                JOIN sources src ON src.source_id = s.source_id
                WHERE o.revision_id = ?
                ORDER BY s.imported_at
                """,
                (hit.revision_id,),
            ).fetchall()
        ]
    _apply_text_budget(page, limits)
    return SearchResult(
        hits=page,
        total_matched=total,
        truncated=total > eff_limit,
        next_cursor=None,
        undated_excluded=0,
        path_unknown_conversations=[],
        interpreted={"date_from": filters.date_from, "date_to": None},
        query_plans=[],
    )
