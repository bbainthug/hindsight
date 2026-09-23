# 任务 D-6：ChatGPT 网页对话采集器（Chrome 扩展 + /ingest）

> 交付给实现 agent 的任务书。遵守 AGENTS.md 工作约定。依赖 D-5（VM inbox 导入）已上线。

## 背景

Codex / DSH / Claude Code / Hermes 的会话已由 `integrations/agent_sync` 自动采集；
ChatGPT 是最大的来源（库里 `main` 命名空间约 3.6 万条），但仍靠手动导出，数据停在 2026-09-06。

ChatGPT 网页前端展示的对话，来自它自己的后端 JSON（`/backend-api/conversation/<id>`），
其结构与官方导出包 `conversations.json` 中的单个对话一致（`title / create_time / update_time /
mapping / current_node / conversation_id`，含真实消息 ID、时间戳与分支树）。现有
`brain import-chatgpt` 可直接导入这种结构。

## 目标

一个本地加载的 Chrome 扩展：在用户已登录的 chatgpt.com 上，把新增或更新的对话以原始 JSON
形式暂存在本地，定时上传到 VM 的 `/ingest`；服务端只把它写成 inbox 批次，由 D-5 的定时导入
完成入库。手机 app 上的对话在下次任意电脑打开 Chrome 时被补采，不需要逐个点开。

## 范围

### A. 服务端 `/ingest`（`src/personal_brain/mcp_server/http_app.py`）

1. `POST /ingest/<namespace>`：请求体为对话数组（导出格式）。只做校验 + 落盘，**不碰数据库**：
   写成 `~/brain-data/inbox/<namespace>/<UTC时间戳>-<sha256前12位>.json`（`.tmp` → rename），
   返回 `202 {"batch": "<文件名>", "conversations": N}`。同内容重复提交得到同名文件，直接返回 200。
2. 鉴权：独立写入 token，环境变量 `BRAIN_INGEST_TOKEN`（与 MCP token 不同，≥32 字节 hex），
   `Authorization: Bearer`；未设置时 `/ingest` 整体不注册。错误一律 404，不区分原因。
3. `namespace` 白名单由配置给出（默认只允许 `main`）；inbox 路径由配置给出，不能来自请求。
4. 限制：请求体 ≤ 20 MB；每 IP 每分钟 ≤ 10 次；最小结构校验（数组、每项有 `mapping` 与 `conversation_id`），
   不合格 400 且不落盘。日志只记 namespace、对话数、字节数、状态码，不记内容。
5. `--transport http` 未设置 inbox 目录时 `/ingest` 不注册，启动日志说明。

### B. 扩展 `integrations/chatgpt_collector/`（Manifest V3，vanilla JS，无构建步骤）

1. **命名空间必须是 `main`**：已有导出数据的事件 ID 形如 `chatgpt:main:<消息ID>`，
   用同一命名空间，插件采到的与历史导出的同一条消息才会被导入器去重，而不是重复入库。
2. 采集方式：**不抓 DOM**。
   - 在 chatgpt.com 页面上下文中，用用户已有的登录态读取对话 JSON；
   - **补采**：扩展启动时和之后每 30 分钟，拉一次按更新时间排序的最近对话列表，
     对 `update_time` 晚于上次记录的对话逐个取完整 JSON；单次最多 20 个、请求间隔 ≥ 2 秒；
   - **即时**：用户在页面上打开或正在聊的对话，更新后也记一份（可复用页面自身的请求结果，或同样按 id 取）。
   - 所有 ChatGPT 内部接口调用集中在 `adapters/chatgpt.js` 一个文件里，改版时只改这里。
3. 本地暂存：IndexedDB，按 `conversation_id` 保存最新一版完整 JSON 与"已上传的 update_time"；
   `chrome.alarms` 每 15 分钟把有变化的对话打包上传 `/ingest/main`，成功才标记已上传，失败保留重试。
