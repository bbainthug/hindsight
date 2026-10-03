"""D-2 集成：合成库 + 假 LLM 全流程（提炼 → 审核 → 时间线 → profile → 撤回复核）。

对应任务书验收：从合成对话提炼 → 审核通过 → timeline 与 export-profile 输出
正确；撤回某条源 revision 后，依赖它的主张标记为需复核（派生失效）。
"""

from __future__ import annotations

import json
from pathlib import Path

from facts_testkit import (
    FakeLLMClient,
    claim_of,
    import_conversations,
    linear_conversation,
    revision_id_of,
)
from synthetic import T0

from personal_brain.facts.pipeline import FactsConfig, run_extraction
from personal_brain.facts.profile_export import (
    MARKER_END,
    MARKER_START,
    build_profile_update,
    write_profile,
)
from personal_brain.facts.review import approve_claim, get_claim_view, pending_claims
from personal_brain.facts.views import decision_timeline, explain_claim
from personal_brain.policy.withdraw import withdraw_event


def _seed(db, importer) -> dict[str, str]:
    conv_a = linear_conversation(
        "conv-flow-a", "方向",
        [
            ("f1", "user", "我决定主攻 AI 测试开发方向。", T0),
            ("f2", "assistant", "好，聚焦。", T0 + 60),
        ],
    )
    conv_b = linear_conversation(
        "conv-flow-b", "偏好",
        [
            ("f3", "user", "写文档我偏好用 Markdown 纯文本。", T0 + 3600),
        ],
    )
    import_conversations(importer, [conv_a, conv_b])
    return {
        "r_decision": revision_id_of(db, "我决定主攻 AI 测试开发方向。"),
        "r_pref": revision_id_of(db, "写文档我偏好用 Markdown 纯文本。"),
    }


def _run(db, client, **over):
    cfg = FactsConfig()
    return run_extraction(
        db, cfg, client, since=None, until=None, account_namespace=None, **over
    )


def test_full_flow_extract_review_timeline_profile_withdraw(db, importer, tmp_path):
    revs = _seed(db, importer)
    # 分段按 conversation_id 排序：conv-flow-a 在前；每段各自给出候选
    client = FakeLLMClient([
        {
            "claims": [
                claim_of(
                    "决定主攻 AI 测试开发方向", "decision", "intention",
                    revs["r_decision"], "主攻 AI 测试开发方向",
                ),
                # 一条 assistant-only 的坏候选：应被整条拒绝
                claim_of(
                    "assistant 说聚焦（不能成为用户事实）", "decision", "intention",
                    _assistant_revision(db), "聚焦",
                ),
            ]
        },
        {
            "claims": [
                claim_of(
                    "偏好用 Markdown 写文档", "preference", "self_report",
                    revs["r_pref"], "偏好用 Markdown",
                ),
            ]
        },
    ])
    report = _run(db, client)
    assert report.candidates_created == 2
    assert report.rejections.get("assistant_only_evidence") == 1

    queue = pending_claims(db)
    assert len(queue) == 2
    by_content = {v.content: v for v in queue}

    # 审核通过决定 + 偏好
    approve_claim(db, by_content["决定主攻 AI 测试开发方向"].claim_id)
    approve_claim(db, by_content["偏好用 Markdown 写文档"].claim_id)

    # 决策时间线：能看到决定，且能点回原话（用户故事 2/4）
    timeline = decision_timeline(db)
    assert [row.content for row in timeline] == ["决定主攻 AI 测试开发方向"]
    row = timeline[0]
    assert row.evidence_count == 1
    explain = explain_claim(db, row.claim_id)
    assert explain is not None
    assert len(explain.evidence) == 1
    ev = explain.evidence[0]
    assert ev["revision_id"] == revs["r_decision"]
    assert "主攻 AI 测试开发方向" in ev["quote"]
    assert ev["source_created_at"] is not None

    # profile 草稿：决策进入草稿；写入后只动标记区间（用户故事 5）
    target = tmp_path / "profile.md"
    target.write_text(
        "# 档案\n手写内容不可动。\n"
        f"{MARKER_START}\n过期内容\n{MARKER_END}\n尾注。\n",
        encoding="utf-8",
    )
    _current, updated = build_profile_update(db, target)
    assert "决定主攻 AI 测试开发方向" in updated
    assert "手写内容不可动。" in updated
    assert "过期内容" not in updated
    write_profile(target, updated)
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# 档案\n手写内容不可动。")
    assert text.index(MARKER_START) < text.index("决定主攻 AI 测试开发方向")

    # 撤回决定依据的源事件 → 主张进复核队列，时间线不再显示（派生失效）
    event_id = db.execute(
        "SELECT event_id FROM event_revisions WHERE revision_id = ?",
        (revs["r_decision"],),
    ).fetchone()["event_id"]
    withdraw_event(db, event_id)
    view = get_claim_view(db, row.claim_id)
    assert view.review_status == "needs_review"
    assert decision_timeline(db) == []
    assert [v.claim_id for v in pending_claims(db)] == [row.claim_id]


