"""评测运行器（§9.2）：检索级指标 + 引用解析 + 权限断言 + 失败分类。

门槛映射：
- Recall@10 ≥ 90%（明确可回答的检索题；``acceptable_event_ids`` 为分母）。
- 多证据问题另计必要证据集合覆盖率（evidence_coverage）。
- citation 解析到正确版本：正常可访问证据要求 100%。
- 禁止访问内容不得出现（forbidden_event_ids / expected_access=deny）。
- 必须拒答样例全部通过；同时统计正常问题回答率，防止靠全拒答通过门槛。
- ``must_distinguish``/``attribution`` 输出供人工复核（门槛 4，需人工确认）。
"""

from __future__ import annotations

import sqlite3
import time as _time
from dataclasses import dataclass, field

from personal_brain.evals.citation import resolve_citation
from personal_brain.evals.suite import EvalCase, EvalSuite
from personal_brain.mcp_server.service import ToolError, profile_filters, tool_get_event
from personal_brain.policy.profiles import AccessProfile
from personal_brain.retrieval.search import (
    QueryTooBroad,
    SearchFilters,
    SearchLimits,
    parse_date_bound,
    search_history,
)

TOP_K = 10


@dataclass
class CaseResult:
    case_id: str
    case_type: str
    passed: bool = False
    is_refusal_case: bool = False
    failure_types: list[str] = field(default_factory=list)
    recall_at_k: float | None = None
    evidence_coverage: float | None = None
    citations_resolved: int = 0
    citations_total: int = 0
    returned_event_ids: list[str] = field(default_factory=list)
    forbidden_seen: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None
    attribution_rows: list[dict] = field(default_factory=list)


