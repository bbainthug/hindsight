"""提炼流水线（设计 §10.3/§10.4 的子集，任务书范围 2）。

流程：
  窗口内 owner 发言 → 分段（有限上下文、出站遮蔽）→ 估算请求/token
  → 预算检查（超出拒绝执行）→ 幂等键检查（重跑不重复生成）
  → schema 约束提炼（LLM）→ 原文核对 / 证据性质检查 → 去重合并
  → 关系建议（只是建议，进审核队列）→ 单事务入库。

- 幂等键 = 输入 revision 集合 + 分段版本 + prompt/schema 版本 + provider/model
  配置（§10.4）；键已存在且 completed 的批次直接跳过，不重复调用模型。
- 提炼与持久化分离：LLM 调用不持有写事务；持久化在单事务内完成，
  失败回滚并把批次标记 failed（断电不留半批）。
- dry-run 只做分段与估算，不调用模型、不写库。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from personal_brain.facts.llm import FactLLMClient
from personal_brain.facts.prompting import (
    EXTRACTION_SCHEMA_VERSION,
    PROMPT_OVERHEAD_TOKENS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_relation_prompt,
    build_segment_prompt,
    parse_extraction_response,
    parse_relation_response,
)
from personal_brain.facts.segmentation import (
    SEGMENTATION_VERSION,
    Segment,
    build_segments,
    segment_fingerprint_ids,
    segment_input_revision_ids,
)
from personal_brain.facts.similarity import ClaimSimilarity
from personal_brain.facts.validation import (
    MAX_CONTENT_CHARS,
    REJECT_BAD_SHAPE,
    SegmentIndex,
    ValidatedCandidate,
    existing_claim_contents,
    validate_candidate,
)
from personal_brain.facts.versioning import RULE_VERSION, SPAN_CHECK_METHOD, SPAN_CHECK_VERSION
from personal_brain.history.timeutil import utc_now_iso

# 关系建议单批上限：避免一次提炼产生无界额外请求
MAX_RELATION_PAIRS = 20


class BudgetExceeded(Exception):
    """估算 token 超出预算（§10.4：不以无限请求消耗预算）。"""


class ExtractionInProgress(Exception):
    """相同幂等键已有运行中的提炼批次。"""


# 请求体核心字段：由客户端自己设置，extra_body 不得覆盖
RESERVED_REQUEST_FIELDS = frozenset(
    {"model", "messages", "temperature", "max_tokens", "response_format", "stream"}
)


@dataclass
class FactsConfig:
    """``facts:`` 配置段（密钥只从环境变量读，不在此出现）。"""

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.2
    timeout_seconds: float = 120.0
    max_segment_chars: int = 6000
    max_output_tokens: int = 2000
    budget_tokens: int = 500_000
    dedup_threshold: float = 0.90
    conflict_threshold: float = 0.45
    max_gap_seconds: int = 3600
    context_messages: int = 4
    # 跳过超过该长度的 owner 发言（粘贴的长日志/JD 等）：None 表示不过滤。
    # v0 用于预算收窄（D-12）：提炼"关于我的事实"时超长粘贴文本边际价值低，
    # 过滤是可配置、可逆的（默认关闭）。
    max_owner_chars: int | None = None
    # 分段是否携带 assistant 消息作上下文。默认携带（质量更好）；
    # 预算收窄时可关闭（真实库实测 assistant 上下文占输入字符的绝大部分）。
    include_assistant_context: bool = True
    # 合并进请求体的附加字段（如 DeepSeek 关闭默认思考模式：
    # ``{"thinking": {"type": "disabled"}}``，否则思考内容会吃掉 max_tokens）。
    # 不能覆盖 model/messages 等核心字段。
    extra_body: dict[str, object] | None = None

    def validate(self) -> None:
        if not self.base_url.strip() or not self.model.strip() or not self.api_key_env.strip():
            raise ValueError("facts.base_url/model/api_key_env 不能为空")
        if not (0.0 <= self.temperature <= 2.0):
            raise ValueError("facts.temperature 必须在 [0, 2]")
        if self.timeout_seconds <= 0:
            raise ValueError("facts.timeout_seconds 必须 > 0")
        if self.max_segment_chars < 200:
            raise ValueError("facts.max_segment_chars 必须 ≥ 200")
        if self.max_output_tokens < 200:
            raise ValueError("facts.max_output_tokens 必须 ≥ 200")
        if self.budget_tokens <= 0:
            raise ValueError("facts.budget_tokens 必须 > 0")
        if self.max_gap_seconds < 0 or self.context_messages < 0:
            raise ValueError("facts.max_gap_seconds/context_messages 不能为负数")
        if self.max_owner_chars is not None and self.max_owner_chars < 1:
            raise ValueError("facts.max_owner_chars 必须是正整数或 null")
        if self.extra_body is not None:
            if not isinstance(self.extra_body, dict):
                raise ValueError("facts.extra_body 必须是映射或 null")
            reserved = set(self.extra_body) & RESERVED_REQUEST_FIELDS
            if reserved:
                raise ValueError(f"facts.extra_body 不能覆盖核心字段: {sorted(reserved)}")
        if not (0.0 < self.dedup_threshold <= 1.0):
            raise ValueError("facts.dedup_threshold 必须在 (0, 1]")
        if not (0.0 <= self.conflict_threshold < self.dedup_threshold):
            raise ValueError("facts.conflict_threshold 必须 < dedup_threshold")


@dataclass
class Estimate:
    """开跑前估算（任务书用户故事 1：先告诉调用次数与 token）。"""

    owner_revisions: int
    segments: int
    requests: int  # 分段请求 + 至多 1 次关系建议批请求
    input_tokens: int
    output_tokens: int
    total_tokens: int
    budget_tokens: int
    within_budget: bool
    path_unknown_conversations: int
    segmentation_backend: str = SEGMENTATION_VERSION


@dataclass
class Rejection:
    reason: str
    content: str  # 拒绝候选的截断内容（仅统计与排障；不进报告正文）


@dataclass
class RunReport:
    run_id: str
    extraction_key: str
    skipped_idempotent: bool = False
    segments: int = 0
    owner_revisions: int = 0
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    candidates_created: int = 0
    candidates_merged: int = 0
    relations_suggested: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    path_unknown_conversations: int = 0
    wall_seconds: float = 0.0
    notes: str = ""


def load_facts_config(data: dict | None) -> FactsConfig:
    """从 YAML ``facts:`` 段构建配置（缺省用默认值）。"""
    cfg = FactsConfig()
    if not data:
        return cfg
    if not isinstance(data, dict):
        raise ValueError("facts 配置段格式错误")
    mapping = {
        "base_url": "base_url",
        "model": "model",
        "api_key_env": "api_key_env",
        "temperature": "temperature",
        "timeout_seconds": "timeout_seconds",
        "max_segment_chars": "max_segment_chars",
        "max_output_tokens": "max_output_tokens",
        "budget_tokens": "budget_tokens",
        "dedup_threshold": "dedup_threshold",
        "conflict_threshold": "conflict_threshold",
        "max_gap_seconds": "max_gap_seconds",
        "context_messages": "context_messages",
        "max_owner_chars": "max_owner_chars",
        "include_assistant_context": "include_assistant_context",
        "extra_body": "extra_body",
    }
    for key, attr in mapping.items():
        if key in data and data[key] is not None:
            setattr(cfg, attr, data[key])
    cfg.validate()
    return cfg


def compute_extraction_key(
    *,
    revision_ids: list[str],
    provider_name: str,
    model: str,
    model_config: dict[str, object] | None = None,
) -> str:
    """幂等键（§10.4）：输入集合、规则/提示版本与模型配置。"""
    payload = json.dumps(
        {
            "revisions": sorted(revision_ids),
            "segmentation": SEGMENTATION_VERSION,
            "prompt": PROMPT_VERSION,
            "schema": EXTRACTION_SCHEMA_VERSION,
            "rule": RULE_VERSION,
            "provider": provider_name,
            "model": model,
            "model_config": model_config or {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_since(now: float, days: int = 30) -> str:
    """默认窗口：最近 30 天（任务书范围 2）。"""
    from datetime import UTC, datetime, timedelta

    return (datetime.fromtimestamp(now, UTC) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"


def estimate_extraction(
    conn: sqlite3.Connection,
    cfg: FactsConfig,
    *,
    since: str | None,
    until: str | None,
    account_namespace: str | None,
) -> tuple[list[Segment], Estimate]:
    """分段 + 估算（dry-run 与正式开跑共用；不写库、不调用模型）。"""
    segments, path_unknown = build_segments(
        conn,
        since=since,
        until=until,
        account_namespace=account_namespace,
        max_segment_chars=cfg.max_segment_chars,
        max_gap_seconds=cfg.max_gap_seconds,
        context_messages=cfg.context_messages,
        max_owner_chars=cfg.max_owner_chars,
        include_assistant_context=cfg.include_assistant_context,
    )
    revision_ids = segment_fingerprint_ids(segments)
    requests = len(segments) + (1 if segments else 0)  # 关系建议至多一批
    input_chars = sum(len(build_segment_prompt(seg)) for seg in segments)
    # 无 tokenizer 依赖的粗估：字符数 × 0.8 + 每请求模板开销。
    # 关系候选在调用提炼模型前未知，预算门为最多 20 对保留输入空间。
    input_tokens = int(input_chars * 0.8) + len(segments) * PROMPT_OVERHEAD_TOKENS
    if segments:
        facts_tables = conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type='table' AND name IN ('claims', 'claim_revisions')"""
        ).fetchone()[0]
        existing_max = 0
        if facts_tables == 2:
            existing_max = conn.execute(
                """SELECT COALESCE(MAX(length(cr.content)), 0)
                   FROM claims c JOIN claim_revisions cr
                     ON cr.claim_revision_id = c.current_revision_id
                   WHERE c.lifecycle_status IN ('candidate', 'active')
                     AND cr.review_status IN ('pending', 'approved')"""
            ).fetchone()[0]
        relation_chars = MAX_RELATION_PAIRS * (
            MAX_CONTENT_CHARS + int(existing_max) + 64
        )
        input_tokens += int(relation_chars * 0.8) + PROMPT_OVERHEAD_TOKENS
    output_tokens = requests * cfg.max_output_tokens
    total = input_tokens + output_tokens
    est = Estimate(
        owner_revisions=len(revision_ids),
        segments=len(segments),
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        budget_tokens=cfg.budget_tokens,
        within_budget=total <= cfg.budget_tokens,
        path_unknown_conversations=path_unknown,
    )
    return segments, est


