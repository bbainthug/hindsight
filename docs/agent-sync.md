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

- `local.hindsight.agent-sync`：每 15 分钟采集，日志 `$BRAIN_HOME/agent_sync/sync.log`
- `local.hindsight.vm-push`：每 30 分钟把库快照推到 VM（`deploy/vm/push_to_vm.py`），
  库没变就跳过；日志 `vm_push.log`

脚本会被复制到 `$BRAIN_HOME/agent_sync/`（默认 `~/.local/share/personal-brain`）——
launchd 没有权限读 `~/Documents` 这类受 TCC 保护的目录，所以私有数据根和脚本都不能放那里。

## 加一个新来源

写一个 `parse_xxx(path) -> dict | list[dict]`，用 `Conv` 收集 `(时间, 角色, 文本, 稳定ID)`，
在 `SOURCES` 里登记路径 glob。稳定 ID 决定去重，务必用来源自己的消息 ID，不要用行号。
