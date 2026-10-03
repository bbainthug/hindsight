"""brain facts 子命令（任务书范围 3：本地 CLI，不暴露写 MCP）。

- extract：估算 → 预算门 → 幂等检查 → 提炼入库；``--dry-run`` 只估算。
- review：交互式逐条审核（通过/拒绝/修改/跳过、关系裁决）；
  ``--claim`` / ``--relation`` 提供非交互路径（脚本与测试用）。
- list / timeline / explain：只读视图。
- export-profile：profile.md 自动维护小节草稿；默认只打印 diff。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from personal_brain.facts.llm import (
    LLMConfigError,
    LLMError,
    OpenAICompatibleClient,
    resolve_api_key,
)
from personal_brain.facts.pipeline import (
    BudgetExceeded,
    ExtractionInProgress,
    default_since,
    estimate_extraction,
    run_extraction,
)
from personal_brain.facts.profile_export import (
    build_profile_update,
    unified_profile_diff,
    write_profile,
)
from personal_brain.facts.review import (
    ClaimView,
    approve_claim,
    decide_relation,
    edit_claim,
    get_claim_view,
    pending_claims,
    reject_claim,
    skip_claim,
)
from personal_brain.facts.similarity import build_local_claim_similarity
from personal_brain.facts.views import decision_timeline, explain_claim, list_claims
from personal_brain.history.db import connect, connect_readonly
from personal_brain.retrieval.credentials import redact_known_credentials
from personal_brain.retrieval.search import parse_date_bound

_ACTION_HELP = "非交互处理：approve/reject/skip/edit（edit 需 --content）"


def _resolve_claim_id(conn: sqlite3.Connection, claim_id: str) -> str:
    """精确匹配；否则唯一前缀匹配（git 风格）。"""
    row = conn.execute(
        "SELECT claim_id FROM claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if row is not None:
        return row["claim_id"]
    rows = conn.execute(
        "SELECT claim_id FROM claims WHERE claim_id LIKE ?",
        (claim_id + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["claim_id"]
    if len(rows) > 1:
        raise ValueError(f"主张 ID 前缀不唯一（{len(rows)} 个匹配）: {claim_id}")
    raise ValueError(f"主张不存在: {claim_id}")


def _bounds(args: argparse.Namespace, cfg_timezone: str) -> tuple[str | None, str | None]:
    since = (
        parse_date_bound(args.since, cfg_timezone, end_of_day=False)
        if args.since
        else default_since(_now_ts())
    )
    until = (
        parse_date_bound(args.until, cfg_timezone, end_of_day=True)
        if args.until
        else None
    )
    return since, until


def _now_ts() -> float:
    from datetime import UTC, datetime

    return datetime.now(UTC).timestamp()


def _build_client(args: argparse.Namespace, cfg) -> OpenAICompatibleClient:
    facts = cfg.facts
    base_url = args.base_url or facts.base_url
    model = args.model or facts.model
    api_key = resolve_api_key(facts.api_key_env)
    return OpenAICompatibleClient(
        provider_name="openai-compatible",
        base_url=base_url,
        model=model,
        api_key=api_key,
        temperature=facts.temperature,
        timeout_seconds=facts.timeout_seconds,
        max_output_tokens=facts.max_output_tokens,
    )


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def cmd_facts_extract(args, cfg, as_json: bool) -> int:
    # A dry run must not apply pending migrations to the user's archive.
    conn = connect_readonly(cfg.db_path) if args.dry_run else connect(cfg.db_path)
    try:
        since, until = _bounds(args, cfg.timezone)
        facts_cfg = cfg.facts
        if args.budget is not None:
            # --budget 覆盖配置预算（估算与预算门同源）
            facts_cfg = replace(cfg.facts, budget_tokens=args.budget)
        if args.dry_run:
            _segments, est = estimate_extraction(
                conn, facts_cfg,
                since=since, until=until, account_namespace=args.source,
            )
            payload = {"kind": "facts_estimate", **vars(est)}
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            else:
                print(
                    f"范围 {since or '最早'} … {until or '现在'}"
                    f"{' | 来源 ' + args.source if args.source else ' | 来源 全部'}"
                )
                print(
                    f"owner 发言 {est.owner_revisions} 条 → 分段 {est.segments} 段 → "
                    f"预计请求 {est.requests} 次（含至多 1 次关系建议）"
                )
                print(
                    f"估算 tokens：输入 ~{est.input_tokens} + 输出 ~{est.output_tokens}"
                    f" = ~{est.total_tokens}（预算 {est.budget_tokens}）"
                    f" → {'在预算内 ✓' if est.within_budget else '超出预算 ✗'}"
                )
                if est.path_unknown_conversations:
                    print(
                        f"注意：{est.path_unknown_conversations} 个对话所选路径未知，"
                        "已跳过（不猜测路径）"
                    )
            return 0 if est.within_budget else 3

        # Enforce the same estimate before loading credentials or the local
        # embedding model. A budget refusal must not initialize either client.
        _segments, est = estimate_extraction(
            conn, facts_cfg,
            since=since, until=until, account_namespace=args.source,
        )
        if not est.segments:
            if as_json:
                print(json.dumps(
                    {"kind": "facts_run", "status": "empty", **vars(est)},
                    ensure_ascii=False, indent=2, default=str,
                ))
            else:
                print("范围内没有可提炼的 owner 发言；未调用模型、未写库。")
            return 0
        if not est.within_budget:
            print(
                f"超出预算：估算 {est.total_tokens} tokens、{est.requests} 次请求，"
                f"预算 {est.budget_tokens}；请缩小范围或提高 --budget。",
                file=sys.stderr,
            )
            return 3
        if not as_json:
            print(
                f"预算预检：{est.segments} 段，预计最多 {est.requests} 次请求，"
                f"约 {est.total_tokens} tokens（预算 {est.budget_tokens}）。"
            )

        try:
            client = _build_client(args, cfg)
        except LLMConfigError as exc:
            print(f"配置错误: {exc}", file=sys.stderr)
            return 2
        try:
            similarity = build_local_claim_similarity(
                cfg.semantic_model, cfg.semantic_cache_dir
            )
            report = run_extraction(
                conn, cfg.facts, client,
                since=since, until=until, account_namespace=args.source,
                budget_tokens=args.budget,
                similarity=similarity,
            )
        except BudgetExceeded as exc:
            print(f"超出预算: {exc}", file=sys.stderr)
            return 3
        except ExtractionInProgress as exc:
            print(f"提炼未启动: {exc}", file=sys.stderr)
            return 2
        except LLMError as exc:
            print(f"LLM 调用失败（现有检索不受影响）: {exc}", file=sys.stderr)
            return 2
        payload = {"kind": "facts_run", **vars(report)}
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            if report.skipped_idempotent:
                print(
                    f"幂等跳过：同一输入与配置的批次已存在"
                    f"（run {report.run_id[:12]}），未重复调用模型。"
                )
                return 0
            print(
                f"提炼完成（run {report.run_id[:12]}，{report.wall_seconds}s）："
                f"{report.segments} 段 / {report.requests} 次请求 / "
                f"{report.prompt_tokens}+{report.completion_tokens} tokens"
            )
            print(
                f"候选：新建 {report.candidates_created}，合并到已有主张 "
                f"{report.candidates_merged}；关系建议 {report.relations_suggested} 条"
            )
            if report.rejections:
                detail = "，".join(f"{k}={v}" for k, v in sorted(report.rejections.items()))
                print(f"拒绝：{detail}")
            if report.path_unknown_conversations:
                print(f"注意：{report.path_unknown_conversations} 个对话路径未知已跳过")
            if report.notes:
                print(f"说明：{report.notes}")
            print("下一步：brain facts review 逐条审核。")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def _print_claim(view: ClaimView, position: str) -> None:
    print(f"=== {position} claim {view.claim_id[:12]} "
          f"[{view.memory_type}/{view.assertion_kind}] "
          f"({view.review_status}/{view.lifecycle_status}) ===")
    print(f"内容: {redact_known_credentials(view.content)}")
    if not view.evidence:
        print("证据: （无）")
    for i, ev in enumerate(view.evidence, start=1):
        time_str = (ev.source_created_at or "时间未知")[:19]
        print(
            f"  证据{i}. {time_str} 对话 {ev.conversation_id[:24]} "
            f"({ev.speaker_type}) rev={ev.revision_id[:16]} span={ev.span_start}-{ev.span_end}"
        )
        print(f"     原话: 「{redact_known_credentials(ev.quote)}」")
    for rel in view.relations:
        side = "→" if rel["from_claim_id"] == view.claim_id else "←"
        print(
            f"  关系[{rel['relation_id'][:12]}] {rel['status']}/{rel['origin']} "
            f"{side} {rel['relation_type']}"
        )


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def _decide_relations_interactive(conn, view: ClaimView, as_json: bool) -> None:
    """审核前先处理该主张的关系建议（用户故事 3：并排展示、用户拍板）。"""
    for rel in view.relations:
        if rel["status"] != "suggested" or rel["origin"] != "llm":
            continue
        other_id = (
            rel["to_claim_id"] if rel["from_claim_id"] == view.claim_id
            else rel["from_claim_id"]
        )
        other = get_claim_view(conn, other_id)
        print("  ⚠ 与已有主张疑似相关（LLM 建议仅供参考，由你拍板）：")
        print(f"    新候选: {redact_known_credentials(view.content)}")
        print(
            f"    已有主张 [{other.memory_type if other else '?'}]: "
            f"{redact_known_credentials(other.content) if other else other_id}"
        )
        print(f"    建议: {rel['relation_type']}")
        choice = _ask(
            "  裁决? [1]supersedes(取代旧事实) [2]coexists(并存) "
            "[3]拒绝新候选 [4]忽略建议 [Enter=稍后]: "
        )
        if choice == "1":
            decide_relation(conn, rel["relation_id"], "supersedes")
            view.review_status = "approved"
            view.lifecycle_status = "active"
        elif choice == "2":
            decide_relation(conn, rel["relation_id"], "coexists")
        elif choice == "3":
            decide_relation(conn, rel["relation_id"], "reject_new")
            view.review_status = "rejected"
        elif choice == "4":
            decide_relation(conn, rel["relation_id"], "dismiss")


def cmd_facts_review(args, cfg, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        if args.relation:
            if not args.decision:
                print("需要 --decision supersedes|coexists|reject_new|dismiss", file=sys.stderr)
                return 2
            decide_relation(conn, args.relation, args.decision)
            print(f"关系 {args.relation[:12]} 已裁决: {args.decision}")
            return 0
        if args.claim:
            claim_id = _resolve_claim_id(conn, args.claim)
            if args.action == "approve":
                approve_claim(conn, claim_id)
            elif args.action == "reject":
                reject_claim(conn, claim_id, note=args.note)
            elif args.action == "skip":
                skip_claim(conn, claim_id)
            elif args.action == "edit":
                if not args.content:
                    print("edit 需要 --content 提供修改后的措辞", file=sys.stderr)
                    return 2
                edit_claim(conn, claim_id, args.content)
            else:
                print(f"未知动作: {args.action}", file=sys.stderr)
                return 2
            print(f"claim {claim_id[:12]} → {args.action}")
            return 0

        # 交互式逐条审核
        queue = pending_claims(conn)
        needs_review = [v for v in queue if v.review_status == "needs_review"]
        print(
            f"待审核 {len(queue)} 条"
            + (f"（其中需复核 {len(needs_review)} 条）" if needs_review else "")
        )
        if not queue:
            return 0
        handled = 0
        for i, view in enumerate(queue, start=1):
            _print_claim(view, f"[{i}/{len(queue)}]")
            _decide_relations_interactive(conn, view, as_json)
            if view.review_status != "pending":
                handled += 1
                continue
            choice = _ask(
                "处理? [a]通过 [r]拒绝 [e]改写 [s]跳过 [q]退出: "
            )
            if choice in ("a", "y"):
                approve_claim(conn, view.claim_id)
                handled += 1
            elif choice == "r":
                reject_claim(conn, view.claim_id)
                handled += 1
            elif choice == "e":
                new_content = input("新的措辞: ").strip()
                if new_content:
                    edit_claim(conn, view.claim_id, new_content)
                    handled += 1
            elif choice == "q":
                break
            else:
                skip_claim(conn, view.claim_id)
                handled += 1
        print(f"本轮处理 {handled} 条；剩余队列下次运行 brain facts review 继续。")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# list / timeline / explain
# ---------------------------------------------------------------------------


def cmd_facts_list(args, cfg, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        rows = list_claims(conn, memory_type=args.type, status=args.status)
        if as_json:
            print(json.dumps(
                {"kind": "facts_list",
                 "claims": [vars(r) for r in rows]},
                ensure_ascii=False, indent=2, default=str,
            ))
            return 0
        print(f"共 {len(rows)} 条主张")
        for r in rows:
            flags = f"{r.memory_type}/{r.assertion_kind} {r.review_status}/{r.lifecycle_status}"
            superseded = f" ← 被 {r.superseded_by[:12]} 替代" if r.superseded_by else ""
            print(
                f"{r.claim_id[:12]} {flags} {r.asserted_at or '????-??-??'} "
                f"证据{r.evidence_count}{superseded}"
            )
            print(f"  {r.content}")
        return 0
    finally:
        conn.close()


def cmd_facts_timeline(args, cfg, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        rows = decision_timeline(
            conn, memory_type=args.type,
            include_superseded=args.include_superseded,
        )
        if as_json:
            print(json.dumps(
                {"kind": "facts_timeline", "type": args.type,
                 "items": [vars(r) for r in rows]},
                ensure_ascii=False, indent=2, default=str,
            ))
            return 0
        print(f"决策时间线（{args.type}，{len(rows)} 条）")
        for r in rows:
            when = (r.asserted_at or "时间未知")[:19]
            print(
                f"- {when}  {r.content}"
                f"  [claim {r.claim_id[:12]}，证据 {r.evidence_count} 条]"
            )
        if not rows:
            print("（空）")
        return 0
    finally:
        conn.close()


def cmd_facts_explain(args, cfg, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    try:
        claim_id = _resolve_claim_id(conn, args.claim_id)
        data = explain_claim(conn, claim_id)
        if data is None:
            print(f"主张不存在: {args.claim_id}", file=sys.stderr)
            return 4
        if as_json:
            print(json.dumps(
                {"kind": "facts_explain", **vars(data)},
                ensure_ascii=False, indent=2, default=str,
            ))
            return 0
        print(
            f"claim {data.claim_id[:12]} [{data.memory_type}/{data.assertion_kind}] "
            f"{data.review_status}/{data.lifecycle_status} "
            f"verification={data.verification_status}"
        )
        print(f"内容: {data.content}")
        if data.revisions:
            print("修订历史:")
            for rev in data.revisions:
                cur = "（当前）" if rev["is_current"] else ""
                print(
                    f"  - {rev['recorded_at'][:19]} {rev['review_status']}/"
                    f"{rev['lifecycle_status']} {cur} {rev['content']}"
                )
        print(f"证据（{len(data.evidence)} 条，原话逐字）:")
        for ev in data.evidence:
            when = (ev["source_created_at"] or "时间未知")[:19]
            print(
                f"  - {when} 对话 {ev['conversation_id'][:24]} ({ev['speaker_type']}) "
                f"rev={ev['revision_id'][:16]} span={ev['span'][0]}-{ev['span'][1]} "
                f"role={ev['role']} check={ev['support_check']}"
            )
            print(f"    locator: {ev['source_locator']}")
            print(f"    原话: 「{ev['quote']}」")
        for rel in data.relations:
            print(
                f"  关系 {rel['relation_type']} ({rel['status']}/{rel['origin']}) "
                f"{rel['from_claim_id'][:12]} → {rel['to_claim_id'][:12]}"
            )
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# export-profile
# ---------------------------------------------------------------------------


def cmd_facts_export_profile(args, cfg, as_json: bool) -> int:
    conn = connect(cfg.db_path)
    out_path = Path(args.out).expanduser()
    try:
        current, updated = build_profile_update(conn, out_path)
        if args.write:
            write_profile(out_path, updated)
            diff_stat = sum(
                1 for line in unified_profile_diff(current, updated, out_path).splitlines()
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            )
            payload = {
                "kind": "facts_profile_written",
                "path": str(out_path),
                "changed_lines": diff_stat,
            }
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"已写入 {out_path}（仅标记区间；改动行 ±{diff_stat}）")
            return 0
        diff = unified_profile_diff(current, updated, out_path)
        if as_json:
            print(json.dumps(
                {"kind": "facts_profile_draft", "path": str(out_path), "diff": diff},
                ensure_ascii=False, indent=2,
            ))
        else:
            if not diff:
                print("无变化：自动维护小节已是最新。")
            else:
                print(diff)
            print(f"（草稿未写入；确认后加 --write 写入 {out_path}）")
        return 0
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


__all__ = [
    "cmd_facts_extract",
    "cmd_facts_export_profile",
    "cmd_facts_explain",
    "cmd_facts_list",
    "cmd_facts_review",
    "cmd_facts_timeline",
]
