"""集中维护 agent 注入识别规则，供同步解析器与存量清理共用。

规则只使用任务书列出的结构、固定前缀和显式注入标签；不按长度、内容类型
或“看起来像 AI 写的”判断。标记出现在普通正文中时，不触发前缀规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InjectionRule:
    name: str
    kind: str  # session_metadata / prefix / tag
    sources: tuple[str, ...]
    action: str  # skip_session / skip_message / strip_block
    note: str
    positive: Any
    negative: Any
    prefixes: tuple[str, ...] = ()
    tag: str | None = None
    metadata_path: tuple[str, ...] = ()
    metadata_truthy: bool = True


# D-11 任务书中的明确注入特征。正反例与规则放在一起，测试遍历整张表，
# 每条规则都必须同时证明会命中注入内容、不会命中近似的普通用户内容。
RULES: tuple[InjectionRule, ...] = (
    InjectionRule(
        name="codex_subagent_source",
        kind="session_metadata",
        sources=("codex",),
        action="skip_session",
        note="Codex session_meta.payload.source.subagent；guardian / thread_spawn 等子代理会话。",
        positive={"source": {"subagent": {"other": "guardian"}}},
        negative={"source": {"cli": "codex"}},
        metadata_path=("source", "subagent"),
    ),
    InjectionRule(
        name="claude_sidechain",
        kind="session_metadata",
        sources=("claude",),
        action="skip_session",
        note="Claude Code JSONL 的 isSidechain 子代理线程标记。",
        positive={"isSidechain": True},
        negative={"isSidechain": False},
        metadata_path=("isSidechain",),
    ),
    InjectionRule(
        name="claude_is_meta",
        kind="session_metadata",
        sources=("claude",),
        action="skip_message",
        note="Claude Code JSONL 的 isMeta 系统消息标记。",
        positive={"isMeta": True},
        negative={"isMeta": False},
        metadata_path=("isMeta",),
    ),
    InjectionRule(
        name="claude_compact_summary_flag",
        kind="session_metadata",
        sources=("claude",),
        action="skip_message",
        note="Claude Code JSONL 的 isCompactSummary 压缩续写摘要标记。",
        positive={"isCompactSummary": True},
        negative={"isCompactSummary": False},
        metadata_path=("isCompactSummary",),
    ),
    InjectionRule(
        name="codex_guardian_history",
        kind="prefix",
        sources=("codex",),
        action="skip_session",
        note="Codex guardian 审批子会话首条注入的 agent history 标题。",
        positive="The following is the Codex agent history\n",
        negative="用户粘贴的日志：The following is the Codex agent history",
        prefixes=("The following is the Codex agent history",),
    ),
    InjectionRule(
        name="codex_agents_md",
        kind="prefix",
        sources=("codex",),
        action="skip_message",
        note="任务书统计到的 AGENTS.md 自动注入标题。",
        positive="# AGENTS.md instructions\n",
        negative="我想讨论 # AGENTS.md instructions 这段文本",
        prefixes=("# AGENTS.md instructions",),
    ),
    InjectionRule(
        name="continued_conversation_summary",
        kind="prefix",
        sources=("codex", "claude", "dsh"),
        action="skip_message",
        note="任务书列出的上下文压缩续写摘要固定开头。",
        positive="This session is being continued from a previous conversation",
        negative="这是我粘贴的日志：This session is being continued from a previous conversation",
        prefixes=("This session is being continued from a previous conversation",),
    ),
    InjectionRule(
        name="codex_turn_aborted",
        kind="prefix",
        sources=("codex",),
        action="skip_message",
        note="Codex 中断提示标记。",
        positive="<turn_aborted>\nThe turn was interrupted.",
        negative="代码示例中的标记 <turn_aborted>，请解释它",
        prefixes=("<turn_aborted>",),
    ),
    InjectionRule(
        name="claude_caveat",
        kind="prefix",
        sources=("claude",),
        action="skip_message",
        note="任务书提到的 Claude Code 系统 Caveat 提示固定开头。",
        positive="Caveat: the following content is system generated",
        negative="Caveat: 是我写在 JD 里的小标题，后面是岗位要求",
        prefixes=("Caveat: the following content is system generated",),
    ),
    InjectionRule(
        name="dsh_runtime_context",
        kind="prefix",
        sources=("dsh",),
        action="skip_message",
        note="任务书列出的 DSH Current runtime context 注入段。",
        positive="Current runtime context\n",
        negative="文档里提到 Current runtime context 这个术语",
        prefixes=("Current runtime context",),
    ),
    InjectionRule(
        name="dsh_skills_preamble",
        kind="prefix",
        sources=("dsh",),
        action="skip_message",
        note="DSH 自动技能清单固定导语；不用通用 Skills 标题避免误删用户文档。",
        positive="The following skills are available in this session:\n",
        negative="我在简历里写了 The following skills are available in this session: Python",
        prefixes=("The following skills are available in this session:",),
    ),
    InjectionRule(
        name="system_reminder_block",
        kind="tag",
        sources=("claude", "dsh"),
        action="strip_block",
        note="Claude Code / DSH 的 system-reminder 系统块。",
        positive="<system-reminder>Internal reminder</system-reminder>",
        negative="用户询问 system-reminder 标签的含义",
        tag="system-reminder",
    ),
    InjectionRule(
        name="command_name_block",
        kind="tag",
        sources=("claude",),
        action="strip_block",
        note="Claude Code 的 command-name 元数据块。",
        positive="<command-name>/compact</command-name>",
        negative="请搜索 command-name 标签的文档",
        tag="command-name",
    ),
    InjectionRule(
        name="command_message_block",
        kind="tag",
        sources=("claude",),
        action="strip_block",
        note="Claude Code 的 command-message 元数据块。",
        positive="<command-message>compact</command-message>",
        negative="我写的代码变量名是 command-message",
        tag="command-message",
    ),
    InjectionRule(
        name="command_args_block",
        kind="tag",
        sources=("claude",),
        action="strip_block",
        note="Claude Code 的 command-args 元数据块。",
        positive="<command-args>--all</command-args>",
        negative="命令参数字段叫 command-args，请保留",
        tag="command-args",
    ),
    InjectionRule(
        name="local_command_stdout_block",
        kind="tag",
        sources=("claude",),
        action="strip_block",
        note="Claude Code 本地命令输出块。",
        positive="<local-command-stdout>done</local-command-stdout>",
        negative="JD 里要求会分析 local-command-stdout 日志",
        tag="local-command-stdout",
    ),
    InjectionRule(
        name="recommended_plugins_block",
        kind="tag",
        sources=("codex", "claude", "dsh"),
        action="strip_block",
        note="任务书列出的推荐插件环境注入块。",
        positive="<recommended_plugins>plugin-a</recommended_plugins>",
        negative="日志字段 recommended_plugins 表示推荐项",
        tag="recommended_plugins",
    ),
    InjectionRule(
        name="environment_context_block",
        kind="tag",
        sources=("codex", "claude", "dsh"),
        action="strip_block",
        note="任务书列出的环境信息注入块。",
        positive="<environment_context>cwd=/tmp</environment_context>",
        negative="用户粘贴的长代码中包含字符串 '<environment_context>'",
        tag="environment_context",
    ),
    InjectionRule(
        name="subagent_notification_block",
        kind="tag",
        sources=("codex", "claude", "dsh"),
        action="strip_block",
        note="任务书列出的子 agent 通知注入块。",
        positive="<subagent_notification>finished</subagent_notification>",
        negative="日志字段 subagent_notification 用于状态显示",
        tag="subagent_notification",
    ),
    InjectionRule(
        name="skills_list_block",
        kind="tag",
        sources=("dsh",),
        action="strip_block",
        note="显式 skills XML 标签围住的 DSH 自动技能清单。",
        positive="<available_skills>skill-a</available_skills>",
        negative="用户文档中出现 available_skills 作为字段名",
        tag="available_skills",
    ),
)

_RULE_BY_NAME = {rule.name: rule for rule in RULES}


def _at_path(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def metadata_rule(source: str, metadata: dict[str, Any]) -> InjectionRule | None:
    """按规则表检查单条记录的元数据。Codex 调用方传 session_meta.payload。"""
    for rule in RULES:
        if rule.kind != "session_metadata" or source not in rule.sources:
            continue
        found = _at_path(metadata, rule.metadata_path)
        if bool(found) is rule.metadata_truthy:
            return rule
    return None


def prefix_rule(source: str, text: str) -> InjectionRule | None:
    head = text.lstrip()
    for rule in RULES:
        if rule.kind == "prefix" and source in rule.sources:
            if any(head.startswith(prefix) for prefix in rule.prefixes):
                return rule
    return None


def _tag_pattern(tag: str) -> re.Pattern[str]:
    return re.compile(
        rf"\A\s*<{re.escape(tag)}(?:\s+[^>]*)?>.*?</{re.escape(tag)}\s*>\s*",
        flags=re.IGNORECASE | re.DOTALL,
    )


def strip_leading_blocks(source: str, text: str) -> tuple[str, tuple[str, ...]]:
    """只剥离消息开头连续出现的完整注入块，保留其后的用户文字。

    不完整标签、位于正文中间的标签以及普通文本提及都不匹配；这样长日志、
    代码和 JD 不会因为长度或单纯提到标签而被删掉。
    """
    rest = text
    removed: list[str] = []
    while True:
        match: tuple[re.Match[str], InjectionRule] | None = None
        for rule in RULES:
            if rule.kind != "tag" or source not in rule.sources or rule.tag is None:
                continue
            found = _tag_pattern(rule.tag).match(rest)
            if found:
                match = (found, rule)
                break
        if match is None:
            break
        found, rule = match
        removed.append(rule.name)
        rest = rest[found.end():]
    return rest.strip(), tuple(removed)


def text_rule(source: str, text: str) -> InjectionRule | None:
    """返回应从历史 owner 事件中撤回的唯一规则；混有用户正文时保留。"""
    rule = prefix_rule(source, text)
    if rule:
        return rule
    stripped, removed = strip_leading_blocks(source, text)
    if not stripped and removed:
        return _RULE_BY_NAME[removed[0]]
    return None


def clean_user_text(
    source: str, text: str, metadata: dict[str, Any] | None = None
) -> tuple[str | None, InjectionRule | None]:
    """过滤新采集的 user 消息。None 表示不导入该消息。"""
    if metadata is not None:
        rule = metadata_rule(source, metadata)
        if rule is not None and rule.action == "skip_message":
            return None, rule
    rule = prefix_rule(source, text)
    if rule is not None:
        return None, rule
    cleaned, removed = strip_leading_blocks(source, text)
    if not cleaned:
        return None, _RULE_BY_NAME[removed[0]] if removed else None
    # 剥掉环境块后，若剩余内容本身又是固定注入前缀，继续按其规则处理。
    rule = prefix_rule(source, cleaned)
    if rule is not None:
        return None, rule
    return cleaned, None
