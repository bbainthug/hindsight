# 任务 D-3：远程接入（HTTP transport + REST + 查询页 + VM 部署）

> 交付给实现 agent 的任务书。遵守 AGENTS.md 工作约定。规格文件为根目录
> `Personal_Brain_技术设计与Codex实施规格_v0.2.md`（AGENTS.md 里的旧路径以此为准）。

## 背景

Hindsight 现在只有 stdio MCP（`src/personal_brain/mcp_server/server.py`），只能被同一台
机器上的进程拉起。库已经复制到一台 Azure VM（Ubuntu 24.04，2 核 / 1 GB / 2 GB swap，
代码与 `uv sync --extra semantic` 已就绪，配置在 `~/brain-data/remote.yaml`，profile 名
`remote`，`delivery_boundary: remote_model_allowed`）。目标是：手机 / 网页版 claude.ai /
任何远程 agent 都能通过 HTTPS 访问它，并且有一个人能直接用的查询页。

规格 §7.5 写的"服务端不开网络监听"是首版边界，本任务**有意放开**，但只在以下前提下：
进程只绑定 127.0.0.1，公网侧由 Cloudflare Tunnel + Access 承担 TLS 与身份；服务端保持只读。
完成后把 server.py 顶部 docstring 与规格中的这条边界改成"stdio 默认；HTTP 需显式开启且仅
监听回环"。

## 范围（只做这些）

1. `personal-brain-mcp` 增加 `--transport {stdio,http}`（默认 stdio，行为不变）
2. HTTP 模式：MCP Streamable HTTP 端点 + 一组只读 REST 端点 + 静态查询页，同一进程同一端口
3. 访问控制：MCP 端点走能力 URL（路径含随机 token）；REST/页面走 Cloudflare Access（服务端
   校验 `Cf-Access-Jwt-Assertion` 可选、默认关闭）
4. `deploy/vm/`：systemd unit、安装脚本、cloudflared 配置模板、Mac 侧库同步脚本
5. 测试：进程内 ASGI 测试覆盖 MCP HTTP、REST、鉴权、只读
6. 文档：`docs/remote-access.md`；README 加"远程接入"一节

## 非目标

- 不做 OAuth 服务端、不做多用户、不做用户注册
- 不做任何写入端点（导入、标注、撤回一律不暴露）
- 不改检索、策略、profile 语义；不做 D-2
- 不在服务端调用远程模型

## 设计要求

### 传输层

- 用 MCP Python SDK 自带的 `StreamableHTTPSessionManager`（已在依赖里，
  `mcp.server.streamable_http_manager`），无状态模式（`stateless=True`，只读服务不需要会话）。
- 用 Starlette 承载：`/mcp/<token>` 挂 MCP，`/api/*` 挂 REST，`/` 与 `/static/*` 挂页面。
  运行时用 `uvicorn`。新增依赖放进可选组 `remote = ["starlette", "uvicorn"]`，核心安装不变。
- CLI：`personal-brain-mcp --config X --transport http --host 127.0.0.1 --port 8765`。
  `--host` 默认 127.0.0.1；传 `0.0.0.0` 必须同时传 `--i-know-this-is-public`，否则拒绝启动。
- 同一个 `build_server(config, conn)` 复用，不复制工具逻辑。SQLite 连接按请求从连接池取
  （`check_same_thread=False` 的只读连接若干个），不要跨线程共享单连接。

### 访问控制

- MCP 路径 token：从环境变量 `BRAIN_MCP_TOKEN` 读取（≥32 字节随机 hex），不写进 yaml。
  未设置时 HTTP 模式拒绝启动并提示生成命令。路径不匹配一律 404（不区分"存在但无权"）。
  理由：claude.ai / 手机端的远程 MCP 连接器只能给 URL，不能加自定义 header。
- REST 与页面：默认信任前置的 Cloudflare Access。可选配置
  `remote.access_aud`（Access 应用的 AUD）+ `remote.access_team_domain`，设置后服务端校验
  `Cf-Access-Jwt-Assertion`（用 Cloudflare 公钥 JWKS，缓存 1 小时）；未设置则不校验，
  但启动日志必须打印一行醒目警告。
- 所有响应加 `Cache-Control: no-store`；不设 CORS 头（页面与 API 同源）。
- 简单限速：每来源 IP（取 `Cf-Connecting-Ip`，缺失时取 peer）60 请求 / 分钟，超出 429。

### REST（全部 GET，JSON，复用 `service.tool_*`）

| 路径 | 对应 | 说明 |
|---|---|---|
| `/api/status` | `tool_brain_status` | |
| `/api/search?q=&mode=&match_mode=&speaker=&from=&to=&source=&limit=&cursor=` | `tool_search_history` | 参数名与 MCP 工具一致 |
| `/api/recent?limit=&cursor=` | `tool_get_recent_events` | |
| `/api/event/{event_id}?text_offset=&context_radius=` | `tool_get_event` | |