4. 设置页：Hindsight 地址、写入 token、Cloudflare Access 服务令牌（Client ID / Secret），
   保存在 `chrome.storage.local`（不用 `storage.sync`）。弹出页：上次同步时间、待上传数量、
   最近错误、暂停开关、"立即同步"按钮。
5. 隐私：
   - ChatGPT 的会话 token 只在内存里用于请求 chatgpt.com，**绝不写入存储、绝不发给 Hindsight**；
   - 跳过临时聊天（temporary chat）；
   - 支持按对话排除（弹出页里"不采这个对话"），被排除的对话本地删除且不再上传；
   - `host_permissions` 只包含 `https://chatgpt.com/*` 与用户在设置里填的 Hindsight 地址。
6. 平台适配结构留好（`adapters/` 目录），本任务只实现 ChatGPT。

### C. 部署

1. VM：`install.sh` 支持生成 `BRAIN_INGEST_TOKEN` 写入 `~/.config/hindsight/env`（已存在不覆盖），
   `remote.yaml` 增加 inbox 目录与 namespace 白名单示例。
2. Cloudflare：`/ingest` 需要一个 Access 应用（路径 `brain.<domain>/ingest`），策略为 **Service Auth**，
   扩展带 `CF-Access-Client-Id / CF-Access-Client-Secret` 头通过。在 `deploy/vm/README.md` 写清
   创建服务令牌和策略的步骤——这些在 Cloudflare 控制台由我来点。
3. 扩展安装：`chrome://extensions` → 开发者模式 → 加载已解压的扩展，步骤写进 `integrations/chatgpt_collector/README.md`。

## 非目标

- 不做 claude.ai / Gemini / Perplexity 适配（只留目录结构）。
- 不抓 DOM，不截图。
- 不做 LLM 提炼、事实抽取（那是 D-2）。
- 不上架 Chrome 应用商店，不支持 Firefox / Safari。
- 服务端不存任何 ChatGPT 凭据，不代用户去拉 ChatGPT。

## 测试

- 服务端（pytest，进程内 ASGI，不出网）：token 错误 / 缺失 / 未配置 → 404；超大请求、结构不合格 → 400 且不落盘；
  同内容两次提交 → 同一文件名；namespace 不在白名单 → 404；**全程数据库文件未被修改**（比对 mtime/hash）；
  日志不含对话内容。
- 端到端（合成数据）：`/ingest/main` 收到的批次，经 D-5 的 `import_inbox.sh` 导入后可被 `search_history` 检到；
  与已存在的同一对话（相同消息 ID）重复导入，新增事件为 0。
- 扩展：`node --test` 覆盖纯逻辑（变更检测、打包、排除列表、重试状态机）；
  内部接口调用部分用录制的合成响应做 fixture，不连真网站。
- 现有测试全绿；ruff / mypy 干净。

## 交付

改动摘要、可复现命令与结果、未解决问题；另附：
- 在我的浏览器里跑一轮的实测：补采了多少对话、上传批次字节数、VM 导入后新增事件数；
- 对照一条我在手机 app 里聊过的对话，确认在 Chrome 打开后 30 分钟内出现在 `search_history` 里。

需要我操作的步骤（加载扩展、Cloudflare 服务令牌与策略、VM 上重启服务）逐条列给我。

## 已知取舍（不用来回问）

- 读 ChatGPT 自己的后端 JSON 而不是抓 DOM：拿得到真实消息 ID、时间与分支，能和历史导出去重；
  代价是依赖未公开接口，改版时只改 `adapters/chatgpt.js`。
- 请求节流（单次 ≤ 20 个、间隔 ≥ 2 秒、每 30 分钟一轮）：宁可慢，不要看起来像爬虫。
- 只有 Chrome 开着时采集；手机上的对话靠补采在下次打开时追上，不在服务器上保存 ChatGPT 登录态。
- 写入口只能往 inbox 放文件：即使写入 token 泄露，也无法读库或改库。
