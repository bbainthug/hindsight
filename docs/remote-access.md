# D-3：远程接入（HTTP transport）

对应任务书 [`docs/tasks/D3-remote-access.md`](tasks/D3-remote-access.md)。承接
Phase B 的只读 MCP（[`docs/phase-b-mcp.md`](phase-b-mcp.md)）：同一套
`build_server(config, conn)` / `service.tool_*`，只是多了一层 HTTP transport，
访问控制与"只读"边界完全不变。

## 架构

```
                          ┌───────────────────────────┐
  手机 / claude.ai ──────►│   Cloudflare（边缘网络）    │
  任意远程 agent           │   - TLS 终结               │
                          │   - Access：邮箱 OTP 登录   │  （只保护 REST/页面）
                          └──────────────┬──────────────┘
                                         │ Cloudflare Tunnel（cloudflared）
                                         ▼
                          ┌───────────────────────────┐
                          │  VM（Ubuntu 24.04）        │
                          │  systemd --user 常驻服务    │
                          │  personal-brain-mcp        │
                          │  --transport http           │
                          │  只绑 127.0.0.1:8765        │
                          │  ├─ /mcp/<token>  (MCP)     │
                          │  ├─ /api/*        (REST)    │
                          │  └─ /            (查询页)   │
                          └──────────────┬──────────────┘
                                         │ 只读 SQLite 连接池
                                         ▼
                          ~/brain-data/brain.sqlite（Mac 侧快照副本，单向 push）
```

进程本身**不做**TLS、不做 DDoS 防护、不做身份认证的密码学基础设施——这些全部
交给 Cloudflare；服务端只做两件事：校验能力 URL token / 可选校验 Access JWT，
以及保持只读。

## 两条接入路径

### 路径 A：claude.ai / 手机 agent（MCP 能力 URL）

在 claude.ai（或任何支持自定义 MCP 连接器的客户端）里添加：

```
https://brain.<your-domain>/mcp/<token>
```

`<token>` 是 `BRAIN_MCP_TOKEN`（≥32 字节随机 hex，部署时由
[`deploy/vm/install.sh`](../deploy/vm/install.sh) 生成，存于
`~/.config/hindsight/env`）。这条路径**不走 Cloudflare Access**——远程 MCP
连接器目前既不能完成 Access 的登录跳转，也不能附带自定义 HTTP header，所以
访问控制完全落在"URL 本身是秘密"上（见下面的威胁模型）。

### 路径 B：浏览器查询页（Cloudflare Access）

```
https://brain.<your-domain>/
```

打开后先过 Cloudflare Access 的邮箱 OTP 登录（只允许配置里指定的邮箱），
登录后看到单文件查询页（[`src/personal_brain/mcp_server/static/index.html`](../src/personal_brain/mcp_server/static/index.html)）：
搜索框 + exact/hybrid 切换 + 说话者过滤 + 时间范围，结果可展开原文（分页），
底部常驻显示覆盖期与 delivery_boundary 提醒。页面调用的 `/api/*` REST 端点
同样受 Access 保护（若配置了 `remote.access_aud`）。

## 威胁模型

- **MCP token 泄露 = 该 profile 可见的全部内容泄露。** token 只是路径的一段，
  一旦被日志、浏览器历史、截图、第三方连接器缓存等途径记录并外泄，任何人
  凭这个 URL 都能像受信任客户端一样调用全部工具。这是"能力 URL"模式固有的
  代价（换来的是不需要在服务端存密码、不需要 OAuth 服务端）。
  - **轮换方法**：编辑 `~/.config/hindsight/env` 里的 `BRAIN_MCP_TOKEN`，
    `systemctl --user restart hindsight-mcp`；旧 URL 立即 404。轮换后需要
    在所有已配置的客户端里更新新 URL。
  - 路径不匹配一律 404（不区分"token 错误"与"路径不存在"），避免给爆破
    尝试任何可区分信号。
- **Cloudflare Access 保护页面/REST，不保护 MCP 路径。** 这是有意的架构
  取舍（见任务书"已知取舍"），不是疏漏——不要误以为配置了 Access 就等于
  MCP 端点也有登录保护。真正保护 MCP 端点的是 token 本身的熵与保密性。
- **服务端只读**是纵深防御的一层：即使 token 泄露，攻击者也无法导入、
  标注、撤回或以任何方式修改数据；能读到的内容边界由绑定的 `profile`
  （`allow_scopes` / `deny_sensitivity` / `delivery_boundary`）决定，和本地
  stdio 部署完全一样的权限模型。
- **请求日志不记录 query 内容与返回正文**（只记录路径、状态码、耗时），
  降低"日志本身变成敏感信息泄露点"的风险；`/mcp/<token>` 的路径同样会被
  遮蔽为 `/mcp/<redacted>`——否则日志会把能力 URL 的 token 明文写进
  `journalctl`，等于自己制造了一个新的泄露面。但 `journalctl` 仍然是本机
  可读的，VM 账户本身的安全性（SSH 密钥、系统更新）不在本任务范围内。
