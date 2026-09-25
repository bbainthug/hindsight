"""只读 MCP 服务层（§8）：4 个工具的统一实现，供 stdio 服务器与测试共用。

安全语义（§7.3/§7.6）：
- profile 来自受信任启动配置；请求参数只能**收窄**过滤，永不放宽。
- 过滤在 SQL 层、snippet/排序之前完成；计数（total_matched、
  undated_excluded、brain_status 覆盖）只统计调用者可见档案，
  不泄露隐藏记录数量（§7.3）。
- ``get_event`` 按事件逐次重新授权；无权限与不存在返回相同的
  ``NOT_FOUND_OR_NOT_ALLOWED``；撤回内容不因引用 ID 绕过（§8.1）。
- 请求返回前核对 policy epoch；执行中策略变化 → 整体重跑（最多 3 次），
  仍不稳定则放弃结果（TRANSIENT_POLICY_CHANGE）。
- 逻辑定位符只暴露 snapshot_id/node_id，不暴露磁盘路径（§8.1）。
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict

from personal_brain.history.policy_epoch import current_policy_epoch
from personal_brain.policy.profiles import AccessProfile
from personal_brain.retrieval.credentials import redact_known_credentials
from personal_brain.retrieval.embeddings import EmbeddingProvider
from personal_brain.retrieval.search import (
    SEARCH_MODES,
    QueryTooBroad,
    SearchFilters,
    SearchHit,
    SearchLimits,
    parse_date_bound,
    recent_events,
    search_history,
)
from personal_brain.retrieval.semantic_index import SemanticConfig

SCHEMA_VERSION = "0.2"
CONTENT_TRUST = "untrusted_archive"
MAX_CONTEXT_RADIUS = 3
EPOCH_RETRIES = 3
# 兼容常量：与 retrieval.search.NOTE_EXACT_ONLY 文本一致（exact 模式默认能力说明）。
# hybrid 模式的 warnings 改为来自 result.notes（推断声明/退化原因）。
CAPABILITY_NOTE = (
    "语义扩展未实现：同义表达（如“求职/找工作”）需分别检索（§6.3 已知能力边界）"
)
CREDENTIAL_REDACTION_NOTE = (
    "远程交付前已遮蔽可识别的凭据形态；这是启发式拦截，不是完整 DLP。"
)


class PolicyEpochChurn(RuntimeError):
    """策略在请求执行中持续变化；放弃结果而非返回可能越权的正文。"""


class ToolError(Exception):
    """工具级错误：转为 MCP isError 结果，不中断服务器。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


# ---------------------------------------------------------------------------
# 授权过滤
# ---------------------------------------------------------------------------


def profile_filters(profile: AccessProfile) -> SearchFilters:
    """profile → 强制过滤。'*' 通配 = 不限 scope；agent 默认不开放未分类。"""
    return SearchFilters(
        allow_scopes=None if "*" in profile.allow_scopes else profile.allow_scopes,
        deny_sensitivity=profile.deny_sensitivity or None,
        allow_unclassified=profile.allow_unclassified,
    )


def _remote_delivery(profile: AccessProfile) -> bool:
    """Whether response content crosses the configured remote-model boundary."""

    return profile.delivery_boundary == "remote_model_allowed"


def _authorized_revision_ids(
    conn: sqlite3.Connection, filters: SearchFilters, event_id: str
) -> list[str]:
    """给定事件中调用者可见的 revision 列表（逐事件授权，§7.3）。"""
    params: list = [event_id]
    sql = """
    SELECT r.revision_id FROM event_revisions r
    JOIN sources s ON s.source_id = (
        SELECT source_id FROM events WHERE event_id = r.event_id)
    WHERE r.event_id = ? AND s.status != 'withdrawn'
      AND EXISTS (
        SELECT 1 FROM revision_policy_state ps
        WHERE ps.revision_id = r.revision_id
          AND ps.valid_to IS NULL AND ps.availability = 'available')
    """
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
    if not filters.allow_unclassified:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps2"
            " WHERE ps2.revision_id = r.revision_id AND ps2.valid_to IS NULL"
            " AND ps2.classification_status != 'unclassified')"
        )
    sql += " ORDER BY r.recorded_at, r.revision_id"
    return [r["revision_id"] for r in conn.execute(sql, params)]


def _event_authorized(conn: sqlite3.Connection, filters: SearchFilters, event_id: str) -> bool:
    return bool(_authorized_revision_ids(conn, filters, event_id))


def _policy_exists_sql(ps_alias: str, filters: SearchFilters, params: list) -> str:
    """D-7：与 _authorized_revision_ids 完全同一套可见性条件，拼在
    ``<ps_alias>.revision_id = <外层 revision 列>`` 的 EXISTS 之外层。

    只供新工具（recall/timeline）复用；既有工具的 SQL 不动，避免改变
    现有行为。调用方负责把 params 追加到查询参数。条件顺序与语义与
    _authorized_revision_ids 一致：available + scope 白名单 + 敏感拒绝 +
    未分类开关。
    """
    p = ps_alias
    sql = f"""
      EXISTS (SELECT 1 FROM revision_policy_state {p}
        WHERE {p}.revision_id = r.revision_id
          AND {p}.valid_to IS NULL AND {p}.availability = 'available')"""
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
    if not filters.allow_unclassified:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps2"
            " WHERE ps2.revision_id = r.revision_id AND ps2.valid_to IS NULL"
            " AND ps2.classification_status != 'unclassified')"
        )
    return sql


# ---------------------------------------------------------------------------
# 结果映射
# ---------------------------------------------------------------------------