def run_extraction(
    conn: sqlite3.Connection,
    cfg: FactsConfig,
    client: FactLLMClient,
    *,
    since: str | None,
    until: str | None,
    account_namespace: str | None,
    budget_tokens: int | None = None,
    similarity: ClaimSimilarity | None = None,
    now: str | None = None,
) -> RunReport:
    """正式提炼（真实调用模型）。预算不足抛 BudgetExceeded。"""
    now = now or utc_now_iso()
    budget = budget_tokens if budget_tokens is not None else cfg.budget_tokens
    started = time.perf_counter()

    segments, est = estimate_extraction(
        conn, cfg, since=since, until=until, account_namespace=account_namespace
    )
    owner_revision_ids = segment_fingerprint_ids(segments)
    revision_ids = segment_input_revision_ids(segments)
    key = compute_extraction_key(
        revision_ids=revision_ids,
        provider_name=client.provider_name,
        model=client.model_id,
        model_config={
            "base_url": getattr(client, "base_url", None),
            "temperature": cfg.temperature,
            "max_output_tokens": cfg.max_output_tokens,
            "max_segment_chars": cfg.max_segment_chars,
            "max_gap_seconds": cfg.max_gap_seconds,
            "context_messages": cfg.context_messages,
            "dedup_threshold": cfg.dedup_threshold,
            "conflict_threshold": cfg.conflict_threshold,
            "extra_body": getattr(client, "extra_body", None),
            "similarity_backend": (
                similarity.backend if similarity is not None else "bigram-jaccard"
            ),
        },
    )

    report = RunReport(
        run_id="", extraction_key=key,
        segments=len(segments), owner_revisions=len(owner_revision_ids),
        path_unknown_conversations=est.path_unknown_conversations,
    )
    if not segments:
        report.notes = "窗口内没有可提炼的 owner 发言"
        return report

    # 幂等（§10.4）：同键已完成批次直接跳过，不重复生成
    existing = conn.execute(
        "SELECT run_id, status FROM extraction_runs WHERE extraction_key = ?",
        (key,),
    ).fetchone()
    if existing is not None and existing["status"] == "completed":
        report.run_id = existing["run_id"]
        report.skipped_idempotent = True
        return report

    if est.total_tokens > budget:
        raise BudgetExceeded(
            f"估算 {est.total_tokens} tokens（{est.requests} 次请求）超出预算 "
            f"{budget}；请提高 --budget 或缩小 --since/--until/--source 范围"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT run_id, status FROM extraction_runs WHERE extraction_key = ?",
            (key,),
        ).fetchone()
        if existing is not None and existing["status"] == "completed":
            conn.rollback()
            report.run_id = existing["run_id"]
            report.skipped_idempotent = True
            return report
        if existing is not None and existing["status"] == "running":
            raise ExtractionInProgress("相同幂等键的批次仍在运行")
        if existing is not None:
            run_id = existing["run_id"]
            conn.execute(
                """
                UPDATE extraction_runs SET started_at=?, finished_at=NULL,
                    status='running', since=?, until=?, source=?, provider=?, model=?,
                    prompt_version=?, schema_version=?, segmentation_version=?,
                    rule_version=?, input_revision_count=?, segment_count=?,
                    request_count=0, prompt_tokens=0, completion_tokens=0,
                    candidates_generated=0, candidates_rejected=0, notes=NULL
                WHERE run_id=?
                """,
                (
                    now, since, until, account_namespace, client.provider_name,
                    client.model_id, PROMPT_VERSION, EXTRACTION_SCHEMA_VERSION,
                    SEGMENTATION_VERSION, RULE_VERSION, len(revision_ids),
                    len(segments), run_id,
                ),
            )
        else:
            run_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO extraction_runs (
                    run_id, extraction_key, started_at, status, since, until, source,
                    provider, model, prompt_version, schema_version,
                    segmentation_version, rule_version,
                    input_revision_count, segment_count
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, key, now, since, until, account_namespace,
                    client.provider_name, client.model_id, PROMPT_VERSION,
                    EXTRACTION_SCHEMA_VERSION, SEGMENTATION_VERSION, RULE_VERSION,
                    len(revision_ids), len(segments),
                ),
            )
        report.run_id = run_id
    except BaseException:
        conn.rollback()
        raise
    conn.commit()

    # LLM 阶段：不持有写事务；任何失败 → 批次标记 failed，不产生半批数据
    index_by_segment = {id(seg): SegmentIndex([seg]) for seg in segments}
    accepted: list[ValidatedCandidate] = []
    try:
        for seg in segments:
            report.requests += 1
            data = client.complete_json(SYSTEM_PROMPT, build_segment_prompt(seg))
            for raw in parse_extraction_response(data):
                candidate, reason = validate_candidate(
                    raw, index_by_segment[id(seg)]
                )
                if candidate is None:
                    reason = reason or REJECT_BAD_SHAPE
                    report.rejections[reason] = report.rejections.get(reason, 0) + 1
                    continue
                accepted.append(candidate)
        relations = _suggest_relations(
            conn, client, cfg, accepted, report, similarity=similarity
        )
        # 持久化阶段：单事务（§5.2 可见性边界）
        conn.execute("BEGIN IMMEDIATE")
        try:
            for candidate in accepted:
                _persist_candidate(
                    conn,
                    candidate,
                    run_id,
                    report,
                    similarity=similarity,
                    dedup_threshold=cfg.dedup_threshold,
                    now=now,
                )
            for rel in relations:
                _persist_relation(conn, rel, run_id, now)
            conn.execute(
                """
                UPDATE extraction_runs SET
                    finished_at=?, status='completed',
                    request_count=?, prompt_tokens=?, completion_tokens=?,
                    candidates_generated=?, candidates_rejected=?, notes=?
                WHERE run_id=?
                """,
                (
                    utc_now_iso(), report.requests, client.usage[0], client.usage[1],
                    report.candidates_created + report.candidates_merged,
                    sum(report.rejections.values()),
                    json.dumps(report.rejections, ensure_ascii=False),
                    run_id,
                ),
            )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    except BaseException as exc:
        _mark_run_failed(conn, run_id, f"失败: {type(exc).__name__}: {exc}")
        raise

    report.prompt_tokens, report.completion_tokens = client.usage
    report.relations_suggested = len(relations)
    report.wall_seconds = round(time.perf_counter() - started, 2)
    return report


