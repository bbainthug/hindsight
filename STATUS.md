# Hindsight 现状

> 项目当前状态的唯一可信来源。开始工作前先读；每次任务结束时更新（改"最后更新"、挪动条目）。
> 历史与理由看 `git log`、`docs/tasks/`（任务书带交付记录）、`docs/decisions-phase-a.md`；
> "当时怎么想的"去 Hindsight 本身检索。

最后更新：2026-10-03

## 一句话

把用户与各 AI（ChatGPT、Claude Code、Codex、DSH、Hermes、Gemini）的对话汇成一个可逐字溯源的本地档案，
通过只读 MCP 让 ChatGPT / Claude / Claude Code 检索。公开仓库：github.com/bbainthug/hindsight。

## 已上线

| 部分 | 状态 |
|---|---|
| 存储与导入 | SQLite；事件-版本模型，幂等导入；数据来源见下表 |
| 检索 | FTS5 中文二元切分 + 原文核对；语义检索（bge-small-zh，int8 量化 + fp32 重排）；RRF 混合 |
| 工具 | `brain_status` `search_history` `recall` `timeline` `get_recent_events` `get_event`（全部只读） |
| 远程 | Azure VM（2 vCPU / 1 GB）+ Cloudflare Tunnel，`https://brain.bbainthug.tech/mcp/<token>`；默认 `semantic_default: hybrid` |
| 接入方 | ChatGPT 连接器、claude.ai 自定义连接器（网页/手机/Claude Code 同步可用） |
| 测试 | 426 项 pytest，ruff / mypy 干净（D-8 时） |

### 数据来源与采集方式

| 来源 | 采集 | 延迟 |
|---|---|---|
| Claude Code / Codex / DSH / Hermes | Mac launchd `local.hindsight.agent-sync` 每 15 分钟 → `local.hindsight.vm-push` → VM `inbox/` → VM 导入定时器每 10 分钟 | 15–30 分钟 |
| ChatGPT | Chrome 扩展 `integrations/chatgpt_collector`（D-6）+ 官方导出（`main`） | Chrome 开着时分钟级 |
| Gemini | 一次性：`integrations/gemini_export`（页面导出 390 对话 / 5469 条，**无时间**） | 手动 |
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

- **D-2 事实层 v0**：实现在本地分支 `feat/d2-fact-layer`（3 个提交，510 测试通过，未推送）。合并前必须：
  ① rebase 到 main，**把 D-2 的 migration 从 8 改为 9**（main 的 8 已被 D-11 的 `withdrawal_audit` 占用，VM 与本地库都已升到 8）；
  ② 在清理后的本地库重跑 `brain facts extract --dry-run --since 2026-09-03`。按比例粗估约 450 万 tokens（清理前 6400 万），
  仍超 50 万预算，v0 可能先只提炼部分来源；③ 用户决定模型与预算后再正式提炼。

## 已完成（近期）

- **D-11 采集器假发言过滤**（2026-10-03，PR #2）：规则表 `injected_rules.py` 解析与清理共用；`brain clean-injected`
  默认 dry-run、`--apply` 先备份再逻辑撤回（原因写入 `withdrawal_audit`）。
  - 本地库与 VM 库都已清理：各撤回 8665 个事件（约 5640 万字）；备份在各自 `backups/backup-2026-10-03T…`。
  - 最近 30 天 Codex owner 发言 2609 万字 → 85 万字；VM 上搜索注入文本已无 owner 命中。
  - launchd 部署副本 `~/.local/share/personal-brain/agent_sync/` 已同步新版 `sync_agents.py` 与 `injected_rules.py`
    （副本无 personal_brain 包时从同目录导入规则表）；旧版留 `sync_agents.py.bak-20261003`。
  - VM 清理时用的 Codex 子 agent 识别，来自 Mac 上 633 个会话文件的精简 `session_meta`（仅 id 与 source）。

## 下一步 / 暂缓（用户决定"先用用看"）

- **D-11 遗留**：Claude Code 的 `<task-notification>` 后台任务通知仍被当作 owner 发言，需补一条规则；
  Codex 桌面版会导入 Claude Code 会话，导致同一批 Claude 对话在 claude 与 codex 两个来源各有一份（如 20 条续写摘要），待去重。
- **Gemini 导出缺陷**：每条用户发言带页面隐藏标签"你说"且正文重复一遍（`export_gemini.js` 抓到了读屏标签），
  需修导出与转换，并撤回重导已入库的 gemini owner 发言。
- **D-9**：宽泛词 exact 检索在 VM 上 3–7 s。嫌慢再立项。
- **D-10（候选）**：采集 claude.ai 网页/App 对话（先试官方数据导出 + 导入器）。
- **候选**：存工具调用摘要（只存工具名 + 命令，不存输出，过脱敏），用于"我上次部署跑了哪些命令"。

## 待办小事

- VM `~/brain-data/backups/brain-pre-d8.sqlite`（1.9 GB）：确认稳定后删除。

## 运维速查

- SSH：`ssh hindsight-vm`（经 Cloudflare Access，SSH config 已配代理）；`hindsight-ts` 走 Tailscale
- VM 服务：`systemctl --user {status,restart} hindsight-mcp`；导入 `hindsight-import.timer`
- VM 配置：`~/brain-data/remote.yaml`；令牌在 `~/.config/hindsight/env`（不要打印）
- 本地库：`~/.local/share/personal-brain/db/brain.sqlite`；`uv run brain --db <库> status`
- 部署：`git archive HEAD | ssh hindsight-vm 'cd ~/hindsight && tar -x'`，注意脚本可执行位；
  有 migration 时先停 MCP，详见 `deploy/vm/README.md`
