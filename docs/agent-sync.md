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
