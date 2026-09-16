"""soak 基准测试（任务 B）：小语料端到端——报告结构、门槛字段、只读无副作用。

规格：小语料 50 会话×20 消息（=1000 events）、并发 2、--total-queries 200；
ladder 档与预热按比例缩小以控制测试时长，门槛字段存在性逐项断言。
"""

from __future__ import annotations

from pathlib import Path

from personal_brain.evals.soak import SoakOptions, run_soak


def _small_options() -> SoakOptions:
    return SoakOptions(
        mode="both",
        conversations=50,
        messages=20,
        seed=20250801,
        source="soaktest",
        levels=[1, 2],
        warmup_seconds=1.0,
        queries_per_level=100,
        concurrency=2,
        duration_seconds=5.0,
        total_queries=200,
        rss_interval_s=0.5,
        # 运行时长秒级：RSS 门槛基线按比例缩放到 0.5s（默认 30s 针对分钟级运行）
        rss_baseline_after_s=0.5,
    )


class TestSoakReport:
    def test_end_to_end_small_corpus(self, tmp_path: Path):
        report = run_soak(
            tmp_path / "soak.sqlite", tmp_path / "archives", _small_options()
        )

        # 顶层结构（规格 3.2 要求的键全部存在）
        for key in ("environment", "config", "ladder", "soak", "memory_curves",
                    "drift", "gates", "passed"):
            assert key in report
        assert report["environment"]["sqlite"]
        assert report["environment"]["cpu_count"]
        assert report["config"]["corpus_events"] == 1000
        assert len(report["config"]["query_pool"]) == 9  # 复用 default_queries()

        # ladder 段：两档各 100 次查询，延迟分位数与错误计数字段齐备
        ladder = report["ladder"]
        assert ladder is not None and len(ladder["levels"]) == 2
        for lv in ladder["levels"]:
            for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms", "mean_ms", "qps",
                         "errors", "expected_rejections", "queries", "wall_s",
                         "per_worker"):
                assert name in lv, f"ladder level missing {name}"
            assert lv["queries"] == 100
            assert lv["errors"] == 0
            assert lv["p95_ms"] > 0

        # soak 段：整体分位数 + 停止原因
        soak = report["soak"]
        assert soak is not None
        for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms", "mean_ms", "qps",
                     "errors", "expected_rejections", "queries", "concurrency",
                     "requested_duration_s", "requested_total_queries", "stop_reason"):
            assert name in soak, f"soak missing {name}"
        assert soak["queries"] == 200
        assert soak["stop_reason"] in ("duration", "total_queries")

        # 漂移：比值可计算，逐 worker 明细存在
        drift = report["drift"]
        assert drift is not None
        assert drift["ratio"] is not None and drift["ratio"] > 0
        assert len(drift["per_worker"]) == 2

        # 内存曲线：父进程 + 两个 soak worker 都有采样点，点为 [t_s, rss_mb]
        curves = report["memory_curves"]
        assert curves["parent"]
        assert set(curves["soak"].keys()) == {"0", "1"}
        for points in curves["soak"].values():
            assert points
            assert all(len(point) == 2 and point[1] > 0 for point in points)

        # 只读无副作用：integrity ok 且行数（含 FTS）前后一致
        integrity = report["integrity"]
        assert integrity["integrity_check"] == "ok"
        assert integrity["all_counts_equal"] is True
        assert integrity["counts_before"] == integrity["counts_after"]

        # 门槛字段存在；未运行档（c4/c8）passed=None，不阻塞 overall
        gates = report["gates"]
        for name in ("ladder_c4_warm_p95_le_1000ms", "ladder_c8_warm_p95_le_2000ms",
                     "ladder_errors_zero", "soak_drift_ratio_le_1_3",
                     "soak_worker_rss_growth_le_1_2", "soak_errors_zero",
                     "integrity_check_ok", "counts_unchanged"):
            assert name in gates, f"gate missing {name}"
        assert gates["ladder_c4_warm_p95_le_1000ms"]["passed"] is None
        assert gates["ladder_c8_warm_p95_le_2000ms"]["passed"] is None
        assert gates["ladder_errors_zero"]["passed"] is True
        assert gates["soak_drift_ratio_le_1_3"]["passed"] is not None
        assert gates["integrity_check_ok"]["passed"] is True
        assert gates["counts_unchanged"]["passed"] is True
        assert report["passed"] is True