def _mark_run_failed(conn: sqlite3.Connection, run_id: str, note: str) -> None:
    """批次失败落库（不抛：保留原始异常）。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE extraction_runs SET status='failed', finished_at=?, notes=? "
                "WHERE run_id=?",
                (utc_now_iso(), note[:500], run_id),
            )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    except sqlite3.Error:
        pass


def _persist_candidate(
    conn: sqlite3.Connection,
    candidate: ValidatedCandidate,
    run_id: str,
    report: RunReport,
    *,
    similarity: ClaimSimilarity | None,
    dedup_threshold: float,
    now: str,
) -> None:
    """去重（§10.3 duplicate check）→ 新主张或既有主张合并新证据。"""
    sim = similarity or ClaimSimilarity()
    existing = existing_claim_contents(conn)
    merge_target: tuple[str, str, str] | None = None
    for claim_id, content, memory_type in existing:
        if memory_type != candidate.memory_type:
            continue
        if sim.similarity(content, candidate.content) >= dedup_threshold:
            merge_target = (claim_id, content, memory_type)
            break

    if merge_target is not None:
        claim_id = merge_target[0]
        current = conn.execute(
            """SELECT cr.claim_revision_id, cr.review_status
               FROM claims c JOIN claim_revisions cr
                 ON cr.claim_revision_id = c.current_revision_id
               WHERE c.claim_id = ?""",
            (claim_id,),
        ).fetchone()
        assert current is not None
        _insert_evidence(
            conn, current["claim_revision_id"], candidate, check_note=None
        )
        if current["review_status"] == "approved":
            # 已生效主张获得新证据：保守降级为需复核（不悄悄扩权）
            conn.execute(
                "UPDATE claim_revisions SET review_status='needs_review' "
                "WHERE claim_revision_id = ?",
                (current["claim_revision_id"],),
            )
            conn.execute(
                """
                INSERT INTO review_log (
                    claim_id, claim_revision_id, action, actor, note, decided_at
                ) VALUES (?, ?, 'needs_review', 'system:dedup', ?, ?)
                """,
                (
                    claim_id,
                    current["claim_revision_id"],
                    "去重合并新增证据，原审核结论待人工复核",
                    now,
                ),
            )
        report.candidates_merged += 1
        return

    claim_id = uuid.uuid4().hex
    claim_revision_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO claims (claim_id, memory_type, lifecycle_status, created_at, updated_at)
        VALUES (?, ?, 'candidate', ?, ?)
        """,
        (claim_id, candidate.memory_type, now, now),
    )
    conn.execute(
        """
        INSERT INTO claim_revisions (
            claim_revision_id, claim_id, content, memory_type, assertion_kind,
            review_status, lifecycle_status, asserted_at, recorded_at,
            extraction_run_id, rule_version
        ) VALUES (?, ?, ?, ?, ?, 'pending', 'candidate', ?, ?, ?, ?)
        """,
        (
            claim_revision_id, claim_id, candidate.content, candidate.memory_type,
            candidate.assertion_kind, candidate.asserted_at, now, run_id, RULE_VERSION,
        ),
    )
    conn.execute(
        "UPDATE claims SET current_revision_id = ? WHERE claim_id = ?",
        (claim_revision_id, claim_id),
    )
    _insert_evidence(conn, claim_revision_id, candidate, check_note=None)
    report.candidates_created += 1


