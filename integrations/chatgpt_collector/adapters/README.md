# 平台适配器（adapters/）

每个平台一个文件，**该平台全部内部接口调用必须收敛在这一个文件里**（改版时只改它）。
当前只实现了 ChatGPT（`chatgpt.js`）；claude.ai / Gemini / Perplexity 不在本任务范围
（D-6 非目标），目录留作扩展点。

## 新增平台的约定

- 以 **MAIN world content script** 注入目标站（`manifest.json` 的 `content_scripts`
  加一项，`world: "MAIN"`，`run_at: document_start`），复用用户已有登录态调用该平台
  自己的后端 JSON —— 不抓 DOM。
- 对外只暴露两个动作，与 `content/bridge.js` 的消息协议对接：
  - `crawl`（payload: `{offset, limit}`）→ 按更新时间排序的对话列表；
  - `get`（payload: `{ids: [...]}`）→ 逐个返回与官方导出包结构一致的完整对话 JSON
    （至少含 `title / create_time / update_time / mapping / current_node / conversation_id`）。
- 平台会话凭据（如 ChatGPT 的 accessToken）只在本文件内存变量里使用：
  **绝不写 chrome.storage，绝不发给 Hindsight**，也不出现在 postMessage 载荷里。
- 节流决策（单轮条数、间隔）不在适配器里做——统一由 `background.js` + `lib/core.js`
  按任务书常量控制。
- 新平台的转换逻辑（平台 JSON → 导出包结构）若需要测试，仿照
  `tests/chatgpt_adapter.test.js` 用 `node:vm` 加载 + fixture 录制响应。
