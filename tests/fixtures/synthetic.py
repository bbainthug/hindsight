"""合成 ChatGPT 导出数据生成器（测试专用，全部内容为合成）。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path


def text_message(
    message_id: str,
    role: str,
    text: str,
    create_time: float | None,
    update_time: float | None = None,
    content_type: str = "text",
    parts: list | None = None,
    author_name: str | None = None,
) -> dict:
    if parts is None:
        parts = [text] if text is not None else []
    if update_time is None and create_time is not None:
        update_time = create_time  # 真实导出中 update_time 与 create_time 同现
    author: dict = {"role": role, "name": author_name, "metadata": {}}
    return {
        "id": message_id,
        "author": author,
        "create_time": create_time,
        "update_time": update_time,
        "content": {"content_type": content_type, "parts": parts},
        "status": "finished_successfully",
        "end_turn": None,
        "weight": 1.0,
        "metadata": {},
        "recipient": "all",
    }


def node(
    node_id: str,
    message: dict | None = None,
    parent: str | None = None,
    children: list[str] | None = None,
) -> tuple[str, dict]:
    return node_id, {
        "id": node_id,
        "message": message,
        "parent": parent,
        "children": children or [],
    }


def build_conversation(
    conversation_id: str,
    title: str,
    create_time: float,
    mapping: dict[str, dict],
    current_node: str | None,
) -> dict:
    conv = {
        "title": title,
        "create_time": create_time,
        "update_time": create_time + 600 if create_time is not None else None,
        "mapping": mapping,
        "current_node": current_node,
    }
    if conversation_id is not None:
        conv["conversation_id"] = conversation_id
    return conv


# 固定合成时间基点：2026-08-01 08:00:00 UTC
T0 = 1785571200.0


def simple_conversation() -> dict:
    """两轮对话：user → assistant → user → assistant（线性单链）。"""
    m1 = text_message("m-0001", "user", "我最近正在考虑职业方向变化", T0)
    a1 = text_message("a-0001", "assistant", "可以从目标行业和现有技能入手。", T0 + 60)
    m2 = text_message("m-0002", "user", "目标是 AI 工程方向", T0 + 120)
    a2 = text_message("a-0002", "assistant", "建议先补基础与项目经验。", T0 + 180)
    mapping = dict(
        [
            node("root", None, None, ["m-0001"]),
            node("m-0001", m1, "root", ["a-0001"]),
            node("a-0001", a1, "m-0001", ["m-0002"]),
            node("m-0002", m2, "a-0001", ["a-0002"]),
            node("a-0002", a2, "m-0002", []),
        ]
    )
    return build_conversation("conv-1", "职业讨论", T0, mapping, "a-0002")


def branched_conversation() -> dict:
    """共享祖先分支：a-0001 有两个子节点（编辑/重新生成），current 在右支。"""
    m1 = text_message("b-0001", "user", "帮我规划学习路线", T0)
    a1 = text_message("b-0002", "assistant", "先学数学基础。", T0 + 30)
    m2a = text_message("b-0003", "user", "左支：偏理论研究", T0 + 60)
    m2b = text_message("b-0004", "user", "右支：偏工程实践", T0 + 90)
    a3 = text_message("b-0005", "assistant", "右支回复：做项目。", T0 + 120)
    mapping = dict(
        [
            node("root", None, None, ["b-0001"]),
            node("b-0001", m1, "root", ["b-0002"]),
            node("b-0002", a1, "b-0001", ["b-0003", "b-0004"]),
            node("b-0003", m2a, "b-0002", []),
            node("b-0004", m2b, "b-0002", ["b-0005"]),
            node("b-0005", a3, "b-0004", []),
        ]
    )
    return build_conversation("conv-branch", "分支对话", T0, mapping, "b-0005")


def multimodal_conversation() -> dict:
    """多模态 + 代码 + 不支持结构 + 无 ID 消息。"""
    mm = text_message(
        "mm-0001",
        "user",
        None,
        T0,
        parts=[
            "看看这张图",
            {
                "content_type": "image_asset_pointer",
                "asset_pointer": "file-service://synthetic-image-1",
                "size": 1024,
            },
            12345,  # 不支持的结构 → UNSUPPORTED_CONTENT
        ],
        content_type="multimodal_text",
    )
    code = text_message(
        "mm-0002",
        "assistant",
        "print('hello')",
        T0 + 30,
        content_type="code",
    )
    no_id = text_message(None, "user", "没有稳定 ID 的消息", T0 + 60)  # 无 id 字段
    no_id.pop("id")
    mapping = dict(
        [
            node("root", None, None, ["mm-0001"]),
            node("mm-0001", mm, "root", ["mm-0002"]),
            node("mm-0002", code, "mm-0001", ["mm-0003"]),
            node("mm-0003", no_id, "mm-0002", []),
        ]
    )
    return build_conversation("conv-mm", "多模态合成", T0, mapping, "mm-0003")


def naive_time_conversation() -> dict:
    """时间缺失：create_time/update_time 为 None。"""
    m1 = text_message("t-0001", "user", "时间未知的一条", None)
    mapping = dict(
        [node("root", None, None, ["t-0001"]), node("t-0001", m1, "root", [])]
    )
    return build_conversation("conv-time", "时间未知", T0, mapping, "t-0001")


def build_export(conversations: list[dict]) -> dict:
    return conversations


def write_zip(payload: object, path: Path, member_name: str = "conversations.json") -> Path:
    """标准导出 ZIP：根目录 conversations.json。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member_name, json.dumps(payload, ensure_ascii=False))
    return path


def write_zip_bytes_varied(
    payload: object, path: Path, member_name: str = "conversations.json"
) -> Path:
    """同内容但字节不同的 ZIP（不同压缩级别/成员名），用于测试新快照包含旧消息。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_BZIP2) as zf:
        zf.writestr("./" + member_name, json.dumps(payload, ensure_ascii=False, indent=1))
    return path


def write_directory(payload: object, path: Path, member_name: str = "conversations.json") -> Path:
    """已解压目录形态。"""
    (path / member_name).parent.mkdir(parents=True, exist_ok=True)
    (path / member_name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path
