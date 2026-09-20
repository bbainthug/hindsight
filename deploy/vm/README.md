# D-3 VM 部署（远程接入）

把 `personal-brain-mcp --transport http` 跑在一台小型 VM 上，公网侧完全由
Cloudflare Tunnel + Access 承担 TLS 与身份，服务进程自己只绑 `127.0.0.1`。
完整威胁模型与架构图见 [`docs/remote-access.md`](../../docs/remote-access.md)。

本目录内容：

| 文件 | 用途 |
|---|---|
| `install.sh` | VM 上以当前用户安装 systemd --user 服务（非 root） |
| `hindsight-mcp.service` | systemd --user unit（`install.sh` 会复制到 `~/.config/systemd/user/`） |
| `cloudflared.example.yml` | cloudflared ingress 配置示例（占位符域名/token） |
| `sync-db.sh` | Mac 侧：一致性快照 → rsync → 原子替换 → 重启远端服务 |

## 前提

- VM：Ubuntu 24.04，代码已 clone 到 `~/hindsight`，`~/brain-data/remote.yaml`
  已按 `config/unified.example.yaml` 的格式准备好（`profile: remote`，
  `delivery_boundary: remote_model_allowed`）。
- 已安装 `uv`（<https://docs.astral.sh/uv/getting-started/installation/>）。
- 已安装 `cloudflared`（<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>）。

## 步骤

### 1. 安装并启动 MCP 服务

```bash
cd ~/hindsight
./deploy/vm/install.sh
```

这会：`uv sync --extra semantic --extra remote` → 生成 `BRAIN_MCP_TOKEN` 写入
`~/.config/hindsight/env`（600 权限，不进 git）→ 安装并启动
`systemctl --user enable --now hindsight-mcp` → `loginctl enable-linger`
（保证无登录会话时服务仍常驻）。

验证：

```bash
curl -s http://127.0.0.1:8765/api/status | head -c 200
```

### 2. 配置 Cloudflare Tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create hindsight-mcp
cloudflared tunnel route dns hindsight-mcp brain.<your-domain>
cp deploy/vm/cloudflared.example.yml ~/.cloudflared/config.yml
# 编辑 ~/.cloudflared/config.yml：替换 <TUNNEL_ID> 与 brain.<your-domain>
cloudflared tunnel run hindsight-mcp
```

生产建议把 `cloudflared tunnel run` 也做成 systemd --user 服务常驻（同
`hindsight-mcp.service` 的模式），这里只给隧道配置本身。

### 3. 配置 Cloudflare Access

在 Cloudflare Zero Trust 控制台（`cloudflared.example.yml` 里也有同样的步骤注释）：

1. **Access > Applications > Add an application > Self-hosted**
   - Application domain: `brain.<your-domain>`
2. **Policy**：Action=Allow，Include=Emails → 只填你自己的邮箱（邮箱 OTP 登录）
3. **关键**：给 `/mcp/*` 路径单独加一条 **Bypass** 策略——claude.ai / 手机端等
   远程 MCP 连接器无法完成 Access 的登录跳转，也无法附带自定义 header；MCP
   路径本身已经用高熵能力 URL token 做访问控制（见 `docs/remote-access.md`
   威胁模型一节）。
4.（可选，更强防御）在 `remote.yaml` 里设置 `remote.access_aud` +
   `remote.access_team_domain`，服务端会额外校验 `Cf-Access-Jwt-Assertion`。

### 4. 手机 / claude.ai 接入

- claude.ai 自定义连接器：URL 填 `https://brain.<your-domain>/mcp/<token>`
  （`<token>` 来自 `~/.config/hindsight/env` 的 `BRAIN_MCP_TOKEN`）。
- 浏览器打开 `https://brain.<your-domain>/`：走 Access 邮箱 OTP 登录后看到查询页。

### 5. 库同步（Mac → VM）

```bash
./deploy/vm/sync-db.sh ~/brain-data/brain.sqlite hindsight-vm brain-data/brain.sqlite
```

可接到现有每日备份 launchd job 之后（例如在 `backup()` 成功后调用本脚本）。
只做单向 push，VM 上的库是 Mac 的快照副本，不做双向同步。

## 运维

```bash
systemctl --user status hindsight-mcp    # 状态
journalctl --user -u hindsight-mcp -f    # 日志（不含 query 内容/正文，见 docs/remote-access.md）
systemctl --user restart hindsight-mcp   # 重启（换库/轮换 token 后）
```

轮换 `BRAIN_MCP_TOKEN`：编辑 `~/.config/hindsight/env`，写入新 token，
`systemctl --user restart hindsight-mcp`，旧的能力 URL 立即失效。