def _occurrence_ref(conn: sqlite3.Connection, revision_id: str) -> dict[str, str]:
    """逻辑定位符：snapshot_id + node_id，不含磁盘路径（§8.1）。"""
    row = conn.execute(
        """
        SELECT s.snapshot_id, o.source_locator FROM revision_occurrences o
        JOIN source_snapshots s ON s.snapshot_id = o.snapshot_id
        WHERE o.revision_id = ? ORDER BY s.imported_at LIMIT 1
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        return {"snapshot_id": "", "node_id": ""}
    locator = row["source_locator"] or ""
    node_id = locator.rsplit("/mapping/", 1)[-1] if "/mapping/" in locator else ""
    return {"snapshot_id": row["snapshot_id"], "node_id": node_id}


def _hit_to_result(conn: sqlite3.Connection, hit: SearchHit) -> dict:
    return {
        "event_id": hit.event_id,
        "revision_id": hit.revision_id,
        "is_current": hit.is_current,
        "speaker_type": hit.speaker_type,
        "timestamp": hit.source_created_at,
        "time_known": hit.time_known,
        "snippet": hit.snippet,
        "citation_id": f"pb:{hit.revision_id}",
        "evidence_level": "message_record",
        "match_kind": hit.match_kind,
        "semantic_score": hit.semantic_score,
        "source_locator": _occurrence_ref(conn, hit.revision_id),
    }


# ---------------------------------------------------------------------------
# 覆盖期缓存（D-5b）：_coverage 全库聚合在 840MB 级库上约 7s，每个工具调用都要
# 付一次。键 = (filters 规范化签名, branch_scope, policy_epoch, import_watermark)：
# - policy_epoch：撤回/标注递增 → 撤回立即反映（不等 TTL）；
# - import_watermark：import_jobs 的 MAX(rowid)。import_jobs 行与本次导入的
#   事件在同一事务内提交（history/store.py::publish_snapshot），所以它和
#   "这批事件对其他连接可见" 是同一个提交点，任何连接读到的都是同一提交历史
#   下的同一个值 —— 跨连接可比。
#   最初用的是 PRAGMA data_version，但它只保证"同一连接内，值变化 ⇒ 库被别的
#   连接改过"，不保证跨连接可比：REST 连接池会新开连接，新连接的 data_version
#   起始值可能和旧连接缓存过的某个值撞上，造成新连接读到导入前的旧缓存。
_COVERAGE_CACHE_MAX = 32
_coverage_cache: OrderedDict[tuple, dict] = OrderedDict()
_coverage_cache_lock = threading.Lock()


def _import_watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM import_jobs").fetchone()
    return int(row[0])


def _coverage_cache_key(
    conn: sqlite3.Connection, filters: SearchFilters, branch_scope: str
) -> tuple:
    """缓存键：filters 的规范化签名 + branch_scope + 两个失效计数。"""
    sig = (
        tuple(filters.allow_scopes) if filters.allow_scopes is not None else None,
        tuple(filters.deny_sensitivity) if filters.deny_sensitivity is not None else None,
        filters.allow_unclassified,
        filters.date_from,
        filters.date_to,
        filters.source_id,
        filters.account_namespace,
        filters.conversation_id,
        filters.speaker_type,
        filters.path_mode,
    )
    epoch = current_policy_epoch(conn)
    watermark = _import_watermark(conn)
    return (sig, branch_scope, epoch, watermark)


def _coverage(
    conn: sqlite3.Connection, filters: SearchFilters, branch_scope: str
) -> dict:
    key = _coverage_cache_key(conn, filters, branch_scope)
    with _coverage_cache_lock:
        cached = _coverage_cache.get(key)
        if cached is not None:
            _coverage_cache.move_to_end(key)
            return dict(cached)
    # 锁外计算：全库聚合可达秒级，不阻塞其他键的并发命中
    value = _coverage_uncached(conn, filters, branch_scope)
    with _coverage_cache_lock:
        if key in _coverage_cache:  # 并发同键：采用先算完的结果（一致）
            value = dict(_coverage_cache[key])
        else:
            _coverage_cache[key] = dict(value)
            while len(_coverage_cache) > _COVERAGE_CACHE_MAX:
                _coverage_cache.popitem(last=False)
    return value


def _coverage_uncached(
    conn: sqlite3.Connection, filters: SearchFilters, branch_scope: str
) -> dict:
    """调用者可见档案的覆盖期（§7.3：不泄露隐藏记录数量）。"""
    params: list = []
    sql = """
    SELECT MIN(r.source_created_at) AS lo, MAX(r.source_created_at) AS hi
    FROM event_revisions r
    JOIN sources s ON s.source_id = (
        SELECT source_id FROM events WHERE event_id = r.event_id)
    WHERE s.status != 'withdrawn' AND r.source_created_at IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM revision_policy_state ps
        WHERE ps.revision_id = r.revision_id
          AND ps.valid_to IS NULL AND ps.availability = 'available')
    """
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
    if not filters.allow_unclassified:
        sql += (
            " AND EXISTS (SELECT 1 FROM revision_policy_state ps2"
            " WHERE ps2.revision_id = r.revision_id AND ps2.valid_to IS NULL"
            " AND ps2.classification_status != 'unclassified')"
        )
    row = conn.execute(sql, params).fetchone()
    return {
        "known_start": row["lo"],
        "known_end": row["hi"],
        "completeness": "unknown",
        "branch_scope": branch_scope,
    }


def _envelope(
    results: list[dict],
    *,
    truncated: bool,
    next_cursor: str | None,
    coverage: dict,
    warnings: list[str] | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "content_trust": CONTENT_TRUST,
        "coverage": coverage,
        "results": results,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "warnings": warnings or [],
    }


# ---------------------------------------------------------------------------
# 4 个工具（§8）
# ---------------------------------------------------------------------------


def tool_brain_status(
    conn: sqlite3.Connection, profile: AccessProfile, limits: SearchLimits
) -> dict:
    """健康、调用者可见覆盖期、最后成功导入、解析警告、可用能力。"""
    filters = profile_filters(profile)
    coverage = _coverage(conn, filters, branch_scope="all")
    params: list = []
    visible_sql = """
    SELECT s.source_id, s.account_namespace, s.status,
           MAX(sp.imported_at) AS last_import,
           MAX(sp.parse_status) AS any_partial,
           (SELECT COUNT(*) FROM import_job_errors e
            JOIN import_jobs j ON j.job_id = e.job_id
            WHERE j.source_id = s.source_id AND e.error_code LIKE 'UNSUPPORTED%') AS unsupported
    FROM sources s
    JOIN source_snapshots sp ON sp.source_id = s.source_id
    WHERE s.status != 'withdrawn'
    """
    if filters.allow_scopes is not None:
        visible_sql += (
            " AND EXISTS (SELECT 1 FROM events e2"
            " JOIN revision_policy_state ps3 ON ps3.revision_id = ("
            "   SELECT revision_id FROM event_revisions r WHERE r.event_id = e2.event_id)"
            " JOIN revision_scope_labels sl ON sl.state_id = ps3.state_id"
            " WHERE e2.source_id = s.source_id AND ps3.valid_to IS NULL"
            " AND sl.label IN (" + ",".join("?" for _ in filters.allow_scopes) + "))"
        )
        params.extend(filters.allow_scopes)
    visible_sql += " GROUP BY s.source_id ORDER BY s.created_at"
    sources = []
    warnings: list[str] = []
    for r in conn.execute(visible_sql, params).fetchall():
        sources.append(
            {
                "account_namespace": r["account_namespace"],
                "last_successful_import": r["last_import"],
            }
        )
        if r["unsupported"]:
            # 只报存在性，不报数量/位置（§7.3 计数与错误信息也过权限）
            warnings.append(
                f"来源 {r['account_namespace']} 存在未支持的对话结构（已跳过）"
            )
        if r["any_partial"] == "partial":
            warnings.append(f"来源 {r['account_namespace']} 存在部分解析的快照")
    return _envelope(
        [],
        truncated=False,
        next_cursor=None,
        coverage=coverage,
        warnings=warnings,
    ) | {
        "health": "ok",
        "sources": sources,
        "capabilities": {"tools": list(profile.tools), "read_only": True,
                         "delivery_boundary": profile.delivery_boundary},
    }


def tool_search_history(
    conn: sqlite3.Connection,
    profile: AccessProfile,
    args: dict,
    limits: SearchLimits,
    timezone: str = "UTC",
    embedding_provider: EmbeddingProvider | None = None,
    semantic_config: SemanticConfig | None = None,
) -> dict:
    """授权匹配结果、游标、覆盖说明（§8 search_history）。

    ``embedding_provider``/``semantic_config`` 供评测与基准注入
    HashEmbeddingProvider（离线确定性）；MCP 常驻路径留空，由
    search_history 按配置解析本地 provider（查询路径不下载模型）。
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_PARAMS", "query 必须是非空字符串")
    match_mode = args.get("match_mode", "literal")
    order = args.get("order", "chronological")
    branch_scope = args.get("branch_scope", "all")
    if branch_scope not in ("selected", "all"):
        raise ToolError("INVALID_PARAMS", f"未知 branch_scope: {branch_scope}")
    mode = args["mode"] if "mode" in args else profile.semantic_default
    if mode not in SEARCH_MODES:
        raise ToolError("INVALID_PARAMS", f"未知 mode: {mode}（可选 exact|hybrid）")
    filters = profile_filters(profile)
    filters = SearchFilters(
        date_from=parse_date_bound(args["date_from"], timezone, end_of_day=False)
        if args.get("date_from") else None,
        date_to=parse_date_bound(args["date_to"], timezone, end_of_day=True)
        if args.get("date_to") else None,
        source_id=args.get("source_id"),
        account_namespace=args.get("account_namespace"),
        conversation_id=args.get("conversation_id"),
        speaker_type=args.get("speaker_type"),
        path_mode=branch_scope,
        allow_scopes=filters.allow_scopes,
        deny_sensitivity=filters.deny_sensitivity,
        allow_unclassified=filters.allow_unclassified,
    )
    try:
        result = search_history(
            conn,
            query,
            match_mode=match_mode if match_mode in ("literal", "all_terms") else "literal",
            filters=filters,
            order=(
                order
                if order in ("chronological", "reverse_chronological", "relevance")
                else "chronological"
            ),
            limit=args.get("limit"),
            cursor=args.get("cursor"),
            limits=limits,
            redact_credentials=_remote_delivery(profile),
            mode=mode,
            embedding_provider=embedding_provider,
            semantic_config=semantic_config,
        )
    except QueryTooBroad as exc:
        raise ToolError("QUERY_TOO_BROAD", str(exc)) from exc
    except ValueError as exc:
        raise ToolError("INVALID_PARAMS", str(exc)) from exc
    # 能力说明动态化（D-1）：exact 保留词面边界说明；hybrid 激活时声明
    # 语义命中为推断；退化时给出 semantic_unavailable 原因。
    warnings = list(result.notes)
    if result.credentials_redacted:
        warnings.append(CREDENTIAL_REDACTION_NOTE)
    return _envelope(
        [_hit_to_result(conn, h) for h in result.hits],
        truncated=result.truncated,
        next_cursor=result.next_cursor,
        coverage=_coverage(conn, profile_filters(profile), branch_scope),
        warnings=warnings,
    )