- 错误码沿用 `ToolError`：`INVALID_PARAMS`→400、`NOT_FOUND`/无权→404（同一响应体）、
  `QUERY_TOO_BROAD`→422、其他→500 且不带内部信息。
- 响应体就是工具的返回对象，不另造格式。
- 日志只记录路径、状态码、耗时；**不记录 query 内容和返回正文**。

### 查询页（`src/personal_brain/mcp_server/static/index.html`，单文件，无构建）

- 手机优先：搜索框、`exact | hybrid` 切换、说话者过滤（owner/assistant/全部）、时间范围。
- 结果列表显示时间、来源、`match_kind` 标记（semantic 命中要有明显"推断"标识）、snippet。
- 点结果展开原文，走 `/api/event`，长文本用 `text_offset` 分页；显示 `revision_id`。
- 底部常驻显示 `/api/status` 的 coverage（known_start/known_end）与 profile 的 delivery
  边界，提醒"远程 profile，凭据已遮蔽、敏感内容不可见"。
- 用 vanilla JS，总大小 < 40 KB，不引外部 CDN（页面在 Access 后面，别泄露访问模式）。

### 部署（`deploy/vm/`）

- `install.sh`：在 VM 上以当前用户安装 systemd user unit（不要 root 跑服务），
  `uv sync --extra semantic --extra remote`，生成 token 写入
  `~/.config/hindsight/env`（600 权限），`systemctl --user enable --now hindsight-mcp`，
  `loginctl enable-linger`。
- `hindsight-mcp.service`：`ExecStart=uv run personal-brain-mcp --config %h/brain-data/remote.yaml --transport http --port 8765`，
  `EnvironmentFile=%h/.config/hindsight/env`，`Restart=on-failure`，`MemoryMax=600M`
  （机器只有 1 GB）。
- `cloudflared.example.yml`：ingress `brain.<your-domain>` → `http://127.0.0.1:8765`，
  其余 404。附 README 步骤：`cloudflared tunnel login/create/route dns/run`，以及
  Access 应用配置（邮箱 OTP，只允许用户自己的邮箱；`/mcp/*` 路径设 bypass 策略）。
- `sync-db.sh`（Mac 侧）：用 sqlite backup API 出快照 → `rsync` 到 VM 临时文件 →
  原子 `mv` 覆盖 → `ssh systemctl --user restart hindsight-mcp`。可接到现有每日备份
  launchd 之后。
- 所有脚本不含真实域名、token、路径以外的私有信息；示例值明显是占位符。

### 测试

- `tests/integration/test_remote_http.py`，用 `httpx.ASGITransport` 进程内跑，不监听端口：
  - MCP over HTTP：initialize → tools/list（与 stdio 列表一致）→ tools/call search_history
  - token 错误 / 缺失 → 404；正确 → 200
  - 四个 REST 端点正常路径 + 每种错误码
  - 无权 event 与不存在 event 响应体逐字节相同
  - 限速触发 429
  - `Cf-Access-Jwt-Assertion` 校验：配置了 aud 时无 header → 401，伪造签名 → 401
    （JWKS 用测试内置密钥，不出网）
  - 日志断言：请求日志里不出现 query 字符串
- 现有 stdio 测试与 315 个用例全部不变、全绿。
- 没有 `remote` 可选组时 `--transport http` 给出清楚的安装提示而不是 traceback。

### 文档

- `docs/remote-access.md`：架构图（手机/claude.ai → Cloudflare → Tunnel → 127.0.0.1:8765）、
  两条接入路径（claude.ai 自定义连接器填 `https://brain.<domain>/mcp/<token>`；浏览器打开
  `https://brain.<domain>/` 走 Access 登录）、威胁模型一段（token 泄露 = 该 profile 可见
  内容泄露，轮换方法；Access 保护页面不保护 MCP 路径）、限制（单用户、只读、无 OAuth）。
- README 加一节，三行命令能起来。

## 交付

按 AGENTS.md：改动摘要、可复现命令（`uv run pytest`、`uv run ruff check src tests`、
`uv run mypy src`）、结果、未解决问题。另附：在合成库上 `--transport http` 的
`/api/search` 与 MCP `tools/call` 各自的 P95（用现有 bench 方式，20 次以上）。

## 已知取舍（不用来回问）

- MCP 走能力 URL 而不是 OAuth，是因为目标客户端不支持自定义 header 且 OAuth 服务端超出范围。
- 页面走 Cloudflare Access 而不是自建登录，是因为 Access 免费且不用在服务端存任何凭据。
- 服务只绑回环，是把 TLS、DDoS、身份全部交给 Cloudflare；不要在服务端加 TLS。
- 单进程单端口，是为 1 GB 内存的机器省资源；不要拆成两个服务。
- VM 上的库是 Mac 的快照副本，同步是手动/定时 push，不做双向同步。
