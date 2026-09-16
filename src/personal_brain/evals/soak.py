"""并发与长稳（soak）基准（任务包 CAREER-20260915-PB-REPRO-SOAK 任务 B）。

目标：证明检索路径在**多进程并发读**与**长时间持续查询**下
延迟不漂移、内存不增长、库不损坏。只读场景为主：

- 语料复用 ``synthgen.build_benchmark_archive`` + 真实导入器（与 bench 同源）；
- 查询集复用 ``benchmark.default_queries()``（含单独披露的短词/唯一 token 查询；
  其 QUERY_TOO_BROAD 拒绝是 §6.3 资源边界的**预期行为**，单独计数，不计入错误）；
- ``multiprocessing``（spawn 上下文）每 worker 自开 ``mode=ro`` 连接读 WAL 库，
  父进程只收集；``PRAGMA integrity_check`` 与行数前后一致证明只读无副作用；
- 内存曲线：worker 与父进程每 ``--rss-interval`` 秒经 Queue 上报
  ``resource.getrusage(RUSAGE_SELF).ru_maxrss``（高水位，macOS 为字节/Linux 为 KB）；
- 门槛写死在报告 ``gates``（全部通过才 ``passed: true``），口径见
  ``docs/evals/soak-<date>.md`` 与决策记录。

两种模式（``--mode both`` 一次跑完，同一份 JSON）：
- ``ladder``：并发梯度（默认 1,2,4,8），每档预热后采样固定查询数；
- ``soak``：固定并发，``--duration`` 与 ``--total-queries`` 先到为准；
  漂移 = 后 10% 查询 P95 / 前 10% 查询 P95（对齐各 worker 首样本时刻后的全局时间序）。
"""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import queue as queue_mod
import random
import resource
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personal_brain.evals.benchmark import (
    _corpus_reference_time,
    _percentile,
    collect_environment,
    default_queries,
)
from personal_brain.evals.synthgen import build_benchmark_archive
from personal_brain.history.db import connect, connect_readonly
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.mcp_server.service import (
    ToolError,
    run_with_epoch_retry,
    tool_get_event,
    tool_get_recent_events,
    tool_search_history,
)
from personal_brain.policy.profiles import AccessProfile
from personal_brain.retrieval.search import SearchLimits

# 门槛常量（写死；改动即改口径，必须同步决策记录）
LADDER_GATE_P95_C4_MS = 1000.0
LADDER_GATE_P95_C8_MS = 2000.0
SOAK_GATE_DRIFT_RATIO = 1.3
SOAK_GATE_RSS_GROWTH = 1.2
MAX_REPORTED_ERROR_MESSAGES = 10


# ---------------------------------------------------------------------------
# worker（multiprocessing 目标函数，必须模块顶层可 pickle）
# ---------------------------------------------------------------------------


@dataclass
class WorkerConfig:
    """单个 worker 的执行参数（跨进程 pickle 传递）。"""

    worker_id: int
    db_path: str
    sample_event_id: str
    queries: list[dict[str, Any]]  # BenchQuery 字段字典（含 disclosed_separately）
    warmup_seconds: float = 0.0
    quota: int | None = None  # 计入样本的查询数上限（含预期拒绝/错误）
    deadline: float | None = None  # time.monotonic() 绝对时刻（soak 模式）
    rss_interval_s: float = 5.0
    seed: int = 20250801


def rss_mb() -> float:
    """当前进程 ru_maxrss（高水位）→ MB；macOS 单位为字节，Linux 为 KB。"""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 * 1024) if sys.platform == "darwin" else value / 1024, 2)


def _soak_profile() -> AccessProfile:
    """soak 读取 profile：本地全权（与 bench 不加 --label 时一致）。"""
    return AccessProfile(
        name="soak",
        allow_scopes=("*",),
        deny_sensitivity=(),
        allow_unclassified=True,
        tools=("search_history", "get_recent_events", "get_event"),
        delivery_boundary="local_only",
    )


def _tool_impls(
    conn: sqlite3.Connection, profile: AccessProfile, limits: SearchLimits, now_ref: str
) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """与 benchmark 相同的产品路径（MCP 服务层函数，含 epoch 核对）。"""
    return {
        "search": lambda args: run_with_epoch_retry(
            tool_search_history, conn, profile, args, limits, "UTC"
        ),
        "recent": lambda args: run_with_epoch_retry(
            tool_get_recent_events, conn, profile, {**args, "now": now_ref}, limits
        ),
        "get_event": lambda args: run_with_epoch_retry(
            tool_get_event, conn, profile, args, limits
        ),
    }


