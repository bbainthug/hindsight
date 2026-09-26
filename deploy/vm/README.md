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
| `import_inbox.sh` | VM 侧：就地导入 inbox 批次（D-5，由 timer 每 10 分钟驱动） |
| `hindsight-import.service` / `.timer` | 批次导入的 systemd user 单元（每 10 分钟，`MemoryMax=400M`） |
| `push_batches.py` | Mac 侧：把 outbox 批次 rsync 到 VM inbox（D-5 常规同步） |
| `reset_vm_db.py` | Mac 侧：**手动整库重置工具**（原 `push_to_vm.py`，见"何时用整库重置"） |
| `sync-db.sh` | Mac 侧：旧的手动快照推送脚本（等价 shell 版） |

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

### 5. ChatGPT 采集器（D-6：扩展 → /ingest → inbox）

服务端 `/ingest/<namespace>` 只把浏览器扩展上传的对话批次写进 inbox（不碰数据库），
由已有的 `hindsight-import.timer`（D-5）定时导入。启用步骤：

1. **remote.yaml 加 ingest 段**（`~/brain-data/remote.yaml`）：

   ```yaml
   ingest:
     inbox: ~/brain-data/inbox
     namespaces: [main]
   ```

2. **生成写入 token**（`install.sh` 现在会自动做；已有 env 文件时补一条即可）：

   ```bash
   grep -q BRAIN_INGEST_TOKEN ~/.config/hindsight/env || \
     printf 'BRAIN_INGEST_TOKEN=%s\n' \
       "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> ~/.config/hindsight/env
   chmod 600 ~/.config/hindsight/env
   ```

3. **重启服务**（新路由与 token 生效）：

   ```bash
   systemctl --user restart hindsight-mcp
   journalctl --user -u hindsight-mcp -n 20 | grep ingest   # 应看到 /ingest 注册说明
   ```

4. **Cloudflare Access：为 /ingest 建 Service Auth 应用**（控制台由你操作）：

   1. **Access > Applications > Add an application > Self-hosted**
      - Application domain: `brain.<your-domain>/ingest`（只覆盖 /ingest 路径）
   2. **Policy**：Action=Allow，Include = **Service Auth**，填 Service Token：
      - 在 **Access > Service Auth > Service Tokens > Create Service Token** 生成
        Client ID 与 Client Secret（Secret 只显示一次，记下来）；
      - 扩展设置页填这对 Client ID / Secret；请求头
        `CF-Access-Client-Id / CF-Access-Client-Secret` 由扩展自动附上。
   3. 该应用只挂 `/ingest` 路径，不影响主应用的邮箱 OTP 与 `/mcp/*` Bypass 策略。

5. **扩展安装与设置**：见 `integrations/chatgpt_collector/README.md`
   （chrome://extensions 开发者模式加载 → 设置页填 Hindsight 地址、
   `BRAIN_INGEST_TOKEN`、Cloudflare 服务令牌 → 立即同步）。

6. **验证**：

   ```bash
   ls ~/brain-data/inbox/main/          # 扩展同步后应出现 <UTC>-<hash>.json
   journalctl --user -u hindsight-import -n 20
   ```

安全边界：写入 token 只能往 inbox 放文件（20 MB/次、10 次/分钟/IP），读不了库也改不了库；
ChatGPT 的会话 token 不经过 VM，也不落任何盘。

### 5. 批次同步与定时导入（D-5，常规路径）

Mac 侧 launchd（`install-launchd.sh --vm-host hindsight-vm`）每 15 分钟做两件事：
`sync_agents.py --export-dir …/outbox` 导出新增会话批次（`<UTC时间戳>-<内容
sha256前12位>.json`，原子写、导出断点与导入断点独立），`push_batches.py` 把
未推送批次 rsync 到 `~/brain-data/inbox/<source>/`（走 Cloudflare 隧道 SSH，
失败不删下次重试，成功移 outbox/sent/ 保留 14 天）。**只传批次，永不传库。**

VM 侧启用定时导入（systemd --user，非 root）：

```bash
cp ~/hindsight/deploy/vm/hindsight-import.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hindsight-import.timer
```

导入脚本 `import_inbox.sh` 按文件名顺序单批次顺序导入（`flock` 防重叠，
`MemoryMax=400M`，与 `hindsight-mcp` 读服务并存、WAL 下互不阻塞、不重启服务）；
成功移 `inbox/done/<source>/`，失败移 `inbox/failed/` 并记日志、不阻塞后续批次。
导入后如语义索引已存在则增量 reindex（`HINDSIGHT_SEMANTIC_REINDEX=0` 关闭）。
D-8 后增量 reindex 判定改为 `revision_id + index_version`（`event_revisions`
正文不可变，不需要任何内容哈希/指纹），另加一层水位短路（高水位 rowid +
policy_epoch 都没变就整段跳过）；无新数据时实测 ~1.0 s（10 万 revision 合成
库，此前 ~11–45 s），新增 50 个事件约 10.8 s，都不加载模型。

### 5b. 向量量化转换（D-8，一次性）