@dataclass
class EvalReport:
    suite_name: str
    passed: bool
    cases: list[CaseResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    human_review: list[dict] = field(default_factory=list)


def run_suite(
    conn: sqlite3.Connection,
    suite: EvalSuite,
    limits: SearchLimits | None = None,
    timezone: str = "UTC",
) -> EvalReport:
    limits = limits or SearchLimits()
    filters = profile_filters(suite.profile)
    results = [
        _run_case(conn, suite.profile, case, filters, limits, timezone)
        for case in suite.cases
    ]

    answerable = [r.recall_at_k for r in results if r.recall_at_k is not None]
    recall_mean = sum(answerable) / len(answerable) if answerable else None
    cit_total = sum(r.citations_total for r in results)
    cit_ok = sum(r.citations_resolved for r in results)
    refusal_cases = [r for r in results if r.is_refusal_case]
    normal_cases = [r for r in results if not r.is_refusal_case]
    answered = [r for r in normal_cases if r.returned_event_ids]
    gates = {
        "recall_at10": recall_mean,
        "recall_at10_gate": recall_mean is not None and recall_mean >= 0.90,
        "citation_resolution": (cit_ok / cit_total) if cit_total else None,
        "citation_gate": cit_total == 0 or cit_ok == cit_total,
        "refusal_all_passed": all(r.passed for r in refusal_cases),
        "answer_rate_normal": (len(answered) / len(normal_cases)) if normal_cases else None,
        "forbidden_access_seen": any(r.forbidden_seen for r in results),
    }
    hard_fail = (
        (gates["recall_at10_gate"] if answerable else True)
        and gates["citation_gate"]
        and gates["refusal_all_passed"]
        and not gates["forbidden_access_seen"]
    )
    human_review = [
        {
            "case_id": case.id,
            "must_distinguish": case.must_distinguish,
            "attribution": result.attribution_rows,
            "forbidden_conclusions": list(case.forbidden_conclusions),
        }
        for case, result in zip(suite.cases, results)
        if case.must_distinguish or case.attribution
    ]
    return EvalReport(
        suite_name=suite.name,
        passed=bool(hard_fail and all(r.passed for r in results)),
        cases=results,
        summary={
            "gates": gates,
            "cases_total": len(results),
            "cases_passed": sum(1 for r in results if r.passed),
        },
        human_review=human_review,
    )


def _run_case(
    conn: sqlite3.Connection,
    profile: AccessProfile,
    case: EvalCase,
    base_filters: SearchFilters,
    limits: SearchLimits,
    timezone: str,
) -> CaseResult:
    res = CaseResult(
        case_id=case.id,
        case_type=case.type,
        is_refusal_case=case.allowed_refusal or case.expected_access == "deny",
    )
    try:
        if case.mode == "get_event":
            _execute_get_event(conn, profile, case, limits, res)
        else:
            _execute_search(conn, case, base_filters, limits, timezone, res)
    except ToolError as exc:
        res.error = exc.code
        if case.expected_access == "deny" and exc.code == "NOT_FOUND_OR_NOT_ALLOWED":
            res.passed = True
        else:
            res.failure_types.append("unexpected_error")
        return res
    except QueryTooBroad:
        res.error = "QUERY_TOO_BROAD"
        res.failure_types.append("query_too_broad")
        return res
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
        res.failure_types.append("unexpected_error")
        return res
    _finalize(conn, case, base_filters, res)
    return res


def _case_filters(case: EvalCase, base: SearchFilters, timezone: str) -> SearchFilters:
    return SearchFilters(
        date_from=parse_date_bound(case.time.date_from, case.time.timezone, end_of_day=False)
        if case.time and case.time.date_from else None,
        date_to=parse_date_bound(case.time.date_to, case.time.timezone, end_of_day=True)
        if case.time and case.time.date_to else None,
        path_mode=case.branch_scope,
        allow_scopes=base.allow_scopes,
        deny_sensitivity=base.deny_sensitivity,
        allow_unclassified=base.allow_unclassified,
    )


def _execute_search(
    conn: sqlite3.Connection,
    case: EvalCase,
    base_filters: SearchFilters,
    limits: SearchLimits,
    timezone: str,
    res: CaseResult,
) -> None:
    started = _time.perf_counter()
    result = search_history(
        conn,
        case.query or "",
        match_mode=case.match_mode,
        filters=_case_filters(case, base_filters, timezone),
        order=case.order,
        limit=TOP_K,
        limits=limits,
    )
    res.latency_ms = (_time.perf_counter() - started) * 1000
    res.returned_event_ids = [h.event_id for h in result.hits]
    res.citations_total = len(result.hits)
    res.attribution_rows = [
        {
            "event_id": h.event_id,
            "revision_id": h.revision_id,
            "speaker_type": h.speaker_type,
            "timestamp": h.source_created_at,
            "citation_id": f"pb:{h.revision_id}",
        }
        for h in result.hits
    ]
    accepted = set(case.acceptable_event_ids)
    if accepted:
        top = set(res.returned_event_ids)
        res.recall_at_k = len(accepted & top) / len(accepted)
        res.evidence_coverage = res.recall_at_k


def _execute_get_event(
    conn: sqlite3.Connection,
    profile: AccessProfile,
    case: EvalCase,
    limits: SearchLimits,
    res: CaseResult,
) -> None:
    started = _time.perf_counter()
    payload = tool_get_event(
        conn,
        profile,
        {"event_id": case.event_id, "context_radius": case.context_radius},
        limits,
    )
    res.latency_ms = (_time.perf_counter() - started) * 1000
    item = payload["results"][0]
    res.returned_event_ids = [item["event_id"]]
    res.citations_total = 1
    res.attribution_rows = [
        {
            "event_id": item["event_id"],
            "revision_id": item["revision_id"],
            "speaker_type": item["speaker_type"],
            "timestamp": item["timestamp"],
            "citation_id": item["citation_id"],
        }
    ]


def _finalize(
    conn: sqlite3.Connection, case: EvalCase, filters: SearchFilters, res: CaseResult
) -> None:
    failures: list[str] = []
    returned = set(res.returned_event_ids)
    forbidden_seen = sorted(returned & set(case.forbidden_event_ids))
    res.forbidden_seen = forbidden_seen
    if forbidden_seen:
        failures.append("forbidden_access")
    # 引用解析 100%（正常可访问证据，§9.2）
    for row in res.attribution_rows:
        if resolve_citation(conn, row["citation_id"], filters) is not None:
            res.citations_resolved += 1
    if res.citations_total and res.citations_resolved < res.citations_total:
        failures.append("citation_unresolved")
    if res.recall_at_k is not None and res.recall_at_k < 0.90:
        failures.append("recall_fail")
    if case.expected_access == "deny":
        # deny 用例：结果必须为空（search）或不返回（get_event 抛错已在上方处理）
        if res.returned_event_ids:
            failures.append("forbidden_access")
    elif not res.returned_event_ids and not case.allowed_refusal:
        failures.append("unexpected_empty")
    # 归属断言：声明的说话者类型与实际不符 → 错误归属（门槛 4 的机器可查部分）
    for speaker, evs in case.attribution.items():
        for ev in evs:
            match = next(
                (a for a in res.attribution_rows if a["event_id"] == ev), None
            )
            if match is not None and match["speaker_type"] != speaker:
                failures.append("attribution_mismatch")
    res.failure_types = failures
    res.passed = not failures
