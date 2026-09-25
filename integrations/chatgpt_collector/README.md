# Hindsight ChatGPT 采集器（Chrome 扩展 + /ingest）

在已登录 chatgpt.com 的 Chrome 上，把新增或更新的对话以**后端原始 JSON**（与官方导出包
`conversations.json` 单条一致）定时上传到自己的 Hindsight VM：扩展 → `/ingest/main`
→ inbox 批次 → D-5 定时导入入库。手机 app 上的对话靠下次打开 Chrome 时的补采追上。

## 硬性约定（实现即契约，改动需重新评审）

- **namespace 固定 `main`**：历史导出的事件 ID 形如 `chatgpt:main:<消息ID>`；同一命名空间
  下同消息 ID 才会被导入器去重，不会重复入库。
- **不抓 DOM**：对话内容来自 ChatGPT 自己的后端 JSON（`/backend-api/conversation/<id>`），
  拿得到真实消息 ID、时间戳与分支树。
- **ChatGPT 会话 token 绝不落盘、绝不发给 Hindsight**：accessToken 只存在于
  `adapters/chatgpt.js`（MAIN world 页面脚本）的内存变量里；扩展的 `chrome.storage.local`
  只存 Hindsight 地址、写入 token 与 Cloudflare 服务令牌。
- **/ingest 只写 inbox 文件、不碰数据库**：服务端只做校验 + 原子落盘；即使写入 token 泄露，
  攻击面也只有"往 inbox 放文件"，无法读库/改库。
- **节流不放宽**：补采每 30 分钟一轮；单轮 ≤ 20 个对话、请求间隔 ≥ 2 秒；
  上传每 15 分钟一轮；`/ingest` 每 IP ≤ 10 次/分钟、单请求 ≤ 20 MB。
- ChatGPT 内部接口调用全部集中在 `adapters/chatgpt.js`（ChatGPT 改版时只改这个文件）。

## 目录结构

```
chatgpt_collector/
├── manifest.json           # MV3；host_permissions 只有 chatgpt.com，Hindsight 地址用 optional 授权
├── background.js           # service worker：调度（alarms）、节流、上传、状态机
├── db.js                   # IndexedDB 封装（对话/水位线/排除列表）
├── adapters/
│   ├── chatgpt.js          # ★ 唯一允许调用 ChatGPT 内部接口的文件（MAIN world）
│   └── README.md           # 新增平台（claude.ai 等）时在此放对应适配器
├── content/bridge.js       # isolated world 桥：页面脚本 ↔ service worker
├── lib/core.js             # 纯逻辑（变更检测/打包/排除/重试），node --test 可测
├── options.html / options.js
├── popup.html  / popup.js
└── tests/                  # node --test；fixture 为录制的合成响应，不连真网站
```

## 安装（加载扩展）

1. 打开 `chrome://extensions` → 右上角打开**开发者模式** → **加载已解压的扩展程序** →
   选择本目录（`integrations/chatgpt_collector/`）。
2. 点扩展图标 → **设置**，填写：
   - **Hindsight 地址**：`https://brain.<your-domain>`（保存时会请求对该域名的访问权限）；
   - **写入 token**：VM `~/.config/hindsight/env` 里的 `BRAIN_INGEST_TOKEN`
     （`install.sh` 自动生成；`ssh <vm> grep BRAIN_INGEST_TOKEN ~/.config/hindsight/env` 查看）；
   - **Cloudflare Access 服务令牌**（走 Access Service Auth 时填，见 `deploy/vm/README.md`）。
3. 回到弹出页点**立即同步**跑一轮；之后每 30 分钟自动补采、每 15 分钟自动上传。
4. 验证：VM 上 `ls ~/brain-data/inbox/main/` 应出现 `*.json` 批次；等下一轮导入后
   `brain search-history <对话里的词>` 能检到。

## 行为细节

- **补采**：后台开一个不聚焦的 chatgpt.com 标签页（有已打开的标签则复用），拉一次按更新
  时间排序的列表，对晚于水位线的对话逐个取完整 JSON，取完关闭标签页。
- **即时采集**：patch 页面自身的 `fetch`，捕获你在页面上打开/聊的对话响应——不额外发请求。
- **临时聊天跳过**：`is_temporary === true` 的对话不采不传。
- **按对话排除**：弹出页列表里"不采"→ 本地删除 + 加入排除列表，之后永不采集/上传。
- **失败重试**：上传失败按指数退避（2/4/8…上限 60 分钟）保留重试；成功才标记
  `uploaded_update_time`。
- **重复内容**：与已上传内容逐字节相同的批次，服务端返回同一文件名（200）不重复落盘；
  内容更新但消息 ID 相同的对话，导入器按事件 ID 幂等（新增事件 0）。

## 测试

```bash
node --test integrations/chatgpt_collector/tests/*.test.js
```

覆盖：变更检测、上传打包（字节上限分组）、排除列表、重试退避状态机、节流常量锁定；
`adapters/chatgpt.js` 用 node:vm 加载 + 录制 fixture（列表、单对话、401 重取、临时聊天、
页面 fetch 捕获），全程不连真网站。