def tool_get_recent_events(
    conn: sqlite3.Connection, profile: AccessProfile, args: dict, limits: SearchLimits
) -> dict:
    """按源时间倒序的授权事件；明确参考时间（§8 get_recent_events）。"""
    from datetime import UTC, datetime

    days = args.get("days", 7)
    if not isinstance(days, int) or days < 0 or days > 3650:
        raise ToolError("INVALID_PARAMS", "days 必须是 0..3650 的整数")
    ref_raw = args.get("now")  # 测试注入参考时间；生产忽略
    now = (
        datetime.fromisoformat(ref_raw)
        if isinstance(ref_raw, str) and ref_raw
        else datetime.now(UTC)
    )
    filters = profile_filters(profile)
    filters = SearchFilters(
        source_id=args.get("source_id"),
        account_namespace=args.get("account_namespace"),
        conversation_id=args.get("conversation_id"),
        allow_scopes=filters.allow_scopes,
        deny_sensitivity=filters.deny_sensitivity,
        allow_unclassified=filters.allow_unclassified,
    )
    result = recent_events(
        conn,
        days=days,
        limit=args.get("limit"),
        limits=limits,
        now=now,
        cursor=args.get("cursor"),
        filters=filters,
        redact_credentials=_remote_delivery(profile),
    )
    warnings = [CREDENTIAL_REDACTION_NOTE] if result.credentials_redacted else []
    return _envelope(
        [_hit_to_result(conn, h) for h in result.hits],
        truncated=result.truncated,
        next_cursor=result.next_cursor,
        coverage=_coverage(conn, profile_filters(profile), branch_scope="all"),
        warnings=warnings,
    ) | {"reference_time": now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"}


def tool_get_event(
    conn: sqlite3.Connection, profile: AccessProfile, args: dict, limits: SearchLimits
) -> dict:
    """原文 + 有界上下文 + 版本/分支信息；逐次重新授权（§8 get_event）。"""
    event_id = args.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ToolError("INVALID_PARAMS", "event_id 必须是非空字符串")
    radius = args.get("context_radius", 0)
    if not isinstance(radius, int) or radius < 0 or radius > MAX_CONTEXT_RADIUS:
        raise ToolError(
            "INVALID_PARAMS", f"context_radius 必须是 0..{MAX_CONTEXT_RADIUS} 的整数"
        )
    filters = profile_filters(profile)
    redact_credentials = _remote_delivery(profile)
    auth_revs = _authorized_revision_ids(conn, filters, event_id)
    if not auth_revs:
        # 无权限与不存在返回相同错误（§7.3）
        raise ToolError("NOT_FOUND_OR_NOT_ALLOWED")
    ev = conn.execute(
        """
        SELECT e.event_id, e.current_revision_id, e.revision_resolution,
               e.speaker_id, e.speaker_type, e.conversation_id, s.account_namespace
        FROM events e JOIN sources s ON s.source_id = e.source_id
        WHERE e.event_id = ?
        """,
        (event_id,),
    ).fetchone()
    requested = args.get("revision_id")
    if requested and requested not in auth_revs:
        raise ToolError("NOT_FOUND_OR_NOT_ALLOWED")
    revision_id = requested or (
        ev["current_revision_id"] if ev["current_revision_id"] in auth_revs else auth_revs[-1]
    )
    rev = conn.execute(
        "SELECT * FROM event_revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    ref = _occurrence_ref(conn, revision_id)

    context: list[dict] = []
    context_redacted = False
    if radius:
        context, context_redacted = _authorized_context(
            conn,
            filters,
            event_id,
            ev["conversation_id"],
            revision_id,
            radius,
            redact_credentials=redact_credentials,
        )

    offset = args.get("text_offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolError("INVALID_PARAMS", "text_offset 必须为非负整数")
    # Context and body share the configured text budget, including long imported messages.
    budget = limits.max_text_chars
    bounded_context = []
    for item in context:
        if budget <= 1:
            break
        snippet = item["snippet"][:min(200, budget // 4)]
        bounded_context.append({**item, "snippet": snippet})
        budget -= len(snippet)
    raw_text = rev["raw_text"] or ""
    safe_raw_text = (
        redact_known_credentials(raw_text) if redact_credentials else raw_text
    )
    assert safe_raw_text is not None
    credentials_redacted = context_redacted or safe_raw_text != raw_text
    page_text = safe_raw_text[offset:offset + budget]
    next_offset = offset + len(page_text) if offset + len(page_text) < len(raw_text) else None

    versions = [
        {
            "revision_id": rid,
            "is_current": rid == ev["current_revision_id"],
        }
        for rid in auth_revs
    ]
    result = {
        "event_id": event_id,
        "revision_id": revision_id,
        "is_current": revision_id == ev["current_revision_id"],
        "speaker_type": ev["speaker_type"],
        "speaker_id": ev["speaker_id"],
        "conversation_id": ev["conversation_id"],
        "account_namespace": ev["account_namespace"],
        "timestamp": rev["source_created_at"],
        "content_type": rev["content_type"],
        "raw_text": page_text,
        "text_offset": offset,
        "next_text_offset": next_offset,
        "raw_text_total_chars": len(raw_text),
        "revision_resolution": ev["revision_resolution"],
        "versions": versions,
        "branch_context": {
            "conversation_id": ev["conversation_id"],
            "note": "仅所选路径上的有界邻接，不返回完整对话树（§8）",
        },
        "citation_id": f"pb:{revision_id}",
        "evidence_level": "message_record",
        "source_locator": ref,
        "context_radius": radius,
        "context": bounded_context,
    }
    return _envelope(
        [result],
        truncated=next_offset is not None or len(bounded_context) < len(context),
        next_cursor=None,
        coverage=_coverage(conn, filters, branch_scope="all"),
        warnings=[CREDENTIAL_REDACTION_NOTE] if credentials_redacted else [],
    )


def _authorized_context(
    conn: sqlite3.Connection,
    filters: SearchFilters,
    event_id: str,
    conversation_id: str,
    revision_id: str,
    radius: int,
    *,
    redact_credentials: bool = False,
) -> tuple[list[dict], bool]:
    """所选路径上有界邻接；每个邻接事件**单独授权**，未授权静默略去。"""
    node = conn.execute(
        "SELECT node_key FROM conversation_nodes WHERE conversation_id = ? AND event_id = ?",
        (conversation_id, event_id),
    ).fetchone()
    if node is None:
        return [], False
    path = conn.execute(
        """
        SELECT sp.node_key, sp.depth, cn.event_id
        FROM conversation_selected_path sp
        JOIN conversation_nodes cn ON cn.node_key = sp.node_key
        WHERE sp.conv_snapshot_id = (
            SELECT cs2.conv_snapshot_id FROM conversation_snapshots cs2
            WHERE cs2.conversation_id = ?
            ORDER BY cs2.rowid DESC LIMIT 1
        ) ORDER BY sp.depth
        """,
        (conversation_id,),
    ).fetchall()
    keys = [r["node_key"] for r in path]
    try:
        pos = keys.index(node["node_key"])
    except ValueError:
        return [], False
    out: list[dict] = []
    credentials_redacted = False
    for i in range(max(0, pos - radius), min(len(path), pos + radius + 1)):
        if i == pos:
            continue
        neighbor_event = path[i]["event_id"]
        if not neighbor_event or not _event_authorized(conn, filters, neighbor_event):
            continue
        nr = conn.execute(
            """
            SELECT r.revision_id, r.raw_text, r.source_created_at,
                   e.speaker_type, e.current_revision_id
            FROM event_revisions r JOIN events e ON e.event_id = r.event_id
            WHERE r.event_id = ? AND r.revision_id = e.current_revision_id
            """,
            (neighbor_event,),
        ).fetchone()
        if nr is None or nr["revision_id"] not in _authorized_revision_ids(
            conn, filters, neighbor_event
        ):
            continue
        raw_text = nr["raw_text"] or ""
        safe_text = (
            redact_known_credentials(raw_text) if redact_credentials else raw_text
        )
        assert safe_text is not None
        credentials_redacted |= safe_text != raw_text
        out.append(
            {
                "event_id": neighbor_event,
                "revision_id": nr["revision_id"],
                "offset": i - pos,  # 负=之前，正=之后
                "speaker_type": nr["speaker_type"],
                "timestamp": nr["source_created_at"],
                "snippet": safe_text[:200],
                "citation_id": f"pb:{nr['revision_id']}",
            }
        )
    return out, credentials_redacted


# ---------------------------------------------------------------------------
# D-7：recall（一次调用的回忆）与 timeline（时间线）
# ---------------------------------------------------------------------------

RECALL_MAX_SNIPPETS = 12
RECALL_DEFAULT_SNIPPETS = 6
RECALL_MAX_RADIUS = 4
RECALL_DEFAULT_RADIUS = 2
RECALL_DEFAULT_CHAR_BUDGET = 6000
RECALL_MAX_CHAR_BUDGET = 12000
TIMELINE_MAX_DAYS = 62
TIMELINE_DEFAULT_PER_DAY = 20
TIMELINE_MAX_PER_DAY = 50
TIMELINE_FIRST_LINE_CHARS = 80


def _selected_path_events(
    conn: sqlite3.Connection, conversation_id: str
) -> list[sqlite3.Row]:
    """对话最新快照的选中路径事件序列（与 _authorized_context 同一查询）。"""
    return conn.execute(
        """
        SELECT sp.node_key, sp.depth, cn.event_id
        FROM conversation_selected_path sp
        JOIN conversation_nodes cn ON cn.node_key = sp.node_key
        WHERE sp.conv_snapshot_id = (
            SELECT cs2.conv_snapshot_id FROM conversation_snapshots cs2
            WHERE cs2.conversation_id = ?
            ORDER BY cs2.rowid DESC LIMIT 1
        ) ORDER BY sp.depth
        """,
        (conversation_id,),
    ).fetchall()


def _conversation_titles(
    conn: sqlite3.Connection, conversation_ids: list[str]
) -> dict[str, str]:
    """对话 → 最新快照标题（批量；无快照则空串）。"""
    out: dict[str, str] = {}
    ids = [c for c in conversation_ids if c]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        qmarks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT conversation_id, title FROM conversation_snapshots
            WHERE rowid IN (
                SELECT MAX(rowid) FROM conversation_snapshots
                WHERE conversation_id IN ({qmarks}) GROUP BY conversation_id
            )
            """,
            chunk,
        ).fetchall()
        for row in rows:
            out[row["conversation_id"]] = row["title"] or ""
    return out


def _representative_revision(
    conn: sqlite3.Connection, filters: SearchFilters, event_id: str
) -> sqlite3.Row | None:
    """事件的调用者可见代表版本：current 可见优先，否则最早可见版本。

    与 get_event 的选取规则一致（current 在授权集合中用之，否则取集合中
    最后一个——此处按 recorded_at 序即 auth_revs[-1]）。
    """
    auth_revs = _authorized_revision_ids(conn, filters, event_id)
    if not auth_revs:
        return None
    current = conn.execute(
        "SELECT current_revision_id FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    revision_id = (
        current["current_revision_id"]
        if current and current["current_revision_id"] in auth_revs
        else auth_revs[-1]
    )
    return conn.execute(
        """
        SELECT r.revision_id, r.raw_text, r.source_created_at,
               e.speaker_type, e.conversation_id
        FROM event_revisions r JOIN events e ON e.event_id = r.event_id
        WHERE r.revision_id = ?
        """,
        (revision_id,),
    ).fetchone()


def _parse_recall_args(
    profile: AccessProfile, args: dict, limits: SearchLimits, timezone: str
) -> tuple[str, str, SearchFilters, dict]:
    """recall 的参数校验与过滤构造（source/speaker 字段名与 REST 保持一致）。"""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_PARAMS", "query 必须是非空字符串")
    mode = args["mode"] if "mode" in args else profile.semantic_default
    if mode not in SEARCH_MODES:
        raise ToolError("INVALID_PARAMS", f"未知 mode: {mode}（可选 exact|hybrid）")

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        value = args.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ToolError("INVALID_PARAMS", f"{name} 必须是 {lo}..{hi} 的整数")
        return value

    max_snippets = _int("max_snippets", RECALL_DEFAULT_SNIPPETS, 1, RECALL_MAX_SNIPPETS)
    context_radius = _int("context_radius", RECALL_DEFAULT_RADIUS, 0, RECALL_MAX_RADIUS)
    char_budget = _int(
        "char_budget", RECALL_DEFAULT_CHAR_BUDGET, 200,
        min(limits.max_text_chars, RECALL_MAX_CHAR_BUDGET),
    )
    base = profile_filters(profile)
    match_mode = args.get("match_mode", "literal")
    if match_mode not in ("literal", "all_terms"):
        raise ToolError("INVALID_PARAMS", f"未知 match_mode: {match_mode}")
    filters = SearchFilters(
        date_from=parse_date_bound(args["date_from"], timezone, end_of_day=False)
        if args.get("date_from") else None,
        date_to=parse_date_bound(args["date_to"], timezone, end_of_day=True)
        if args.get("date_to") else None,
        source_id=args.get("source"),
        speaker_type=args.get("speaker"),
        allow_scopes=base.allow_scopes,
        deny_sensitivity=base.deny_sensitivity,
        allow_unclassified=base.allow_unclassified,
    )
    return query, mode, filters, {
        "match_mode": match_mode, "max_snippets": max_snippets,
        "context_radius": context_radius, "char_budget": char_budget,
    }


def _merge_adjacent_hits(
    hits: list[SearchHit], positions: list[int], radius: int
) -> list[list[SearchHit]]:
    """D-7 片段合并：同一对话内路径位置相距不超过 2*radius 条的命中
    （即两窗口相接或重叠）并入一个片段；跨对话不合并（调用方分组保证）。
    输入按位置升序；纯函数，供单元测试。"""
    groups: list[list[SearchHit]] = []
    last: int | None = None
    for pos, hit in zip(positions, hits, strict=True):
        if last is not None and pos - last <= 2 * radius + 1:
            groups[-1].append(hit)
        else:
            groups.append([hit])
        last = pos
    return groups


def _build_fragment_messages(
    conn: sqlite3.Connection, filters: SearchFilters, frag: dict, redact_credentials: bool
) -> tuple[list[dict], bool] | None:
    """把一个片段物化成消息列表（授权过滤后、未做预算装填）。

    返回 None 表示片段内没有任何可见消息（命中被撤回等）——整段不返回。
    邻居消息逐事件授权（与 _authorized_context 相同规则）：未授权的
    邻居静默跳过，既不返回也不暴露存在。
    """
    messages: list[dict] = []
    redacted_any = False
    hit_ids = {h.event_id for h in frag["hits"]}

    def _message_for(event_id: str, *, is_hit: bool) -> dict | None:
        nonlocal redacted_any
        row = _representative_revision(conn, filters, event_id)
        if row is None:
            return None
        raw_text = row["raw_text"] or ""
        safe_text = (
            redact_known_credentials(raw_text) if redact_credentials else raw_text
        )
        assert safe_text is not None
        redacted_any |= safe_text != raw_text
        return {
            "event_id": event_id,
            "revision_id": row["revision_id"],
            "speaker_type": row["speaker_type"],
            "timestamp": row["source_created_at"],
            "text": safe_text,
            "is_hit": is_hit,
            "truncated": False,
        }

    if frag["in_path"]:
        lo, hi = frag["window"]
        for i in range(lo, hi):
            event_id = frag["path"][i]["event_id"]
            if not event_id:
                continue
            msg = _message_for(event_id, is_hit=event_id in hit_ids)
            if msg is not None:
                messages.append(msg)
    else:
        msg = _message_for(frag["hits"][0].event_id, is_hit=True)
        if msg is not None:
            messages.append(msg)

    if not any(m["is_hit"] for m in messages):
        return None  # 命中本身不可见：空片段不返回
    return messages, redacted_any


def _fit_fragment_messages(
    messages: list[dict], remaining: int
) -> tuple[list[dict], int]:
    """按预算裁剪一个已物化的片段：命中消息必装（超长截断并给 text_offset），
    上下文消息按序装入、放不下即止。返回（裁剪后消息, 实际使用字数）。"""
    out: list[dict] = []
    used = 0
    for msg in messages:
        if msg["is_hit"]:
            allowance = remaining - used
            if allowance < 1:
                break  # 连命中都放不下（调用方已用 hit_need 预检，防御性兜底）
            shown = msg["text"][:allowance]
            truncated = len(shown) < len(msg["text"])
            entry = {
                **{k: msg[k] for k in (
                    "event_id", "revision_id", "speaker_type", "timestamp", "is_hit")},
                "text": shown,
                "truncated": truncated,
            }
            if truncated:
                entry["text_offset"] = len(shown)  # get_event(text_offset=…) 续读
            out.append(entry)
            used += len(shown)
        else:
            if used + len(msg["text"]) > remaining:
                break  # 预算尽：后续消息（含其后的上下文）不再装入
            out.append({**msg, "text": msg["text"], "truncated": False})
            used += len(msg["text"])
    return out, used


def tool_recall(
    conn: sqlite3.Connection,
    profile: AccessProfile,
    args: dict,
    limits: SearchLimits,
    timezone: str = "UTC",
    embedding_provider: EmbeddingProvider | None = None,
    semantic_config: SemanticConfig | None = None,
) -> dict:
    """一次调用返回带上下文的回忆片段（D-7）。

    候选来自 search_history（同一套授权/验证/遮蔽，无旁路查询）；同一对话
    中路径位置相距不超过 2*context_radius 条的命中合并成一个片段；片段
    上下文沿用 _authorized_context 的选中路径与逐事件授权规则——未授权的
    邻居既不返回也不暴露存在。按检索排序依次装入预算，宁可少几个片段。
    """
    query, mode, filters, opts = _parse_recall_args(profile, args, limits, timezone)
    max_snippets = opts["max_snippets"]
    radius = opts["context_radius"]
    char_budget = opts["char_budget"]
    redact_credentials = _remote_delivery(profile)

    try:
        result = search_history(
            conn,
            query,
            match_mode=opts["match_mode"],
            filters=filters,
            order="relevance",
            limit=min(max_snippets * 3, limits.max_limit),
            cursor=None,
            limits=limits,
            redact_credentials=redact_credentials,
            mode=mode,
            embedding_provider=embedding_provider,
            semantic_config=semantic_config,
        )
    except QueryTooBroad as exc:
        raise ToolError("QUERY_TOO_BROAD", str(exc)) from exc
    except ValueError as exc:
        raise ToolError("INVALID_PARAMS", str(exc)) from exc

    hits = [h for h in result.hits if h.source_created_at is not None]

    # --- 合并：同一对话内路径位置相近的命中并入一个片段 ---
    by_conv: dict[str, list[SearchHit]] = {}
    for hit in hits:
        by_conv.setdefault(hit.conversation_id, []).append(hit)
    titles = _conversation_titles(conn, list(by_conv))
    sources = _fragment_sources(conn, list(by_conv))

    fragments: list[dict] = []  # 保持检索顺序
    for conversation_id, conv_hits in by_conv.items():
        path = _selected_path_events(conn, conversation_id)
        pos_map = {row["event_id"]: i for i, row in enumerate(path) if row["event_id"]}
        positioned = sorted(
            ((pos_map[h.event_id], h) for h in conv_hits if h.event_id in pos_map),
            key=lambda t: t[0],
        )
        orphan_hits = [h for h in conv_hits if h.event_id not in pos_map]
        groups = _merge_adjacent_hits(
            [h for _, h in positioned], [p for p, _ in positioned], radius,
        )
        for group in groups:
            fragments.append({
                "conversation_id": conversation_id,
                "path": path,
                "window": (
                    max(0, pos_map[group[0].event_id] - radius),
                    min(len(path), pos_map[group[-1].event_id] + radius + 1),
                ),
                "hits": list(group),
                "in_path": True,
            })
        for hit in orphan_hits:  # 不在选中路径上的命中：单独成片段，无邻接
            fragments.append({
                "conversation_id": conversation_id,
                "path": None,
                "window": None,
                "hits": [hit],
                "in_path": False,
            })

    # --- 装填：按检索排序，预算内尽量装；装不下整段计入 dropped ---
    snippets: list[dict] = []
    chars_used = 0
    dropped = 0
    credentials_redacted = False
    for frag in fragments:
        if len(snippets) >= max_snippets:
            dropped += 1
            continue
        built = _build_fragment_messages(conn, filters, frag, redact_credentials)
        if built is None:
            dropped += 1  # 命中不可见（撤回等）：不返回空片段
            continue
        messages, redacted_any = built
        credentials_redacted |= redacted_any
        remaining = char_budget - chars_used
        hit_need = sum(len(m["text"]) for m in messages if m["is_hit"])
        if hit_need > remaining:  # 连命中本身都装不下：整段放弃，不做部分装填
            dropped += 1
            continue
        fitted, frag_chars = _fit_fragment_messages(messages, remaining)
        if not any(m["is_hit"] for m in fitted):
            dropped += 1
            continue
        kinds = {h.match_kind for h in frag["hits"]}
        match_kind = "both" if ("both" in kinds or {"exact", "semantic"} <= kinds) else kinds.pop()
        times = [m["timestamp"] for m in fitted if m["timestamp"]]
        snippets.append({
            "conversation_id": frag["conversation_id"],
            "conversation_title": titles.get(frag["conversation_id"], ""),
            "source": sources.get(frag["conversation_id"], ""),
            "time_range": [min(times), max(times)] if times else [None, None],
            "match_kind": match_kind,
            "messages": fitted,
        })
        chars_used += frag_chars

    warnings = list(result.notes)
    if credentials_redacted:
        warnings.append(CREDENTIAL_REDACTION_NOTE)
    payload = _envelope(
        snippets,
        truncated=dropped > 0,
        next_cursor=None,
        coverage=_coverage(conn, filters, branch_scope="all"),
        warnings=warnings,
    )
    payload["budget"] = {
        "chars_used": chars_used,
        "char_budget": char_budget,
        "snippets_dropped": dropped,
    }
    return payload


def _fragment_sources(conn: sqlite3.Connection, conversation_ids: list[str]) -> dict[str, str]:
    """对话 → 来源 ID（来源归属对话本身，非消息内容）。"""
    out: dict[str, str] = {}
    ids = [c for c in conversation_ids if c]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        qmarks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT cn.conversation_id, s.source_id
            FROM conversation_nodes cn
            JOIN events e ON e.event_id = cn.event_id
            JOIN sources s ON s.source_id = e.source_id
            WHERE cn.conversation_id IN ({qmarks})
            GROUP BY cn.conversation_id
            """,
            chunk,
        ).fetchall()
        for row in rows:
            out[row["conversation_id"]] = row["source_id"]
    return out


def tool_timeline(
    conn: sqlite3.Connection,
    profile: AccessProfile,
    args: dict,
    limits: SearchLimits,
    timezone: str = "UTC",
) -> dict:
    """按天列出当天活跃对话的结构化统计（D-7）。

    只统计 profile 可见的事件：某对话在调用者 profile 下一 条可见消息都
    没有时整条不出现，不可见对话的数量与存在性都不泄露（§7.3）。计数
    口径与 search_history 一致：按授权 revision 的 source_created_at 落窗。
    """
    from datetime import UTC, datetime
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    raw_from = args.get("date_from")
    raw_to = args.get("date_to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise ToolError("INVALID_PARAMS", "date_from 与 date_to 必填（YYYY-MM-DD）")
    try:
        d_from = _date.fromisoformat(raw_from)
        d_to = _date.fromisoformat(raw_to)
    except ValueError as exc:
        raise ToolError("INVALID_PARAMS", f"日期格式错误: {exc}") from exc
    if d_from > d_to:
        raise ToolError("INVALID_PARAMS", "date_from 不能晚于 date_to")
    if (d_to - d_from).days >= TIMELINE_MAX_DAYS:
        raise ToolError(
            "INVALID_PARAMS", f"时间区间不能超过 {TIMELINE_MAX_DAYS} 天"
        )
    limit_per_day = args.get("limit_per_day", TIMELINE_DEFAULT_PER_DAY)
    if isinstance(limit_per_day, bool) or not isinstance(limit_per_day, int) \
            or not 1 <= limit_per_day <= TIMELINE_MAX_PER_DAY:
        raise ToolError(
            "INVALID_PARAMS", f"limit_per_day 必须是 1..{TIMELINE_MAX_PER_DAY} 的整数"
        )
    with_first_line = args.get("with_first_line", True)
    if not isinstance(with_first_line, bool):
        raise ToolError("INVALID_PARAMS", "with_first_line 必须是布尔值")
    base = profile_filters(profile)
    filters = SearchFilters(
        source_id=args.get("source"),
        allow_scopes=base.allow_scopes,
        deny_sensitivity=base.deny_sensitivity,
        allow_unclassified=base.allow_unclassified,
    )
    redact_credentials = _remote_delivery(profile)

    lo = parse_date_bound(raw_from, timezone, end_of_day=False)
    hi = parse_date_bound(raw_to, timezone, end_of_day=True)
    params: list = [lo, hi]
    policy_sql = _policy_exists_sql("ps", filters, params)
    if filters.source_id:
        params.append(filters.source_id)
    source_sql = " AND s.source_id = ?" if filters.source_id else ""
    rows = conn.execute(
        f"""
        SELECT r.revision_id, r.event_id, e.conversation_id, e.speaker_type,
               e.current_revision_id, r.source_created_at
        FROM event_revisions r
        JOIN events e ON e.event_id = r.event_id
        JOIN sources s ON s.source_id = e.source_id
        WHERE s.status != 'withdrawn'
          AND r.source_created_at >= ? AND r.source_created_at < ?
          AND {policy_sql}
          {source_sql}
        ORDER BY r.source_created_at, r.revision_id
        """,
        params,
    ).fetchall()

    tz = ZoneInfo(timezone)

    def _day_of(ts: str) -> str | None:
        try:
            dt = datetime.strptime(ts[:26], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None  # 时间未知的事件不归入任何一天（宁可少计不误计）
        return dt.astimezone(tz).date().isoformat()

    # 事件去重：窗口内同一事件的多个可见 revision 取一条（current 优先，
    # 否则时间最早的；ORDER BY 已保证时间序）
    seen: dict[str, dict] = {}
    for row in rows:
        day = _day_of(row["source_created_at"] or "")
        if day is None:
            continue
        prev = seen.get(row["event_id"])
        is_current = row["revision_id"] == row["current_revision_id"]
        if prev is None or (is_current and not prev["is_current"]):
            seen[row["event_id"]] = {
                "day": day,
                "conversation_id": row["conversation_id"],
                "speaker_type": row["speaker_type"],
                "timestamp": row["source_created_at"],
                "revision_id": row["revision_id"],
                "is_current": is_current,
            }
    events = list(seen.values())

    # 按 (day, conversation) 聚合
    days_map: dict[str, dict[str, dict]] = {}
    for ev in events:
        bucket = days_map.setdefault(ev["day"], {}).setdefault(
            ev["conversation_id"],
            {"messages": 0, "owner_messages": 0, "first_at": None, "last_at": None,
             "first_owner_revision": None},
        )
        bucket["messages"] += 1
        if ev["speaker_type"] == "owner":
            bucket["owner_messages"] += 1
        if bucket["first_at"] is None or ev["timestamp"] < bucket["first_at"]:
            bucket["first_at"] = ev["timestamp"]
        if bucket["last_at"] is None or ev["timestamp"] > bucket["last_at"]:
            bucket["last_at"] = ev["timestamp"]
        if ev["speaker_type"] == "owner" and bucket["first_owner_revision"] is None:
            bucket["first_owner_revision"] = ev["revision_id"]

    all_convs = [c for day in days_map.values() for c in day]
    titles = _conversation_titles(conn, all_convs)
    sources_of_conv = _fragment_sources(conn, all_convs)

    # first_owner_line：批量取正文（substr 预留遮蔽余量），遮蔽后截 80 字
    first_lines: dict[str, str] = {}
    if with_first_line:
        need = [
            (conv, bucket["first_owner_revision"])
            for day in days_map.values()
            for conv, bucket in day.items()
            if bucket["first_owner_revision"]
        ]
        for i in range(0, len(need), 400):
            chunk = need[i:i + 400]
            qmarks = ",".join("?" for _ in chunk)
            rows2 = conn.execute(
                f"SELECT revision_id, substr(raw_text, 1, 400) AS head "
                f"FROM event_revisions WHERE revision_id IN ({qmarks})",
                [rid for _, rid in chunk],
            ).fetchall()
            by_rid = {r["revision_id"]: r["head"] or "" for r in rows2}
            for conv, rid in chunk:
                text = by_rid.get(rid, "")
                safe = redact_known_credentials(text) if redact_credentials else text
                assert safe is not None
                first_lines[conv] = safe[:TIMELINE_FIRST_LINE_CHARS]

    days_out = []
    truncated = False
    for day in sorted(days_map):
        convs = sorted(
            days_map[day].items(),
            key=lambda kv: kv[1]["last_at"] or "",
            reverse=True,  # 最近活跃优先
        )
        if len(convs) > limit_per_day:
            convs = convs[:limit_per_day]
            truncated = True
        days_out.append({
            "date": day,
            "conversations": [{
                "conversation_id": conv,
                "title": titles.get(conv, ""),
                "source": sources_of_conv.get(conv, ""),
                "messages": bucket["messages"],
                "owner_messages": bucket["owner_messages"],
                "first_at": bucket["first_at"],
                "last_at": bucket["last_at"],
                **({"first_owner_line": first_lines[conv]}
                   if with_first_line and conv in first_lines else {}),
            } for conv, bucket in convs],
        })

    payload = _envelope(
        [],
        truncated=truncated,
        next_cursor=None,
        coverage=_coverage(conn, filters, branch_scope="all"),
        warnings=[],
    )
    payload["days"] = days_out
    return payload




# ---------------------------------------------------------------------------
# epoch 重验（§7.3：返回前核对，执行中变化 → 重新授权或放弃）
# ---------------------------------------------------------------------------


def run_with_epoch_retry(fn, *args, **kwargs):
    """执行工具并在返回前核对 policy epoch；不稳定则整体重跑。"""
    conn = args[0]
    last: dict | None = None
    for _ in range(EPOCH_RETRIES):
        before = current_policy_epoch(conn)
        last = fn(*args, **kwargs)
        if current_policy_epoch(conn) == before:
            return last
    raise PolicyEpochChurn(
        "策略状态在请求执行中持续变化，已放弃本次结果（TRANSIENT_POLICY_CHANGE）"
    )
