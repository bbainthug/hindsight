# agent_sync：把本机 coding agent 的会话自动收进库

`integrations/agent_sync/sync_agents.py` 每隔一段时间扫本机各家 agent 的会话存储，
把新增/变化的会话转成 ChatGPT 导出格式，交给 `brain import-chatgpt` 导入。
导入器本身幂等（按消息 ID 去重），所以重复跑、中途断都没关系。

| 来源 | 位置 | 格式 |
|---|---|---|
| Codex | `~/.codex/sessions/**/*.jsonl` | 每会话一个 jsonl |
| DSH（DeepSeek Harness） | `~/.dsh/sessions/**/session.jsonl.zstd` | zstd 压缩 jsonl，需要 `zstd` 命令 |
| Claude Code | `~/.claude/projects/**/*.jsonl` | 每会话一个 jsonl |
| Hermes | `~/.hermes/state.db` | sqlite，一个库多会话 |

只采 `user` / `assistant` 正文；工具调用、系统提示、思考过程不进库。
每个来源在库里是独立的 `source`（`--source codex` 等），检索时可按来源过滤。

## 过滤规则

解析器和 `brain clean-injected` 共用 `personal_brain.injected_rules.RULES` 规则表。
只检查明确的会话/消息元数据、固定前缀和完整标签块；不按消息长度判断。
所以用户自己粘贴的长日志、代码和 JD 会保留。规则正反例由
`tests/unit/test_injected_rules.py` 遍历整张表验证。

| 类型 | 规则名 | 识别方式 | 处理方式 |
|---|---|---|---|
| 会话元数据 | `codex_subagent_source` | `session_meta.payload.source.subagent` 存在（guardian、thread_spawn 等） | 跳过整个会话；存量清理按会话撤回 |
| 会话元数据 | `claude_sidechain` | Claude Code JSONL `isSidechain: true` | 跳过整个会话 |
| 会话元数据 | `claude_is_meta` | Claude Code JSONL `isMeta: true` | 跳过该消息 |
| 会话元数据 | `claude_compact_summary_flag` | Claude Code JSONL `isCompactSummary: true` | 跳过该消息 |
| 前缀 | `codex_guardian_history` | `The following is the Codex agent history` | 跳过整个会话 |
| 前缀 | `codex_agents_md` | `# AGENTS.md instructions` | 跳过该条 user 消息 |
| 前缀 | `continued_conversation_summary` | `This session is being continued from a previous conversation` | 跳过该条 user 消息 |
| 前缀 | `codex_turn_aborted` | `<turn_aborted>` | 跳过该条 user 消息 |
| 前缀 | `claude_caveat` | `Caveat: the following content is system generated` | 跳过该条 user 消息 |
| 前缀 | `dsh_runtime_context` | `Current runtime context` | 跳过该条 user 消息 |
| 前缀 | `dsh_skills_preamble` | `The following skills are available in this session:` | 跳过该条 user 消息 |
| 标签 | `system_reminder_block` | 消息开头的完整 `<system-reminder>…</system-reminder>` | 剥离该块；剩余为空则跳过 |
| 标签 | `command_name_block` | 消息开头的完整 `<command-name>…</command-name>` | 剥离该块；剩余为空则跳过 |
| 标签 | `command_message_block` | 消息开头的完整 `<command-message>…</command-message>` | 剥离该块；剩余为空则跳过 |
| 标签 | `command_args_block` | 消息开头的完整 `<command-args>…</command-args>` | 剥离该块；剩余为空则跳过 |
| 标签 | `local_command_stdout_block` | 消息开头的完整 `<local-command-stdout>…</local-command-stdout>` | 剥离该块；剩余为空则跳过 |
| 标签 | `recommended_plugins_block` | 消息开头的完整 `<recommended_plugins>…</recommended_plugins>` | 剥离该块；剩余为空则跳过 |
| 标签 | `environment_context_block` | 消息开头的完整 `<environment_context>…</environment_context>` | 剥离该块；剩余为空则跳过 |
| 标签 | `subagent_notification_block` | 消息开头的完整 `<subagent_notification>…</subagent_notification>` | 剥离该块；剩余为空则跳过 |
| 标签 | `skills_list_block` | 消息开头的完整 `<available_skills>…</available_skills>` | 剥离该块；剩余为空则跳过 |
| 标签 | `claude_task_notification_block` | 消息开头的完整 `<task-notification>…</task-notification>`（Claude Code 后台任务通知，D-12） | 剥离该块；剩余为空则跳过 |
| 导入对照 | `codex_imported_from_claude` | `~/.codex/external_agent_session_imports.json` 的 `records[].imported_thread_id`（Codex 桌面版导入的 Claude 会话，原件已由 claude 来源采集；只读该字段，D-12） | 同步时跳过整个会话；存量清理按会话撤回 |
| 归一化 | `gemini_label_dup` | Gemini 导出把读屏标签"你说"与正文重复副本一起抓进来（形如"你说 X\nX"，D-12） | 撤回旧 owner 事件；导出转换时归一化（见 gemini_export/README） |