def _assistant_revision(db) -> str:
    return db.execute(
        "SELECT revision_id FROM event_revisions WHERE raw_text = '好，聚焦。'"
    ).fetchone()["revision_id"]


# ---------------------------------------------------------------------------
# CLI 端到端（子命令走 argparse 主入口）
# ---------------------------------------------------------------------------


def _cli(db_path: Path, *argv: str, env_key: str | None = None):
    from personal_brain.cli import main as cli_main

    args = ["--db", str(db_path), "facts", *argv]
    return cli_main(args)


def test_cli_dry_run_prints_estimate_without_writing(db, importer, capsys):
    _seed(db, importer)
    db_path = db.execute("SELECT 1").connection.execute(
        "SELECT file FROM pragma_database_list WHERE name='main'"
    ).fetchone()[0]
    code = _cli(
        Path(db_path), "extract", "--dry-run",
        "--since", "2026-07-31", "--until", "2026-08-02",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "预计请求" in out and "预算" in out
    # dry-run 不写任何主张/批次
    assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 0


def test_cli_dry_run_uses_readonly_connection(db, importer, capsys, monkeypatch):
    _seed(db, importer)
    db_path = db.execute(
        "SELECT file FROM pragma_database_list WHERE name='main'"
    ).fetchone()[0]
    from personal_brain.facts import cli as facts_cli
    from personal_brain.history.db import connect_readonly

    opened: list[str] = []

    def readonly(path):
        opened.append(str(path))
        return connect_readonly(path)

    def writable(_path):
        raise AssertionError("dry-run must not open a writable database connection")

    monkeypatch.setattr(facts_cli, "connect_readonly", readonly)
    monkeypatch.setattr(facts_cli, "connect", writable)
    assert _cli(
        Path(db_path), "extract", "--dry-run", "--since", "2026-07-31"
    ) == 0
    capsys.readouterr()
    assert opened == [db_path]


def test_cli_dry_run_over_budget_exits_3(db, importer, capsys, monkeypatch):
    _seed(db, importer)
    monkeypatch.setattr(
        "personal_brain.facts.pipeline.FactsConfig.budget_tokens", 1
    )
    db_path = db.execute(
        "SELECT file FROM pragma_database_list WHERE name='main'"
    ).fetchone()[0]
    code = _cli(
        Path(db_path), "extract", "--dry-run",
        "--since", "2026-07-31", "--until", "2026-08-02", "--budget", "1",
    )
    assert code == 3


def test_cli_dry_run_zero_budget_is_not_ignored(db, importer):
    _seed(db, importer)
    db_path = Path(
        db.execute("SELECT file FROM pragma_database_list WHERE name='main'")
        .fetchone()[0]
    )
    code = _cli(
        db_path, "extract", "--dry-run", "--budget", "0",
        "--since", "2026-07-31", "--until", "2026-08-02",
    )
    assert code == 3


def test_formal_cli_budget_gate_runs_before_api_key_lookup(db, importer, monkeypatch):
    _seed(db, importer)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db_path = Path(
        db.execute("SELECT file FROM pragma_database_list WHERE name='main'")
        .fetchone()[0]
    )
    code = _cli(
        db_path, "extract", "--budget", "0",
        "--since", "2026-07-31", "--until", "2026-08-02",
    )
    assert code == 3
    assert db.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 0


def test_formal_cli_empty_window_needs_no_api_key(db, capsys, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db_path = Path(
        db.execute("SELECT file FROM pragma_database_list WHERE name='main'")
        .fetchone()[0]
    )
    code = _cli(db_path, "extract", "--since", "2035-01-01")
    assert code == 0
    assert "未调用模型" in capsys.readouterr().out
    assert db.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 0


def test_cli_extract_llm_unreachable_marks_run_failed(db, importer, monkeypatch, capsys):
    _seed(db, importer)
    db_path = Path(
        db.execute("SELECT file FROM pragma_database_list WHERE name='main'")
        .fetchone()[0]
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    code = _cli(
        db_path, "extract",
        "--since", "2026-07-31", "--until", "2026-08-02",
        "--base-url", "http://127.0.0.1:9",  # 不可达端口，不出网
    )
    assert code == 2
    row = db.execute("SELECT status FROM extraction_runs").fetchone()
    assert row["status"] == "failed"
    assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_cli_review_list_timeline_explain_export(db, importer, tmp_path, capsys):
    revs = _seed(db, importer)
    client = FakeLLMClient([
        {
            "claims": [
                claim_of(
                    "决定主攻 AI 测试开发方向", "decision", "intention",
                    revs["r_decision"], "主攻 AI 测试开发方向",
                ),
            ]
        }
    ])
    _run(db, client)
    claim_id = pending_claims(db)[0].claim_id
    db_path = Path(
        db.execute("SELECT file FROM pragma_database_list WHERE name='main'")
        .fetchone()[0]
    )

    assert _cli(db_path, "review", "--claim", claim_id[:12], "--action", "approve") == 0
    assert _cli(db_path, "list", "--status", "active") == 0
    assert "决定主攻" in capsys.readouterr().out

    assert _cli(db_path, "timeline") == 0
    timeline_out = capsys.readouterr().out
    assert "决策时间线" in timeline_out and "决定主攻" in timeline_out

    assert _cli(db_path, "explain", claim_id[:12]) == 0
    explain_out = capsys.readouterr().out
    assert "主攻 AI 测试开发方向" in explain_out  # 原话可见（点回原话）

    target = tmp_path / "cli-profile.md"
    assert _cli(db_path, "export-profile", "--out", str(target)) == 0
    diff_out = capsys.readouterr().out
    assert "--write" in diff_out  # 草稿默认不写入
    assert not target.exists()
    assert _cli(db_path, "export-profile", "--out", str(target), "--write") == 0
    assert target.exists()
    assert "决定主攻" in target.read_text(encoding="utf-8")


def test_cli_json_output_shapes(db, importer, capsys):
    revs = _seed(db, importer)
    client = FakeLLMClient([
        {"claims": [
            claim_of("决定主攻 AI 测试开发方向", "decision", "intention",
                     revs["r_decision"], "主攻 AI 测试开发方向"),
        ]}
    ])
    _run(db, client)
    db_path = Path(
        db.execute("SELECT file FROM pragma_database_list WHERE name='main'")
        .fetchone()[0]
    )
    from personal_brain.cli import main as cli_main

    code = cli_main(["--db", str(db_path), "--json", "facts", "list"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "facts_list"
    assert payload["claims"][0]["content"] == "决定主攻 AI 测试开发方向"
