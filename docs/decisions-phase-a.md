# Phase A（数据层切片）实现决策记录

范围：v0.2 规格中 Phase A 的**数据模型、ChatGPT 导入、事件版本、分支、幂等**部分。
检索（§6）、权限执行/MCP（§7–8）、标签操作、备份/撤回机制（§14）等未实现，仅保证
Schema 不与之冲突。

## 文档未定细节的选择（最简、可逆、可测试）

| # | 决策 | 依据/影响 | 可逆性 |
|---|---|---|---|
| 1 | `event_id = "{source_kind}:{account_namespace}:{source_message_id}"` 直接拼接 | §4.2「由三者确定」的直读；可读、可调试 | 改派生规则 = 迁移重建 L1，可从 L0 重建 |
| 2 | `revision_id = "{event_id}:r{revision_hash 前 20 位}"` 确定性派生 | 幂等天然成立；同库重建（§3.2）产生相同 ID | 同上 |
| 3 | `revision_hash` 覆盖 content_type/raw_text/speaker/时间四元组/source_updated_at/附件引用；**排除** recorded_at、归档路径、locator、search_text | §4.2 明确排除导入时间与路径；search_text 归一化版本升级不应改写内容身份 | 哈希算法版本变化时可引入 hash_version 列 |
| 4 | 版本排序键 = `COALESCE(update_time, create_time)` 的 UTC 归一值；双方皆无时间则"先后不明" | §5.3「版本先后不明保留冲突」的可操作化 | `resolve_current()` 独立函数，可整体替换 |
| 5 | 冲突状态：`current_revision_id=NULL` + `resolution=unresolved`，双方版本保留；严格更新到达即恢复 resolved | §4.2 current 可空 + §5.3 保留冲突 | 规则集中、可改 |
| 6 | ChatGPT 无 conversation_id 时派生 `conv-fb-` ID（title+create_time+最小节点 ID） | 真实导出均含 conversation_id；此为容错兜底 | 已知限制，见下 |
| 7 | `source_locator = 成员路径#/conversations/{id}/mapping/{node_id}`，用稳定节点 ID 不用数组下标 | §4.2 允许「JSON pointer / 稳定节点 ID」；下标跨导出不稳定 | 格式可换 |
| 8 | 快照/来源 ID 为 uuid5 确定性派生；同（来源, sha256）必同快照 | 重复导入幂等在 ID 层即成立，DB 唯一约束兜底 | — |
| 9 | ZIP 成员不落盘，按上限流式读取解析；归档目录只保存**原始归档字节** | L0 权威=原始字节；避免解压面 | — |
| 10 | 目录输入的 `archive_sha256` = 按成员路径排序的 名称+内容哈希 规范摘要 | 相同内容目录与重复导入得到同摘要 | — |
| 11 | fallback 消息身份 = 来源+对话+结构位置(父节点|兄弟序)+说话者+时间原值+内容摘要 | §4.2 要求组合定位，防止不同时间相同发言合并 | — |
| 12 | 每个内容版本初始化一条 `revision_policy_state`（available/unclassified/policy v0）；标签变更留待标签阶段写入新版本行 | §4.2 标签物理隔离在版本化表 | 表结构已就绪 |

## 已知限制（本切片内不做、不假装已做）

- 中文检索/FTS、时间过滤查询接口未实现（`search_text` 已按 §6.2 生成）。
- 权限执行、MCP、标签 CLI 未实现；`classification_status` 恒为 unclassified。
- 源撤回/清除（§14）、备份恢复未实现；`sources.status` 目前恒为 active。
- 内存中 `json.loads` 解析对话成员（上限内可接受）；超大导出的流式解析留待实测后再做。
- `supersedes_snapshot_id` 字段就绪但导入器从不自动设置（§4.1「不静默覆盖」）。
- 未做端到端答案可靠性验证（规格 §19 明确禁止此类宣称）。