- **限速是简单的防误用阈值**（每来源 IP 60 请求/分钟），不是防 DDoS——
  DDoS 防护同样交给 Cloudflare。

## 限制

- **单用户**：没有多租户、没有用户注册；`profile` 在服务启动时绑定，所有
  通过某个 token/Access 登录进来的调用方共享同一个 profile 的可见范围。
- **只读**：不暴露任何写入端点（导入、标注、撤回一律不在 HTTP 路径上）。
- **无 OAuth**：MCP 端走能力 URL 而不是标准 OAuth 流程，因为目标客户端
  （claude.ai 自定义连接器、手机 agent）目前不支持自定义 header，搭一个
  OAuth 授权服务器又超出本任务范围。如果未来目标客户端支持 OAuth，这里是
  首选的升级路径。
- **单进程单端口**：MCP + REST + 静态页面共享同一个 uvicorn 进程，是为
  1 GB 内存的 VM 省资源；不要拆成两个服务（会翻倍内存开销）。
- **VM 库由批次同步驱动（D-5）**：Mac 每 15 分钟导出新增会话批次并 rsync 到
  VM inbox（`push_batches.py`，走 Cloudflare 隧道 SSH），VM 每 10 分钟由
  `hindsight-import.timer` 就地导入（`import_inbox.sh`，flock 防重叠、
  单批次顺序、与读服务并存不重启）。只传批次（常规几十 KB～几 MB），永不传库；
  VM 上不产生新会话数据，也不回传。整库快照（`reset_vm_db.py`，原
  `push_to_vm.py` / `sync-db.sh`）仅作手动重置手段——VM 库损坏或需要带上
  撤回/标注时使用。

## 部署

见 [`deploy/vm/README.md`](../deploy/vm/README.md)（`install.sh` 一条命令起
服务 + cloudflared 配置步骤 + Access 策略配置 + 批次同步/定时导入 + 手动整库重置）。

## 快速开始（本地跑 HTTP transport，不经过 Cloudflare）

```bash
uv sync --extra remote
export BRAIN_MCP_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
personal-brain-mcp --config config/config.yaml --transport http --port 8765
# 另一个终端：
curl -s http://127.0.0.1:8765/api/status
open "http://127.0.0.1:8765/mcp/$BRAIN_MCP_TOKEN"   # 仅用于确认路径存在，浏览器直接打开不是合法 MCP 请求
```

## 性能（D-5b：覆盖期缓存）

背景：VM（2 核 / 1 GB）上 `/api/search` 空载约 9 秒，导入进行中 11–16 秒，
定位在 `mcp_server/service.py::_coverage()`——每次工具调用都对全库做一次
`MIN/MAX(source_created_at)` 聚合（带 policy_state/标签的 EXISTS 子查询）。
`tool_brain_status`、`tool_search_history`、`tool_get_recent_events`、
`tool_get_event` 四处都调用它，一次 claude.ai/ChatGPT 回答常连调 3–4 次工具，
按原速度会超时。

**改动**：给 `_coverage()` 加进程内 LRU 缓存（上限 32），键为
`(filters 规范化签名, branch_scope, policy_epoch, import_watermark)`：

- `policy_epoch`：撤回/标注同事务递增，缓存立即失效（不等 TTL）；
- `import_watermark`：`import_jobs` 表的 `MAX(rowid)`。这张表的写入与本次
  导入的事件在**同一事务**里提交（`history/store.py::publish_snapshot`），
  跟"这批事件对其他连接可见"是同一个提交点，任何连接读到的都是同一提交
  历史下的同一个值——**跨连接可比**。最初实现用的是 `PRAGMA data_version`，
  但它只保证同一连接内"值变化 ⇒ 库被别的连接改过"，不保证跨连接可比：
  REST 连接池会新开连接，新连接的 `data_version` 起始值可能和旧连接缓存过
  的某个值撞上，读到导入前的旧缓存（已用回归测试覆盖：见
  `tests/integration/test_coverage_cache_integration.py::
  test_new_connection_sees_import_from_other_connection`）。