def _run_one(
    impls: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
    bq: dict[str, Any],
    sample_event_id: str,
) -> tuple[float, str | None]:
    """执行单条查询 → (耗时 ms, 错误码或 None)。"""
    args = dict(bq["args"])
    if bq["kind"] == "get_event":
        args.setdefault("event_id", sample_event_id)
    started = time.perf_counter()
    try:
        impls[str(bq["kind"])](args)
    except ToolError as exc:
        return (time.perf_counter() - started) * 1000, f"{exc.code}: {exc.message[:120]}"
    return (time.perf_counter() - started) * 1000, None


def _is_expected_rejection(bq: dict[str, Any], code: str) -> bool:
    """单独披露查询的 QUERY_TOO_BROAD = §6.3 资源边界的预期拒绝（非错误）。"""
    return bool(bq.get("disclosed_separately")) and code.split(":", 1)[0] == "QUERY_TOO_BROAD"


WRITER_BATCH_CONVERSATIONS = 1
WRITER_BATCH_MESSAGES = 20
WRITER_INTERVAL_S = 15.0


def _writer_main(
    db_path: str,
    archive_dir: str,
    seed: int,
    interval_s: float,
    stop_event: Any,
    out_queue: Any,
) -> None:
    """加分项 --writer：写侧持续导入小批次，验证 WAL 下读延迟不受写影响。

    批次 = 同源（soakuser）同对话同消息 ID、但 seed 递增 → 文本不同 →
    同一事件追加**新版本**（事件数/来源数不变，revisions/occurrences/FTS 增长），
    使 counts 门槛在写侧存在时仍可精确断言。
    """
    started = time.monotonic()
    summary: dict[str, Any] = {
        "type": "writer",
        "batches": 0,
        "revisions_added": 0,
        "errors": [],
        "rss_curve": [[0.0, rss_mb()]],
        "rss_first_mb": rss_mb(),
        "rss_last_mb": 0.0,
        "failure": None,
    }
    try:
        conn = connect(db_path)
        try:
            batch_idx = 0
            while not stop_event.is_set():
                zip_path = Path(archive_dir) / f"writer-batch-{batch_idx:05d}.zip"
                build_benchmark_archive(
                    zip_path, WRITER_BATCH_CONVERSATIONS, WRITER_BATCH_MESSAGES,
                    seed=seed + 100_000 + batch_idx,
                )
                importer = ChatGPTImporter(conn, Path(archive_dir))
                result = importer.import_archive(zip_path, "soakuser")
                summary["batches"] += 1
                summary["revisions_added"] += int(result.revisions_new)
                summary["rss_curve"].append(
                    [round(time.monotonic() - started, 3), rss_mb()]
                )
                batch_idx += 1
                stop_event.wait(interval_s)
        finally:
            conn.close()
    except Exception as exc:  # 写侧错误回传父进程，读侧继续
        summary["failure"] = f"{type(exc).__name__}: {exc}"[:300]
    summary["rss_last_mb"] = rss_mb()
    out_queue.put(summary)


