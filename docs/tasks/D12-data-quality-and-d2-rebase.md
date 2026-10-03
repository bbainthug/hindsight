# 任务 D-12：D-2 合并准备 + 三个遗留数据质量问题

> 交付给实现 agent 的任务书。遵守 AGENTS.md：开工先读 `STATUS.md`，收工更新它。
> 背景见 `docs/tasks/D11-collector-quality.md`、`docs/tasks/D2-fact-layer.md` 与 STATUS.md 的"进行中""下一步"。
> 分两条分支、两个 PR 做，互不混在一起。

## 第一部分：D-2 合并准备（分支 `feat/d2-fact-layer`）

现状：D-2 事实层 v0 已在 `feat/d2-fact-layer`（3 个提交，未推送），它新增的 migration 编号是 **8**（`claims` 等表）。
但 main 上 D-11 已占用 migration 8（`withdrawal_audit`），**本地库和 VM 库都已升到 8**。

要做：
1. 把 `feat/d2-fact-layer` rebase 到最新 main，把 D-2 的 migration **改为 9**；所有涉及 schema 版本号的断言同步更新
   （不许删测试或放宽断言）。
2. 写一个回归测试：一个已经在 v8（含 `withdrawal_audit`、不含 `claims`）的库，打开后能正确升级到 v9 并建出 D-2 的表。
3. 在清理后的真实本地库（`~/.local/share/personal-brain/db/brain.sqlite`）上运行
   `brain facts extract --dry-run --since 2026-09-03`，报告估算的请求数与 token 数；**不做正式提炼**。
   如果 dry-run 会改动数据库（例如触发 migration 9），先拷贝一份库到 `/tmp` 在副本上跑，真实库保持不动。
4. 估算仍超过 50 万 tokens 时，给出 2–3 个收窄方案的估算（例如只提炼 ChatGPT + Claude Code；或按 `--source` 分批；
   或跳过超长的粘贴文本），写进交付报告，由用户选。
5. 推送分支、开 PR，CI 全绿后**等主会话验收再合并**（不要自己合并）。

## 第二部分：三个遗留数据质量问题（新分支 `fix/data-quality-2`，从 main 拉）

### A. Claude Code 后台任务通知被当成用户发言

Claude Code 会把 `<task-notification>…</task-notification>` 作为 user 消息注入（后台任务完成通知）。
在 `src/personal_brain/injected_rules.py` 的规则表里新增一条标签规则（开头完整块，剥离后为空则跳过），带正例反例测试。

### B. Codex 导入 Claude Code 会话造成的重复

Codex 桌面版会把 Claude Code 的会话导入成自己的线程。对照表在 `~/.codex/external_agent_session_imports.json`
（`records[].imported_thread_id` 是 Codex 线程 ID，`source_path` 是原 Claude 会话文件）。同一批对话因此在 `claude` 和 `codex`
两个来源各有一份。

- 采集：`sync_agents.py` 解析 Codex 时，读取这份对照表，`imported_thread_id` 命中的会话整体跳过（原件已由 Claude 来源采集）。
  对照表不存在或读不了时照常采集，不报错。
- 存量：`brain clean-injected` 增加对应规则（规则名如 `codex_imported_from_claude`），用同一份对照表识别，撤回 codex 来源里的重复会话。
  注意：清理时**只读**对照表的 `imported_thread_id` 字段。
- 规则表的注释写清这条规则的依据。

### C. Gemini 导出把读屏标签"你说"和正文重复抓了进来

`integrations/gemini_export/export_gemini.js` 用 `textContent` 读用户发言时，把页面上给读屏软件用的隐藏标签"你说"连同正文的另一份副本也抓了进来，
所以每条用户发言都是"你说 X（换行）X"的样子。

- 修导出脚本：读用户发言时排除隐藏的读屏元素（先在真实页面结构上确认是哪个元素；如果拿不到真实页面，就按
  "去掉开头的'你说'标签 + 去掉与前一段完全重复的副本"在转换器里处理，并在 README 里注明）。
- 修转换器 `convert_gemini.py`：增加规范化函数，对已有导出文件生效（`~/Downloads/gemini-export.json` 与
  `~/Downloads/gemini-export-p*.json` 还在）；单元测试覆盖"你说 X\n X"→"X"、正文本身不重复时不误删。
- 存量：用修好的转换器重新转换、导入（会产生新事件），再把旧的、带"你说"前缀的 gemini owner 发言逻辑撤回
  （通过 `clean-injected` 新规则或专门的一次性命令，撤回原因写清 `gemini_label_dup`）。assistant 消息不受影响的话保持不动。

### 第二部分的执行边界

- 所有清理先 `--dry-run`，在报告里给出分规则统计；抽样核对：每条规则随机抽 10 条，只输出前 40 个字符写到
  `/tmp/d12_sample.md`（不进仓库）。**不要执行 `--apply`，不要导入新的 Gemini 数据**，等用户确认。
- 部署到 launchd 副本（`~/.local/share/personal-brain/agent_sync/`）和 VM 的步骤由主会话做；你只在交付报告里写清楚需要部署哪些文件。

## 通用要求

- 全量 `uv run pytest -q`、`uv run ruff check .`、`uv run mypy src/personal_brain`、
  `node --test integrations/chatgpt_collector/tests/*.test.js` 全部通过；两个 PR 的 CI 都要绿。
- 不连接 VM，不读取或打印任何密钥 / 令牌文件，不直接推送 main，不自己合并 PR。
- 同一问题试两次仍失败就停下，写明原因与尝试。

## 交付

分两部分汇报：
1. D-2：rebase 与 migration 改号说明、回归测试、dry-run 估算（以及收窄方案的估算）、PR 链接与 CI 状态。
2. 数据质量：三条修复的改动摘要、规则表新增项、dry-run 分规则统计、抽样文件路径、需要主会话部署的文件清单、PR 链接与 CI 状态、未解决问题。