def _insert_evidence(
    conn: sqlite3.Connection,
    claim_revision_id: str,
    candidate: ValidatedCandidate,
    *,
    check_note: str | None,
) -> None:
    for ev in candidate.evidence:
        source = conn.execute(
            "SELECT raw_text FROM event_revisions WHERE revision_id = ?",
            (ev.revision_id,),
        ).fetchone()
        if source is None or source["raw_text"] is None:
            raise ValueError("证据 revision 已不存在或没有原文")
        raw_text = source["raw_text"]
        if ev.span_start < 0 or ev.span_end > len(raw_text) or ev.span_end <= ev.span_start:
            raise ValueError("证据跨度超出原文范围")
        span_sha256 = hashlib.sha256(
            raw_text[ev.span_start : ev.span_end].encode("utf-8")
        ).hexdigest()
        conn.execute(
            """
            INSERT OR IGNORE INTO claim_evidence (
                claim_revision_id, revision_id, span_start, span_end, span_sha256,
                role, support_check, check_method, check_version, checked_at
            ) VALUES (?, ?, ?, ?, ?, 'support', 'supported', ?, ?, ?)
            """,
            (
                claim_revision_id, ev.revision_id, ev.span_start, ev.span_end,
                span_sha256, SPAN_CHECK_METHOD, SPAN_CHECK_VERSION,
                check_note or utc_now_iso(),
            ),
        )