def _worker_main(cfg: WorkerConfig, stop_event: Any, out_queue: Any) -> None:
    """worker 主循环：自开 ro 连接，随机抽查询循环执行，经 Queue 上报。

    上报协议（同一 Queue）：
    - ``{"type": "rss", "worker_id", "t_s", "rss_mb"}``：每 rss_interval_s 一次；
    - ``{"type": "summary", ...}``：结束时一次（含延迟样本、错误、RSS 首/末值）。
    """
    started = time.monotonic()
    summary: dict[str, Any] = {
        "type": "summary",
        "worker_id": cfg.worker_id,
        "samples": [],  # [[t_ms 相对本 worker 起点, 耗时 ms], ...]
        "rejection_samples": [],
        "errors": [],  # [t_ms, 耗时 ms, "CODE: message"]
        "expected_rejections": 0,
        "rss_first_mb": rss_mb(),
        "rss_last_mb": 0.0,
        "rss_curve": [[0.0, rss_mb()]],  # 权威曲线随 summary 回传
        "n_executed": 0,
        "failure": None,
    }
    last_rss_t = started
    done = 0
    try:
        conn = connect_readonly(cfg.db_path)
        try:
            profile = _soak_profile()
            limits = SearchLimits()
            now_ref = _corpus_reference_time(conn)
            impls = _tool_impls(conn, profile, limits, now_ref)
            rng = random.Random(cfg.seed * 1_000_003 + cfg.worker_id)
            queries = cfg.queries

            def maybe_sample_rss() -> None:
                nonlocal last_rss_t
                now = time.monotonic()
                if now - last_rss_t >= cfg.rss_interval_s:
                    last_rss_t = now
                    rel = round(now - started, 3)
                    value = rss_mb()
                    summary["rss_curve"].append([rel, value])
                    out_queue.put({"type": "rss", "worker_id": cfg.worker_id,
                                   "t_s": rel, "rss_mb": value})

            # 预热（不计样本）：页缓存 + statement cache 热身
            warm_end = started + cfg.warmup_seconds
            while time.monotonic() < warm_end:
                if stop_event is not None and stop_event.is_set():
                    break
                bq = queries[rng.randrange(len(queries))]
                _run_one(impls, bq, cfg.sample_event_id)

            # 计入样本的主循环
            while True:
                if cfg.quota is not None and done >= cfg.quota:
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                if cfg.deadline is not None and time.monotonic() >= cfg.deadline:
                    break
                bq = queries[rng.randrange(len(queries))]
                t_ms = round((time.monotonic() - started) * 1000, 3)
                lat_ms, err = _run_one(impls, bq, cfg.sample_event_id)
                done += 1
                if err is None:
                    summary["samples"].append([t_ms, round(lat_ms, 3)])
                elif _is_expected_rejection(bq, err):
                    summary["expected_rejections"] += 1
                    summary["rejection_samples"].append([t_ms, round(lat_ms, 3)])
                else:
                    summary["errors"].append([t_ms, round(lat_ms, 3), err])
                maybe_sample_rss()
        finally:
            conn.close()
    except Exception as exc:  # worker 致命错误必须回传父进程，不能让父进程空等
        summary["failure"] = f"{type(exc).__name__}: {exc}"[:300]
    summary["n_executed"] = done
    summary["rss_last_mb"] = rss_mb()
    summary["rss_curve"].append(
        [round(time.monotonic() - started, 3), summary["rss_last_mb"]]
    )
    out_queue.put(summary)


# ---------------------------------------------------------------------------
# 父进程编排
# ---------------------------------------------------------------------------


@dataclass
class SoakOptions:
    """soak 基准配置（CLI --mode both = ladder + soak 同一份报告）。"""

    mode: str = "both"  # ladder | soak | both
    conversations: int = 500
    messages: int = 200
    seed: int = 20250801
    source: str = "soakuser"
    levels: list[int] = field(default_factory=lambda: [1, 2, 4, 8])
    warmup_seconds: float = 30.0
    queries_per_level: int = 5000
    concurrency: int = 4
    duration_seconds: float = 1800.0
    total_queries: int = 50000
    rss_interval_s: float = 5.0
    rss_baseline_after_s: float = 30.0  # RSS 门槛基线：预热后首样本（冷启动比单独披露）
    writer: bool = False


@dataclass
class _PhaseResult:
    stats: dict[str, Any]
    samples_by_worker: dict[str, list[list[float]]]
    worker_curves: dict[str, list[list[float]]]
    worker_rss: dict[str, tuple[float, float]]
    wall_s: float


def _spawn_workers(
    ctx: Any,
    out_queue: Any,
    stop_event: Any,
    common: dict[str, Any],
    quotas: list[int],
    warmup_seconds: float,
    deadline: float | None,
) -> list[Any]:
    procs = []
    for wid, quota in enumerate(quotas):
        cfg = WorkerConfig(
            worker_id=wid,
            db_path=str(common["db_path"]),
            sample_event_id=str(common["sample_event_id"]),
            queries=common["queries"],
            warmup_seconds=warmup_seconds,
            quota=quota,
            deadline=deadline,
            rss_interval_s=common["rss_interval_s"],
            seed=common["seed"],
        )
        proc = ctx.Process(target=_worker_main, args=(cfg, stop_event, out_queue))
        proc.start()
        procs.append(proc)
    return procs


def _drain(out_queue: Any) -> None:
    """丢弃队列中的滞留消息（看门狗终止 worker 后的残留，防止污染下一档）。"""
    try:
        while True:
            out_queue.get_nowait()
    except queue_mod.Empty:
        return


