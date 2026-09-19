"""brain 命令行入口（§16 首版 CLI，Phase A 适用命令）。

本地 CLI 属受信任本地边界（§7.4 local_review 性质）：检索结果不做
标签强制过滤（可用 --scope 显式启用过滤演练）；MCP 端策略执行属
Phase B。所有命令支持 --json 结构化输出。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

from personal_brain.backup import backup, restore_check
from personal_brain.config import BrainConfig
from personal_brain.history.db import connect
from personal_brain.history.status import collect_status
from personal_brain.importers.importer import ChatGPTImporter
from personal_brain.policy.labels import label_event, label_source
from personal_brain.policy.withdraw import withdraw_event, withdraw_source
from personal_brain.retrieval.search import (
    QueryTooBroad,
    SearchFilters,
    parse_date_bound,
    recent_events,
    search_history,
)


def _emit(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        _emit_human(data)


def _emit_human(data: dict) -> None:
    kind = data.get("kind")
    if kind == "import":
        r = data["result"]
        print(
            f"[{r['job_kind']}] 快照 {r['snapshot_id']}\n"
            f"  对话 {r['conversations_seen']} | 新事件 {r['events_new']} | "
            f"新版本 {r['revisions_new']} | 新出现 {r['occurrences_new']} | "
            f"不支持 {r['unsupported_count']} | parse_status={r['parse_status']}"
        )
        for e in r["errors"][:10]:
            print(f"  ! {e['code']} @ {e['locator']}: {e['message']}")
        if len(r["errors"]) > 10:
            print(f"  ! …共 {len(r['errors'])} 条错误")
    elif kind == "status":
        for s in data["sources"]:
            print(
                f"来源 {s['account_namespace']} ({s['source_kind']}, {s['status']}) "
                f"快照 {s['snapshots']} 事件 {s['events']} 覆盖 "
                f"{s['coverage_start']} … {s['coverage_end']}"
            )
        print(f"合计: {json.dumps(data['totals'], ensure_ascii=False)}")
        print(f"作业: {json.dumps(data['jobs'], ensure_ascii=False)}")
        print(f"派生: {json.dumps(data['derived'], ensure_ascii=False)}")
        if not data["sources"]:
            print("（无来源）")
    elif kind == "search":
        for h in data["hits"]:
            mark = "*" if h["is_current"] else " "
            kind_mark = (
                "≈" if h.get("match_kind") == "semantic"
                else "×" if h.get("match_kind") == "both" else ""
            )
            time_str = h["source_created_at"] or "(时间未知)"
            speaker = f"{h['speaker_type']}:{h['speaker_id'] or ''}"
            print(
                f"{mark}{kind_mark} {time_str} [{h['revision_id'][:28]}] {speaker} "
                f"对话 {h['conversation_id'][:20]}"
            )
            if h.get("snippet"):
                print(f"    {h['snippet']}")
        meta = {
            k: data[k]
            for k in ("total_matched", "truncated", "next_cursor",
                      "undated_excluded", "path_unknown_conversations")
        }
        print(f"共 {meta['total_matched']} 条"
              + (f"（已截断，游标 {meta['next_cursor']}）" if meta["truncated"] else ""))
        if meta["undated_excluded"]:
            print(f"注意：{meta['undated_excluded']} 条未知时间记录未参与时间过滤")
        for c in meta["path_unknown_conversations"]:
            print(f"注意：对话 {c} 所选路径未知，selected 模式已排除")
        for note in data.get("notes", ()):
            print(f"说明：{note}")
        if not data["hits"]:
            print("（无匹配）")
    elif kind == "label":
        print(
            f"已标注 {data['target']} {data['target_id']}: "
            f"scope={list(data['scope_labels'])} sensitivity={list(data['sensitivity_labels'])} "
            f"({data['revisions_relabeled']} 个版本, {data['classification_status']})"
        )
        for w in data.get("warnings", []):
            print(f"⚠ {w}")
    elif kind == "withdraw":
        print(
            f"已撤回 {data['target']} {data['target_id']}: "
            f"{data['revisions_withdrawn']} 个版本, 影响 {data['events_affected']} 个事件"
        )
    elif kind == "backup":
        print(f"备份完成: {data['backup_dir']}\n  sha256={data['snapshot_sha256']}")
    elif kind == "restore_check":
        print(
            f"备份校验: integrity={data['integrity']} "
            f"外键违规={data['foreign_key_violations']} schema_v={data['schema_version']} "
            f"manifest={'ok' if data['manifest_ok'] else 'MISMATCH'}"
        )
        print(f"计数: {json.dumps(data['counts'], ensure_ascii=False)}")
    elif kind == "event":
        print(json.dumps(data["event"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------


def cmd_import(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        importer = ChatGPTImporter(conn, cfg.archive_dir)
        result = importer.import_archive(Path(args.path), args.source)
        payload = {"kind": "import", "result": asdict(result)}
        _emit(payload, as_json)
        return 0
    finally:
        conn.close()


def cmd_status(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        st = collect_status(conn)
        _emit(
            {
                "kind": "status",
                "sources": [asdict(s) for s in st.sources],
                "totals": st.totals,
                "jobs": st.jobs,
                "derived": st.derived,
            },
            as_json,
        )
        return 0
    finally:
        conn.close()


def cmd_label_source(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        summary = label_source(
            conn,
            args.source_id,
            scope_labels=tuple(args.scope or ()),
            sensitivity_labels=tuple(args.sensitivity or ()),
        )
        _emit({"kind": "label", **asdict(summary)}, as_json)
        return 0
    finally:
        conn.close()


def cmd_label_event(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        summary = label_event(
            conn,
            args.event_id,
            scope_labels=tuple(args.scope or ()),
            sensitivity_labels=tuple(args.sensitivity or ()),
        )
        _emit({"kind": "label", **asdict(summary)}, as_json)
        return 0
    finally:
        conn.close()


def cmd_search(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        limits = cfg.search
        from personal_brain.retrieval.search import SearchLimits

        sl = SearchLimits(
            default_limit=limits.default_limit,
            max_limit=limits.max_limit,
            snippet_chars=limits.snippet_chars,
            max_text_chars=limits.max_text_chars,
            max_scan=limits.max_scan,
        )
        tz = args.timezone or cfg.timezone
        filters = SearchFilters(
            date_from=parse_date_bound(args.date_from, tz, end_of_day=False)
            if args.date_from else None,
            date_to=parse_date_bound(args.date_to, tz, end_of_day=True)
            if args.date_to else None,
            account_namespace=args.source,
            conversation_id=args.conversation,
            speaker_type=args.speaker,
            path_mode=args.path,
            allow_scopes=tuple(args.scope) if args.scope else None,
            deny_sensitivity=tuple(args.deny) if args.deny else None,
            allow_unclassified=not args.no_unclassified,
        )
        try:
            from personal_brain.retrieval.semantic_index import SemanticConfig

            result = search_history(
                conn,
                args.query,
                match_mode=args.match_mode,
                filters=filters,
                order=args.order,
                limit=args.limit,
                cursor=args.cursor,
                limits=sl,
                mode=args.mode,
                semantic_config=SemanticConfig(
                    model_id=cfg.semantic_model,
                    chunk_chars=cfg.semantic_chunk_chars,
                    chunk_overlap=cfg.semantic_chunk_overlap,
                    cache_dir=cfg.semantic_cache_dir,
                ),
            )
        except QueryTooBroad as exc:
            print(f"QUERY_TOO_BROAD: {exc}", file=sys.stderr)
            return 3
        _emit({"kind": "search", **asdict(result)}, as_json)
        return 0
    finally:
        conn.close()


def cmd_recent(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        from personal_brain.retrieval.search import SearchLimits

        sl = SearchLimits(
            default_limit=cfg.search.default_limit,
            max_limit=cfg.search.max_limit,
            snippet_chars=cfg.search.snippet_chars,
            max_text_chars=cfg.search.max_text_chars,
        )
        result = recent_events(conn, days=args.days, limit=args.limit, limits=sl)
        _emit({"kind": "search", **asdict(result)}, as_json)
        return 0
    finally:
        conn.close()


def cmd_show_event(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        ev = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (args.event_id,)
        ).fetchone()
        if ev is None:
            print(f"NOT_FOUND: {args.event_id}", file=sys.stderr)
            return 4
        if args.revision:
            revs = conn.execute(
                "SELECT * FROM event_revisions WHERE revision_id = ?",
                (args.revision,),
            ).fetchall()
        else:
            revs = conn.execute(
                "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY recorded_at",
                (args.event_id,),
            ).fetchall()
        out_revs = []
        for r in revs:
            policy = conn.execute(
                """
                SELECT availability, classification_status, policy_version, valid_from
                FROM revision_policy_state
                WHERE revision_id = ? AND valid_to IS NULL
                """,
                (r["revision_id"],),
            ).fetchone()
            attachments = [
                dict(a)
                for a in conn.execute(
                    "SELECT asset_pointer, content_type, member_path "
                    "FROM revision_attachments WHERE revision_id = ?",
                    (r["revision_id"],),
                ).fetchall()
            ]
            out_revs.append(
                {
                    "revision_id": r["revision_id"],
                    "content_type": r["content_type"],
                    "raw_text": r["raw_text"],
                    "source_created_at": r["source_created_at"],
                    "source_updated_at": r["source_updated_at"],
                    "original_time_value": r["original_time_value"],
                    "time_precision": r["time_precision"],
                    "timezone_status": r["timezone_status"],
                    "recorded_at": r["recorded_at"],
                    "source_locator": r["source_locator"],
                    "availability": policy["availability"] if policy else "unknown",
                    "classification_status": (
                        policy["classification_status"] if policy else "unknown"
                    ),
                    "attachments": attachments,
                }
            )
        _emit(
            {
                "kind": "event",
                "event": {
                    "event_id": ev["event_id"],
                    "identity_quality": ev["identity_quality"],
                    "revision_resolution": ev["revision_resolution"],
                    "current_revision_id": ev["current_revision_id"],
                    "speaker_id": ev["speaker_id"],
                    "speaker_type": ev["speaker_type"],
                    "conversation_id": ev["conversation_id"],
                    "revisions": out_revs,
                },
            },
            as_json,
        )
        return 0
    finally:
        conn.close()


def cmd_withdraw_source(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        summary = withdraw_source(conn, args.source_id)
        _emit({"kind": "withdraw", **asdict(summary)}, as_json)
        return 0
    finally:
        conn.close()


def cmd_withdraw_event(args, cfg: BrainConfig, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        summary = withdraw_event(conn, args.event_id)
        _emit({"kind": "withdraw", **asdict(summary)}, as_json)
        return 0
    finally:
        conn.close()


def cmd_backup(args, cfg: BrainConfig, as_json: bool) -> int:
    result = backup(
        cfg.db_path,
        Path(args.dest) if args.dest else cfg.backup_dir,
        archive_dir=cfg.archive_dir,
    )
    _emit(
        {
            "kind": "backup",
            "backup_dir": str(result.backup_dir),
            "snapshot_path": str(result.snapshot_path),
            "snapshot_sha256": result.snapshot_sha256,
            "archive_files": result.archive_files,
            "created_at": result.created_at,
        },
        as_json,
    )
    return 0


def cmd_restore_check(args, cfg: BrainConfig, as_json: bool) -> int:
    result = restore_check(Path(args.backup_path))
    _emit(
        {
            "kind": "restore_check",
            "backup_dir": str(result.backup_dir),
            "integrity": result.integrity,
            "foreign_key_violations": result.foreign_key_violations,
            "schema_version": result.schema_version,
            "counts": result.counts,
            "manifest_ok": result.manifest_ok,
        },
        as_json,
    )
    return 0


def cmd_eval(args, cfg: BrainConfig, as_json: bool) -> int:
    """§16 brain eval --suite <path>：运行评测套件（合成开发集或私有真实集）。"""
    import json as _json

    from personal_brain.evals.runner import run_suite
    from personal_brain.evals.suite import load_suite
    from personal_brain.retrieval.search import SearchLimits

    suite = load_suite(Path(args.suite))
    conn = connect(cfg.db_path)
    try:
        sl = SearchLimits(
            default_limit=cfg.search.default_limit,
            max_limit=cfg.search.max_limit,
            snippet_chars=cfg.search.snippet_chars,
            max_text_chars=cfg.search.max_text_chars,
            max_scan=cfg.search.max_scan,
        )
        report = run_suite(
            conn, suite, limits=sl, timezone=cfg.timezone, mode=args.mode
        )
    finally:
        conn.close()
    out = _json.dumps(asdict(report), ensure_ascii=False, indent=2)
    if as_json:
        print(out)
    elif report.precondition_failures:
        print(f"评测套件 {report.suite_name}: 未运行——套件前置条件不满足")
        for msg in report.precondition_failures:
            print(f"  - {msg}")
        print("  （先按上述说明准备环境，再重跑；红色失败只应代表真实缺陷）")
        return 1
    else:
        gates = report.summary["gates"]
        print(f"评测套件 {report.suite_name}: {'通过' if report.passed else '未通过'}")
        print(f"  用例 {report.summary['cases_passed']}/{report.summary['cases_total']} 通过")
        print(
            f"  Recall@10 = {gates['recall_at10'] if gates['recall_at10'] is not None else 'n/a'}"
            f"（门槛 ≥0.90：{'✓' if gates['recall_at10_gate'] else '✗'}）"
        )
        print(
            f"  citation 解析 = "
            f"{gates['citation_resolution'] if gates['citation_resolution'] is not None else 'n/a'}"
            f"（门槛 100%：{'✓' if gates['citation_gate'] else '✗'}）"
        )
        print(
            f"  拒答样例全过：{'✓' if gates['refusal_all_passed'] else '✗'} | "
            f"正常问题回答率 = {gates['answer_rate_normal']}"
        )
        print(f"  禁止访问出现：{'是 ✗' if gates['forbidden_access_seen'] else '否 ✓'}")
        for r in report.cases:
            if not r.passed:
                print(f"  ✗ {r.case_id}: {r.failure_types or r.error}")
        if report.human_review:
            print(f"  人工复核项：{len(report.human_review)} 条（见 --json 输出）")
    return 0 if report.passed else 1


def cmd_bench(args, cfg: BrainConfig, as_json: bool) -> int:
    """§9.2 门槛 7：合成数据规模基准（预热 P95，硬件/SQLite 版本入档）。"""
    import tempfile

    from personal_brain.evals.benchmark import run_benchmark
    from personal_brain.evals.synthgen import build_benchmark_archive
    from personal_brain.policy.labels import label_source
    from personal_brain.policy.profiles import AccessProfile

    n_conv, n_msg = args.conversations, args.messages
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg.db_path)
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            total = n_conv * n_msg
            print(f"生成 {total} 条合成消息（{n_conv}×{n_msg}）…", file=sys.stderr)
            zp = build_benchmark_archive(tmp / "bench.zip", n_conv, n_msg, seed=args.seed)
            importer = ChatGPTImporter(conn, cfg.archive_dir)
            started = time.perf_counter()
            importer.import_archive(zp, args.source)
            import_ms = (time.perf_counter() - started) * 1000
            print(f"导入完成：{import_ms:.0f} ms", file=sys.stderr)
            if args.label:
                source_id = conn.execute(
                    "SELECT source_id FROM sources WHERE account_namespace = ?",
                    (args.source,),
                ).fetchone()["source_id"]
                label_source(conn, source_id, scope_labels=("bench",))
            sample = conn.execute(
                "SELECT event_id FROM events LIMIT 1"
            ).fetchone()["event_id"]
            profile = AccessProfile(
                name="bench",
                allow_scopes=("bench",) if args.label else ("*",),
                deny_sensitivity=(),
                allow_unclassified=not args.label,
                tools=("search_history", "get_recent_events", "get_event"),
                delivery_boundary="local_only",
            )
            report = run_benchmark(
                conn,
                profile,
                sample,
                db_path=cfg.db_path,
                repeats=args.repeats,
            )
    finally:
        conn.close()
    if args.out:
        Path(args.out).write_text(report.to_json(), encoding="utf-8")
        print(f"报告已写入 {args.out}", file=sys.stderr)
    if as_json:
        print(report.to_json())
    else:
        print(
            f"环境: {report.environment['platform']} | "
            f"SQLite {report.environment['sqlite']} | Python {report.environment['python']}"
        )
        print(f"规模: {json.dumps(report.rows, ensure_ascii=False)}")
        print(f"门槛: {report.gate} → {'通过 ✓' if report.passed else '未通过 ✗'}")
        for r in report.results:
            mark = " [单独披露]" if r.disclosed_separately else ""
            print(
                f"  {r.name:32s} P50 {r.p50_ms:8.1f}ms  P95 {r.p95_ms:8.1f}ms  "
                f"P99 {r.p99_ms:8.1f}ms  命中 {r.result_count}{mark}"
            )
    return 0 if report.passed else 1


def cmd_soak(args, cfg: BrainConfig, as_json: bool) -> int:
    """并发 + 长稳基准（任务 B）：多进程只读、漂移/内存/完整性门槛。"""
    from personal_brain.evals.soak import SoakOptions, report_to_json, run_soak

    opts = SoakOptions(
        mode=args.mode,
        conversations=args.conversations,
        messages=args.messages,
        seed=args.seed,
        source=args.source,
        levels=[int(x) for x in str(args.levels).split(",") if x.strip()],
        warmup_seconds=args.warmup_seconds,
        queries_per_level=args.queries_per_level,
        concurrency=args.concurrency,
        duration_seconds=args.duration,
        total_queries=args.total_queries,
        rss_interval_s=args.rss_interval,
        rss_baseline_after_s=args.rss_baseline_after,
        writer=args.writer,
    )
    report = run_soak(cfg.db_path, cfg.archive_dir, opts)
    if args.out:
        Path(args.out).write_text(report_to_json(report), encoding="utf-8")
        print(f"报告已写入 {args.out}", file=sys.stderr)
    if as_json:
        print(report_to_json(report))
    else:
        env = report["environment"]
        print(
            f"环境: {env['platform']} | SQLite {env['sqlite']} | Python {env['python']} | "
            f"CPU {env['cpu_count']}"
        )
        print(f"规模: {report['config']['corpus_events']} events | 模式 {opts.mode}")
        if report["ladder"]:
            print("ladder（预热后）:")
            for lv in report["ladder"]["levels"]:
                print(
                    f"  并发 {lv['concurrency']}: 查询 {lv['queries']}  QPS {lv['qps']}  "
                    f"P50 {lv['p50_ms']:.1f}ms  P95 {lv['p95_ms']:.1f}ms  "
                    f"P99 {lv['p99_ms']:.1f}ms  错误 {lv['errors']}"
                    f"（预期拒绝 {lv['expected_rejections']}）"
                )
        if report["soak"]:
            s = report["soak"]
            print(
                f"soak: 并发 {s['concurrency']} × {s['wall_s']:.0f}s / {s['queries']} 次查询  "
                f"QPS {s['qps']}  P95 {s['p95_ms']:.1f}ms  错误 {s['errors']}"
            )
        if report["drift"]:
            d = report["drift"]
            print(
                f"漂移: 后10% P95 {d['p95_last10_ms']:.1f}ms / 前10% P95 "
                f"{d['p95_first10_ms']:.1f}ms = {d['ratio']}"
            )
        integrity = report["integrity"]
        print(
            f"完整性: integrity_check={integrity['integrity_check']} "
            f"行数前后一致={integrity['all_counts_equal']}"
        )
        print(f"门槛: {sum(1 for g in report['gates'].values() if g['passed'] is False)} 项未过"
              f" → {'通过 ✓' if report['passed'] else '未通过 ✗'}")
        for name, g in report["gates"].items():
            mark = "✓" if g["passed"] is True else ("—" if g["passed"] is None else "✗")
            print(f"  {mark} {name}: value={g['value']} expect={g['expect']}")
    return 0 if report["passed"] else 1


# ---------------------------------------------------------------------------


def cmd_reindex_semantic(args, cfg: BrainConfig, as_json: bool) -> int:
    """D-1：构建/增量更新本地语义索引（可选项未装时给出可操作指引）。"""
    import time as _time

    from personal_brain.retrieval.embeddings import build_provider
    from personal_brain.retrieval.semantic_index import reindex_semantic

    conn = connect(cfg.db_path)
    try:
        try:
            provider = build_provider(
                args.provider,
                args.model or cfg.semantic_model,
                args.cache_dir or cfg.semantic_cache_dir,
            )
        except ImportError:
            print(
                "缺少可选依赖。安装：pip install -e '.[semantic]'"
                "（或 uv sync --extra semantic）。"
                "离线验证可用 --provider hash（不下载模型）。",
                file=sys.stderr,
            )
            return 2
        except RuntimeError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
        if args.provider == "fastembed":
            print(
                f"提示：首次运行会从 HuggingFace 下载模型 {provider.model_id}"
                "（之后缓存本地、离线使用）；embedding 全程本地计算。",
                file=sys.stderr,
            )
        started = _time.perf_counter()
        try:
            stats = reindex_semantic(
                conn,
                provider,
                full=args.full,
                chunk_chars=cfg.semantic_chunk_chars,
                chunk_overlap=cfg.semantic_chunk_overlap,
            )
        except RuntimeError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
        stats["wall_ms"] = int((_time.perf_counter() - started) * 1000)
        stats["db_bytes"] = cfg.db_path.stat().st_size
        if as_json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(
                f"语义索引{'全量重建' if stats['mode'] == 'full' else '增量更新'}完成:"
            )
            print(
                f"  模型 {stats['model_id']}（dim={stats['dimension']}）"
                f" index_version={stats['index_version']}"
            )
            print(
                f"  扫描 {stats['revisions_scanned']} 个可索引版本，"
                f"新增 embed {stats['revisions_embedded']} 个"
                f"（+{stats['chunks_new']} chunks）"
            )
            print(
                f"  索引规模 {stats['revisions_total']} 个版本 / "
                f"{stats['chunks_total']} chunks，耗时 {stats['wall_ms']} ms"
            )
            print(
                "  幂等：重复运行无新增；撤回的来源/事件已从向量与 chunk 中清除。"
            )
        return 0
    finally:
        conn.close()


def cmd_recall(args, cfg: BrainConfig, as_json: bool) -> int:
    """Use the same trusted profile and payload as the MCP entry point."""
    import json

    from personal_brain.history.db import connect_readonly
    from personal_brain.mcp_server.service import run_with_epoch_retry
    from personal_brain.mcp_server.unified import search_brain
    from personal_brain.policy.profiles import load_mcp_config
    from personal_brain.retrieval.search import SearchLimits

    if not args.config:
        raise ValueError("recall 需要 --config 指定 MCP profile 配置")
    config = load_mcp_config(Path(args.config))
    if "search_brain" not in config.profile.tools:
        raise ValueError("profile 未授权 search_brain")
    conn = connect_readonly(config.db_path)
    try:
        result = run_with_epoch_retry(
            search_brain, conn, config,
            {"query": args.query, "source": args.source, "limit": args.limit,
             **({"speaker_type": args.speaker} if args.speaker else {}),
             **({"mode": args.mode} if getattr(args, "mode", None) else {})},
            SearchLimits(**config.search_limits),
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else 0


def cmd_doctor(args, cfg: BrainConfig, as_json: bool) -> int:
    import json
    import shutil

    from personal_brain.history.db import connect_readonly
    from personal_brain.policy.profiles import load_mcp_config

    if not args.config:
        raise ValueError("doctor 需要 --config 指定 MCP profile 配置")
    config = load_mcp_config(Path(args.config))
    conn = connect_readonly(config.db_path)
    try:
        conn.execute("SELECT revision_id FROM event_revisions LIMIT 1").fetchone()
        # D-1：语义索引状态（只读；扩展/索引缺失时 available 字段说明原因）
        from personal_brain.retrieval.semantic_index import semantic_status

        semantic = semantic_status(conn)
    finally:
        conn.close()
    result = {
        "database_readable": True,
        "profile": config.profile.name,
        "delivery_boundary": config.profile.delivery_boundary,
        "tools": list(config.profile.tools),
        "semantic_index": semantic,
        "vault_configured": config.vault is not None,
        "vault_root_exists": config.vault.root.is_dir() if config.vault else None,
        "tunnel_client_installed": shutil.which("tunnel-client") is not None,
        "chatgpt_connection_verified": False,
        "automatic_chat_capture": False,
        "note": "本地配置检查不代表网页版已连接；尚未验证隧道和账号权限。",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["vault_root_exists"] is not False else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain", description="Personal Brain CLI")
    parser.add_argument("--config", help="YAML 配置文件路径")
    parser.add_argument("--db", help="覆盖数据库路径")
    parser.add_argument("--archive-dir", help="覆盖归档目录")
    parser.add_argument("--backup-dir", help="覆盖备份目录")
    parser.add_argument("--timezone", help="覆盖自然日期解析时区")
    parser.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="只读检查统一入口配置与本地接入前提")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("recall", help="使用 MCP profile 统一检索聊天与 Vault")
    p.add_argument("query")
    p.add_argument("--source", choices=["all", "history", "vault"], default="all")
    p.add_argument("--speaker", choices=["owner", "assistant"])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument(
        "--mode", choices=["exact", "hybrid"],
        help="检索模式（默认随 profile semantic_default）",
    )
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("import-chatgpt", help="导入 ChatGPT 导出（ZIP 或目录）")
    p.add_argument("path")
    p.add_argument("--source", required=True, help="账号别名（稳定、非敏感）")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("status", help="数据与派生状态汇总")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("label-source", help="来源级显式标注")
    p.add_argument("source_id")
    p.add_argument("--scope", action="append", help="scope 标签（可重复）")
    p.add_argument("--sensitivity", action="append", help="sensitivity 标签（可重复）")
    p.set_defaults(func=cmd_label_source)

    p = sub.add_parser("label-event", help="事件级显式标注（混合话题用）")
    p.add_argument("event_id")
    p.add_argument("--scope", action="append")
    p.add_argument("--sensitivity", action="append")
    p.set_defaults(func=cmd_label_event)

    p = sub.add_parser("search-history", help="中文/精确历史检索")
    p.add_argument("query")
    p.add_argument("--from", dest="date_from", help="自然日期（含）")
    p.add_argument("--to", dest="date_to", help="自然日期（含，转为右开边界）")
    p.add_argument("--match-mode", choices=["literal", "all_terms"], default="literal")
    p.add_argument(
        "--order",
        choices=["chronological", "reverse_chronological", "relevance"],
        default="chronological",
    )
    p.add_argument("--limit", type=int)
    p.add_argument("--cursor")
    p.add_argument("--path", choices=["selected", "all"], default="all")
    p.add_argument("--source", help="按账号别名过滤")
    p.add_argument("--conversation", help="按对话 ID 过滤")
    p.add_argument("--speaker", help="按说话者类型过滤")
    p.add_argument("--scope", action="append", help="启用 scope 过滤（默认不过滤）")
    p.add_argument("--deny", action="append", help="启用 sensitivity 拒绝过滤")
    p.add_argument("--no-unclassified", action="store_true", help="排除未分类内容")
    p.add_argument(
        "--mode", choices=["exact", "hybrid"], default="exact",
        help="exact=词面精确；hybrid=叠加本地语义召回（缺依赖/索引时退化并说明）",
    )
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recent", help="最近事件（专用接口，非全库导出）")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("show-event", help="按 ID 查看事件与全部版本")
    p.add_argument("event_id")
    p.add_argument("--revision", help="仅显示指定版本")
    p.set_defaults(func=cmd_show_event)

    p = sub.add_parser("withdraw-source", help="逻辑撤回整个来源（§14.1）")
    p.add_argument("source_id")
    p.set_defaults(func=cmd_withdraw_source)

    p = sub.add_parser("withdraw-event", help="逻辑撤回单个事件")
    p.add_argument("event_id")
    p.set_defaults(func=cmd_withdraw_event)

    p = sub.add_parser("backup", help="一致性备份（快照 + Raw 归档 + manifest）")
    p.add_argument("--dest")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore-check", help="校验备份可用性")
    p.add_argument("backup_path")
    p.set_defaults(func=cmd_restore_check)

    p = sub.add_parser("eval", help="运行评测套件（§9：Recall@10/引用解析/权限断言）")
    p.add_argument("--suite", required=True, help="评测套件 JSON 路径")
    p.add_argument(
        "--mode", choices=["exact", "hybrid"], default="exact",
        help="exact=基线（semantic 用例跳过）；hybrid=含语义用例 + HashEmbedding",
    )
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser(
        "reindex-semantic",
        help="D-1：构建/增量更新本地语义索引（--full 全量重建）",
    )
    p.add_argument("--full", action="store_true", help="全量重建（清空现有向量）")
    p.add_argument(
        "--provider", choices=["fastembed", "hash"], default="fastembed",
        help="fastembed=本地 BGE 模型（首次下载）；hash=确定性测试向量（离线）",
    )
    p.add_argument("--model", help="覆盖 semantic.model（仅 fastembed）")
    p.add_argument("--cache-dir", help="覆盖 semantic.cache_dir（仅 fastembed）")
    p.set_defaults(func=cmd_reindex_semantic)

    p = sub.add_parser("bench", help="合成数据性能基准（§9.2：预热 P95，短词单独披露）")
    p.add_argument("--conversations", type=int, default=500)
    p.add_argument("--messages", type=int, default=200)
    p.add_argument("--seed", type=int, default=20250801)
    p.add_argument("--repeats", type=int, default=20)
    p.add_argument("--source", default="benchuser")
    p.add_argument("--label", action="store_true", help="导入后标注 bench scope")
    p.add_argument("--out", help="报告 JSON 输出路径")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser(
        "soak", help="并发与长稳基准（多进程只读，漂移/RSS/完整性门槛，任务 B）"
    )
    p.add_argument("--conversations", type=int, default=500)
    p.add_argument("--messages", type=int, default=200)
    p.add_argument("--seed", type=int, default=20250801)
    p.add_argument("--source", default="soakuser")
    p.add_argument("--mode", choices=["ladder", "soak", "both"], default="both")
    p.add_argument("--levels", default="1,2,4,8", help="ladder 并发梯度（逗号分隔）")
    p.add_argument("--warmup-seconds", type=float, default=30.0)
    p.add_argument("--queries-per-level", type=int, default=5000)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--duration", type=float, default=1800.0, help="soak 最长持续秒数")
    p.add_argument("--total-queries", type=int, default=50000, help="soak 查询总数上限")
    p.add_argument("--rss-interval", type=float, default=5.0, help="RSS 采样间隔秒")
    p.add_argument("--rss-baseline-after", type=float, default=30.0,
                   help="RSS 门槛基线取样时刻（秒）；冷启动比单独披露不计门槛")
    p.add_argument("--writer", action="store_true",
                   help="加分项：并发读的同时开一个写进程持续导入小批次")
    p.add_argument("--out", help="报告 JSON 输出路径")
    p.set_defaults(func=cmd_soak)

    return parser


def resolve_config(args: argparse.Namespace) -> BrainConfig:
    cfg = BrainConfig.load(Path(args.config) if args.config else None)
    if args.db:
        cfg.db_path = Path(args.db)
    if args.archive_dir:
        cfg.archive_dir = Path(args.archive_dir)
    if args.backup_dir:
        cfg.backup_dir = Path(args.backup_dir)
    if args.timezone:
        cfg.timezone = args.timezone
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = resolve_config(args)
    as_json = bool(args.json)
    try:
        return int(args.func(args, cfg, as_json))
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as exc:
        print(f"数据库错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