@dataclass(frozen=True)
class _RelationSuggestion:
    from_content: str
    from_memory_type: str
    to_claim_id: str
    relation_type: str


def _suggest_relations(
    conn: sqlite3.Connection,
    client: FactLLMClient,
    cfg: FactsConfig,
    accepted: list[ValidatedCandidate],
    report: RunReport,
    *,
    similarity: ClaimSimilarity | None,
) -> list[_RelationSuggestion]:
    """冲突/相关检测（§10.3/§11.1）：只产生建议，不自动生效。"""
    sim = similarity or ClaimSimilarity()
    existing = existing_claim_contents(conn)
    pairs: list[tuple[str, str, str, str]] = []
    # (new_content, memory_type, existing claim_id, existing_content)
    for candidate in accepted:
        for claim_id, content, memory_type in existing:
            # §11.1：仅同 memory_type、主题可比较的主张才进入关系判定
            if memory_type != candidate.memory_type:
                continue
            score = sim.similarity(content, candidate.content)
            if cfg.conflict_threshold <= score < cfg.dedup_threshold:
                pairs.append((candidate.content, candidate.memory_type, claim_id, content))
                if len(pairs) >= MAX_RELATION_PAIRS:
                    break
        if len(pairs) >= MAX_RELATION_PAIRS:
            break
    if not pairs:
        return []

    report.requests += 1
    try:
        data = client.complete_json(
            SYSTEM_PROMPT,
            build_relation_prompt([(a, existing) for a, _t, _c, existing in pairs]),
        )
    except Exception:  # noqa: BLE001 - 关系建议失败不阻断候选入库（记录后继续）
        report.notes = (report.notes + " 关系建议请求失败，已跳过。").strip()
        return []
    suggestions: list[_RelationSuggestion] = []
    for idx, rel in parse_relation_response(data):
        if 0 <= idx < len(pairs) and rel != "unrelated":
            new_content, memory_type, claim_id, _existing = pairs[idx]
            suggestions.append(
                _RelationSuggestion(
                    from_content=new_content,
                    from_memory_type=memory_type,
                    to_claim_id=claim_id,
                    relation_type=rel,
                )
            )
    return suggestions


def _persist_relation(
    conn: sqlite3.Connection, rel: _RelationSuggestion, run_id: str, now: str
) -> None:
    """按内容找到本次批次里的新候选主张并落建议边（同事务，状态尚未提交）。"""
    row = conn.execute(
        """
        SELECT cr.claim_id FROM claim_revisions cr
        WHERE cr.content = ? AND cr.memory_type = ? AND cr.extraction_run_id = ?
        ORDER BY cr.recorded_at DESC LIMIT 1
        """,
        (rel.from_content, rel.from_memory_type, run_id),
    ).fetchone()
    if row is None or row["claim_id"] == rel.to_claim_id:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO claim_relations (
            relation_id, from_claim_id, to_claim_id, relation_type,
            status, origin, created_at
        ) VALUES (?, ?, ?, ?, 'suggested', 'llm', ?)
        """,
        (uuid.uuid4().hex, row["claim_id"], rel.to_claim_id, rel.relation_type, now),
    )


__all__ = [
    "BudgetExceeded",
    "Estimate",
    "ExtractionInProgress",
    "FactsConfig",
    "RunReport",
    "compute_extraction_key",
    "default_since",
    "estimate_extraction",
    "load_facts_config",
    "run_extraction",
]