def _collect(
    procs: list[Any],
    out_queue: Any,
    hard_deadline: float,
    curves_sink: dict[str, list[list[float]]],
) -> dict[str, dict[str, Any]]:
    """收集 worker 汇总（父进程只收集；含卡死看门狗）。"""
    summaries: dict[str, dict[str, Any]] = {}
    while len(summaries) < len(procs):
        if time.monotonic() > hard_deadline:
            for idx in range(len(procs)):
                if str(idx) not in summaries:
                    summaries[str(idx)] = {
                        "type": "summary", "worker_id": idx,
                        "samples": [], "rejection_samples": [],
                        "errors": [[0.0, 0.0, "WATCHDOG: worker 未在时限内上报"]],
                        "expected_rejections": 0,
                        "rss_first_mb": 0.0, "rss_last_mb": 0.0,
                        "n_executed": 0, "failure": "watchdog_timeout",
                    }
            for proc in procs:
                proc.terminate()
            break
        try:
            msg = out_queue.get(timeout=1.0)
        except queue_mod.Empty:
            continue
        if msg.get("type") == "rss":
            curves_sink.setdefault(str(msg["worker_id"]), []).append(
                [msg["t_s"], msg["rss_mb"]]
            )
        elif msg.get("type") == "summary":
            summaries[str(msg["worker_id"])] = msg
    for proc in procs:
        proc.join(timeout=30.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=10.0)
    return summaries