标签只剥离消息开头连续出现的完整块；普通正文中提到标签、未闭合标签不会触发。
`codex_imported_from_claude` 由对照表驱动（默认读取，`--no-codex-import-map` 关闭），
对照表不存在或读不了时照常工作。`gemini_label_dup` 只作用于 gemini 来源的 owner
发言，归一化实现 `normalize_gemini_user_text` 同时被 `convert_gemini.py` 用于新导出。
同步时，剥离后仍有用户文字就只保留剩余文字。对旧库，清理命令只逻辑撤回
整条都由注入块构成的事件；混有用户文字的旧事件会保留，避免为了清掉注入片段
而误撤回用户内容。字数按 `raw_text` 的 Unicode 字符数统计。

用法：

```bash
brain --db ~/.local/share/personal-brain/db/brain.sqlite clean-injected --dry-run
brain --db ~/.local/share/personal-brain/db/brain.sqlite clean-injected --dry-run \
  --sample-file /tmp/d11_sample.md --sample-size 30
brain --db ~/.local/share/personal-brain/db/brain.sqlite clean-injected --apply
```

默认 dry-run 使用只读 SQLite 连接。`--apply` 会先生成一致性备份，再按
`injected_by_agent:<rule_name>` 写入撤回审计并逻辑撤回；不物理删除内容。Codex
旧会话没有把 `session_meta` 持久化进库，因此本机清理会只读扫描
`~/.codex/sessions/**/*.jsonl` 的 `session_meta` 行来关联 thread_spawn / guardian
会话。若在其他机器对迁移来的库运行，需提供原会话目录；目录缺失时只能靠存量正文
规则识别这些会话。

## 手动跑

```bash
brain --help >/dev/null   # 确认 brain 在 PATH，或用 BRAIN_CLI=/path/to/brain
python3 integrations/agent_sync/sync_agents.py                      # 四个来源全扫
python3 integrations/agent_sync/sync_agents.py --sources codex,hermes
```

断点存在 `$BRAIN_HOME/agent_sync/state.json`（按文件 mtime+size），删掉就全量重扫。

## 定时（macOS launchd）

```bash
BRAIN_CLI=/path/to/brain integrations/agent_sync/install-launchd.sh
# 有远程副本的话：
BRAIN_CLI=/path/to/brain integrations/agent_sync/install-launchd.sh --vm-host hindsight-vm
```

- `local.hindsight.agent-sync`：每 15 分钟采集 + 本地导入 + 导出批次（`--export-dir`），
  日志 `$BRAIN_HOME/agent_sync/sync.log`
- `local.hindsight.vm-push`：每 15 分钟把 outbox 里未推送的批次 rsync 到 VM
  （`deploy/vm/push_batches.py`）；outbox 空则跳过；日志 `vm_push.log`

脚本会被复制到 `$BRAIN_HOME/agent_sync/`（默认 `~/.local/share/personal-brain`）——
launchd 没有权限读 `~/Documents` 这类受 TCC 保护的目录，所以私有数据根和脚本都不能放那里。

## 批次导出与 VM 同步（D-5）

`sync_agents.py --export-dir DIR`（launchd 模板默认
`$BRAIN_HOME/agent_sync/outbox`）把每个来源本轮转写的会话额外写成批次文件：

```
outbox/<source>/<UTC时间戳>-<内容sha256前12位>.json
```

- 原子写（先 `.tmp` 再 rename）；文件名哈希段保证同一内容只对应一个批次文件，
  推送与导入都天然去重。
- 导出断点 `--export-state`（默认 `$BRAIN_HOME/agent_sync/export_state.json`）
  与本地导入断点 `--state` 完全独立：删掉其中一个不影响另一个；`--no-import`
  只产出批次、不推进导入断点。
- `deploy/vm/push_batches.py` 把 outbox 里未推送批次 rsync 到
  `hindsight-vm:~/brain-data/inbox/<source>/`（走 Cloudflare 隧道 SSH），
  成功后移 `outbox/sent/`（保留 14 天自动清理），失败留在原地下次重试。
  **只传批次，永不传库**——常规增量每批几十 KB～几 MB。
- VM 侧 `deploy/vm/import_inbox.sh` 由 systemd user timer
  `hindsight-import.timer`（每 10 分钟）驱动：按文件名顺序单批次顺序导入，
  成功移 `inbox/done/`、失败移 `inbox/failed/`（不阻塞后续批次），
  `flock` 防重叠；导入后如语义索引已存在则做一次增量 reindex
  （`HINDSIGHT_SEMANTIC_REINDEX=0` 可关闭）。部署与运维见
  `deploy/vm/README.md`。

### 首次追平（不需要传整库）

VM 库落后很多时：清空（或换新的）`export_state.json` 跑一次全量导出，推过去后
VM 导入器按消息 ID 幂等去重即可追平。追平批次的体积 ≈ 全部可索引会话的 JSON
总量（本机实测约 100 MB / 4 个批次，一次性）；之后常规轮转每 15 分钟通常只有
几十 KB～几 MB。追平完成后记得把追平用的导出断点复制回默认路径，避免 launchd
再全量导出一次。

## 加一个新来源

写一个 `parse_xxx(path) -> dict | list[dict]`，用 `Conv` 收集 `(时间, 角色, 文本, 稳定ID)`，
在 `SOURCES` 里登记路径 glob。稳定 ID 决定去重，务必用来源自己的消息 ID，不要用行号。
