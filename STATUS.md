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

- **D-11 采集器假发言过滤**：实现已在本地分支 `fix/collector-injected`；同步解析器与存量清理共用规则表，新增带撤回原因审计的逻辑清理命令。尚未部署。
  - 真实本地库只读 dry-run：4806 条 owner 发言 / 56,336,814 字符命中规则，影响 8656 个事件；识别 Codex 子 agent 会话 489 / 633。
  - 最近 30 天 owner 发言预期（当前 → 清理后，条数 / 字符）：Claude 707 / 360,223 → 684 / 306,554；Codex 2389 / 26,030,080 → 1250 / 847,185；DSH 548 / 1,427,355 → 325 / 922,280；Hermes 68 / 34,570、main 186 / 15,174 不变。Gemini 没有近 30 天时间戳可供统计（另有 2727 条未知时间）。
  - **存量清理尚未执行**，等待用户抽样核对并确认；未连接 VM。
  - ruff、mypy、collector JS 检查通过；完整 pytest 中旧 soak 汇总门槛连续两次未通过，单独运行该测试一次通过，未放宽检查。

## 下一步 / 暂缓（用户决定"先用用看"）

- **D-11**：先核对 `/tmp/d11_sample.md`（撤回 / 保留各 30 条，每条至多 40 字符）；用户确认后再执行本地 `clean-injected --apply`。pytest 的 soak 汇总门槛失败原因尚未定位；按约定停止重复尝试。VM 清理仍待主会话验收后执行。
- **D-9**：宽泛词 exact 检索在 VM 上 3–7 s。嫌慢再立项。
- **D-2**：从聊天记录自动提炼事实（偏好、经历、决定）。需用户先定方向。
- **D-10（候选）**：采集 claude.ai 网页/App 对话（先试官方数据导出 + 导入器）。
- **候选**：存工具调用摘要（只存工具名 + 命令，不存输出，过脱敏），用于"我上次部署跑了哪些命令"。

## 待办小事

- VM `~/brain-data/backups/brain-pre-d8.sqlite`（1.9 GB）：确认稳定后删除。
- 本地比 GitHub 多 1 个提交（Gemini 导出工具），推送需用户同意。
- 简历上 "280 项 Pytest" 已过时（现 426）。

## 运维速查

- SSH：`ssh hindsight-vm`（经 Cloudflare Access，SSH config 已配代理）；`hindsight-ts` 走 Tailscale
- VM 服务：`systemctl --user {status,restart} hindsight-mcp`；导入 `hindsight-import.timer`
- VM 配置：`~/brain-data/remote.yaml`；令牌在 `~/.config/hindsight/env`（不要打印）
- 本地库：`~/.local/share/personal-brain/db/brain.sqlite`；`uv run brain --db <库> status`
- 部署：`git archive HEAD | ssh hindsight-vm 'cd ~/hindsight && tar -x'`，注意脚本可执行位；
  有 migration 时先停 MCP，详见 `deploy/vm/README.md`
