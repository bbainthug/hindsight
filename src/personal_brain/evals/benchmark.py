"""性能基准（§9.2 门槛 7）：

- 10 万条合成文本消息、记录硬件与 SQLite 版本；
- 常用查询**预热后** P95 目标 ≤ 1 秒；
- 短词扫描（回退路径）单独测量并披露，不与常用查询混算。

测量对象是产品路径（MCP 服务层函数，含 epoch 核对），不是绕过服务层的裸 SQL。
"""

from __future__ import annotations

import json
import platform
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_brain.mcp_server.service import (
    ToolError,
    run_with_epoch_retry,
    tool_get_event,
    tool_get_recent_events,
    tool_search_history,
)
from personal_brain.policy.profiles import AccessProfile
from personal_brain.retrieval.search import SearchLimits


def collect_environment(db_path: Path) -> dict:
    import os

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "cpu_count": os.cpu_count(),
        "db_size_bytes": Path(db_path).stat().st_size if Path(db_path).exists() else 0,
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
    }


@dataclass
class BenchQuery:
    name: str
    kind: str  # search | recent | get_event
    args: dict
    disclosed_separately: bool = False  # 短词扫描单独披露


@dataclass
class BenchResult:
    name: str
    kind: str
    repeats: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float
    result_count: int
    disclosed_separately: bool = False
    error: str | None = None


@dataclass
class BenchmarkReport:
    environment: dict
    rows: dict[str, int]
    results: list[BenchResult] = field(default_factory=list)
    passed: bool = False
    gate: str = "常用查询预热 P95 ≤ 1000ms（§9.2）"

    def to_json(self) -> str:
        return json.dumps(
            {
                "environment": self.environment,
                "rows": self.rows,
                "gate": self.gate,
                "passed": self.passed,
                "results": [r.__dict__ for r in self.results],
            },
            ensure_ascii=False,
            indent=2,
        )


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


def default_queries() -> list[BenchQuery]:
    """常用查询 + 单独披露的短词扫描。"""
    return [
        BenchQuery("search_2char_cjk_职业", "search", {"query": "职业"}),
        BenchQuery("search_4char_cjk_职业方向", "search", {"query": "职业方向"}),
        BenchQuery("search_mixed_AI工程", "search", {"query": "AI工程"}),
        BenchQuery("search_punct_你好世界", "search", {"query": "你好，世界"}),
        # 唯一 token（含单字 CJK 段 → 回退路径）——单独披露
        BenchQuery("search_unique_token", "search", {"query": "主题00042号"},
                   disclosed_separately=True),
        BenchQuery(
            "recent_7d", "recent", {"days": 7, "limit": 10}
        ),  # now 运行时从库内最大消息时间推导（写死日期会随语料漂移而 0 命中）
        # 短词扫描（回退路径）——单独披露
        BenchQuery("fallback_single_hanzi_安", "search", {"query": "安"},
                   disclosed_separately=True),
        BenchQuery("fallback_latin_ai", "search", {"query": "ai"},
                   disclosed_separately=True),
        BenchQuery("get_event_by_id", "get_event", {}),
    ]


def run_benchmark(
    conn,
    profile: AccessProfile,
    sample_event_id: str,
    *,
    db_path: Path,
    repeats: int = 20,
    queries: list[BenchQuery] | None = None,
) -> BenchmarkReport:
    report = BenchmarkReport(environment=collect_environment(db_path), rows=_row_counts(conn))
    limits = SearchLimits()
    # D-1：hybrid 基准用 HashEmbedding（确定性、离线、CI 可跑）；
    # 先构建语义索引（幂等），依赖缺失时跳过 hybrid 行（bench 不因此失败）。
    hash_provider = None
    try:
        from personal_brain.retrieval.embeddings import HashEmbeddingProvider
        from personal_brain.retrieval.semantic_index import (
            SemanticConfig,
            reindex_semantic,
        )

        hash_provider = HashEmbeddingProvider()
        reindex_semantic(conn, hash_provider)
        hybrid_ready = True
    except Exception:  # noqa: BLE001 - sqlite-vec 不可用等环境因素
        hybrid_ready = False
    hybrid_kwargs: dict = (
        {
            "embedding_provider": hash_provider,
            "semantic_config": SemanticConfig(hash_provider.model_id),
        }
        if hybrid_ready and hash_provider is not None
        else {}
    )
    impls = {
        "search": lambda args: run_with_epoch_retry(
            tool_search_history, conn, profile, args, limits, "UTC",
            **hybrid_kwargs,
        ),
        "recent": lambda args: run_with_epoch_retry(
            tool_get_recent_events, conn, profile, args, limits
        ),
        "get_event": lambda args: run_with_epoch_retry(
            tool_get_event, conn, profile, args, limits
        ),
    }
    bench_queries = list(queries or default_queries())
    if hybrid_ready and queries is None:
        bench_queries.append(
            BenchQuery(
                "search_hybrid_职业方向", "search",
                {"query": "职业方向", "mode": "hybrid"},
                disclosed_separately=True,
            )
        )
    for bq in bench_queries:
        fn = impls[bq.kind]
        args = dict(bq.args)
        if bq.kind == "get_event":
            args.setdefault("event_id", sample_event_id)
        if bq.kind == "recent" and "now" not in args:
            # recent 窗口锚定库内最大消息时间（写死日期会随语料漂移而 0 命中，
            # 丢失结果物化路径的覆盖——验收建议 4）
            args["now"] = _corpus_reference_time(conn)
        # 预热 2 次（页缓存 + SQLite statement cache）
        try:
            for _ in range(2):
                fn(args)
        except ToolError as exc:
            report.results.append(
                BenchResult(
                    name=bq.name, kind=bq.kind, repeats=0,
                    p50_ms=0.0, p95_ms=0.0, p99_ms=0.0, max_ms=0.0,
                    mean_ms=0.0, result_count=0,
                    disclosed_separately=bq.disclosed_separately,
                    error=f"TOOL_ERROR {exc.code}: {exc.message[:80]}",
                )
            )
            continue
        samples: list[float] = []
        count = 0
        for _ in range(repeats):
            started = time.perf_counter()
            payload = fn(args)
            samples.append((time.perf_counter() - started) * 1000)
            results = payload.get("results") or []
            count = len(results)
        samples.sort()
        report.results.append(
            BenchResult(
                name=bq.name,
                kind=bq.kind,
                repeats=repeats,
                p50_ms=_percentile(samples, 0.50),
                p95_ms=_percentile(samples, 0.95),
                p99_ms=_percentile(samples, 0.99),
                max_ms=samples[-1],
                mean_ms=statistics.fmean(samples),
                result_count=count,
                disclosed_separately=bq.disclosed_separately,
            )
        )
    common = [r for r in report.results if not r.disclosed_separately]
    report.passed = (
        bool(common)
        and all(r.p95_ms <= 1000 for r in common)
        and all(r.error is None for r in common)
    )
    return report


def _corpus_reference_time(conn) -> str:
    """recent 窗口参考点：库内最大消息时间 + 1 天（ISO 带 +00:00）。"""
    row = conn.execute(
        "SELECT MAX(source_created_at) AS m FROM event_revisions"
        " WHERE source_created_at IS NOT NULL"
    ).fetchone()
    if row is None or not row["m"]:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    base = datetime.fromisoformat(str(row["m"]).replace("Z", "+00:00"))
    return (base + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _row_counts(conn) -> dict[str, int]:
    out = {}
    for table in ("sources", "events", "event_revisions", "revision_occurrences",
                  "event_revisions_fts"):
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out
