# 任务 D-11：采集数据质量——过滤 agent 自动注入的"假发言"

> 交付给实现 agent 的任务书。遵守 AGENTS.md：开工先读 `STATUS.md`，收工更新它。

## 背景（2026-10-03 实测）

`integrations/agent_sync/sync_agents.py` 把 Codex / Claude Code / DSH 本地会话里 role=user 的消息一律记成 owner（用户本人）发言。
实际上其中绝大部分是 agent 自动注入的内容。对最近 400 个 Codex 会话文件的统计（只看前缀与结构）：

| 类型 | 识别方式 | 规模 |
|---|---|---|
| guardian 审批子会话 | `session_meta.payload.source = {"subagent": {"other": "guardian"}}`；首条 user 消息以 "The following is the Codex agent history" 开头 | 270 个会话，约 2900 万字 |
| 其他子 agent 会话 | `session_meta.payload.source.subagent.thread_spawn` | 52 个会话（user 消息是父 agent 下发的任务，不是用户本人） |
| AGENTS.md 注入 | user 消息以 `# AGENTS.md instructions` 开头 | 256 条，约 266 万字 |
| 插件推荐 / 环境信息 / 中断提示 / 子 agent 通知 | 以 `<recommended_plugins>`、`<environment_context>`、`<turn_aborted>`、`<subagent_notification>` 开头 | 约 280 条，约 120 万字 |
| 上下文压缩续写摘要 | "This session is being continued from a previous conversation" | 20 条，约 74 万字 |
| 用户真正的发言 | 其余 | 约 1832 条，约 41 万字 |

最近 30 天本地库里 Codex 来源的 "owner" 发言共 2595 万字，真实发言只占约 1%。后果：
1. D-2 事实提炼的 dry-run 估算被撑到 6400 万 tokens；
2. Hindsight 日常检索"我说过什么"时混入大量 agent 生成的长文本（检索质量问题，与 D-2 无关也必须修）。

Claude Code 与 DSH 也有同类问题，量较小：Claude Code 的 user 消息里夹着 `<system-reminder>`、`<command-name>`、
`<local-command-stdout>`、"Caveat:" 等系统内容，以及压缩续写摘要；DSH 的 user 消息里有 `<system-reminder>`、
"Current runtime context"、技能清单等注入块。

## 目标

1. 采集器只把**用户本人输入的文字**记为 owner；agent 自动注入的内容一律不进库，或至少不以 owner 身份进库。
2. 把已经入库的"假发言"清理掉（逻辑撤回，可追溯，不物理删除）。
3. 清理后用数字证明效果。

## 范围

1. **解析器过滤**（`integrations/agent_sync/sync_agents.py`）：
   - Codex：读 `session_meta`，`source` 为任何 `subagent`（guardian、thread_spawn 等）的**整个会话跳过**；
     普通会话里以上表所列前缀开头的 user 消息跳过。
   - Claude Code：user 消息中去掉 `<system-reminder>…</system-reminder>`、`<command-name>`/`<command-message>`/`<command-args>`、
     `<local-command-stdout>` 等系统块后，剩余文本为空就跳过；`isMeta: true`、压缩续写摘要（`isCompactSummary` 或固定开头）跳过。
   - DSH：同理去掉注入块；只保留用户输入的文本块。
   - 规则集中写成一张可测试的规则表（前缀 / 标签 / 会话元数据三类），每条带注释说明来源；不要散落在各解析函数里。
   - 用户自己粘贴的长文本（日志、代码、JD）**保留**：判断依据是"是不是 agent 注入"，不是长度。
2. **存量清理**：新增 `brain clean-injected`（或 `scripts/clean_injected.py`），用同一张规则表扫描已入库的 owner 发言：
   - 默认 `--dry-run`：按来源与规则输出将被撤回的条数和字数；
   - `--apply`：通过现有撤回机制（`withdraw-event` / policy 版本化撤回）逻辑撤回，记录原因 `injected_by_agent:<规则名>`；
     必须先做一致性备份（`brain backup`）；
   - 子 agent / guardian 会话按对话整体撤回。
   - 适用于任意库路径（本地库与 VM 库是同一套代码）。**不要连接 VM**，VM 上的清理由主会话验收后执行。
3. **防回归**：清理后重新跑 agent_sync，被撤回的内容不能"复活"（撤回语义已有，补测试确认）。
4. **文档**：`docs/agent-sync.md` 增加"过滤规则"一节；`STATUS.md` 更新数据来源说明与清理前后数字。

## 测试

- 单元：每条规则至少一个正例、一个反例（用户手动粘贴 `<environment_context>` 字样的普通长文本不应被误杀，如可区分）；
  guardian / thread_spawn 会话整体跳过；Claude Code 系统块剥离后为空即跳过、有用户文字则保留用户文字。
- 集成（合成会话文件）：解析 → 导入 → `clean-injected --dry-run` 计数正确 → `--apply` 后检索不再命中、
  再次同步不复活。
- 全量 pytest、ruff、mypy、collector JS 测试干净；CI 绿。

## 自用验收（真实本地库，只读统计，不输出任何正文）

- `clean-injected --dry-run` 的分来源、分规则统计；
- 抽样核对：随机抽 30 条将被撤回的、30 条将被保留的 owner 发言，**只输出前 40 个字符**给用户确认没有误杀；用户确认后再 `--apply`；
- `--apply` 后：最近 30 天各来源 owner 发言条数与总字数（预期 Codex 从约 2595 万字降到约百万字以下），并重跑
  `brain facts extract --dry-run --since 2026-09-03` 报告新的 token 估算。

## 交付

在新分支 `fix/collector-injected` 上开发（从 main 拉出；`feat/d2-fact-layer` 分支保持不动），走 PR，CI 全绿再合并。
不推送到 main、不动 VM、不打印密钥。交付：改动摘要、规则表、测试结果、dry-run 统计、抽样核对说明、清理前后数字、未解决问题。

## 已知取舍

- 宁可漏删，不可误删：规则只认明确的注入特征；拿不准的保留并在报告里列出。
- 撤回而非物理删除：可追溯、可恢复。
- VM 库的清理与 D-2 的正式提炼，都等主会话和用户确认后再做。
