# Hindsight 现状

> 项目当前状态的唯一可信来源。开始工作前先读；每次任务结束时更新（改"最后更新"、挪动条目）。
> 历史与理由看 `git log`、`docs/tasks/`（任务书带交付记录）、`docs/decisions-phase-a.md`；
> "当时怎么想的"去 Hindsight 本身检索。

最后更新：2026-10-04

## 一句话

把用户与各 AI（ChatGPT、Claude Code、Codex、DSH、Hermes、Gemini）的对话汇成一个可逐字溯源的本地档案，
通过只读 MCP 让 ChatGPT / Claude / Claude Code 检索。公开仓库：github.com/bbainthug/hindsight。

## 已上线

| 部分 | 状态 |
|---|---|
| 存储与导入 | SQLite；事件-版本模型，幂等导入；数据来源见下表 |
| 检索 | FTS5 中文二元切分 + 原文核对；语义检索（bge-small-zh，int8 量化 + fp32 重排）；RRF 混合 |
| 工具 | `brain_status` `search_history` `recall` `timeline` `get_recent_events` `get_event`（全部只读） |
| 事实层 | D-2 v0 本地 CLI（schema v9，PR #3 已合并）；候选经人工审核后激活；仅本地库，VM 未部署 |
| 远程 | Azure VM（2 vCPU / 1 GB）+ Cloudflare Tunnel，`https://brain.bbainthug.tech/mcp/<token>`；默认 `semantic_default: hybrid` |
| 接入方 | ChatGPT 连接器、claude.ai 自定义连接器（网页/手机/Claude Code 同步可用）、DSH 桌面端（本地 stdio 转接器 `integrations/dsh/hindsight_remote_bridge.py`，经 `127.0.0.1:7891` 代理连云端；dsh-mcp-client 的 Node 运行时不走系统代理，直连会被重置，且 Cloudflare 拒绝 Python-urllib UA） |
| 测试 | main（含 D-2 + D-12）全量 pytest 通过（2026-10-03）；CI：ruff / mypy / pytest / collector Node / gitleaks |

### 数据来源与采集方式

| 来源 | 采集 | 延迟 |
|---|---|---|
| Claude Code / Codex / DSH / Hermes | Mac launchd `local.hindsight.agent-sync` 每 15 分钟 → `local.hindsight.vm-push` → VM `inbox/` → VM 导入定时器每 10 分钟 | 15–30 分钟 |
| ChatGPT | Chrome 扩展 `integrations/chatgpt_collector`（D-6）+ 官方导出（`main`） | Chrome 开着时分钟级 |
| Gemini | 一次性：`integrations/gemini_export`（页面导出 390 对话 / 5469 条，**无时间**；D-12 修好转换器后重导） | 手动 |
| claude.ai 网页 / App | **未采集** | — |

只采 user / assistant 正文；工具调用、命令输出、系统提示、子代理线程一律不进库（安全、噪音、体积）。

### D-8 部署前后对比（2026-09-26，VM 空闲，热态）

| 指标 | 转换前（float 全量 KNN） | 转换后（int8 + fp32 重排） |
|---|---|---|
| 单次向量 KNN（10 个查询 P50） | 3.55 s | 0.91 s |
| `/api/search` hybrid "找工作" | P50 4.5 s / P95 5.9 s | P50 1.36 s / P95 1.50 s |
| `/api/recall` "找工作" | P50 5.2 s / P95 5.9 s | P50 1.74 s / P95 1.81 s |
| 真实库 10 个查询 top-10 重合率 | — | 1.0（均值与最低值） |

根因：向量表 457 MB，VM 总内存约 900 MB、MCP 常驻约 430 MB，放不进页缓存，每次查询读盘。
开发中测出过召回重合率 0.68，是两个问题叠加：评估用了无语义结构的 hash 假向量；int8 scale 先是固定 ×127
（只用到约 4% 动态范围），改动态 scale 时又一度写反成 `max|v|/127`。改用真实 bge 模型评估、修正 scale 后，
合成库 50 查询 int8 + oversample 4 重合率 1.000（见 `docs/unified-retrieval.md`、`semantic_index.py` 注释）。

### VM 实测（2026-09-26 D-8 部署后）

- recall P95 1.8 s；search hybrid P95 1.5 s（"找工作"）/ 0.26 s（"面试准备"）；timeline 30 天 0.35 s
- 宽泛两字词（"工作""项目"）连 **exact 本身**就要 3–7 s（旧问题，见 D-9）
- 增量语义索引：新增几个事件约 30 s
- 数据量（2026-09-28）：111,189 事件，其中 gemini 5,469

## 进行中

