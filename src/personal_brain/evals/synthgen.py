"""合成语料生成器（§9.2）：种子确定、可放大到 10 万条文本消息。

导出格式与 ``tests/fixtures/synthetic.py`` 的结构契约一致（由
``test_synthgen_imports_via_real_importer`` 保证：生成物必须能通过
真实导入器发布）。文本构造：
- 每条消息含唯一编号 token（``主题{cid}号`` / ``编号U{cid}M{mid}``）供精确召回断言；
- 按比例散布常用词（职业/安全/AI/求职/工程/学习/项目）模拟真实词频分布；
- 全部为文本消息（§9.2 门槛限定"10 万条合成文本消息"）。
"""

from __future__ import annotations

import json
import random
import zipfile
from pathlib import Path

T0 = 1754035200.0  # 2025-08-01T08:00:00Z（生成语料时间基点）

_TOPICS = ["职业方向", "系统安全", "AI工程选型", "求职进展", "学习方法", "项目排期"]
_COMMON = ["职业", "安全", "AI", "求职", "工程", "学习", "项目"]


def _message(message_id: str, role: str, text: str, create_time: float) -> dict:
    return {
        "id": message_id,
        "author": {"role": role, "name": None, "metadata": {}},
        "create_time": create_time,
        "update_time": create_time,
        "content": {"content_type": "text", "parts": [text]},
        "status": "finished_successfully",
        "end_turn": None,
        "weight": 1.0,
        "metadata": {},
        "recipient": "all",
    }


def generate_conversations(
    n_conversations: int, messages_per_conversation: int, seed: int = 20250801
) -> list[dict]:
    """生成 n_conversations × messages_per_conversation 条文本消息的对话集合。"""
    rng = random.Random(seed)
    conversations: list[dict] = []
    for cid in range(n_conversations):
        conv_id = f"bench-{cid:05d}"
        root_id = f"r-{conv_id}"
        mapping: dict[str, dict] = {
            root_id: {
                "id": root_id, "message": None, "parent": None,
                "children": ["m-0000"],
            }
        }
        prev = root_id
        create = T0 + cid * 600
        for mid in range(messages_per_conversation):
            node_id = f"m-{cid:05d}-{mid:05d}"  # 全源唯一（事件 ID 含 message_id）
            topic = _TOPICS[(cid + mid) % len(_TOPICS)]
            common = _COMMON[(cid + mid) % len(_COMMON)]
            role = "user" if mid % 3 != 1 else "assistant"
            # 会话首条问候语保证标点查询（你好，世界）在高频词分布外稳定命中，
            # 使基准每条常用查询都有结果物化路径（snippet/offset/citation）可测。
            greeting = "你好，世界。" if mid == 0 else ""
            text = (
                f"{greeting}主题{cid}号第{mid}条：{topic}与{common}相关的讨论，"
                f"编号U{cid}M{mid}，随机参考{rng.randrange(10**6)}"
            )
            create += 60 + (mid % 7)
            mapping[node_id] = {
                "id": node_id, "message": _message(node_id, role, text, create),
                "parent": prev, "children": [],
            }
            mapping[prev]["children"] = [node_id]
            prev = node_id
        conversations.append(
            {
                "conversation_id": conv_id,
                "title": f"基准对话{cid}",
                "create_time": T0 + cid * 600,
                "update_time": T0 + cid * 600 + 600,
                "mapping": mapping,
                "current_node": prev,
            }
        )
    return conversations


def build_benchmark_archive(
    dest_zip: Path,
    n_conversations: int,
    messages_per_conversation: int,
    seed: int = 20250801,
) -> Path:
    """生成并打包为 ChatGPT 导出 ZIP（走真实导入路径）。"""
    conversations = generate_conversations(
        n_conversations, messages_per_conversation, seed
    )
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "conversations.json", json.dumps(conversations, ensure_ascii=False)
        )
    return dest_zip