def _latency_stats(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0, "mean_ms": 0.0}
    ordered = sorted(latencies)
    return {
        "p50_ms": round(_percentile(ordered, 0.50), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "p99_ms": round(_percentile(ordered, 0.99), 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(statistics.fmean(ordered), 3),
    }


def _rss_growth(
    curve: list[list[float]], baseline_after_s: float
) -> tuple[float | None, float | None]:
    """返回 (warm_ratio, cold_ratio)：门槛用预热后基线，冷启动比单独披露。

    实测 worker RSS 在首 5-10s 完成解释器/页缓存预热后**全程平稳**
    （ru_maxrss 高水位口径），"末值/首值"若以冷启动首样本为基线会把
    10s 预热误判为 4.5× 增长；门槛基线取 `baseline_after_s` 后首个样本。
    """
    if not curve:
        return None, None
    cold = curve[0][1]
    warm = next((v for t, v in curve if t >= baseline_after_s), None)
    last = curve[-1][1]
    cold_ratio = round(last / cold, 4) if cold > 0 else None
    warm_ratio = round(last / warm, 4) if warm and warm > 0 else cold_ratio
    return warm_ratio, cold_ratio


def _run_phase(
    ctx: Any,
    out_queue: Any,
    common: dict[str, Any],
    *,
    concurrency: int,
    warmup_seconds: float,
    total_quota: int,
    deadline: float | None,
    hard_deadline: float,
    phase_label: str,
    rss_baseline_after_s: float = 30.0,
) -> _PhaseResult:
    """启动一档/一段 worker 并收集，聚合为阶段统计。"""
    base = total_quota // concurrency
    extra = total_quota % concurrency
    quotas = [base + (1 if idx < extra else 0) for idx in range(concurrency)]
    curves_sink: dict[str, list[list[float]]] = {}
    stop_event = ctx.Event()
    wall_start = time.monotonic()
    procs = _spawn_workers(
        ctx, out_queue, stop_event, common, quotas, warmup_seconds, deadline
    )
    summaries = _collect(procs, out_queue, hard_deadline, curves_sink)
    _drain(out_queue)
    wall_s = time.monotonic() - wall_start

    latencies: list[float] = []
    rejections = 0
    errors: list[list[Any]] = []
    samples_by_worker: dict[str, list[list[float]]] = {}
    worker_curves: dict[str, list[list[float]]] = {}
    worker_rss: dict[str, tuple[float, float]] = {}
    rss_ratios: dict[str, tuple[float | None, float | None]] = {}
    n_executed = 0
    for wid in sorted(summaries, key=int):
        s = summaries[wid]
        samples = [[float(t), float(lat)] for t, lat in s["samples"]]
        samples_by_worker[wid] = samples
        latencies.extend(lat for _, lat in samples)
        rejections += int(s["expected_rejections"])
        errors.extend(s["errors"])
        n_executed += int(s["n_executed"])
        worker_rss[wid] = (float(s["rss_first_mb"]), float(s["rss_last_mb"]))
        # 权威曲线以 worker summary 为准；流式点仅用于 worker 未上报时的兜底
        summary_curve = [[float(t), float(v)] for t, v in s.get("rss_curve", [])]
        curve = summary_curve or curves_sink.get(wid, [])
        worker_curves[wid] = curve
        warm_ratio, cold_ratio = _rss_growth(curve, rss_baseline_after_s)
        rss_ratios[wid] = (warm_ratio, cold_ratio)
    stats: dict[str, Any] = _latency_stats(latencies)
    stats.update(
        {
            "phase": phase_label,
            "concurrency": concurrency,
            "warmup_s": warmup_seconds,
            "queries": n_executed,
            "qps": round(n_executed / wall_s, 3) if wall_s > 0 else 0.0,
            "expected_rejections": rejections,
            "errors": len(errors),
            "error_messages": sorted({str(e[2]) for e in errors})[:MAX_REPORTED_ERROR_MESSAGES],
            "wall_s": round(wall_s, 3),
            "per_worker": [
                {
                    "worker_id": int(wid),
                    "executed": int(summaries[wid]["n_executed"]),
                    "failure": summaries[wid].get("failure"),
                    "rss_first_mb": worker_rss[wid][0],
                    "rss_last_mb": worker_rss[wid][1],
                    "rss_growth": rss_ratios[wid][0],
                    "rss_growth_cold": rss_ratios[wid][1],
                }
                for wid in sorted(summaries, key=int)
            ],
        }
    )
    return _PhaseResult(stats, samples_by_worker, worker_curves, worker_rss, wall_s)


def _drift_analysis(samples_by_worker: dict[str, list[list[float]]]) -> dict[str, Any]:
    """漂移：对齐各 worker 首样本时刻后按全局时间排序，前/后 10% 的 P95 之比。

    per_worker 明细按**时间序**切片（与汇总同一口径），不得按延迟排序。
    """
    aligned: list[tuple[float, float]] = []
    per_worker: list[dict[str, Any]] = []
    for wid, samples in sorted(samples_by_worker.items(), key=lambda kv: int(kv[0])):
        if not samples:
            continue
        offset = samples[0][0]
        aligned.extend((t - offset, lat) for t, lat in samples)
        series = [lat for _, lat in samples]  # 样本本身已按完成时间序追加
        cut = max(1, len(series) // 10)
        p95_first = _percentile(series[:cut], 0.95)
        p95_last = _percentile(series[-cut:], 0.95)
        per_worker.append({
            "worker_id": int(wid),
            "queries": len(series),
            "p95_first10_ms": round(p95_first, 3),
            "p95_last10_ms": round(p95_last, 3),
            "ratio": round(p95_last / p95_first, 4) if p95_first > 0 else None,
        })
    aligned.sort(key=lambda pair: pair[0])
    lats = [lat for _, lat in aligned]
    n = len(lats)
    cut = max(1, n // 10)
    p95_first = _percentile(lats[:cut], 0.95)
    p95_last = _percentile(lats[-cut:], 0.95)
    return {
        "method": "对齐各 worker 首样本时刻后按全局时间排序；前/后 10% 各取 n//10 条",
        "queries": n,
        "n_each_side": cut,
        "p95_first10_ms": round(p95_first, 3),
        "p95_last10_ms": round(p95_last, 3),
        "ratio": round(p95_last / p95_first, 4) if p95_first > 0 else None,
        "per_worker": per_worker,
    }


# ---------------------------------------------------------------------------
# 完整性与门槛
# ---------------------------------------------------------------------------


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in ("sources", "events", "event_revisions", "revision_occurrences",
                  "event_revisions_fts"):
        out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return out


def _gate(expect: str, value: Any, passed: bool | None, note: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"expect": expect, "value": value, "passed": passed}
    if note:
        entry["note"] = note
    return entry


def _build_gates(
    ladder: dict[str, Any] | None,
    soak: dict[str, Any] | None,
    drift: dict[str, Any] | None,
    soak_rss_growth: float | None,
    integrity: dict[str, Any],
    writer: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """门槛写死在此（改动即改口径，必须同步决策记录）。未运行段的门槛 passed=None。"""
    gates: dict[str, dict[str, Any]] = {}
    if ladder is not None:
        by_c = {lv["concurrency"]: lv for lv in ladder["levels"]}
        for c, threshold in ((4, LADDER_GATE_P95_C4_MS), (8, LADDER_GATE_P95_C8_MS)):
            name = f"ladder_c{c}_warm_p95_le_{threshold:.0f}ms"
            if c in by_c:
                gates[name] = _gate(
                    f"≤ {threshold:.0f}ms",
                    by_c[c]["p95_ms"],
                    bool(by_c[c]["p95_ms"] <= threshold and by_c[c]["errors"] == 0),
                    "预热后 P95（前提：该档错误数=0）",
                )
            else:
                # 档位未运行（如测试用 --levels 1,2 缩小梯度）：字段保留，不参与判定
                gates[name] = _gate(
                    f"≤ {threshold:.0f}ms", None, None, "该并发档未运行，不参与判定"
                )
        total_errors = sum(lv["errors"] for lv in ladder["levels"])
        gates["ladder_errors_zero"] = _gate("== 0", total_errors, total_errors == 0)
    if soak is not None:
        if drift is not None and drift["ratio"] is not None:
            gates["soak_drift_ratio_le_1_3"] = _gate(
                f"≤ {SOAK_GATE_DRIFT_RATIO}",
                drift["ratio"],
                drift["ratio"] <= SOAK_GATE_DRIFT_RATIO,
                "后 10% / 前 10% 查询 P95 之比",
            )
        else:
            gates["soak_drift_ratio_le_1_3"] = _gate(
                f"≤ {SOAK_GATE_DRIFT_RATIO}", None, False, "漂移比不可计算（样本不足）"
            )
        gates["soak_worker_rss_growth_le_1_2"] = _gate(
            f"≤ {SOAK_GATE_RSS_GROWTH}",
            soak_rss_growth,
            soak_rss_growth is not None and soak_rss_growth <= SOAK_GATE_RSS_GROWTH,
            "各 worker ru_maxrss 末值/预热后基线之比的最大值（高水位口径；冷启动比见 "
            "rss_growth_cold 单独披露）",
        )
        gates["soak_errors_zero"] = _gate("== 0", soak["errors"], soak["errors"] == 0)
    gates["integrity_check_ok"] = _gate(
        "ok", integrity["integrity_check"], integrity["integrity_check"] == "ok"
    )
    before, after = integrity["counts_before"], integrity["counts_after"]
    deltas = {k: after[k] - before[k] for k in before}
    if writer is not None:
        # writer 模式：来源/事件数仍须不变；三张版本侧表的增量必须一致且等于写侧报告
        sources_events_equal = deltas["sources"] == 0 and deltas["events"] == 0
        rev_deltas = (
            deltas["event_revisions"], deltas["revision_occurrences"],
            deltas["event_revisions_fts"],
        )
        growth_consistent = (
            len(set(rev_deltas)) == 1
            and rev_deltas[0] == writer["revisions_added"]
            and writer["batches"] > 0
            and writer["failure"] is None
            and not writer["errors"]
        )
        gates["counts_unchanged"] = _gate(
            "sources/events 前后一致",
            {k: [before[k], after[k]] for k in before},
            sources_events_equal,
            "writer 模式：版本侧表增长属写侧预期，由 writer_growth_consistent 断言",
        )
        gates["writer_growth_consistent"] = _gate(
            "revisions/occurrences/fts 增量一致且 = 写侧 revisions_added",
            {"deltas": deltas, "writer_revisions_added": writer["revisions_added"],
             "writer_batches": writer["batches"]},
            growth_consistent,
            f"每批 {WRITER_BATCH_CONVERSATIONS}×{WRITER_BATCH_MESSAGES} 条同事件新版本",
        )
    else:
        gates["counts_unchanged"] = _gate(
            "前后一致",
            {k: [before[k], after[k]] for k in before},
            integrity["all_counts_equal"],
            "只读无副作用：查询前后行数（含 FTS）不变",
        )
    return gates


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _serialize_queries() -> list[dict[str, Any]]:
    return [dataclasses.asdict(bq) for bq in default_queries()]


def run_soak(db_path: Path, archive_dir: Path, opts: SoakOptions) -> dict[str, Any]:
    """执行 soak 基准并返回报告 dict（不写文件；CLI 层负责输出与退出码）。"""
    queries = _serialize_queries()
    ctx = multiprocessing.get_context("spawn")
    out_queue: Any = ctx.Queue()
    parent_curve: list[list[float]] = []
    sampler_stop = threading.Event()

    def parent_sampler() -> None:
        """父进程自身 RSS 曲线（t 相对 run_soak 开始，含语料导入段）。"""
        while not sampler_stop.is_set():
            parent_curve.append([round(time.monotonic() - _t0, 3), rss_mb()])
            sampler_stop.wait(opts.rss_interval_s)

    _t0 = time.monotonic()
    sampler = threading.Thread(target=parent_sampler, daemon=True)
    sampler.start()

    # 语料：与 bench 同一生成器 + 真实导入器；导入连接干净关闭后 worker 只读打开
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        total = opts.conversations * opts.messages
        print(f"生成 {total} 条合成消息（{opts.conversations}×{opts.messages}）…", file=sys.stderr)
        zip_path = build_benchmark_archive(
            tmp / "soak.zip", opts.conversations, opts.messages, seed=opts.seed
        )
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(archive_dir).mkdir(parents=True, exist_ok=True)
        conn = connect(db_path)
        try:
            importer = ChatGPTImporter(conn, archive_dir)
            import_started = time.perf_counter()
            importer.import_archive(zip_path, opts.source)
            import_s = round(time.perf_counter() - import_started, 3)
            counts_before = _row_counts(conn)
            sample_event_id = str(
                conn.execute("SELECT event_id FROM events LIMIT 1").fetchone()["event_id"]
            )
        finally:
            conn.close()

        common = {
            "db_path": str(db_path),
            "sample_event_id": sample_event_id,
            "queries": queries,
            "rss_interval_s": opts.rss_interval_s,
            "seed": opts.seed,
        }
        ladder_out: dict[str, Any] | None = None
        ladder_curves: dict[str, dict[str, list[list[float]]]] = {}
        soak_out: dict[str, Any] | None = None
        soak_curves: dict[str, list[list[float]]] = {}
        soak_rss_growth: float | None = None
        drift: dict[str, Any] | None = None
        writer_out: dict[str, Any] | None = None

        writer_proc = None
        writer_stop = None
        if opts.writer:
            writer_stop = ctx.Event()
            writer_proc = ctx.Process(
                target=_writer_main,
                args=(str(db_path), str(archive_dir), opts.seed,
                      WRITER_INTERVAL_S, writer_stop, out_queue),
            )
            writer_proc.start()
            print(
                f"writer：每 {WRITER_INTERVAL_S:.0f}s 导入 "
                f"{WRITER_BATCH_CONVERSATIONS}×{WRITER_BATCH_MESSAGES} 条新版本 …",
                file=sys.stderr,
            )

        if opts.mode in ("ladder", "both"):
            levels_stats: list[dict[str, Any]] = []
            for level in opts.levels:
                print(f"ladder 并发 {level}：预热 {opts.warmup_seconds:.0f}s …", file=sys.stderr)
                # 看门狗：预热 + 每查询 2s 保守估算 + 300s 余量（实测均值 ~0.4s/查询）
                hard = time.monotonic() + opts.warmup_seconds + 300.0 + (
                    opts.queries_per_level * 2.0 / level
                )
                result = _run_phase(
                    ctx, out_queue, common,
                    concurrency=level,
                    warmup_seconds=opts.warmup_seconds,
                    total_quota=opts.queries_per_level,
                    deadline=None,
                    hard_deadline=hard,
                    phase_label=f"ladder_c{level}",
                    rss_baseline_after_s=opts.rss_baseline_after_s,
                )
                levels_stats.append(result.stats)
                ladder_curves[f"c{level}"] = result.worker_curves
            ladder_out = {
                "levels_config": opts.levels,
                "queries_per_level": opts.queries_per_level,
                "levels": levels_stats,
            }

        if opts.mode in ("soak", "both"):
            print(
                f"soak：并发 {opts.concurrency} × 最长 {opts.duration_seconds:.0f}s "
                f"或 {opts.total_queries} 次查询（先到为准）…",
                file=sys.stderr,
            )
            deadline = time.monotonic() + opts.duration_seconds
            result = _run_phase(
                ctx, out_queue, common,
                concurrency=opts.concurrency,
                warmup_seconds=0.0,
                total_quota=opts.total_queries,
                deadline=deadline,
                hard_deadline=deadline + 600.0,
                phase_label="soak",
                rss_baseline_after_s=opts.rss_baseline_after_s,
            )
            soak_out = dict(result.stats)
            soak_out["requested_duration_s"] = opts.duration_seconds
            soak_out["requested_total_queries"] = opts.total_queries
            soak_out["stop_reason"] = (
                "total_queries" if result.stats["queries"] >= opts.total_queries else "duration"
            )
            soak_curves = result.worker_curves
            ratios = [
                w["rss_growth"] for w in result.stats["per_worker"]
                if w["rss_growth"] is not None
            ]
            soak_rss_growth = round(max(ratios), 4) if ratios else None
            cold_ratios = [
                w["rss_growth_cold"] for w in result.stats["per_worker"]
                if w["rss_growth_cold"] is not None
            ]
            soak_out["rss_growth_cold_max"] = (
                round(max(cold_ratios), 4) if cold_ratios else None
            )
            drift = _drift_analysis(result.samples_by_worker)

        if writer_proc is not None and writer_stop is not None:
            writer_stop.set()
            writer_deadline = time.monotonic() + WRITER_INTERVAL_S + 120.0
            writer_summary: dict[str, Any] | None = None
            while time.monotonic() < writer_deadline:
                try:
                    msg = out_queue.get(timeout=1.0)
                except queue_mod.Empty:
                    continue
                if msg.get("type") == "writer":
                    writer_summary = msg
                    break
            if writer_summary is None:
                writer_proc.terminate()
                writer_summary = {"batches": 0, "revisions_added": 0,
                                  "errors": [], "rss_curve": [],
                                  "rss_first_mb": 0.0, "rss_last_mb": 0.0,
                                  "failure": "writer 未按时上报"}
            writer_proc.join(timeout=30.0)
            writer_out = {k: v for k, v in writer_summary.items() if k != "type"}
            writer_out["batch_config"] = {
                "conversations": WRITER_BATCH_CONVERSATIONS,
                "messages": WRITER_BATCH_MESSAGES,
                "interval_s": WRITER_INTERVAL_S,
                "note": "同源同事件 ID、seed 递增 → 同一事件追加新版本（非新事件）",
            }

        # 只读无副作用验证（integrity_check 是纯读 PRAGMA，父进程只读连接执行）
        ro = connect_readonly(db_path)
        try:
            integrity: dict[str, Any] = {
                "counts_before": counts_before,
                "counts_after": _row_counts(ro),
                "integrity_check": str(ro.execute("PRAGMA integrity_check").fetchone()[0]),
            }
        finally:
            ro.close()
        integrity["all_counts_equal"] = (
            integrity["counts_before"] == integrity["counts_after"]
        )

    sampler_stop.set()
    sampler.join(timeout=5.0)

    gates = _build_gates(ladder_out, soak_out, drift, soak_rss_growth, integrity, writer_out)
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "environment": collect_environment(db_path),
        "config": {
            "mode": opts.mode,
            "conversations": opts.conversations,
            "messages": opts.messages,
            "corpus_events": total,
            "seed": opts.seed,
            "source": opts.source,
            "import_s": import_s,
            "levels": opts.levels,
            "warmup_seconds": opts.warmup_seconds,
            "queries_per_level": opts.queries_per_level,
            "concurrency": opts.concurrency,
            "duration_seconds": opts.duration_seconds,
            "total_queries": opts.total_queries,
            "rss_interval_s": opts.rss_interval_s,
            "rss_baseline_after_s": opts.rss_baseline_after_s,
            "writer": opts.writer,
            "db_path": str(db_path),
            "query_pool": [q["name"] for q in queries],
            "note": (
                "查询集复用 benchmark.default_queries()；单独披露查询的 QUERY_TOO_BROAD "
                "是 §6.3 资源边界预期拒绝，单独计数不计入错误（与 bench 门槛口径一致）"
            ),
        },
        "ladder": ladder_out,
        "soak": soak_out,
        "memory_curves": {
            "interval_s": opts.rss_interval_s,
            "metric": "ru_maxrss 高水位 MB（macOS 字节 / Linux KB 归一）",
            "origin": "t=0 相对各自进程/worker 起点",
            "parent": parent_curve,
            "ladder": ladder_curves,
            "soak": soak_curves,
            "writer": (writer_out or {}).get("rss_curve", []),
        },
        "writer": writer_out,
        "drift": drift,
        "integrity": integrity,
        "gates": gates,
        "passed": all(g["passed"] is not False for g in gates.values()),
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return report


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