- **D-2 事实层首次正式提炼**（用户 2026-10-04 同意）：按推荐方案按来源分 5 批，`--since 2026-09-03`，
  owner≤300 字、无 assistant 上下文、段 48k、输出 1200；估算 claude 35 万 / codex 47 万 / dsh 22 万 / main 10 万 /
  hermes 4 万 tokens（D-12 清理前的副本估算，清理后会更少）。模型 DeepSeek，密钥由用户写入本机 env 文件。
  提炼结果全部是候选，需 `brain facts review` 人工审核。

## 已完成（近期）

- **D-12 三个数据质量修复**（2026-10-03，PR #4；同日 PR #3 D-2 rebase 后合并）：
  规则表新增 `claude_task_notification_block`、`codex_imported_from_claude`（Codex 导入对照表去重：
  同步跳过 + 存量整会话撤回，只读 `imported_thread_id`）、`gemini_label_dup`（"你说"+重复副本归一化，转换器与清理共用）。
  - 本地库与 VM 库都已 `--apply`：各撤回 38,604 个事件（owner 78 / 791 / 2,234）；备份：本地
    `~/.local/share/personal-brain/backups/backup-2026-10-03T063854…`，VM `~/.local/share/personal-brain/backups/backup-2026-10-03T070324…`（2.8 GB，注意不在 `~/brain-data/backups`）。
  - 37 个被 Codex 导入的 Claude 会话，原件全部在库（核对缺失 0），撤回只去副本。
  - Gemini 用修好的转换器重导：本地与 VM 各新增 4,464 事件。**坑**：转换器的消息身份用 fallback
    （父节点 + 序号 + 内容），用户发言改动后其后 assistant 的父节点变了，2,230 条 assistant 也生成了新事件；
    旧 assistant 与新版逐字相同（2230/2230 核对），用一次性脚本按"不在最新快照所选路径上"撤回，原因 `gemini_superseded_reimport`
    （脚本未入库）。以后任何改写用户正文的重导都会有同样问题。
  - VM 语义索引增量 4,464 事件耗时 18.6 分钟（导入服务 MemoryMax 400M 顶满，用了 swap）。
  - launchd 副本已同步 `sync_agents.py` 与 `injected_rules.py`（旧版 `*.bak-20261003-d12`），试跑正常。
- **D-11 采集器假发言过滤**（2026-10-03，PR #2）：规则表 `injected_rules.py` 解析与清理共用；`brain clean-injected`
  默认 dry-run、`--apply` 先备份再逻辑撤回（原因写入 `withdrawal_audit`）。
  - 本地库与 VM 库都已清理：各撤回 8665 个事件（约 5640 万字）；备份在各自 `backups/backup-2026-10-03T…`。
  - 最近 30 天 Codex owner 发言 2609 万字 → 85 万字；VM 上搜索注入文本已无 owner 命中。
  - VM 清理时用的 Codex 子 agent 识别，来自 Mac 上 633 个会话文件的精简 `session_meta`（仅 id 与 source）。

## 下一步 / 暂缓（用户决定"先用用看"）

- **Gemini 剩余 495 条"你说"发言**：另一种形态"你说 <截断预览>…\n<完整正文>"（预览带省略号，形式不一），
  `gemini_label_dup` 不处理。与 `export_gemini.js` 本体修复一起做（需真实页面结构）；修后要再重导 + 撤回旧事件（含 assistant，见上面的坑）。
- **D-9**：宽泛词 exact 检索在 VM 上 3–7 s。嫌慢再立项。
- **D-10（候选）**：采集 claude.ai 网页/App 对话（先试官方数据导出 + 导入器）。
- **候选**：存工具调用摘要（只存工具名 + 命令，不存输出，过脱敏），用于"我上次部署跑了哪些命令"。

## 待办小事

- VM `~/brain-data/backups/brain-pre-d8.sqlite`（1.9 GB）：确认稳定后删除。
- 仓库根目录有来历不明的未跟踪文件 `:memory:.ses`，未处理。

## 运维速查

- SSH：`ssh hindsight-vm`（经 Cloudflare Access，SSH config 已配代理）；`hindsight-ts` 走 Tailscale
- VM 服务：`systemctl --user {status,restart} hindsight-mcp`；导入 `hindsight-import.timer`
- VM 配置：`~/brain-data/remote.yaml`；令牌在 `~/.config/hindsight/env`（不要打印）
- 本地库：`~/.local/share/personal-brain/db/brain.sqlite`；`uv run brain --db <库> status`
- 部署：`git archive HEAD | ssh hindsight-vm 'cd ~/hindsight && tar -x'`，注意脚本可执行位；
  有 migration 时先停 MCP，详见 `deploy/vm/README.md`
