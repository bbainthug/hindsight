"""ChatGPT 导出解析器（§5.1）。

- 按结构识别对话数据（含 ``mapping`` 节点图的 JSON 集合），不凭文件名猜测。
- 导出结构视为外部可变格式，解析宽松、记录严格。
- 保留多模态 content_type；无法解析的结构记为 unsupported，不无声丢弃。
- 全部内容作为不可信数据处理，不执行任何导出内指令。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from personal_brain.history.timeutil import ParsedTime, parse_unix_seconds
from personal_brain.importers.models import (
    IncomingAttachment,
    IncomingConversation,
    IncomingMessage,
    JobError,
)

PARSER_VERSION = "chatgpt/0.1.0"

# 角色映射：user=档案所有者，tool 输出归 system（§7.2 第三方/系统输出默认收紧由后续标签阶段处理）
_ROLE_MAP = {"user": "owner", "assistant": "assistant", "system": "system", "tool": "system"}


class ChatGPTParseError(Exception):
    pass


def find_conversation_members(
    members: list[tuple[str, bytes]],
) -> tuple[list[tuple[str, bytes]], list[JobError]]:
    """按结构识别对话数据成员，返回 (命中成员, 结构错误)。

    结构判据：JSON 顶层为数组且存在元素含 dict 型 ``mapping`` 键；
    或顶层为 ``{"conversations": [...]}``。
    """
    hits: list[tuple[str, bytes]] = []
    errors: list[JobError] = []
    for member_path, data in members:
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(
                JobError(
                    code="MEMBER_NOT_JSON",
                    locator=member_path,
                    message=f"非 JSON 成员: {exc}",
                )
            )
            continue
        if _is_conversation_collection(value):
            hits.append((member_path, data))
    if not hits:
        errors.append(
            JobError(
                code="NO_CONVERSATION_DATA",
                locator=None,
                message="未找到符合对话结构的成员（按 mapping 图结构识别，不依赖文件名）",
            )
        )
    return hits, errors


def _is_conversation_collection(value: object) -> bool:
    items: list[object]
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict) and isinstance(value.get("conversations"), list):
        items = value["conversations"]
    else:
        return False
    return any(_looks_like_conversation(c) for c in items)


def _looks_like_conversation(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    mapping = obj.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        return False
    sample = list(mapping.values())[:5]
    return all(isinstance(n, dict) for n in sample) and any(
        {"parent", "children"} & set(n) or "message" in n for n in sample
    )


def parse_conversation(
    raw: dict,
    member_path: str,
    errors: list[JobError],
) -> IncomingConversation | None:
    """解析单个对话对象；失败记录结构化错误并返回 None（partial 语义）。"""
    mapping = raw.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        errors.append(
            JobError(
                code="CONVERSATION_PARSE_FAILED",
                locator=f"{member_path}#/{raw.get('conversation_id') or ''}",
                message="mapping 缺失或不是对象",
            )
        )
        return None

    conv_id = _conversation_id(raw, member_path)
    if conv_id is None:
        return None

    nodes: list[IncomingMessage] = []
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            errors.append(
                JobError(
                    code="UNSUPPORTED_NODE",
                    locator=_locator(member_path, conv_id, str(node_id)),
                    message="mapping 节点不是对象",
                )
            )
            continue
        nodes.append(
            _parse_node(
                node_id=str(node_id),
                node=node,
                conv_id=conv_id,
                member_path=member_path,
                errors=errors,
            )
        )

    # child_order = 本节点在父节点 children 数组中的下标（兄弟顺序是图的一部分）
    for node in nodes:
        parent_node = mapping.get(node.parent_node_id) if node.parent_node_id else None
        if isinstance(parent_node, dict):
            children = parent_node.get("children") or []
            if node.node_id in children:
                node.child_order = children.index(node.node_id)

    title = raw.get("title")
    current = raw.get("current_node")
    return IncomingConversation(
        conversation_id=conv_id,
        title=title if isinstance(title, str) else None,
        current_node_id=current if isinstance(current, str) else None,
        nodes=nodes,
    )


def _conversation_id(raw: dict, member_path: str) -> str | None:
    """对话身份：优先导出自带 conversation_id；缺失时派生 fallback ID。"""
    cid = raw.get("conversation_id")
    if isinstance(cid, str) and cid:
        return cid
    # 文档未定细节的最简可逆实现：标题 + 创建时间 + 最小节点 ID 派生，
    # 记录为 fallback（见 docs/decisions-phase-a.md 已知限制）。
    title = raw.get("title") or ""
    created = raw.get("create_time")
    node_ids = sorted(
        str(k) for k in (raw.get("mapping") or {}).keys()
    )
    if not node_ids:
        return None
    anchor = f"{title}|{created}|{node_ids[0]}"
    return "conv-fb-" + hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:16]


def _parse_node(
    *,
    node_id: str,
    node: dict,
    conv_id: str,
    member_path: str,
    errors: list[JobError],
) -> IncomingMessage:
    message = node.get("message")
    parent = node.get("parent")
    locator = _locator(member_path, conv_id, node_id)

    if message is None or not isinstance(message, dict):
        return IncomingMessage(
            conversation_id=conv_id,
            node_id=node_id,
            parent_node_id=parent if isinstance(parent, str) else None,
            child_order=0,
            is_structural=True,
            locator=locator,
        )

    author = message.get("author") or {}
    role = author.get("role") if isinstance(author, dict) else None
    speaker_type = (
        _ROLE_MAP.get(role, "unknown") if isinstance(role, str) else "unknown"
    )
    speaker_id = author.get("name") if isinstance(author, dict) else None
    if not speaker_id and isinstance(role, str):
        speaker_id = role

    content = message.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    content_type = str(content.get("content_type") or "unknown")
    raw_text, attachments, unsupported = _extract_content(content)
    for _ in range(unsupported):
        errors.append(
            JobError(
                code="UNSUPPORTED_CONTENT",
                locator=locator,
                message=f"未解析的内容结构（content_type={content_type}），已保留类型与节点，不无声丢弃",
            )
        )

    created = _parse_time(message.get("create_time"))
    updated = _parse_time(message.get("update_time"))

    msg_id = message.get("id")
    source_message_id = msg_id if isinstance(msg_id, str) and msg_id else None

    return IncomingMessage(
        conversation_id=conv_id,
        node_id=node_id,
        parent_node_id=parent if isinstance(parent, str) else None,
        child_order=0,  # 由 parse_conversation 按父节点 children 序回填
        is_structural=False,
        source_message_id=source_message_id,
        speaker_id=speaker_id,
        speaker_type=speaker_type,
        content_type=content_type,
        raw_text=raw_text,
        attachments=attachments,
        created=created,
        updated=updated,
        locator=locator,
    )


def _extract_content(
    content: dict,
) -> tuple[str | None, list[IncomingAttachment], int]:
    """提取正文与附件引用。返回 (raw_text, attachments, unsupported 计数)。"""
    ctype = content.get("content_type")
    parts = content.get("parts")
    texts: list[str] = []
    attachments: list[IncomingAttachment] = []
    unsupported = 0

    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                if part.get("content_type") == "image_asset_pointer" and isinstance(
                    part.get("asset_pointer"), str
                ):
                    attachments.append(
                        IncomingAttachment(
                            asset_pointer=str(part["asset_pointer"]),
                            content_type=str(part.get("content_type")),
                            member_path=None,
                        )
                    )
                elif isinstance(part.get("text"), str):
                    texts.append(part["text"])
                else:
                    unsupported += 1
            else:
                unsupported += 1
    elif isinstance(content.get("text"), str):
        texts.append(content["text"])
    elif ctype in ("user_editable_context", "model_editable_context"):
        # 上下文类结构：拼接已知字符串字段；其余字段视为不支持
        for key in ("user_profile_message", "user_instructions", "model_messages"):
            v = content.get(key)
            if isinstance(v, str):
                texts.append(v)
            elif v is not None:
                unsupported += 1
    elif ctype == "unknown" or (ctype is None and not content):
        unsupported += 1
    else:
        unsupported += 1

    raw_text = "\n".join(t for t in texts if t != "") or None
    return raw_text, attachments, unsupported


def _parse_time(value: object) -> ParsedTime | None:
    """ChatGPT 导出时间为 unix 秒浮点；缺失/为 None 表示时间未知。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        return parse_unix_seconds(value)
    return None


def _locator(member_path: str, conv_id: str, node_id: str) -> str:
    """来源定位：成员路径 + 稳定节点 ID（不用数组下标，跨导出稳定）。"""
    return f"{PurePosixPath(member_path).as_posix()}#/conversations/{conv_id}/mapping/{node_id}"