**首算索引**（migration 6）：用 `EXPLAIN QUERY PLAN` 核实后发现，
`event_revisions` 本身没有可剪枝的等值/范围前缀（`source_created_at IS NOT
NULL` 选择性太低），outer 扫描恒为 `SCAN r`，给它加索引（无论单列还是复合列）
都不会被走到，是死重量——最初加的
`(source_created_at, revision_id)` 索引就是这种情况，已改正。真正被重复
求值的是每行的两个相关子查询（`EXISTS ... revision_policy_state ps
WHERE availability=?` / `... classification_status!=?`），旧索引
`ix_policy_state_current(revision_id, valid_to)` 不覆盖这两列，每行都要多一次
回表；migration 6 改成给 `revision_policy_state` 加
`(revision_id, valid_to, availability, classification_status)` 覆盖索引后，
`EXPLAIN QUERY PLAN` 确认这两个子查询从 `SEARCH ... USING INDEX` 变成
`SEARCH ... USING COVERING INDEX`（不再回表）。本地合成库（1 万 revision，
数据全在页缓存里）测不出差异，VM 慢盘下每行省一次回表 I/O 应该更明显——
下面是 VM 实测数字。

**`/api/search` 分段（本地合成库 1 万 events、命中缓存后的热态，仅供参考，
不代表 VM 磁盘 I/O 特征）**：

| 段 | 占比（cProfile cumtime，200 次调用） |
| --- | --- |
| FTS 候选行获取（`_fetch_fts_rows`，含 SQL 里内嵌的授权 WHERE） | ~74% |
| 逐候选授权验证（`_verify`） | ~11% |
| 排序（`_sort_hits`） | ~7% |
| snippet 渲染（`_snippet_window`/`_render_snippet`） | ~3% |
| 序列化（`_hit_to_result`/`_occurrence_ref`） | <1% |
| 覆盖期（`_coverage`，缓存命中） | <1% |

说明："授权"不是独立阶段——过滤条件内嵌在 FTS 候选查询的 SQL WHERE 里，
和取候选行是同一次 `execute`。另外发现 `revision_occurrences` 在
`search_history()` 富化阶段和 `_hit_to_result`/`_occurrence_ref` 各查了一次
（同一 revision 两次），量级很小（本地 <1%），按任务书"只报告，不顺手优化"
未动，记录在这里供后续参考。本地整体热态 P50 ≈ 11ms（200 次调用，10k
events 合成库），没有任何一段单独超过 1 秒，因此没有触发"单段超 1s 才优化"
的条件。

**VM 实测（2026-09-25，Azure B2ats v2 2 核 / 1 GB，生产库 6.5 万 events，schema 6）**：

| 路径 | 改动前 | 改动后 P50 / P95（热态，各 20 次） |
| --- | --- | --- |
| REST `/api/search`（VM 本机） | 约 9 s | 49 ms / 52 ms |
| MCP `tools/call search_history`（VM 本机） | 约 9 s | 51 ms / 52 ms |
| MCP `search_history`（经公网 Cloudflare 隧道，客户端在中国大陆） | 3–16 s | 首次 1.3 s，之后 0.43–0.45 s |

"导入进行中"未单独测：D-5 之后一轮批次导入通常不到 20 秒。

注意：`hindsight-mcp` 以只读方式打开库，**不会执行 migration**。升级代码后先让导入器或一次读写连接
把 schema 升到最新（`hindsight-import.service` 下一轮导入会自动完成），再重启服务。
`perf_probe.py` 默认 `--repeats 30` 加预热会超过每分钟 60 次的限速，得到 429；用 `--repeats 20`，两次运行间隔一分钟。

```bash
# 先升级 schema（只读服务不会做），再重启服务：
uv run python -c "from pathlib import Path; from personal_brain.history.db import connect; connect(Path.home()/'brain-data/db/brain.sqlite')"
systemctl --user restart hindsight-mcp

# 空载：REST /api/search 与 MCP tools/call search_history 的 P50/P95（各 30 次，可调 --repeats）
./deploy/vm/perf_probe.py --label 空载 --repeats 20

# 导入进行中：另开一个终端触发一轮导入，同时跑探针
systemctl --user start hindsight-import.service   # 或等 hindsight-import.timer 自然触发
./deploy/vm/perf_probe.py --label 导入进行中 --repeats 20

# _coverage 首算耗时 + soak 漂移/RSS/完整性门槛（合成库，不碰生产库）：
uv run brain soak --mode both
```

目标（任务书 D-5b）：热态 P95 ≤ 1.5s（空载）/ ≤ 3s（导入进行中）。

## 已知问题

- 本仓库当前锁定的 `mcp>=2.1.1` 把 `starlette`（连同 `pyjwt[crypto]`）声明为
  **核心**依赖（连 stdio 传输也要 `import` 它，见
  `mcp.server.lowlevel.server`），并非本任务引入。`pyproject.toml` 里的
  `remote` 可选组仍按任务书要求声明 `starlette`/`uvicorn`（面向未来更精简的
  mcp 发行版，或不同的安装环境），但在当前锁定版本下，`uvicorn` 才是唯一
  真正会缺失的可选依赖；`tests/integration/test_remote_http.py` 里
  "没有 remote 可选组" 的用例因此以隐藏 `uvicorn` 来复现（隐藏 `starlette`
  会让 stdio 也一起失败，不是本任务要验证的场景）。
