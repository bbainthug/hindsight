"""D-11: 规则表中的每条规则必须有正例与反例。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from personal_brain.injected_rules import (
    RULES,
    clean_user_text,
    codex_imported_conversation_ids,
    codex_imported_from_claude,
    gemini_label_dup_rule,
    load_imported_thread_ids,
    metadata_rule,
    normalize_gemini_user_text,
    prefix_rule,
    strip_leading_blocks,
    text_rule,
)

_SPEC = importlib.util.spec_from_file_location(
    "sync_agents",
    Path(__file__).resolve().parents[2] / "integrations/agent_sync/sync_agents.py",
)
sync_agents = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("sync_agents", sync_agents)
_SPEC.loader.exec_module(sync_agents)


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.name)
def test_every_rule_has_a_positive_and_negative_example(rule):
    source = rule.sources[0]
    if rule.kind == "session_metadata":
        assert metadata_rule(source, rule.positive).name == rule.name
        assert metadata_rule(source, rule.negative) is None
    elif rule.kind == "import_map":
        # 对照表规则：正例（导入线程）两种会话 ID 形态都命中，反例不命中
        ids = codex_imported_conversation_ids(frozenset({rule.positive}))
        assert f"codex-{rule.positive}" in ids
        assert f"codex-{rule.positive[-12:]}" in ids
        assert f"codex-{rule.negative}" not in ids
        assert codex_imported_from_claude.name == rule.name
    elif rule.kind == "normalize":
        assert gemini_label_dup_rule(rule.positive) is rule
        assert gemini_label_dup_rule(rule.negative) is None
    elif rule.kind == "prefix":
        assert prefix_rule(source, rule.positive).name == rule.name
        assert prefix_rule(source, rule.negative) is None
    else:
        cleaned, removed = strip_leading_blocks(source, rule.positive)
        assert cleaned == ""
        assert rule.name in removed
        assert strip_leading_blocks(source, rule.negative) == (rule.negative, ())
        assert text_rule(source, rule.negative) is None


def _write_codex(path: Path, user_text: str, *, source=None) -> None:
    records = []
    if source is not None:
        records.append({"type": "session_meta", "payload": {"source": source}})
    records.extend(
        [
            {
                "type": "response_item",
                "timestamp": "2026-10-02T08:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": user_text}],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-10-02T08:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "合成答复"}],
                },
            },
        ]
    )
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _message_text(conversation: dict, role: str) -> list[str]:
    return [
        node["message"]["content"]["parts"][0]
        for node in conversation["mapping"].values()
        if node.get("message")
        and node["message"]["author"]["role"] == role
    ]


@pytest.mark.parametrize(
    "subagent",
    [{"other": "guardian"}, {"thread_spawn": {"id": "synthetic-child"}}],
    ids=["guardian", "thread_spawn"],
)
def test_codex_subagent_metadata_skips_whole_conversation(tmp_path, subagent):
    path = tmp_path / "rollout-20261002-01234567-89ab-cdef-0123-456789abcdef.jsonl"
    _write_codex(path, "父 agent 下发的合成任务", source={"subagent": subagent})
    assert sync_agents.parse_codex(path) == {}


def test_codex_injected_prefix_skips_guardian_session(tmp_path):
    path = tmp_path / "rollout-20261002-01234567-89ab-cdef-0123-456789abcdef.jsonl"
    _write_codex(path, "The following is the Codex agent history\n合成内容")
    assert sync_agents.parse_codex(path) == {}


def test_codex_keeps_long_user_log_code_and_job_description(tmp_path):
    path = tmp_path / "rollout-20261002-01234567-89ab-cdef-0123-456789abcdef.jsonl"
    user_text = "我粘贴的日志和 JD：" + ("Python 测试开发岗位代码与日志。" * 4000)
    _write_codex(path, user_text, source={"cli": "codex"})
    parsed = sync_agents.parse_codex(path)
    assert _message_text(parsed, "user") == [user_text]


def test_codex_drops_agents_prefix_but_keeps_assistant_reply(tmp_path):
    path = tmp_path / "rollout-20261002-01234567-89ab-cdef-0123-456789abcdef.jsonl"
    _write_codex(path, "# AGENTS.md instructions\n合成注入指令")
    parsed = sync_agents.parse_codex(path)
    assert _message_text(parsed, "user") == []
    assert _message_text(parsed, "assistant") == ["合成答复"]


def test_claude_strips_only_leading_system_block_and_keeps_user_text(tmp_path):
    path = tmp_path / "synthetic-session.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "synthetic-session",
            "uuid": "u1",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<system-reminder>internal</system-reminder>"},
                    {"type": "text", "text": "请分析这个合成问题"},
                ],
            },
        }
    ]
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    parsed = sync_agents.parse_claude(path)
    assert _message_text(parsed, "user") == ["请分析这个合成问题"]


def test_claude_skips_empty_system_block_and_meta_summary(tmp_path):
    path = tmp_path / "synthetic-session.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "synthetic-session",
            "uuid": "u1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<local-command-stdout>done</local-command-stdout>",
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": "synthetic-session",
            "uuid": "u2",
            "isMeta": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "/compact"}]},
        },
        {
            "type": "user",
            "sessionId": "synthetic-session",
            "uuid": "u3",
            "isCompactSummary": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "摘要"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    assert sync_agents.parse_claude(path) == {}


def test_tag_block_is_not_removed_from_middle_of_a_user_message():
    text = "我在文档里引用 <environment_context> 这个未闭合标签，请保留。"
    assert clean_user_text("codex", text) == (text, None)


def test_mixed_tag_block_removes_injection_and_keeps_user_suffix():
    assert clean_user_text(
        "dsh", "<system-reminder>内部提示</system-reminder>\n这是我的问题"
    ) == ("这是我的问题", None)


# ---------------------------------------------------------------------------
# D-12 A：Claude Code 后台任务通知
# ---------------------------------------------------------------------------


def test_claude_task_notification_block_is_dropped_entirely():
    text = "<task-notification>Bash: pytest 45 passed</task-notification>"
    assert clean_user_text("claude", text) == (None, text_rule("claude", text))
    assert text_rule("claude", text).name == "claude_task_notification_block"


def test_claude_task_notification_keeps_trailing_user_text():
    cleaned, rule = clean_user_text(
        "claude",
        "<task-notification>后台任务完成</task-notification>\n继续刚才的话题",
    )
    assert cleaned == "继续刚才的话题"
    assert rule is None


def test_task_notification_mention_in_middle_is_kept():
    text = "帮我写个脚本解析 task-notification 通知"
    assert clean_user_text("claude", text) == (text, None)


# ---------------------------------------------------------------------------
# D-12 B：Codex 导入对照表
# ---------------------------------------------------------------------------


def test_load_imported_thread_ids_reads_only_that_field(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "imported_thread_id": "01a0dc4c-fbba-71c3-8797-63a2948f891e",
                        "source_path": "/Users/x/.claude/projects/p/s1.jsonl",
                        "title": "不应被读取的字段",
                        "content_sha256": "de-ad-beef",
                    },
                    {"imported_thread_id": None},  # 非字符串：忽略
                    "not-a-dict",  # 坏记录：忽略
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_imported_thread_ids(path) == frozenset(
        {"01a0dc4c-fbba-71c3-8797-63a2948f891e"}
    )


@pytest.mark.parametrize(
    "content",
    ["", "not json", '{"records": "wrong-type"}', '{"other": 1}', "[]"],
)
def test_load_imported_thread_ids_tolerates_broken_files(tmp_path, content):
    path = tmp_path / "map.json"
    path.write_text(content, encoding="utf-8")
    assert load_imported_thread_ids(path) == frozenset()


def test_load_imported_thread_ids_missing_file(tmp_path):
    assert load_imported_thread_ids(tmp_path / "nope.json") == frozenset()


def test_parse_codex_skips_imported_claude_session(tmp_path, monkeypatch):
    tid = "01a0dc4c-fbba-71c3-8797-63a2948f891e"
    monkeypatch.setattr(sync_agents, "_imported_thread_ids", frozenset({tid}))
    path = tmp_path / f"rollout-2026-09-26T14-00-27-{tid}.jsonl"
    _write_codex(path, "这是被 Codex 导入的 Claude 会话内容")
    assert sync_agents.parse_codex(path) == {}


def test_parse_codex_keeps_session_not_in_map(tmp_path, monkeypatch):
    tid = "01a0dc4c-fbba-71c3-8797-63a2948f891e"
    monkeypatch.setattr(sync_agents, "_imported_thread_ids", frozenset({tid}))
    other = "019d7be0-2685-7e32-9cb4-cd9560cf7be6"
    path = tmp_path / f"rollout-2026-04-11T17-29-40-{other}.jsonl"
    _write_codex(path, "普通 Codex 会话：我想讨论简历", source={"cli": "codex"})
    parsed = sync_agents.parse_codex(path)
    assert _message_text(parsed, "user") == ["普通 Codex 会话：我想讨论简历"]


# ---------------------------------------------------------------------------
# D-12 C：Gemini 读屏标签与重复副本
# ---------------------------------------------------------------------------


def test_gemini_normalizes_label_with_duplicated_copy():
    assert normalize_gemini_user_text("你说\n我偏好夜间工作\n我偏好夜间工作") == "我偏好夜间工作"
    assert normalize_gemini_user_text("你说 我偏好夜间工作\n我偏好夜间工作") == "我偏好夜间工作"
    assert normalize_gemini_user_text("你说我偏好夜间工作\n我偏好夜间工作") == "我偏好夜间工作"
    assert normalize_gemini_user_text("你说：\n我偏好夜间工作\n我偏好夜间工作") == "我偏好夜间工作"


def test_gemini_label_stripped_even_without_duplicate():
    # 标签独占一行时无条件剥掉（缺陷形态之一），正文保留
    assert normalize_gemini_user_text("你说\n我偏好夜间工作") == "我偏好夜间工作"


def test_gemini_normalizer_does_not_touch_normal_text():
    assert normalize_gemini_user_text("你说得对，我偏好夜间工作") == "你说得对，我偏好夜间工作"
    no_dup = "我偏好夜间工作\n我偏好夜间工作"
    assert normalize_gemini_user_text(no_dup) == no_dup  # 无标签：重复行也不删
    assert normalize_gemini_user_text("讨论：你说 该方案可行") == "讨论：你说 该方案可行"
    assert normalize_gemini_user_text("你说") == ""
    assert normalize_gemini_user_text("") == ""


def test_gemini_label_dup_rule_flags_only_defect():
    assert gemini_label_dup_rule("你说\nX\nX") is not None
    assert gemini_label_dup_rule("你说\nX") is not None
    assert gemini_label_dup_rule("你说得对") is None
    assert gemini_label_dup_rule("X\nX") is None
