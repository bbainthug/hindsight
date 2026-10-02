"""版本常量（§10.4：分段/提示词/schema/规则版本参与幂等键）。"""

from __future__ import annotations

# 分段规则版本：改动切分阈值/上下文重叠规则时必须递增（幂等键随之变化）
SEGMENTATION_VERSION = "facts-seg-v1"
# 提示词版本：改动 system prompt 语义时递增
PROMPT_VERSION = "facts-prompt-v1"
# 期望 LLM 输出的 JSON schema 版本
EXTRACTION_SCHEMA_VERSION = "facts-schema-v1"
# 提炼规则版本（校验/枚举/拒绝口径）
RULE_VERSION = "facts-rule-v1"
# 证据跨度核对方法与版本（原文偏移映射，与检索 snippet 同一实现）
SPAN_CHECK_METHOD = "verbatim_span_norm"
SPAN_CHECK_VERSION = "nfkc-casefold-v2-charmap"
