# 任务 D-5：VM 同步改为"传原料、就地导入"

> 交付给实现 agent 的任务书。遵守 AGENTS.md 工作约定。

## 背景

现在 VM（香港 Azure，1 GB 内存）上的库是 Mac 库的整份副本：`deploy/vm/push_to_vm.py`
每 30 分钟做 SQLite 快照（约 840 MB），经 Tailscale rsync 过去再原子替换。
这条路依赖 Mac 醒着 + Tailscale 通 + 本机代理不接管 `100.x` 路由 + 大文件传完，
实际三天内断了两次（Tailscale 被代理打断、rsync 中途超时）。

`integrations/agent_sync/sync_agents.py` 本来就是先把各家 agent 会话转成
`conversations.json`，再调 `brain import-chatgpt` 导入；导入器幂等（按来源消息 ID 去重）。
所以不必搬库，只搬这些 JSON 批次，让 VM 自己导入即可。每批通常几十 KB。

## 目标

Mac 只推"新增会话批次"到 VM，VM 定时就地导入。去掉对 Tailscale 和大文件传输的依赖；
批次走现有 SSH 别名 `hindsight-vm`（Cloudflare 隧道）即可。

## 范围

1. `sync_agents.py` 增加 `--export-dir DIR`：除（或代替，由 `--no-import` 控制）本地导入外，
   把每个来源的 `conversations.json` 写成批次文件
   `DIR/<source>/<UTC时间戳>-<内容sha256前12位>.json`（原子写：先写 `.tmp` 再 rename）。
   批次用**独立的断点文件**（`--export-state`，默认 `$BRAIN_HOME/agent_sync/export_state.json`），
   与本地导入的 `state.json` 互不影响。
2. Mac 侧 `deploy/vm/push_batches.py`：把 outbox 里未推送的批次 rsync 到 VM 的
   `~/brain-data/inbox/<source>/`，成功后移到 `outbox/sent/`（保留 14 天后清理）。
   失败不删、下次重试。只传批次，永不传库。
3. VM 侧 `deploy/vm/import_inbox.sh` + systemd **user** timer `hindsight-import.timer`（每 10 分钟）：
   按文件名顺序对每个批次执行 `brain --db ~/brain-data/db/brain.sqlite import-chatgpt <批次所在目录> --source <source>`，
   成功移到 `inbox/done/`，失败移到 `inbox/failed/` 并记日志，不阻塞后续批次。
   用 `flock` 防止两次运行重叠。导入后如语义索引已存在，跑一次 `reindex-semantic` 增量（可用配置关闭）。
4. launchd：`local.hindsight.agent-sync` 改为同时导出批次；`local.hindsight.vm-push` 改为跑
   `push_batches.py`（每 15 分钟）。更新 `integrations/agent_sync/install-launchd.sh` 与模板。
5. 首次追平：VM 当前库停在 2026-09-20。部署后用空的 `export_state.json` 跑一次全量导出，
   VM 导入时幂等去重即可追平，**不需要再传整库**。文档里写清这一步和预期批次体积。
6. 保留 `push_to_vm.py`，改名/标注为手动"整库重置"工具，文档说明何时用（VM 库损坏或要带上撤回/标注时）。
7. 文档：更新 `docs/agent-sync.md`、`docs/remote-access.md` 的同步一节；运维说明写进 `deploy/vm/README.md`。

## 非目标

- 不做双向同步；VM 不回写 Mac。
- 不同步撤回 / 标注：它们只在 Mac 库生效，需要时用整库重置。在文档"已知限制"写明。
- ChatGPT 官方导出（`main` 来源）仍在 Mac 手动导入，本任务不处理；需要时手动把导出 zip 放进 VM inbox 即可，文档写一句。
- 不改导入器、检索、权限代码。

## 设计要求

- 批次文件名含内容哈希：同一内容重复导出得到同名文件，推送与导入都天然去重。
- VM 导入与 `hindsight-mcp` 读服务并存：库为 WAL 模式，导入期间读不阻塞；不重启服务。
  需实测一次：导入进行中调用 `/api/search` 仍返回 200。
- 1 GB 内存：导入按单批次顺序跑，不并行；systemd unit 设 `MemoryMax=400M`。
- 所有路径、主机别名可用环境变量覆盖；仓库里不出现真实主机名、IP、token。
- 日志只记批次名、事件数、耗时，不记会话正文。

## 测试

- 单元：`--export-dir` 写出的文件名稳定（同内容同名）、原子写、导出断点与导入断点互不干扰。
- 集成（合成数据，临时目录，不出网）：同一批次导入两次，第二次新增事件为 0；
  故意放一个损坏批次，确认它进 `failed/` 且后续批次照常导入。
- 现有测试全绿；`ruff check src tests integrations deploy`、`mypy src` 干净。

## 交付

改动摘要、可复现命令与结果、未解决问题；另附：
- 在我机器上一次常规同步的批次数与字节数（取 `vm_push.log` 实际数据）；
- 首次追平导出的总字节数与 VM 导入耗时；
- 导入进行中 `/api/search` 的状态码与耗时。

## 已知取舍（不用来回问）

- 两端各跑一次导入器、各自持有一份库：换来的是同步只传 KB 级数据、不依赖私网。
- 撤回 / 标注不跨端：我几乎不用，出现需要时整库重置一次即可。
- 走 Cloudflare 隧道的 SSH：批次很小，不会重演整库传输堵塞隧道的问题。
