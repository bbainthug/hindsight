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
from typing import Literal
from zoneinfo import ZoneInfo

from personal_brain.history.normalization import normalize_with_map, raw_span
from personal_brain.retrieval.bigram import TermPlan, build_fts_query, plan_term
from personal_brain.retrieval.credentials import redact_known_credentials
from personal_brain.retrieval.embeddings import EmbeddingProvider
from personal_brain.retrieval.fts import fts_match
from personal_brain.retrieval.semantic_index import SemanticConfig


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
    match_kind: Literal["exact", "semantic", "both"] = "exact"  # D-1
    semantic_score: float | None = None  # 语义相似度 1/(1+L2距离)；exact-only 为 None


# D-1：模式常量与动态说明文案（§390 语义命中必须可区分、可关闭）
SEARCH_MODES = ("exact", "hybrid")
NOTE_EXACT_ONLY = (
    "语义扩展未实现：同义表达（如“求职/找工作”）需分别检索（§6.3 已知能力边界）"
)
NOTE_SEMANTIC_INFERRED = "semantic_hits_are_inferred: 语义命中为推断，非原文连续出现"
RRF_K = 60


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
    notes: tuple[str, ...] = (NOTE_EXACT_ONLY,)
    credentials_redacted: bool = False


_BASE_FROM_WHERE = """
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
# raw_text 不进 search 池查询（§9.2 规模）：候选可达数万行，正文仅页内
# ≤limit 行需要，页确定后按 revision_id 补取——显著降低每次查询的
# I/O 与对象物化成本。recent 路径只取 limit+1 行，正文列直接带上。


def _filter_sql(
    filters: SearchFilters, params: list, *, include_raw_text: bool = False
) -> str:
    columns = (
        "SELECT r.revision_id, r.event_id, r.raw_text, r.search_text, r.content_type,"
        " r.source_created_at, r.source_locator, e.conversation_id, e.speaker_id,"
        " e.speaker_type, e.current_revision_id, s.account_namespace, s.source_id"
        if include_raw_text
        else "SELECT r.revision_id, r.event_id, r.search_text, r.content_type,"
        " r.source_created_at, r.source_locator, e.conversation_id, e.speaker_id,"
        " e.speaker_type, e.current_revision_id, s.account_namespace, s.source_id"
    )
    sql = columns + _BASE_FROM_WHERE
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


def _snippet_window(
    raw_text: str, search_text: str, span: tuple[int, int], width: int
) -> tuple[int, int, str, str]:
    """Return the source-text window and ellipsis markers for a match."""
    norm, offsets = normalize_with_map(raw_text)
    if norm != search_text:
        # 理论不可达：索引与映射同实现；防御性回退为原文开头
        right = min(len(raw_text), width)
        return 0, right, "", ""
    raw_start, raw_end = raw_span(norm, offsets, span[0], span[1])
    ctx = max(0, (width - (raw_end - raw_start)) // 2)
    left = max(0, raw_start - ctx)
    right = min(len(raw_text), raw_end + ctx)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(raw_text) else ""
    return left, right, prefix, suffix


def _render_snippet(
    raw_text: str, window: tuple[int, int, str, str]
) -> str:
    left, right, prefix, suffix = window
    return f"{prefix}{raw_text[left:right]}{suffix}"


def _snippet(raw_text: str, search_text: str, span: tuple[int, int], width: int) -> str:
    """匹配区间 → 原文片段（偏移映射，§6.2 不用归一化索引切原文）。"""
    return _render_snippet(raw_text, _snippet_window(raw_text, search_text, span, width))


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


def _hybrid_sort_key(hit: SearchHit, order: str, rrf: dict[str, float]) -> tuple:
    """返回稳定游标键；时间序中 RRF 只参与同一时间戳的 tie-break。"""
    r = rrf.get(hit.revision_id, 0.0)
    if order == "relevance":
        return (
            -r,
            0 if hit.time_known else 1,
            hit.source_created_at or "",
            hit.revision_id,
        )
    # 游标键只用于相等匹配；实际 reverse 的降序由 _sort_hits_hybrid 处理。
    return (
        0 if hit.time_known else 1,
        hit.source_created_at or "",
        -r,
        hit.revision_id,
    )


def _sort_hits_hybrid(
    hits: list[SearchHit], order: str, rrf: dict[str, float]
) -> list[SearchHit]:
    """按两路 RRF 总分排序；时间序保留时间主序、RRF 仅作同刻 tie-break。"""
    if order == "relevance":
        return sorted(hits, key=lambda h: _hybrid_sort_key(h, order, rrf))
    known = sorted(
        (h for h in hits if h.time_known),
        key=lambda h: (h.source_created_at or "", h.revision_id),
        reverse=order == "reverse_chronological",
    )
    known = _restabilize_time(known, rrf)
    unknown = sorted(
        (h for h in hits if not h.time_known),
        key=lambda h: (-rrf.get(h.revision_id, 0.0), h.revision_id),
    )
    return known + unknown


def _restabilize_time(known: list[SearchHit], rrf: dict[str, float]) -> list[SearchHit]:
    """时间已排序后，把同时间戳组按 RRF 降序、revision_id 升序排列。"""
    out: list[SearchHit] = []
    group: list[SearchHit] = []
    for h in known:
        if group and h.source_created_at != group[0].source_created_at:
            out.extend(
                sorted(group, key=lambda g: (-rrf.get(g.revision_id, 0.0), g.revision_id))
            )
            group = []
        group.append(h)
    out.extend(
        sorted(group, key=lambda g: (-rrf.get(g.revision_id, 0.0), g.revision_id))
    )
    return out


def _encode_cursor(
    hit: SearchHit, order: str, key_fn=None
) -> str:
    key_fn = key_fn or _sort_key
    payload = {"k": [str(x) for x in key_fn(hit, order)]}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _cursor_start(hits: list[SearchHit], cursor: str, order: str, key_fn=None) -> int:
    key_fn = key_fn or _sort_key
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        key = [str(x) for x in data["k"]]
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无效游标: {exc}") from exc
    for i, hit in enumerate(hits):
        if [str(x) for x in key_fn(hit, order)] == key:
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


def _fetch_fts_rows(
    conn: sqlite3.Connection,
    base_sql: str,
    params: list,
    plans: list[TermPlan],
) -> list[sqlite3.Row]:
    """FTS 路径 SQL 侧预过滤：候选词以 MATCH 子查询嵌入基础 SQL。

    策略/时间过滤仍在 SQL 层且只对候选行求值（§9.2 规模门槛）——
    避免每次查询全表载入，也避免分块 IN 的多次往返。返回候选行供 Python
    侧验证（FTS 二元切分是超集，验证语义不可省）。
    """
    for plan in plans:
        base_sql += (
            " AND r.revision_id IN (SELECT revision_id FROM event_revisions_fts"
            " WHERE event_revisions_fts MATCH ?)"
        )
        params.append(build_fts_query(plan.fts_tokens))
    return conn.execute(base_sql, params).fetchall()


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
    redact_credentials: bool = False,
    mode: str = "exact",
    embedding_provider: EmbeddingProvider | None = None,
    semantic_config: SemanticConfig | None = None,
) -> SearchResult:
    """历史检索主入口。

    ``use_fts=False`` 强制全量基线扫描（差分验证用；两者匹配集合
    必须一致，§6.2 候选优化不得改变匹配集合）。
    ``mode="hybrid"``（D-1）：在 exact 之上叠加本地向量召回通道，
    RRF 融合；向量候选走与 exact 同一套过滤与授权（§7.3）。任何
    退化路径（缺依赖/无索引/模型不一致）都不报错，退回 exact 并在
    notes 说明 ``semantic_unavailable: <reason>``。
    """
    limits = limits or SearchLimits()
    filters = filters or SearchFilters()
    if match_mode not in ("literal", "all_terms"):
        raise ValueError(f"未知 match_mode: {match_mode}")
    if mode not in SEARCH_MODES:
        raise ValueError(f"未知 mode: {mode}")
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

    # 候选缩小：可安全预筛选的词用 FTS（超集），其余词回退全过滤池。
    # 任一回退词存在且过滤池超出 max_scan → QUERY_TOO_BROAD（§6.3）。
    needs_fallback = any(p.needs_fallback for p in plans)
    if needs_fallback:
        rows = conn.execute(base_sql, params).fetchall()
        if len(rows) > limits.max_scan:
            raise QueryTooBroad(
                f"回退扫描范围 {len(rows)} 条超出资源上限 {limits.max_scan}；"
                "请缩小时间范围或指定来源（QUERY_TOO_BROAD）"
            )
    else:
        # FTS 路径：MATCH 子查询嵌入基础 SQL（§9.2 规模门槛：不载全表），
        # 候选行即策略过滤后的 FTS 候选；Python 侧仍须验证（超集）。
        rows = _fetch_fts_rows(conn, base_sql, params, plans)
    by_rid = {r["revision_id"]: r for r in rows}

    candidates: set[str] | None = None
    if needs_fallback:
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
    else:
        candidates = set(by_rid)

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
                raw_text=None,  # 懒加载：页确定后按 revision_id 补取
                source_locator=row["source_locator"],
                match_span=span,
            )
        )

    notes_extra: tuple[str, ...] = ()
    semantic_plan: dict[str, object] | None = None
    chunk_spans: dict[str, tuple[int, int]] = {}
    rrf: dict[str, float] | None = None

    if mode == "hybrid":
        from personal_brain.retrieval.semantic_index import (
            KNN_CAP,
            SEMANTIC_MIN_COSINE,
            distance_to_cosine,
            knn_chunks,
            semantic_unavailable_reason,
        )

        provider = embedding_provider
        if provider is not None and semantic_config is None:
            semantic_config = SemanticConfig(provider.model_id)
        cfg = semantic_config or SemanticConfig()
        reason = semantic_unavailable_reason(
            conn, cfg, provider.dimension if provider is not None else None
        )
        if reason is None and provider is None:
            from personal_brain.retrieval.embeddings import get_or_build_fastembed

            provider = get_or_build_fastembed(cfg.model_id, cfg.cache_dir)
            if provider is None:
                reason = "provider_init_failed"
            else:
                reason = semantic_unavailable_reason(conn, cfg, provider.dimension)
        if reason is not None:
            notes_extra = (f"semantic_unavailable: {reason}",)
        else:
            assert provider is not None
            k_sem = min(eff_limit * 5, KNN_CAP)
            qvec = provider.embed([norm_query])[0]
            knn = knn_chunks(conn, qvec, k_sem)
            semantic_plan = {
                "path": "semantic",
                "model": provider.model_id,
                "k": k_sem,
                "filtered": 0,
            }
            if knn:
                # KNN 全局候选 → 与 exact 同一套 _filter_sql 过滤（§7.3 不可妥协）
                ordered_knn = sorted(
                    knn,
                    key=lambda row: (
                        float(row["distance"]),
                        row["revision_id"],
                        int(row["chunk_id"]),
                    ),
                )
                cand_ids: list[str] = []
                for kr in ordered_knn:
                    if kr["revision_id"] not in cand_ids:
                        cand_ids.append(kr["revision_id"])
                sparams: list = []
                ssql = _filter_sql(filters, sparams)
                ssql += (
                    " AND r.revision_id IN ("
                    + ",".join("?" for _ in cand_ids)
                    + ")"
                )
                srows = {
                    r["revision_id"]: r
                    for r in conn.execute(ssql, [*sparams, *cand_ids]).fetchall()
                }
                # 距离升序保留每个 revision 的最优 chunk；selected 路径约束一致
                sem_best: dict[str, tuple[float, int, int]] = {}
                for kr in ordered_knn:
                    rid = kr["revision_id"]
                    if rid not in srows or rid in sem_best:
                        continue
                    if distance_to_cosine(float(kr["distance"])) < SEMANTIC_MIN_COSINE:
                        continue  # ANN 噪声下限（§docs 语义检索）
                    if (
                        selected_events is not None
                        and srows[rid]["event_id"] not in selected_events
                    ):
                        continue
                    sem_best[rid] = (
                        float(kr["distance"]),
                        int(kr["text_start"]),
                        int(kr["text_end"]),
                    )
                semantic_plan["filtered"] = len(sem_best)
                if sem_best:
                    rrf = {}
                    exact_ranked = _sort_hits(hits, order) if hits else []
                    for rank, h in enumerate(exact_ranked, start=1):
                        rrf[h.revision_id] = 1.0 / (RRF_K + rank)
                    for rank, (rid, (_dist, _s, _e)) in enumerate(
                        sorted(sem_best.items(), key=lambda kv: (kv[1][0], kv[0])), start=1
                    ):
                        rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (RRF_K + rank)
                    exact_rids = {h.revision_id for h in hits}
                    for h in hits:
                        if h.revision_id in sem_best:
                            h.match_kind = "both"
                            h.semantic_score = 1.0 / (1.0 + sem_best[h.revision_id][0])
                    by_rid.update(srows)
                    for rid, (dist, tstart, tend) in sem_best.items():
                        if rid in exact_rids:
                            continue
                        row = srows[rid]
                        chunk_spans[rid] = (tstart, tend)
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
                                score=0,
                                raw_text=None,
                                source_locator=row["source_locator"],
                                match_span=None,
                                match_kind="semantic",
                                semantic_score=1.0 / (1.0 + dist),
                            )
                        )
            notes_extra = (NOTE_SEMANTIC_INFERRED,)

    total_matched = len(hits)
    key_fn = None
    if rrf is not None:
        hits = _sort_hits_hybrid(hits, order, rrf)
        key_fn = lambda h, o: _hybrid_sort_key(h, o, rrf)  # noqa: E731
    else:
        hits = _sort_hits(hits, order)
    start_index = (
        _cursor_start(hits, cursor, order, key_fn) if cursor else 0
    )
    page = hits[start_index : start_index + eff_limit]
    truncated = (start_index + eff_limit) < total_matched

    # 页内正文补取（懒加载）：只对 ≤limit 行取 raw_text（§9.2 规模）
    if page:
        page_ids = [h.revision_id for h in page]
        texts = {
            r["revision_id"]: r["raw_text"]
            for r in conn.execute(
                "SELECT revision_id, raw_text FROM event_revisions"
                " WHERE revision_id IN (" + ",".join("?" for _ in page_ids) + ")",
                page_ids,
            ).fetchall()
        }
        for hit in page:
            hit.raw_text = texts.get(hit.revision_id)

    credentials_redacted = False

    # 页内富化：snippet（原文偏移映射，锚定命中处）+ 出现位置（来源追溯）
    for hit in page:
        row = by_rid[hit.revision_id]
        original_text = hit.raw_text
        if original_text:
            safe_text = (
                redact_known_credentials(original_text)
                if redact_credentials
                else original_text
            )
            assert safe_text is not None
            credentials_redacted |= safe_text != original_text
            hit.raw_text = safe_text
            if row["search_text"] and hit.match_span is not None:
                # Calculate offsets against the original text, then render the
                # same-length redacted text. This prevents a secret near a
                # snippet boundary from leaking a partial token.
                window = _snippet_window(
                    original_text,
                    row["search_text"],
                    hit.match_span,
                    limits.snippet_chars,
                )
                hit.snippet = _render_snippet(safe_text, window)
            elif (
                row["search_text"]
                and hit.match_span is None
                and hit.revision_id in chunk_spans
            ):
                # D-1：语义命中没有 match_span，snippet 用所属 chunk 的窗口
                window = _snippet_window(
                    original_text,
                    row["search_text"],
                    chunk_spans[hit.revision_id],
                    limits.snippet_chars,
                )
                hit.snippet = _render_snippet(safe_text, window)
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

    plans_payload: list[dict[str, object]] = [
        {
            "term": p.term,
            "fts_tokens": list(p.fts_tokens),
            "fallback": p.needs_fallback,
        }
        for p in plans
    ]
    if semantic_plan is not None:
        plans_payload.append(semantic_plan)

    return SearchResult(
        hits=page,
        total_matched=total_matched,
        truncated=truncated,
        next_cursor=(
            _encode_cursor(page[-1], order, key_fn) if truncated and page else None
        ),
        undated_excluded=_count_undated(conn, filters),
        path_unknown_conversations=path_unknown,
        interpreted={"date_from": filters.date_from, "date_to": filters.date_to},
        query_plans=plans_payload,
        notes=notes_extra if mode == "hybrid" else (NOTE_EXACT_ONLY,),
        credentials_redacted=credentials_redacted,
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
    cursor: str | None = None,
    filters: SearchFilters | None = None,
    redact_credentials: bool = False,
) -> SearchResult:
    limits = limits or SearchLimits()
    filters = filters or SearchFilters()
    eff_limit = min(limit if limit is not None else limits.default_limit, limits.max_limit)
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    window_start = (ref - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    # 窗口过滤 + 调用方策略/窄化过滤合并（策略过滤永远保留）
    eff_filters = SearchFilters(
        date_from=window_start + "Z",
        source_id=filters.source_id,
        account_namespace=filters.account_namespace,
        conversation_id=filters.conversation_id,
        speaker_type=filters.speaker_type,
        allow_scopes=filters.allow_scopes,
        deny_sensitivity=filters.deny_sensitivity,
        allow_unclassified=filters.allow_unclassified,
    )
    params: list = []
    sql = _filter_sql(eff_filters, params, include_raw_text=True)
    # keyset 游标：(source_created_at, revision_id) 降序；等宽 ISO 字典序=时间序。
    # 空间键 9 的补数不可用于跨字母字符串，直接用降序条件而非倒序索引键。
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            c_ts, c_rid = str(data["t"]), str(data["r"])
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"无效游标: {exc}") from exc
        sql += (
            " AND (r.source_created_at < ?"
            " OR (r.source_created_at = ? AND r.revision_id < ?))"
        )
        params.extend([c_ts, c_ts, c_rid])
    sql += " ORDER BY r.source_created_at DESC, r.revision_id DESC LIMIT ?"
    params.append(eff_limit + 1)
    rows = conn.execute(sql, params).fetchall()
    truncated = len(rows) > eff_limit
    rows = rows[:eff_limit]
    credentials_redacted = False
    hits: list[SearchHit] = []
    for r in rows:
        original_text = r["raw_text"] or ""
        safe_text = (
            redact_known_credentials(original_text)
            if redact_credentials
            else original_text
        )
        assert safe_text is not None
        credentials_redacted |= safe_text != original_text
        hits.append(
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
                raw_text=safe_text,
                snippet=safe_text[: limits.snippet_chars] or None,
                source_locator=r["source_locator"],
            )
        )
    total = len(hits)  # keyset 分页：total_matched = 本页大小，truncated+cursor 表示更多
    for hit in hits:
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
    page = hits
    _apply_text_budget(page, limits)
    return SearchResult(
        hits=page,
        total_matched=total,
        truncated=truncated,
        next_cursor=(
            _encode_recent_cursor(page[-1]) if truncated and page else None
        ),
        undated_excluded=0,
        path_unknown_conversations=[],
        interpreted={"date_from": eff_filters.date_from, "date_to": None},
        query_plans=[],
        credentials_redacted=credentials_redacted,
    )


def _encode_recent_cursor(hit: SearchHit) -> str:
    payload = {"t": hit.source_created_at or "", "r": hit.revision_id}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
