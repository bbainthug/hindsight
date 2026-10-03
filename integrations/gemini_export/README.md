# Gemini 对话导出

Google Takeout 的 "Gemini Apps" 活动记录常常是空的（活动记录关闭或账号类型限制），
而且没有对话结构。这里的做法是直接从已登录的 gemini.google.com 页面读取对话：

1. `export_gemini.js`：在 Gemini 页面里运行（浏览器控制台或自动化工具）。依次打开
   侧边栏每个对话、向上加载完整历史、提取问答文本，打包下载为 JSON。只读 DOM，
   不调用私有接口、不修改任何对话。
2. `convert_gemini.py`：把导出的 JSON 转成 ChatGPT 导出格式，交给
   `brain import-chatgpt --source gemini`（与 `agent_sync` 同一思路，导入器零改动）。

```bash
python integrations/gemini_export/convert_gemini.py gemini-export.json -o out/conversations.json
brain --db <db> --archive-dir <archives> import-chatgpt out/ --source gemini
```

VM：把 `conversations.json` 以 `<UTC时间戳>-<sha12>.json` 放进 `~/brain-data/inbox/gemini/`，
导入定时器会自动处理。

## 已知限制与实测经验

- **没有时间**：Gemini 网页不显示逐条消息时间，导入后为"时间未知"——可检索，但不参与日期过滤。
- **标签页必须在前台**：Chrome 会把后台标签页的定时器严重限速（实测 200ms 变 14s）；Gemini
  的 Trusted Types/CSP 也不允许用 blob Worker 绕开。运行期间保持该标签页可见。
- **窄窗口会自动收起侧边栏**，导致找不到对话链接；运行时需保持侧边栏展开。
- **屏幕外的轮次 `innerText` 为空**（content-visibility），所以用 `textContent` 并在块级元素后补换行。
- 侧边栏是懒加载列表，滚动容器是 `infinite-scroller`；滚到底要多等几轮才算加载完。
- 幂等：消息 ID = 会话 ID + 序号 + 内容哈希，重复导入同一内容新增为 0（实测 5469 条二次导入新增 0）。
- 页面偶尔把 emoji 拆成孤立代理项，转换时成对拼回、落单替换为 U+FFFD。
- **已知缺陷（D-12）**：`export_gemini.js` 用 `textContent` 读用户发言时，把页面上给
  读屏软件的隐藏标签"你说"连同正文的另一份完整副本一起抓了进来，所以旧导出文件里每条
  用户发言是"你说 X\nX"的形态。因拿不到真实页面结构确认该隐藏元素，导出脚本暂未改动；
  `convert_gemini.py` 已在转换时按"去掉开头的"你说"标签 + 去掉与前一段完全重复的副本"
  归一化（见 `personal_brain.injected_rules.normalize_gemini_user_text`），正文本身
  不重复或只是正文中提到"你说"时不会误删。确认页面结构后应修导出脚本本体。

更稳的长跑做法（实测 390 个对话）：每 20 个对话下载一个分段文件；已完成的会话 ID 记在
`localStorage`，中断后重跑自动跳过；最后按会话 ID 合并去重。
