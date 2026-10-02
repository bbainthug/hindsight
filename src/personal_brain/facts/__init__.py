"""D-2 可溯源的个人事实层 v0（设计 §10/§11 的最小可用切片）。

模块边界：
- segmentation：owner 发言按对话所选路径分段（有限上下文重叠）。
- validation：候选主张的原文核对（跨度用原文偏移映射还原）与证据性质检查。
- similarity：去重/冲突检测用的文本 + 向量相近度（无可选依赖时退化）。
- llm：OpenAI 兼容客户端契约与实现（密钥只从环境变量读）。
- prompting：提示词与输出 JSON 解析。
- pipeline：提炼编排（估算 → 预算 → 提炼 → 校验 → 去重 → 关系建议 → 入库）。
- review：人工审核操作（通过/拒绝/修改/跳过、关系裁决）与审核队列视图。
- views：list / timeline / explain 只读视图。
- profile_export：profile.md「自动维护」小节草稿与 diff。
- derivation：派生失效（源 revision 撤回 → 依赖主张 needs_review）。

v0 边界（任务书「非目标」）：全部候选人工审核后激活；不暴露写 MCP；
不做 behavior_pattern / Web 界面 / 自动激活。
"""

from __future__ import annotations

MEMORY_TYPES: tuple[str, ...] = (
    "preference",
    "goal",
    "decision",
    "project_fact",
    "open_question",
)
ASSERTION_KINDS: tuple[str, ...] = (
    "self_report",
    "intention",
    "hypothesis",
    "quotation",
    "external_claim",
    "model_inference",
)
RELATION_TYPES: tuple[str, ...] = (
    "same",
    "refines",
    "supersedes",
    "contradicts",
    "coexists",
    "unrelated",
)
