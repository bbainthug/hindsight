"""导入数据的规范化契约（解析器输出 → 存储层输入）。

解析器（importers/*）把外部可变格式转换为以下 Pydantic 模型；
存储层（history/store.py）只消费本模块，不感知导出格式细节。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from personal_brain.history.timeutil import ParsedTime


class IncomingAttachment(BaseModel):
    """消息内附件引用。"""

    asset_pointer: str
    content_type: str | None = None
    member_path: str | None = None


class IncomingMessage(BaseModel):
    """一条消息（或一个图节点）的规范化形态。

    ``source_message_id`` 为 None 时存储层使用 fallback 身份。
    """

    conversation_id: str
    node_id: str
    parent_node_id: str | None = None
    child_order: int = 0
    is_structural: bool = False  # 无消息的结构节点（§4.3 允许）

    source_message_id: str | None = None
    speaker_id: str | None = None
    speaker_type: str = "unknown"
    content_type: str = "text"
    raw_text: str | None = None
    attachments: list[IncomingAttachment] = Field(default_factory=list)
    created: ParsedTime | None = None
    updated: ParsedTime | None = None
    locator: str = ""


class IncomingConversation(BaseModel):
    """一次对话的图 + 消息。"""

    conversation_id: str
    title: str | None = None
    current_node_id: str | None = None
    nodes: list[IncomingMessage] = Field(default_factory=list)


class MemberManifest(BaseModel):
    """归档成员级清单（§5.2 步骤 1：manifest 与摘要）。"""

    member_path: str
    sha256: str
    size_bytes: int
    kind: str = "other"  # conversation_data | attachment | metadata | other


class JobError(BaseModel):
    """结构化错误（不含正文）。"""

    code: str
    locator: str | None = None
    message: str
