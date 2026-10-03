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
    metadata_rule,
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
