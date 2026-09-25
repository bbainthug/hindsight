# 任务 D-7：一次调用的回忆工具（recall）与时间线（timeline）

> 交付给实现 agent 的任务书。遵守 AGENTS.md 工作约定。

## 背景

远程 agent（claude.ai、ChatGPT、Hermes）现在要回答"我之前关于 X 是怎么想的"，典型路径是：
`search_history` → 看 snippet → 对几条结果分别 `get_event` 取原文和上下文 → 换个词再搜一次。
一次回答 3–6 次工具调用，慢，而且模型常常在第二步就放弃，只凭 snippet 作答。

另一类常见问题是按时间回顾："我这个月在忙什么""9 月 10 号前后我在纠结什么"。现在只有
`get_recent_events`（按事件逐条翻页），没有按天 / 按对话的概览，模型要翻几十页才看得出轮廓。

两个参考做法（见 2026-09 调研）：portable-ai-memory 的 `memory_context_pack`（一次返回
带预算的、有来源的上下文包）；memory-mcp 的 `search_archive_timeline`（按时间段列出会话）。

## 目标

新增两个只读 MCP 工具（同时提供 REST 端点），让最常见的两类问题**一次调用就能拿到够用的证据**：

- `recall`：给一个话题，返回去重后的若干"片段"，每个片段是命中消息 + 前后若干条同对话上下文，
  总量受字数预算约束，每条都带可回查的 `event_id` / `revision_id` / 时间 / 来源。
- `timeline`：给一个日期区间，按天列出当天活跃的对话（标题、来源、消息数、你说了几条），
  可选附每个对话里你的第一句话作为提示。

## 范围

1. `service.tool_recall(conn, profile, args)` 与 `service.tool_timeline(conn, profile, args)`，
   复用现有 `search_history`、`_authorized_context`、授权与遮蔽逻辑；**不新增任何绕过授权的查询路径**。
2. MCP 注册两个工具（`server.py` 的工具声明 + `build_server` 分发），profile 的 `tools` 白名单控制是否暴露；
   `config/unified.example.yaml` 与 VM 的 `remote` profile 示例加上这两个名字。
3. REST：`GET /api/recall?q=&...`、`GET /api/timeline?from=&to=&...`，与其他端点同样的鉴权、限速、错误码。
4. 工具描述写清用法（这是模型选工具的唯一依据）：
   - `recall`：回答"关于某件事我说过 / 想过什么"时**优先用它**；它已经包含上下文，通常不需要再 `get_event`。
   - `timeline`：回答"某段时间我在干什么"时用它；需要细节再对具体对话 `recall` 或 `get_event`。
5. 文档：`docs/unified-retrieval.md` 消费端约定补一节"先 recall / timeline，再细查"；README 工具列表更新。

## 接口

### recall

输入：
- `query`（必填）
- `mode`：`exact | hybrid`，缺省用 profile 的 `semantic_default`
- `date_from` / `date_to`、`source`、`speaker`：同 `search_history`
- `max_snippets`：默认 6，上限 12
- `context_radius`：每个命中前后各取几条同对话消息，默认 2，上限 4
- `char_budget`：返回正文总字数上限，默认 6000，不超过 profile 的 `max_text_chars` 与服务端硬上限 12000

处理：
1. 用 `search_history` 取候选（`limit = max_snippets * 3`，不超过其自身上限）。
2. **合并**：同一对话里相距不超过 `2 * context_radius` 条的命中合并成一个片段，避免同一段对话重复返回。
3. 对每个片段取上下文（沿用 `_authorized_context` 的选中路径与授权规则，未授权的邻居消息跳过且不暴露其存在）。
4. 按检索排序依次装入，直到片段数或字数预算用完；单条消息超长时截断并标注 `truncated: true`、给出 `text_offset`
   以便后续 `get_event` 续读。
5. 远程投递 profile 的凭据遮蔽对所有返回正文生效。

输出（沿用 `_envelope` 外壳，`content_trust: untrusted_archive`）：

```json
{
  "snippets": [
    {
      "conversation_id": "...", "conversation_title": "...", "source": "codex",
      "time_range": ["2026-09-10T08:00:00Z", "2026-09-10T08:12:00Z"],
      "match_kind": "exact|semantic|both",
      "messages": [
        {"event_id": "...", "revision_id": "...", "speaker_type": "owner|assistant",
         "timestamp": "...", "text": "...", "is_hit": true, "truncated": false}
      ]
    }
  ],
  "budget": {"chars_used": 5200, "char_budget": 6000, "snippets_dropped": 2},
  "notes": ["semantic_hits_are_inferred ..."]
}
```

### timeline

输入：`date_from`、`date_to`（必填，按配置时区解释为自然日，区间 ≤ 62 天）、`source`、
`with_first_line`（默认 true）、`limit_per_day`（默认 20，上限 50）。

输出：按日期升序：

```json
{"days": [{"date": "2026-09-10", "conversations": [
  {"conversation_id": "...", "title": "...", "source": "main",
   "messages": 34, "owner_messages": 12, "first_at": "...", "last_at": "...",
   "first_owner_line": "前 80 字"}
]}], "truncated": false}
```

- 只统计 profile 可见的事件；某对话在该 profile 下一条可见消息都没有时整条不出现。
- 对话标题取 `conversation_snapshots` 里最新一次快照的 `title`；没有则为空。
- `first_owner_line` 同样经过遮蔽，截断到 80 字。

## 非目标

- 不做 LLM 摘要（timeline 只做结构化统计，"这个月主要在忙什么"的归纳由调用方模型完成）。
- 不做事实提炼（D-2）。
- 不改 `search_history` / `get_event` 的现有行为与返回格式。

## 性能

在 VM（2 核 / 1 GB，6.5 万事件）上：`recall` 默认参数热态 P95 ≤ 2 s；`timeline` 30 天区间 P95 ≤ 1 s。
`timeline` 需要按日期聚合，必要时在新 migration 里加索引（用 `EXPLAIN QUERY PLAN` 证明被用上），不改查询语义。

## 测试

- 单元：片段合并（相邻命中合并、跨对话不合并）、预算装填（片段 / 字数两种上限、超长消息截断与 `text_offset`）。
- 集成（合成库）：
  - `recall` 返回的每条消息都在 profile 授权集合内；未授权的邻居不出现、也不影响计数；
  - `recall` 与对同一 query 的 `search_history` 命中一致（命中消息 ⊆ search 结果）；
  - 远程 profile 下正文凭据被遮蔽；
  - `timeline` 日期边界按配置时区（跨午夜的消息归到正确的日期）；区间 > 62 天报 INVALID_PARAMS；
  - `timeline` 不泄露不可见对话（数量与存在性都不泄露）。
- MCP：`tools/list` 在 profile 白名单包含时出现、不包含时不出现；`tools/call` 两个工具各一例。
- REST：两个端点正常路径 + 错误码。
- 全量测试、ruff、mypy 干净。

## 交付

改动摘要、可复现命令与结果、未解决问题；另附在合成 10 万事件库上两个工具的 P50/P95。
**不要部署到 VM**：VM 正在建语义索引，部署和重启由我在索引建完后统一做。

## 已知取舍（不用来回问）

- `recall` 返回原文片段而不是摘要：保证每一句都可回查，由调用方模型自己归纳。
- 合并相邻命中、带上下文：宁可少几个片段，也不要同一段对话被切成几块重复返回。
- `timeline` 只做统计不做摘要：零模型成本、零幻觉，归纳交给调用方。
