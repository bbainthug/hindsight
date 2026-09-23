# 任务 D-5b：远程检索提速（覆盖期缓存）

> 交付给实现 agent 的任务书。遵守 AGENTS.md 工作约定。

## 背景

VM（2 核 / 1 GB）上 `/api/search` 空载约 9 秒，导入进行中 11–16 秒。D-5 交付时定位到大头在
`src/personal_brain/mcp_server/service.py` 的 `_coverage()`：每次调用都对全库做一次
`MIN/MAX(source_created_at)` 聚合（带 policy_state、标签的 EXISTS 子查询），约 7 秒。
它被 `tool_brain_status`、`tool_search_history`、`tool_get_recent_events`、`tool_get_event`
四处调用，也就是说**每个工具调用都付一次全库扫描**。

claude.ai / ChatGPT 连接器一次回答常连调 3–4 次工具，按现在的速度会超时或让模型放弃调用。

## 目标

远程 profile 在 VM 上：`/api/search` 与 MCP `tools/call search_history` **热态 P95 ≤ 1.5 s**，
导入进行中 P95 ≤ 3 s；返回内容与现在逐字段一致。

## 范围

1. 给 `_coverage()` 加进程内缓存，键为：
   `(filters 的规范化签名, branch_scope, policy_epoch, PRAGMA data_version)`。
   - `policy_epoch`：撤回 / 标注会递增，保证撤回立即反映到覆盖期；
   - `PRAGMA data_version`：同一连接看到其他连接（导入进程）提交后会变化，保证导入后自动失效。
   - 缓存条目数上限 32，超出 LRU 淘汰；线程安全（REST 连接池与 MCP 专属连接都会用）。
2. 首次计算本身也要快一点：检查 `event_revisions(source_created_at)` 等现有索引是否被用上
   （`EXPLAIN QUERY PLAN`），需要的话在新 migration 里加索引；不改查询语义。
   加索引后在 VM 上实测首算耗时，写进交付。
3. 找出 `_coverage` 之外剩下的耗时（`/api/search` 分段计时：授权、FTS、验证、snippet、序列化），
   写进交付；**只报告，不顺手优化**，除非某一段单独超过 1 秒。
4. `docs/remote-access.md` 性能一节更新实测数字。

## 非目标

- 不改检索、授权、遮蔽的语义与返回格式。
- 不引入外部缓存（Redis 等），不跨进程共享缓存。
- 不做预计算表 / 物化视图（缓存不够时再说）。

## 设计要求

- 缓存命中与未命中的返回值必须完全相同（测试逐字段比较）。
- 撤回一条事件后，下一次调用的覆盖期必须反映撤回（不能等 TTL）。
- 另一个连接导入新事件后，下一次调用的 `known_end` 必须前移。
- 不在日志里打印覆盖期以外的任何内容。

## 测试

- 单元：缓存键签名稳定；LRU 上限；并发调用不重复计算同一键（或重复计算但结果一致，二选一写清楚）。
- 集成（合成库）：
  - 命中 / 未命中结果一致；
  - 另一连接写入后 `known_end` 更新；
  - `withdraw-event` 后覆盖期更新；
  - 不同 profile（allow_scopes / deny_sensitivity 不同）互不串缓存。
- 现有测试全绿；ruff / mypy 干净。

## 交付

改动摘要、可复现命令与结果、未解决问题；另附在 VM 上的实测（各 20 次以上）：
- `/api/search`、MCP `search_history` 热态 P50/P95（空载 / 导入进行中）；
- `_coverage` 首算耗时（加索引前 / 后）；
- 分段计时表。

VM 上需要 `systemctl --user restart hindsight-mcp` 的步骤列给我，我来确认。

## 已知取舍（不用来回问）

- 用 `PRAGMA data_version` 而不是 TTL：导入后立即可见，无需猜过期时间。
- 进程内缓存：服务单进程单 worker，够用；重启后首次调用多付一次计算。