VM 的 float 向量表（457 MB）放不进 900 MB 内存的页缓存，hybrid 查询每次都在
读盘。D-8 把它就地转换为 `int8 量化粗排 + fp32 行表重排`（22 万 chunk 时
int8 粗排表约 114 MB，float 的 1/4）。转换**不重新 embed**——直接把现有
float 向量搬进 fp32 表，再流式量化写回 vec0（int8 的量化 scale 是动态的：
取全库 `127 / max|v|`，不是固定 ×127，见 `docs/unified-retrieval.md`）。
实测（Mac，30 万 chunk 合成库，本次修复后的代码）：**75.7 s，峰值 RSS
139 MB**。VM 是 2 vCPU 突发型、CPU 比 Mac 弱，按量级预计 **5–10 分钟**；
`MemoryMax=450M` 足够（139 MB 峰值 + 系统/连接池基线）。

```bash
# 1) 停 import timer 和 MCP（旧代码的 MCP 不认识 v7 结构，转换期间不要让它读库）
systemctl --user stop hindsight-import.timer
while systemctl --user is-active -q hindsight-import; do sleep 5; done
systemctl --user stop hindsight-mcp

# 2) 备份（回滚唯一的快速路径；约 30 s / 1.9 GB）
python3 -c "import sqlite3; s=sqlite3.connect('$HOME/brain-data/db/brain.sqlite'); \
  d=sqlite3.connect('$HOME/brain-data/backups/brain-pre-d8.sqlite'); s.backup(d)"

# 3) 部署新代码后转换（schema v7 迁移随第一次连接自动应用；--no-vacuum 磁盘紧张时用）。
#    用 systemd-run 限内存；WorkingDirectory 要写绝对路径（%h 不展开）
systemd-run --user --unit hindsight-d8-convert -p MemoryMax=600M \
  --working-directory=$HOME/hindsight \
  $HOME/hindsight/.venv/bin/brain --db $HOME/brain-data/db/brain.sqlite \
  reindex-semantic --convert --quant int8 --oversample 4

# 4) 验证
~/hindsight/.venv/bin/brain --db ~/brain-data/db/brain.sqlite doctor   # 语义段显示 quant_type=int8
systemctl --user start hindsight-mcp
curl -s "http://127.0.0.1:8765/api/search?q=测试&mode=hybrid" | head -c 300

# 5) 起 import timer，并把 semantic_default 切回 hybrid 后重启 MCP
systemctl --user start hindsight-import.timer
#   ~/brain-data/remote.yaml: profiles.remote.semantic_default: exact -> hybrid

# 回滚：停 MCP，把 backups/brain-pre-d8.sqlite 拷回 db/brain.sqlite（删掉 -wal/-shm），
# 部署回 D-8 之前的代码。没有备份时只能 reindex-semantic --full 全量重建（约 7 小时）。
# 转换中断后重跑同一条 --convert 命令即可续跑（幂等）。
```

**首次追平**：VM 库落后很多时，Mac 用空的导出断点跑一次全量导出再推送即可，
VM 导入器按消息 ID 幂等去重，不需要传整库。追平批次体积 ≈ 全部会话 JSON
总量（实测约 100 MB，一次性）；常规轮转每批几十 KB～几 MB。追平后把追平导出
断点复制回默认 `export_state.json`，避免 launchd 重复全量导出。

ChatGPT 官方导出（`main` 来源）仍在 Mac 手动导入；需要同步到 VM 时，把导出
zip 手动放进 VM 的 `~/brain-data/inbox/main/`（zip 同样按导入器幂等去重）。

### 6. 何时用整库重置（手动）

批次同步**不含撤回 / 标注**（它们只在 Mac 库生效，见"已知限制"）。以下两种
情况用 `reset_vm_db.py`（原 `push_to_vm.py`，走 Tailscale 私网，勿走隧道）：

1. VM 库损坏 / 数据漂移，需要从 Mac 库完全重置；
2. 需要把撤回 / 标注同步到 VM。

```bash
./deploy/vm/reset_vm_db.py            # 或带 --host / --remote / --force
```

## 已知限制

- **撤回 / 标注不跨端**：只在 Mac 库生效；VM 检索仍可见已撤回内容。需要一致时
  用整库重置（上节）。
- **单向**：VM 不回写 Mac；VM 上除导入批次外不产生新数据。

## 运维

```bash
systemctl --user status hindsight-mcp    # 读服务状态
systemctl --user status hindsight-import # 批次导入状态（timer 驱动）
journalctl --user -u hindsight-mcp -f    # 读服务日志（不含 query 内容/正文）
journalctl --user -u hindsight-import    # 导入日志（只记批次名/事件数/耗时）
systemctl --user start hindsight-import.service   # 手动触发一轮导入（等 timer 也可以）
systemctl --user restart hindsight-mcp   # 重启（换库/轮换 token 后）
```

导入失败排查：`inbox/failed/<source>/` 里的批次是按文件名序导入时被导入器拒绝的
（坏 JSON、超限等）；修复内容后可改好放回 `inbox/<source>/` 重导，或确认无用直接删。
`inbox/done/` 与 `outbox/sent/`（Mac 侧）可按需清理，保留期外由脚本自动清理。

轮换 `BRAIN_MCP_TOKEN`：编辑 `~/.config/hindsight/env`，写入新 token，
`systemctl --user restart hindsight-mcp`，旧的能力 URL 立即失效。
