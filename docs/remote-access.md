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
- **VM 上的库是 Mac 的单向快照副本**：`sync-db.sh` 只做 Mac → VM 的 push，
  VM 上不产生任何新数据，也不回传。

## 部署

见 [`deploy/vm/README.md`](../deploy/vm/README.md)（`install.sh` 一条命令起
服务 + cloudflared 配置步骤 + Access 策略配置 + `sync-db.sh` 库同步）。

## 快速开始（本地跑 HTTP transport，不经过 Cloudflare）

```bash
uv sync --extra remote
export BRAIN_MCP_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
personal-brain-mcp --config config/config.yaml --transport http --port 8765
# 另一个终端：
curl -s http://127.0.0.1:8765/api/status
open "http://127.0.0.1:8765/mcp/$BRAIN_MCP_TOKEN"   # 仅用于确认路径存在，浏览器直接打开不是合法 MCP 请求
```

## 已知问题

- 本仓库当前锁定的 `mcp>=2.1.1` 把 `starlette`（连同 `pyjwt[crypto]`）声明为
  **核心**依赖（连 stdio 传输也要 `import` 它，见
  `mcp.server.lowlevel.server`），并非本任务引入。`pyproject.toml` 里的
  `remote` 可选组仍按任务书要求声明 `starlette`/`uvicorn`（面向未来更精简的
  mcp 发行版，或不同的安装环境），但在当前锁定版本下，`uvicorn` 才是唯一
  真正会缺失的可选依赖；`tests/integration/test_remote_http.py` 里
  "没有 remote 可选组" 的用例因此以隐藏 `uvicorn` 来复现（隐藏 `starlette`
  会让 stdio 也一起失败，不是本任务要验证的场景）。
