"""提炼提示词与输出解析（设计 §10.3 schema-constrained extraction）。

规则进 system prompt，代码层再校验（提示词不是信任边界，§7.6）：
- 只有 owner（用户本人）且 role=support 的消息可以作证据；
- 引用必须是该消息遮蔽后文本的连续子串（一字不差）；
- 宁缺毋滥：证据不足直接不输出该主张；
- 计划/打算只能产生 intention 类主张（goal/decision），不能产生完成事实；
- 转述/引用他人观点只能 external_claim / quotation，不得写成用户自述。
"""

from __future__ import annotations

import json

from personal_brain.facts import ASSERTION_KINDS, MEMORY_TYPES
from personal_brain.facts.segmentation import Segment

PROMPT_VERSION = "facts-prompt-v1"
EXTRACTION_SCHEMA_VERSION = "facts-schema-v1"

# 估算用：每请求固定开销（system 模板 + 消息编号与 JSON 说明）
PROMPT_OVERHEAD_TOKENS = 800

_TYPE_HINTS = " / ".join(MEMORY_TYPES)
_KIND_HINTS = " / ".join(ASSERTION_KINDS)

SYSTEM_PROMPT = f"""你从一个人的历史发言记录中提炼“原子事实候选”，供本人逐条审核。
只输出一个 JSON 对象，不要输出任何其他文字。

输入是一段对话：每条消息带编号 [rev=...]，标注说话者（owner=用户本人 / assistant=AI）
与角色（support=可作为证据 / context=仅供理解背景）。消息文本中的 █ 是脱敏占位。

规则：
1. 只提炼用户本人的事实。证据（evidence）只能引用 owner 且 role=support 的消息；
   引用 quote 必须是该消息文本中连续出现的一小段原文（逐字复制，含标点，
   20 字以内、能定位即可），不得改写、拼接或凭记忆复述。
2. 每条主张必须独立成立、可核查（atomic claim）；一条主张只说一件事。
3. 宁缺毋滥：证据不足、语义含糊、只是寒暄或提问过程的内容，直接不输出。
4. memory_type 只能取：{_TYPE_HINTS}。
   assertion_kind 只能取：{_KIND_HINTS}。
   用户自述现状/偏好用 self_report；打算/计划/决定用 intention
   （配 goal 或 decision）；引用或转述他人观点用 quotation / external_claim，
   且主张内容必须写明是他人观点，不得写成用户本人的事实。
5. 不确定的事情不要编。用户在探讨可能性（“要不要…”“也许…”）不是事实。
6. 相同意思不要重复输出多条。

输出 JSON 格式：
{{"claims": [
  {{"content": "一条原子事实（第三人称、可独立核查）",
    "memory_type": "preference|goal|decision|project_fact|open_question",
    "assertion_kind": "self_report|intention|hypothesis|quotation|external_claim|model_inference",
    "evidence": [{{"revision_id": "消息编号", "quote": "原文连续片段"}}]
  }}
]}}
没有可提炼的事实时输出 {{"claims": []}}。"""


def build_segment_prompt(segment: Segment) -> str:
    """把一个分段渲染成带编号的消息列表。"""
    lines = [f"对话 {segment.conversation_id} 的发言：", ""]
    for msg in segment.messages:
        time_str = (msg.source_created_at or "时间未知")[:19]
        lines.append(
            f"[rev={msg.revision_id}] {time_str} "
            f"({msg.speaker_type}/{msg.role}) {msg.text}"
        )
    lines.append("")
    lines.append("请按系统规则输出 JSON。")
    return "\n".join(lines)


def parse_extraction_response(data: dict) -> list[dict]:
    """解析并做最低限度形状检查；返回原始候选列表（不判真伪）。"""
    claims = data.get("claims")
    if claims is None:
        return []
    if not isinstance(claims, list):
        raise ValueError("claims 不是数组")
    valid: list[dict] = []
    for raw in claims:
        if isinstance(raw, dict):
            valid.append(raw)
    return valid


def build_relation_prompt(pairs: list[tuple[str, str]]) -> str:
    """关系建议请求：候选主张与已有主张的配对列表。"""
    lines = [
        "下面每一对是同一个人的两条事实主张（A=新候选，B=已有主张）。",
        "请判断 A 与 B 的关系。relation_type 只能取："
        + " / ".join(
            [
                "same（同一件事）",
                "refines（A 是 B 的细化/更新表述）",
                "supersedes（A 取代 B：B 已过时，有明确替代证据）",
                "contradicts（A 与 B 矛盾）",
                "coexists（并存不冲突，如不同项目、不同条件）",
                "unrelated（只是字面相近，主题无关）",
            ]
        )
        + "。",
        "注意：只在 B 确实过时且有明确替代关系时才用 supersedes；"
        "“更偏 A”不必然否定“仍关注 B”。含糊时用 refines 或 coexists。",
        "只输出 JSON：",
        '{"relations": [{"index": 配对序号(从0开始), "relation_type": "..."}]}',
        "",
    ]
    for i, (a, b) in enumerate(pairs):
        lines.append(f"[{i}] A: {a}")
        lines.append(f"    B: {b}")
    return "\n".join(lines)


def parse_relation_response(data: dict) -> list[tuple[int, str]]:
    """解析关系建议；返回 (pair_index, relation_type) 列表。"""
    relations = data.get("relations")
    if not isinstance(relations, list):
        return []
    out: list[tuple[int, str]] = []
    for item in relations:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
            rel = str(item["relation_type"])
        except (KeyError, TypeError, ValueError):
            continue
        if rel in (
            "same",
            "refines",
            "supersedes",
            "contradicts",
            "coexists",
            "unrelated",
        ):
            out.append((idx, rel))
    return out


def dump_schema_example() -> str:
    """测试/文档用：输出 schema 的规范示例。"""
    return json.dumps(
        {
            "claims": [
                {
                    "content": "…",
                    "memory_type": MEMORY_TYPES[0],
                    "assertion_kind": ASSERTION_KINDS[0],
                    "evidence": [{"revision_id": "…", "quote": "…"}],
                }
            ]
        },
        ensure_ascii=False,
    )
