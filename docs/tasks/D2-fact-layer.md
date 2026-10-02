# 任务 D-2：可溯源的个人事实层（v0）

> 交付给实现 agent 的任务书。遵守 AGENTS.md：开工先读 `STATUS.md`，收工更新它。
> 设计依据：`Personal_Brain_技术设计与Codex实施规格_v0.2.md` 第 9.3、10、11 节与 Phase D。本任务是 Phase D 的**最小可用切片**，
> 加上用户点名要的"自己用"的输出。与设计文档冲突时以设计文档为准，并在交付里指出冲突。

## 背景

Hindsight 现在只有"发生过什么"（原始对话，情景记忆）。用户的痛点是：多个对话、多个项目、多个 agent 之间信息不互通，
每个 agent 都得自己翻流水账，或者依赖手写、很快就过时的 `~/me/profile.md` 和各项目的 `STATUS.md`。

市面上的记忆产品（Mem0、Zep、Letta）是"AI 自己决定记什么"，用户看不到、改不了，记错了也难发现。
本项目的定位是**有原话撑腰、由人确认、带时间线的事实层**：每条事实都指回原话（精确到 revision 和字符跨度），
变化不悄悄覆盖而是形成时间线，激活必须经用户确认。**首先是给用户自己天天用的工具。**

## 用户故事（v0 必须满足）

1. 我运行一条命令，系统从最近一段时间我的发言里提炼出候选事实，告诉我"预计调用 N 次、约 X tokens"，我同意后才开跑。
2. 我逐条审核候选：看到事实、类型、原话片段（带时间和来源），按 通过 / 拒绝 / 修改措辞 / 跳过 处理。
3. 新候选和已生效的事实冲突时（比如"两条线并行" vs "主攻 AI 测开"），审核界面把两条并排给我，我选 supersedes / coexists / 拒绝，
   旧事实保留并标记，不删除。
4. 我能看到一条**决策时间线**：所有 decision 类事实按时间排列，每条都能点回原话。
5. 我能一键生成 `profile.md` 里"自动维护"那一节的**草稿和 diff**，我确认后才写入；手写部分不动。

## 范围（v0）

1. **数据模型**：按设计文档 10.1 / 10.2 新增 migration：`claims`（含 claim_revision）、`claim_evidence`、`claim_relations`、
   `extraction_runs`、`review_log`。v0 的 memory_type 只开 `preference / goal / decision / project_fact / open_question`；
   assertion_kind 按设计填；`behavior_pattern` 不做。
2. **提炼流水线**（设计 10.3 的子集）：
   - 输入：owner 发言的 event revision（只用 `speaker_type=owner` 作为 support 证据；assistant 内容只能作 context），按对话的选中路径分段，
     段内带有限上下文；范围由 `--since/--until/--source` 指定，默认最近 30 天。
   - 出站前做凭据遮蔽（复用现有 `redact_known_credentials`）。
   - LLM 按 JSON schema 输出原子主张，每条必须带证据的 revision_id 与原文跨度；**跨度用原文核对**（复用检索的原文偏移逻辑），
     对不上的候选直接拒绝并记原因。
   - 去重：与已有 active / candidate 主张做文本 + 向量相近检查，相近的合并为同一主张的新证据，而不是新主张。
   - 冲突检测：同 memory_type、同主题的 active 主张与新候选，由 LLM 给出关系建议（same / refines / supersedes / contradicts / coexists），
     **只是建议**，进审核队列。
   - 幂等：提炼键 = 输入 revision 集合 + 分段规则版本 + prompt/schema 版本 + 模型配置（设计 10.4）；重跑不重复生成。
   - 开跑前给出请求数与 token 估算，超过 `--budget` 拒绝执行。
3. **CLI**（初期只走本地 CLI，不暴露写 MCP，设计 11.2）：
   - `brain facts extract [--since --until --source --budget --dry-run]`
   - `brain facts review`：交互式逐条审核（终端即可；显示事实、类型、原话片段、时间、来源、冲突对象）
   - `brain facts list [--type --status]`、`brain facts timeline [--type decision]`、`brain facts explain <claim_id>`（列出全部证据原话）
   - `brain facts export-profile --out <path> [--write]`：生成 `profile.md` 的"自动维护"小节草稿；默认只打印 diff，`--write` 才写入，
     只替换 `<!-- facts:auto:start -->` 与 `<!-- facts:auto:end -->` 之间的内容
4. **LLM 提供方**：OpenAI 兼容接口，配置写在 `config` 的 `facts:` 段（base_url、model、温度、超时、单批上限）；密钥只从环境变量读。
   具体用哪家由用户决定（见文末）。外部模型失败不影响现有检索。
5. **文档**：`docs/facts.md` 写清模型、流程、命令、已知限制；README "能做什么"加一节；STATUS.md 更新。

## 非目标（v0 不做）

- 自动激活（全部人工审核后才 active；设计 9.3 的对照评测通过前不开自动激活）
- MCP 读接口 `search_memory` / `explain_memory`（v1 再做；做时只读、带权限，与现有工具同一套授权）
- 自动改写各项目 `STATUS.md`（v1：生成草稿，用户确认后写）
- Web 审核界面、behavior_pattern、Obsidian 双向同步、VM 部署

## 测试与验收

- 单元：跨度核对（对不上必拒）、assistant-only 证据必拒、幂等键、去重合并、关系建议不自动生效、export-profile 只改标记区间。
- 集成（合成库 + 假 LLM）：从合成对话提炼 → 审核通过 → timeline 与 export-profile 输出正确；撤回某条源 revision 后，
  依赖它的主张标记为需复核（派生失效，设计 Phase D）。
- **自用验收**：在用户真实本地库上，用最近 30 天跑一次（先 `--dry-run` 看估算），用户审核后：
  ① 生成的 profile 自动区能覆盖 `~/me/profile.md` 手写版里的主要事实；② 决策时间线里能看到"主攻 AI 测试开发（2026-10-01）"
  并能点回原话；③ 报告候选总数、通过 / 拒绝 / 修改比例、拒绝原因分布、总 token 与耗时。
- 全量 pytest、ruff、mypy 干净；CI 绿。

## 交付

改动摘要、可复现命令、测试结果、自用验收的统计（不含任何真实内容）、未解决问题。真实提炼结果只存在本地库，**不得提交 Git**。

## 已知取舍（不用来回问）

- 宁缺毋滥：证据不足的候选直接丢，不为凑数生成事实。
- 只认用户自己的话：AI 的分析再对，也不能当成用户的事实。
- 冲突不替用户判：系统只建议关系，最后由用户拍板。
- 先 CLI、后界面：先验证提炼出来的东西有没有用，再投入做界面。
- 本地库里的 ChatGPT 数据只到 2026-09-06（之后的走 VM），v0 接受这个缺口，在 `docs/facts.md` 注明；v1 再考虑从 VM 拉取。

## 开工前需要用户决定

1. **用哪个模型做提炼**：会把你最近 30 天自己的发言（已做凭据遮蔽）发给该模型。候选：DeepSeek（DSH 已在用）、火山方舟 glm-5.3-flash、
   魔搭（免费额度）。
2. **单次预算上限**（例如 50 万 tokens），超出就拒绝执行。
