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
            time_str = h["source_created_at"] or "(时间未知)"
            speaker = f"{h['speaker_type']}:{h['speaker_id'] or ''}"
            print(
                f"{mark} {time_str} [{h['revision_id'][:28]}] {speaker} "
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
            result = search_history(
                conn,
                args.query,
                match_mode=args.match_mode,
                filters=filters,
                order=args.order,
                limit=args.limit,
                cursor=args.cursor,
                limits=sl,
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
        report = run_suite(conn, suite, limits=sl, timezone=cfg.timezone)
    finally:
        conn.close()
    out = _json.dumps(asdict(report), ensure_ascii=False, indent=2)
    if as_json:
        print(out)
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


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain", description="Personal Brain CLI")
    parser.add_argument("--config", help="YAML 配置文件路径")
    parser.add_argument("--db", help="覆盖数据库路径")
    parser.add_argument("--archive-dir", help="覆盖归档目录")
    parser.add_argument("--backup-dir", help="覆盖备份目录")
    parser.add_argument("--timezone", help="覆盖自然日期解析时区")
    parser.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

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
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("bench", help="合成数据性能基准（§9.2：预热 P95，短词单独披露）")
    p.add_argument("--conversations", type=int, default=500)
    p.add_argument("--messages", type=int, default=200)
    p.add_argument("--seed", type=int, default=20250801)
    p.add_argument("--repeats", type=int, default=20)
    p.add_argument("--source", default="benchuser")
    p.add_argument("--label", action="store_true", help="导入后标注 bench scope")
    p.add_argument("--out", help="报告 JSON 输出路径")
    p.set_defaults(func=cmd_bench)

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
