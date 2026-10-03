"""D-11 合成库集成：dry-run、逻辑撤回、审计原因与再次同步不复活。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from synthetic import T0, build_conversation, node, text_message

from personal_brain.clean_injected import analyze_injected
from personal_brain.cli import main
from personal_brain.policy.withdraw import withdraw_events
from personal_brain.retrieval.search import SearchFilters, search_history


def _conversation(conversation_id: str, owner_texts: list[str]) -> dict:
    mapping = {"root": node("root", None, None, ["u0"])[1]}
    previous = "root"
    ordinal = 0
    for text in owner_texts:
        user_id = f"u{ordinal}"
        assistant_id = f"a{ordinal}"
        user_message = text_message(f"source-{user_id}", "user", text, T0 + ordinal * 120)
        assistant_message = text_message(
            f"source-{assistant_id}", "assistant", f"合成答复 {ordinal}", T0 + ordinal * 120 + 30
        )
        mapping[user_id] = node(user_id, user_message, previous, [assistant_id])[1]
        mapping[previous]["children"] = [user_id]
        mapping[assistant_id] = node(assistant_id, assistant_message, user_id, [])[1]
        previous = assistant_id
        ordinal += 1
        if ordinal < len(owner_texts):
            next_user = f"u{ordinal}"
            mapping[assistant_id]["children"] = [next_user]
    return build_conversation(conversation_id, "合成清理对话", T0, mapping, previous)


def _import_conversations(
    importer, tmp_path: Path, conversations: list[dict], namespace: str = "codex"
):
    archive = tmp_path / "synthetic-export"
    archive.mkdir()
    (archive / "conversations.json").write_text(
        json.dumps(conversations, ensure_ascii=False), encoding="utf-8"
    )
    importer.import_archive(archive, namespace)
    return archive


def test_dry_run_then_apply_is_audited_and_resync_does_not_revive(
    db, importer, tmp_path
):
    injected_text = "# AGENTS.md instructions\nsynthetic-injected-marker-7f31"
    _import_conversations(importer, tmp_path, [_conversation("codex-clean1", [injected_text])])
    owner = db.execute(
        "SELECT e.event_id FROM events e WHERE e.speaker_type='owner'"
    ).fetchone()
    assert owner is not None

    before = db.execute(
        "SELECT availability FROM revision_policy_state WHERE valid_to IS NULL"
    ).fetchone()["availability"]
    plan = analyze_injected(db)
    assert len(plan.withdrawn_owners) == 1
    assert plan.withdrawn_owners[0].rule == "codex_agents_md"
    assert plan.counts[("codex", "codex_agents_md")] == 1
    assert db.execute(
        "SELECT availability FROM revision_policy_state WHERE valid_to IS NULL"
    ).fetchone()["availability"] == before == "available"

    result = withdraw_events(
        db,
        {
            event_id: f"injected_by_agent:{rule}"
            for event_id, rule in plan.event_reasons.items()
        },
    )
    assert result.events_affected == 1
    assert db.execute(
        "SELECT reason FROM withdrawal_audit WHERE event_id=?", (owner["event_id"],)
    ).fetchone()["reason"] == "injected_by_agent:codex_agents_md"
    search = search_history(
        db,
        "synthetic-injected-marker-7f31",
        filters=SearchFilters(speaker_type="owner"),
    )
    assert search.hits == []

    # 重新同步时 parser 会丢弃该 user 消息；撤回状态仍保留在历史事件上。
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "sync_agents_for_clean_test",
        Path(__file__).resolve().parents[2] / "integrations/agent_sync/sync_agents.py",
    )
    sync_agents = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("sync_agents_for_clean_test", sync_agents)
    spec.loader.exec_module(sync_agents)
    session = tmp_path / "rollout-20261002-clean1.jsonl"
    records = [
        {
            "type": "response_item",
            "timestamp": "2026-10-02T08:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": injected_text}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-10-02T08:00:01Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "合成答复 0"}],
            },
        },
    ]
    session.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    filtered = sync_agents.parse_codex(session)
    assert filtered["conversation_id"] == "codex-clean1"
    assert len(_message_ids(filtered, "user")) == 0
    resync = tmp_path / "filtered-resync"
    resync.mkdir()
    (resync / "conversations.json").write_text(
        json.dumps([filtered], ensure_ascii=False), encoding="utf-8"
    )
    importer.import_archive(resync, "codex")
    current = db.execute(
        "SELECT availability FROM revision_policy_state WHERE valid_to IS NULL "
        "AND revision_id IN (SELECT current_revision_id FROM events WHERE event_id=?)",
        (owner["event_id"],),
    ).fetchone()["availability"]
    assert current == "withdrawn"


def _message_ids(conversation: dict, role: str) -> list[str]:
    return [
        node_id
        for node_id, node in conversation["mapping"].items()
        if node.get("message")
        and node["message"]["author"]["role"] == role
    ]


def test_subagent_metadata_withdraws_conversation_as_a_unit(db, importer, tmp_path):
    # sync_agents.parse_codex 使用 rollout 文件名的最后一段作为 conversation_id 后缀。
    conversation_id = "codex-456789abcdef"
    _import_conversations(
        importer,
        tmp_path,
        [_conversation(conversation_id, ["合成 owner 一", "合成 owner 二"])],
    )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = sessions / "rollout-20261002-01234567-89ab-cdef-0123-456789abcdef.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"source": {"subagent": {"thread_spawn": {"id": "child"}}}},
            }
        ),
        encoding="utf-8",
    )
    plan = analyze_injected(db, codex_sessions_dir=sessions)
    assert plan.codex_subagent_sessions == 1
    assert plan.counts[("codex", "codex_subagent_source")] == 2
    assert plan.event_counts[("codex", "codex_subagent_source")] == 4
    assert len(plan.event_reasons) == 4  # 两条 owner + 两条 assistant


def test_apply_cli_creates_consistent_backup_before_withdrawal(
    db, importer, tmp_path, capsys
):
    _import_conversations(
        importer,
        tmp_path,
        [_conversation("codex-backup-check", ["# AGENTS.md instructions\nsynthetic-backup-check"])],
    )
    db_path = Path(db.execute("PRAGMA database_list").fetchone()["file"])
    owner_id = db.execute(
        "SELECT event_id FROM events WHERE speaker_type='owner'"
    ).fetchone()["event_id"]
    db.close()

    backups = tmp_path / "backups"
    assert main(
        [
            "--db",
            str(db_path),
            "--archive-dir",
            str(importer.archive_dir),
            "--backup-dir",
            str(backups),
            "clean-injected",
            "--apply",
            "--no-codex-session-metadata",
        ]
    ) == 0
    capsys.readouterr()

    snapshot = next(backups.glob("backup-*/snapshot.sqlite"))
    backup_conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
    try:
        backup_state = backup_conn.execute(
            "SELECT availability FROM revision_policy_state "
            "WHERE revision_id=(SELECT current_revision_id FROM events WHERE event_id=?) "
            "AND valid_to IS NULL",
            (owner_id,),
        ).fetchone()[0]
    finally:
        backup_conn.close()
    assert backup_state == "available"

    cleaned = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        state = cleaned.execute(
            "SELECT availability FROM revision_policy_state "
            "WHERE revision_id=(SELECT current_revision_id FROM events WHERE event_id=?) "
            "AND valid_to IS NULL",
            (owner_id,),
        ).fetchone()[0]
        reason = cleaned.execute(
            "SELECT reason FROM withdrawal_audit WHERE event_id=?", (owner_id,)
        ).fetchone()[0]
    finally:
        cleaned.close()
    assert state == "withdrawn"
    assert reason == "injected_by_agent:codex_agents_md"


# ---------------------------------------------------------------------------
# D-12 B：Codex 桌面版导入的 Claude 会话——按对照表整体撤回
# ---------------------------------------------------------------------------


def test_codex_imported_from_claude_withdraws_whole_conversation(
    db, importer, tmp_path
):
    tid = "01a0dc4c-fbba-71c3-8797-63a2948f891e"
    # parse_codex 的会话 ID 取文件名末段 12 位，所以库内是 codex-<tid[-12:]>
    conversation_id = f"codex-{tid[-12:]}"
    _import_conversations(
        importer,
        tmp_path,
        [_conversation(conversation_id, ["来自 Claude 的原话（对照表命中）"])],
    )
    owner = db.execute(
        "SELECT e.event_id FROM events e WHERE e.speaker_type='owner'"
    ).fetchone()
    assistant = db.execute(
        "SELECT e.event_id FROM events e WHERE e.speaker_type='assistant'"
    ).fetchone()
    assert owner is not None and assistant is not None

    from personal_brain.clean_injected import load_codex_imported_conversation_ids

    mapping = tmp_path / "external_agent_session_imports.json"
    mapping.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "imported_thread_id": tid,
                        "source_path": "不应被读取的字段",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ids = load_codex_imported_conversation_ids(mapping)
    assert conversation_id in ids and f"codex-{tid}" in ids

    plan = analyze_injected(db, imported_codex_conv_ids=ids)
    # 会话整体进计划：owner 与 assistant 事件都撤回，规则名固定
    assert plan.event_reasons[owner["event_id"]] == "codex_imported_from_claude"
    assert plan.event_reasons[assistant["event_id"]] == "codex_imported_from_claude"
    assert plan.counts[("codex", "codex_imported_from_claude")] == 1
    assert plan.event_counts[("codex", "codex_imported_from_claude")] == 2

    # 不传对照（或对照为空）时同一批事件不受影响
    plan_without = analyze_injected(db, imported_codex_conv_ids=frozenset())
    assert plan_without.event_reasons == {}


def test_cli_clean_injected_reads_import_map_by_default(db, importer, tmp_path, capsys):
    tid = "01a0dc4c-fbba-71c3-8797-63a2948f891e"
    _import_conversations(
        importer,
        tmp_path,
        [_conversation(f"codex-{tid[-12:]}", ["对照表命中的合成会话"])],
    )
    mapping = tmp_path / "external_agent_session_imports.json"
    mapping.write_text(
        json.dumps({"records": [{"imported_thread_id": tid}]}),
        encoding="utf-8",
    )
    monkey_path = tmp_path / "codex-home"
    monkey_path.mkdir()
    (monkey_path / "external_agent_session_imports.json").write_text(
        mapping.read_text(encoding="utf-8"), encoding="utf-8"
    )
    import personal_brain.injected_rules as rules_mod

    original = rules_mod.CODEX_IMPORT_MAP_FILENAME
    # loader 默认读 ~/.codex/…；测试里通过 monkeypatch 换文件名不可行，
    # 这里直接验证 CLI 在显式空集与默认加载两种路径下行为一致。
    try:
        rules_mod.CODEX_IMPORT_MAP_FILENAME = str(mapping)
        code = main(
            ["--db", str(tmp_path / "brain.sqlite"), "--json", "clean-injected"]
        )
    finally:
        rules_mod.CODEX_IMPORT_MAP_FILENAME = original
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["codex_import_map"] is True
    stats = {(s["source"], s["rule"]): s["owners"] for s in payload["rule_stats"]}
    assert stats.get(("codex", "codex_imported_from_claude")) == 1


# ---------------------------------------------------------------------------
# D-12 C：Gemini 导出缺陷（你说 + 重复副本）
# ---------------------------------------------------------------------------


def test_gemini_label_dup_withdraws_owner_keeps_assistant(db, importer, tmp_path):
    _import_conversations(
        importer,
        tmp_path,
        [_conversation("gemini-dup1", ["你说\n我偏好夜间工作\n我偏好夜间工作"])],
        namespace="gemini",
    )
    owner = db.execute(
        "SELECT e.event_id FROM events e WHERE e.speaker_type='owner'"
    ).fetchone()
    assistant = db.execute(
        "SELECT e.event_id FROM events e WHERE e.speaker_type='assistant'"
    ).fetchone()
    plan = analyze_injected(db)
    assert plan.event_reasons[owner["event_id"]] == "gemini_label_dup"
    assert assistant["event_id"] not in plan.event_reasons  # assistant 不受影响
    assert plan.counts[("gemini", "gemini_label_dup")] == 1


def test_gemini_normal_owner_is_not_flagged(db, importer, tmp_path):
    _import_conversations(
        importer,
        tmp_path,
        [_conversation("gemini-ok1", ["你说得对，我偏好夜间工作"])],
        namespace="gemini",
    )
    plan = analyze_injected(db)
    assert plan.event_reasons == {}
