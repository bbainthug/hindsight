# Personal Brain：可追溯的个人历史检索与受控记忆系统

**技术设计与 Codex 实施规格 v0.2**  
**日期：2026-09-06**  
**状态：设计修订稿；实现与性能尚未验证**  
**适用对象：开发者、Codex 及其他实现工具**

本文是独立完整的 v0.2 设计，替代 v0.1 的实施顺序与冲突条款。当前交付仅为技术设计，不表示已创建系统、运行依赖、导入个人数据或启用 MCP。

---

## 1. 目标与价值边界

先构建一个能可靠回答“我在什么时间、什么上下文里说过什么”的系统，再验证自动记忆是否改善答案质量和上下文成本。

首版闭环：

```text
ChatGPT Export
    → 不改写的原始档案
    → 可版本化的消息历史
    → 中文关键词 / 时间 / 对话检索
    → 从第一天启用权限的只读 MCP
    → Agent 引用原始证据回答
    → 人工标注问题集评测
```

只有基线通过后，才加入候选记忆、人工审核、有限自动激活和当前状态。

### 1.1 首版必须能回答

- “2026 年 8 月我关于求职说过什么？”
- “现有档案中，我最早什么时候提到某个项目？”
- “这个对话里我表达过哪些决定？给出原句和时间。”
- “这条结论引用了哪段话？说话的是我、AI，还是其他人？”
- “现有记录是否足以回答？数据覆盖到什么时候？”

### 1.2 不承诺从聊天中直接得出

- 用户客观上完成或没有完成某件事。
- 人格、执行力、健康状态等稳定属性。
- 未经确认的现实项目进度或当前优先级。
- 用户人生中真正的“第一次”。系统最多知道授权档案里的最早记录。

**原始记录是记录内容的权威来源，不是现实事实的最终权威。**

“我计划学习三小时”“我昨天学了三小时”“帮我改写：我每天学习三小时”分别可能是意图、自述和引用素材。消息作者相同，不代表证据性质相同。

### 1.3 非目标

首版不做微信接入、自动画像、行为趋势、人生决策、全量 embedding、复杂实体图、自动 Wiki、Dashboard、OCR、ASR、训练模型或多 Agent 自治。支持多个 Agent 访问同一服务，不等于需要实现多 Agent 编排。

---

## 2. v0.1 → v0.2 的主要变化

| 主题 | v0.2 决策 |
|---|---|
| 交付顺序 | 历史检索 → 带权限的只读 MCP → 基线评测 → 候选记忆 → 当前状态 |
| Open Brain | 可选、有时间上限的 Spike；不作为 History MVP 前置依赖 |
| 第一版存储 | 一个 SQLite 数据库承载历史、策略元数据与后续记忆；暂不引入第二个数据库 |
| 事实可靠性 | 区分自述、意图、引用、假设、第三方说法和模型推断；检查证据支持关系 |
| 中文检索 | 明确中文二元切分、短词回退和原文匹配验证 |
| 权限 | 首次 MCP 上线前实现；事件、证据片段、记忆、状态项均适用 |
| 增量导入 | 处理新增、修改、分支变化与派生失效；不只“新增事件” |
| 记忆评分 | 不把模型生成的 confidence 当作事实概率 |
| CURRENT_STATE | 从仍有效证据生成；有覆盖期、复核时间和失效机制，允许完整重建 |
| 人工修改 | 数据库修订是纠错入口；Obsidian 手工区只是独立笔记 |
| 删除 | 区分归档、逻辑撤回和清除；覆盖派生数据、索引、缓存、快照及备份策略 |
| 验收 | 先验证无记忆基线，再评估自动记忆的增益 |

---

## 3. 总体架构与技术选择

```text
Archive files ── Import / normalize ── SQLite
                                      ├─ source snapshots / message graph
                                      ├─ events / immutable event revisions
                                      ├─ FTS / metadata / import jobs
                                      ├─ access labels / policy epochs
                                      └─ later: claims / evidence / corrections
                                                   │
                                  Authorized retrieval service
                                      │                     │
                                  Local CLI             Read-only MCP
                                                            │
                                                  Authorized Agent client

Later: eligible evidence → candidate extraction → review → active memories
       active evidence / memories → state items → snapshots / Obsidian
```

### 3.1 初始技术栈

- Python 3.12+，`uv`；版本以实现时锁文件为准。
- Pydantic v2：数据契约；SQLite：事务、结构化查询、FTS5。
- 官方 Python MCP SDK；MCP 只是服务适配层，业务查询不写在 tool handler 中。
- pytest、Ruff、一个类型检查器。使用合成数据测试。
- Markdown 文档与 CLI；首版不引入前端。
- SQLite 备份使用一致性快照机制，不能在数据库运行时仅复制主文件并忽略 WAL。

暂不需要 embedding。后续确有收益时，再通过 `EmbeddingProvider` 与可重建的检索索引增加语义召回。

### 3.2 数据权威与可重建性

| 层 | 内容 | 权威来源 |
|---|---|---|
| L0 | 原始导出、文件清单、摘要值 | 被保存的原始字节 |
| L1 | 消息、版本、对话图、导入覆盖信息 | L0 + 导入器版本 |
| L2 | 候选主张、已审核记忆、纠错和证据边 | L1 + 人工输入 / 审核记录 |
| L3 | 检索索引、当前状态、生成的 Markdown | 当前有效的 L1 / L2 + 策略与生成版本 |

L2 中的人工输入和纠错不能仅从原始聊天重建，必须单独备份。模型输出也不能假定再次运行会逐字相同；保留提炼批次和结果版本。

---

## 4. 数据契约

以下是逻辑 Schema；实现时用外键、唯一约束、枚举校验和迁移落实。数组关系优先使用关联表，不能只存在未校验的 JSON 中。

### 4.1 来源与导入快照

`sources`：

- `source_id`：内部稳定 ID。
- `source_kind`：`chatgpt | manual | wechat | other`。
- `account_namespace`：用户配置的稳定、非敏感账号别名；不依赖导出文件名。
- `status`：`active | withdrawn | purging | purged`。

`source_snapshots`：

- `snapshot_id`、`source_id`、`archive_sha256`、`archive_relative_path`。
- `exported_at` 可空、`imported_at`、`parser_version`。
- `coverage_start`、`coverage_end`：已知消息时间边界，不代表期间记录完整。
- `parse_status`：`complete | partial | failed`；`coverage_notes`。
- `supersedes_snapshot_id` 可空：明确选定新的来源视图时记录。

`source_assets` 保存压缩包成员路径、摘要、大小及附件映射；`import_jobs` 保存计数、结构化错误和运行状态。

同一来源的快照不因“更晚导入”就自动判定“导出内容更新”。有可靠生成时间时使用该时间；没有时可并存，出现无法消解的版本冲突则等待明确选择，不能静默覆盖。

### 4.2 事件身份与版本

`events` 表示消息身份：

- `event_id`：由 `source_kind + account_namespace + stable_source_message_id` 确定。
- `source_id`、`conversation_id`、`source_message_id`。
- `current_revision_id` 可空、`revision_resolution`：`resolved | unresolved`。
- `speaker_id`、`speaker_type`：`owner | assistant | contact | system | unknown`。

`event_revisions` 表示不可原地覆盖的消息内容版本：

- `revision_id`、`event_id`、`revision_hash`。
- `content_type`、`raw_text`、`search_text`、`attachment_refs`。
- `source_created_at`、`source_updated_at`、`original_time_value`、`time_precision`、`timezone_status`。
- `recorded_at`：系统记录时间。
- `source_locator`：来源文件成员路径 + JSON pointer / 稳定节点 ID。
- `scope_labels`、`sensitivity_labels`、`classification_status`、`policy_version`。
- `availability`：`available | withdrawn | purged`。

同一内容版本出现在不同导出时，通过 `revision_occurrences` 指向多个快照，不复制事件身份。`revision_hash` 包括影响解释的内容和元数据，但不包括导入时间、磁盘绝对路径。

访问标签与 availability 是关联的策略 / 可用性状态，物理实现放在单独版本化表中；修改权限不会改写原始内容 revision，也不重新计算其内容摘要。历史内容不可原地覆盖不妨碍后续收紧权限或执行清除。

无原生稳定消息 ID 时，必须记录 `identity_quality=fallback`，组合来源、对话、说话者、时间、结构位置和内容摘要；仅凭文本 hash 不得把不同时间的相同发言合并。源信息不足时保留独立记录并标注，不声称已完美去重。

### 4.3 对话树和分支

- `conversation_nodes` 保存原始节点 ID、parent/children 关系及关联事件；允许无消息的结构节点。
- `conversation_snapshots` 保存快照中的图、`current_node` 和所选路径。
- 分支成员关系由图和路径推导。共享祖先可以属于多条路径，不能仅靠单一 `branch_id` 表示。
- 默认提炼仅使用已明确识别的所选路径；无法确定时标注 `active_path_unknown` 并跳过自动提炼。
- 历史检索支持 `selected | all`；`selected` 路径未知时返回明确提示，不暗中混合分支。
- 所选路径变化会使依赖旧路径的候选、记忆和状态项进入待复核状态。

### 4.4 时间语义

查询区间统一为 `[date_from, date_to)`。CLI 接收自然日期时按配置时区转为明确边界，并显示解释后的区间。

有时区的源时间统一存 UTC，保留原值；无时区的时间不能静默当作 UTC。无法确定的日期不以 `imported_at` 代替，日期过滤默认排除并报告未定时间记录的存在。

后续记忆区分：

- `asserted_at`：用户表达的时间。
- `valid_from / valid_to`：所描述事实的适用时间，可未知。
- `recorded_at`：系统得知或记录时间。

---

## 5. ChatGPT 导入器

### 5.1 输入与格式容错

- 支持 ZIP 和已解压目录。按结构识别对话数据，不仅凭 `conversations.json` 文件名猜测。
- 保留原始字节与附件引用，不修改源导出。
- 数据导出结构视为外部可变化格式，不宣称固定 Schema 是官方稳定 API。
- 保留多模态内容类型；无法解析的结构记录为 unsupported，不无声丢弃。
- 解压前限制大小、文件数和压缩展开量；拒绝目录穿越及逃逸的符号链接。
- 不执行附件、脚本或导出内指令。所有历史内容均作为不可信数据处理。

### 5.2 可恢复导入

1. 校验输入，生成归档 manifest 和摘要。
2. 原始文件暂存成功后原子移动到归档目录；失败不发布成功状态。
3. 在暂存批次解析、统计支持与不支持的记录。
4. 按确定的事件身份和内容版本执行 upsert；建立出现位置和图关系。
5. 更新搜索索引；计算受影响派生对象。
6. 发布快照和可查询版本；未发布批次不可见。
7. 标记旧派生对象失效，将重建任务放入可重试队列。

源发布与派生失效标记必须处于同一数据库事务；异步重建期间不能继续返回已知失效的旧状态。大导入可以分批暂存，但最终发布有统一可见性边界。

新内容版本会使依赖被替代版本的派生对象进入 `needs_review`；旧原文仍可作为明确标注版本的历史证据读取。普通 Memory 查询只返回审核通过、支持证据仍可用且关系未失效的对象；历史 Memory 查询也不得绕过撤回或权限检查。

### 5.3 幂等与更新规则

- 相同快照重复导入：新增事件和版本均为 0，不重复触发提炼。
- 新快照重复包含旧消息：增加来源出现位置，事件身份不重复。
- 相同消息 ID、内容变化：新建 revision，保留旧版本。
- 旧消息在新导出中消失：标记该快照未包含，不能推断用户要求删除。
- 遇到版本先后不明：保留冲突，禁止从该事件自动激活记忆。
- 解析器升级：显式重建标准化数据；记录版本和结果差异。

---

## 6. 中文与精确历史检索

### 6.1 检索语义

首版 `search_history` 支持：

- `match_mode=literal`：归一化文本的连续子串匹配。
- `match_mode=all_terms`：空白分隔的查询项全部出现；中文查询项本身保持连续子串语义。
- 时间、来源、对话、说话者、路径过滤。
- `order=chronological | reverse_chronological | relevance`。

“最早提到”必须使用时间排序和覆盖提示，不能从相关性 top-k 中取最早一条冒充全局最早记录。无法确定时间的记录单独报告。

### 6.2 初始索引策略

原文保持不变，`search_text` 使用版本化的 NFKC + casefold 归一化。若归一化长度变化，snippet 必须有原文偏移映射或直接返回对应原文片段，不用归一化索引直接切原文。

FTS5 默认 tokenizer 不能直接满足中文子串搜索。初始应用侧 tokenizer 使用：

- 连续汉字：重叠二元切分，例如“职业方向” → “职业 / 业方 / 方向”。
- 拉丁字母和数字：生成完整 token；大小写归一。
- 混合查询：拆出可安全索引的部分；FTS 仅缩小候选范围。
- 单汉字、拉丁词内子串、标点与其他无法保证完整召回的查询：用参数化 `instr(search_text, ?)` 在授权过滤范围内回退。
- 候选必须通过原始查询语义的子串检查；二元 token 同时存在不等于原文连续出现。

**候选优化不得改变匹配集合。** 先实现可验证的直接子串基线，再增加安全的 FTS 路径，并对两者做差分验证。不得未经完整性证明先截断候选再检查而漏掉匹配结果。

所有 SQL 使用绑定参数，FTS 查询由受控构造器生成；用户输入不能直接作为 FTS 表达式或 SQL 片段。

### 6.3 结果与资源限制

- 默认 10 条，最大 50 条；单条 snippet 默认最多 400 字符，单次文本正文总量最多 8,000 字符。
- 超限使用游标、`truncated` 和稳定排序，不静默丢失证据。
- 空 query 不触发全库导出；最近事件走专用接口。
- 关键词相关性只用于排序，不改变事实有效性。
- 首版不自动扩写成语义查询。“求职”和“找工作”可能需要分别检索，作为已知能力边界写入结果说明。
- 广泛回退扫描若超过资源限制，返回 `QUERY_TOO_BROAD` 并提示缩小时间或来源，不能把超时当作没有结果。

上述数量是初始产品限制，不是已测性能结果；必须可配置并受服务端硬上限约束。

---

## 7. 权限、敏感信息与外发边界

### 7.1 威胁边界

首版面向单用户、本机 stdio MCP。目标是避免经由服务发生误读、过度返回、历史提示注入和不当外发。

若某 Agent 与服务使用同一 OS 身份，且可直接读取原始文件或改写配置，MCP profile 不是强隔离边界。真正隔离需要独立服务身份、受限目录和受保护凭据；远程多用户 MCP 不在首版范围。

### 7.2 访问标签

事件版本、候选、记忆、证据片段和状态项均需标签：

- `scope_labels`：如 `projects`, `career`, `learning`, `relationships`。
- `sensitivity_labels`：如 `private`, `secret`, `third_party_sensitive`；允许多个标签，第三方属性不与秘密级别互斥。
- `classification_status`：`unclassified | rule_classified | human_reviewed`。

未知分类默认不对普通 Agent 开放。初始小样本可由用户在本地把来源 / 对话显式归入 scope；未审核的自动分类不能扩大访问范围。混合话题需要事件级标签，不能仅因对话标题写“职业”就整段开放。

派生内容默认继承所有证据的 scope 并集和敏感标签并集。允许读取必须覆盖全部所需 scope，且没有命中 deny 标签。摘要不能自动降敏；脱敏副本需要独立审查和版本。

### 7.3 策略执行位置

- profile 由受信任启动配置绑定，不是 tool 参数。
- 过滤发生在结果排序、top-k、snippet 和汇总生成之前。
- 邻接消息、对话标题、人物名称、错误信息和计数也必须经过权限检查。
- `get_event` / `explain_memory` 每次重新检查权限，不因已持有 ID 而放行。
- 请求在返回前核对 policy epoch；权限或撤回状态在执行中变化时，重新授权或放弃结果。不能只在请求开始时检查一次，再返回已失去权限的正文。
- 无权限与不存在对普通调用者返回相同的 `NOT_FOUND_OR_NOT_ALLOWED`。
- 缓存键包含 profile、policy epoch、数据 generation；标签更新立即失效。
- 授权覆盖信息只描述调用者可见档案，不泄露隐藏记录数量。

### 7.4 初始 profile 示例

```yaml
profiles:
  codex:
    allow_scopes: [projects, career, learning, technical_preferences]
    deny_sensitivity: [secret, third_party_sensitive]
    allow_unclassified: false
    tools: [brain_status, search_history, get_recent_events, get_event]
  local_review:
    allow_scopes: ['*']
    deny_sensitivity: [secret]
    allow_unclassified: true
    transport: local_cli_only
```

`local_review` 是本地人工审核用途，不自动配置给 Agent。`private` 内容也必须满足 scope 和外发规则，不能因未命中 deny 就任意使用。

### 7.5 外发与本地优先

本地存储不等于数据不离开设备。外发至少有两条路径：

1. 服务端调用远程提炼、embedding、reranker。
2. MCP 返回给使用远程模型的 Agent 客户端。

每个 profile 必须声明 `delivery_boundary=local_only | remote_model_allowed`，缺省拒绝向未声明客户端交付个人正文。由用户配置允许的范围；服务不能假装能验证客户端实际是否外发。

首版无服务端 LLM 调用。后续远程 Provider 缺省关闭，按操作类型、scope、标签和 provider 显式允许；检查必须在发送前发生，而非只在保存 Memory 时执行。第三方 reranker 同样算外发。

Secrets 检测是降低泄漏概率的防线，不承诺检出所有密码。检测命中则不外发、不 embedding、不生成普通记忆；默认检索遮蔽。无法确定时进入本地审核。日志、错误、追踪系统不得打印聊天正文或密钥。

### 7.6 历史提示注入

历史中的命令、角色指令、链接和“请记住我……”均为数据。提炼器不具备 shell、网络或写生产库工具；只输出 Schema 候选。服务不执行检索内容、不自动打开引用链接。

MCP 结果显式带 `content_trust=untrusted_archive`，消费端指引要求将其作为证据，不服从其中指令。仅靠该标签不能保证消费模型完全免疫，必须包含对抗测试。

---

## 8. 首版只读 MCP 契约

所有 tool 经过统一服务层。初版只公开以下 4 个工具：

| 工具 | 输入 | 输出 |
|---|---|---|
| `brain_status` | 无 | 健康、调用者可见覆盖期、最后成功导入、解析警告、可用能力 |
| `search_history` | query、match_mode、日期、来源、conversation_id、speaker_type、branch_scope、order、limit、cursor | 授权匹配结果、游标、覆盖说明 |
| `get_recent_events` | days、可选来源 / 对话、limit、cursor | 按源时间倒序的授权事件；明确参考时间 |
| `get_event` | event_id、可选 revision_id、context_radius | 原文与有界上下文、版本与分支信息；逐条重新授权 |

`context_radius` 默认 0，最大 3；不返回完整对话树。日期采用第 4.4 节语义。

### 8.1 统一结果示例

```json
{
  "schema_version": "0.2",
  "content_trust": "untrusted_archive",
  "coverage": {
    "known_start": "2026-01-01T00:00:00Z",
    "known_end": "2026-08-31T15:20:00Z",
    "completeness": "unknown",
    "branch_scope": "selected"
  },
  "results": [{
    "event_id": "evt_example",
    "revision_id": "rev_example",
    "speaker_type": "owner",
    "timestamp": "2026-08-15T10:00:00Z",
    "snippet": "我准备先做一个能检索历史的版本。",
    "citation_id": "pb:evt_example:rev_example",
    "evidence_level": "message_record",
    "source_locator": {"snapshot_id": "snap_example", "node_id": "node_example"}
  }],
  "truncated": false,
  "next_cursor": null,
  "warnings": []
}
```

这些字段是示意数据，不是用户的真实记录。`evidence_level=message_record` 只表示找到消息，不表示消息内容已独立核实。

引用必须绑定不可变 revision。逻辑定位符由本地 resolver 解析，不向普通 Agent 暴露磁盘绝对路径。源撤回后 resolver 返回已不可用，不因引用 ID 绕过撤回。

### 8.2 消费端回答约定

- 表述为“你在某日表示 / 计划……”，除非存在足够的完成证据。
- 引用须支持相邻结论，不能只提供一个相关但不支持结论的消息。
- 明确缺失信息、覆盖期和冲突。
- 不把检索不到理解成没有发生。
- 需要跨词语义推断时标注推断，并继续寻找直接证据。

MCP 服务能保证访问与返回契约，不能单独保证任意客户端的最终回答质量；端到端评测必须指定客户端 / 模型版本。

---

## 9. 评测优先：无记忆基线

### 9.1 数据集

先建立约 30 个真实需求问题，建议分配：

| 类型 | 初始数量 |
|---|---:|
| 中文关键词、短词、混合词与精确时间 | 10 |
| 多时间点观点与决定、分支和版本变化 | 6 |
| 信息不足、计划与完成、引用与假设 | 6 |
| 权限、第三方信息、删除与历史指令注入 | 8 |

这些是初始设计规模，不足以证明真实泄漏率为零。公开仓库只放合成 fixture；用户真实标注保存在 Git 外的私有评测目录。开发集与保留集分开，避免反复调参只记住答案。

每项记录：query、profile、时间 / 分支条件、可接受证据 revision ID 集合、必须区分的语义、禁止结论、允许的拒答，以及预期访问结果。不能只检查 `expected_contains` 关键词。

### 9.2 初始发布门槛

以下是待实现的验收目标，不是已测指标：

- 导入、重复导入、版本保留、事务恢复和引用解析等确定性测试全部通过。
- 明确可回答的检索题：Recall@10 ≥ 90%；多证据问题另计必要证据集合覆盖率。
- 返回的 citation ID 均可解析到正确版本；对正常可访问证据要求 100%。
- 人工复核关键结论的证据支持关系：不得出现错误归属、把计划当完成、把引用当自述。
- 对抗样例中不得把 assistant-only 证据变成用户事实，不得出现禁止访问内容。
- 必须拒答 / 说明不足的指定样例全部通过；同时统计正常问题回答率，防止靠全部拒答通过。
- 10 万条合成文本消息、记录硬件与 SQLite 版本后，常用查询预热 P95 目标 ≤ 1 秒；短词扫描单独测量并披露。

未满足门槛时留在基线阶段修复，不以“加入自动记忆”掩盖召回或权限问题。

### 9.3 加入 Memory 后的对照

使用相同问题、来源、权限、消费模型和回答预算，比较：

1. History-only。
2. Memory + History fallback。

记录答案正确性、证据充分性、错误用户事实数、陈旧结论数、拒答合理性、输入 token、延迟、调用成本和人工审核耗时。

只有在没有新增关键事实 / 权限错误的前提下，观察到正确性提升，或质量不降低且上下文成本有明确改善，才默认启用 Memory。报告原始题数与逐题差异，不能凭一个汇总分或小样本声称普遍有效。

---

## 10. 候选主张与长期记忆：基线后实施

### 10.1 主张 Schema

用 `claims` 统一承载候选与记忆；明确区分内容类型、证据性质、审核状态和生命周期：

| 字段 | 说明 |
|---|---|
| `claim_id`, `claim_revision_id` | 稳定身份与不可变修订 |
| `memory_type` | preference / goal / decision / project_fact / experience / belief / commitment / open_question / behavior_pattern |
| `subject_id`, `context` | 所属主体、项目、条件、适用范围；避免跨上下文替代 |
| `content` | 一条可独立核查的原子主张 |
| `assertion_kind` | self_report / intention / hypothesis / quotation / external_claim / model_inference |
| `verification_status` | unverified / user_confirmed / independently_supported / disputed |
| `review_status` | pending / approved / rejected / needs_review |
| `lifecycle_status` | candidate / active / superseded / archived / withdrawn |
| `asserted_at`, `valid_from`, `valid_to`, `recorded_at` | 表达、适用与系统时间 |
| `scope_labels`, `sensitivity_labels` | 继承证据标签 |
| `extraction_run_id`, `rule_version` | 提炼与规则版本 |

`user_confirmed` 表示用户确认这条表述，仍不等于客观事实已独立验证。初版提炼只考虑明确偏好、目标、决定、项目自述和待解问题；`behavior_pattern` 不自动激活。

不要向用户展示未经校准的“事实置信度 0.94”。若保留模型打分，只作为内部 `extraction_score` 排序，不参与授权，不独自触发激活；显著性也只是排序信号。

### 10.2 证据边

`claim_evidence` 至少保存：

- claim revision、event revision。
- 原文证据跨度与摘要值；偏移基于原始文本。
- `role=support | contradict | context`。
- `support_check=unreviewed | supported | insufficient | rejected`。
- 检查方法、版本、时间和人工审核记录。

证据必须指向确切 revision，不能只挂一个“相关对话”或可变的 event ID。长结论拆成原子主张分别检查，不能一条用户发言为整段模型分析背书。

### 10.3 提炼与 Evidence Gate

```text
authorized + eligible event revisions
  → selected-path segmentation with bounded context overlap
  → outbound policy / secret checks
  → schema-constrained candidate extraction
  → source identity / span validation
  → assertion-kind and semantic support checks
  → duplicate / context / conflict checks
  → review queue
  → approved claim activation
```

规则：

- 不存在合法 support 证据的候选拒绝。
- 只有 assistant 证据的候选不能成为用户事实。
- 用户“同意”可以作为证据，但需精确解析认同的是哪句话；上下文不清则待审核。
- 复制的简历、角色扮演、引用素材、假设句不能因作者是 owner 就转成自述事实。
- 第三方消息可保留为 external_claim，不自动升级为用户人格属性。
- 计划只能产生 intention / goal / commitment，不能产生完成事实。
- 多次重复自述不是多个独立验证来源。
- 初版全部人工审核后激活；有限自动激活必须按主张类型分别通过后续评测。

LLM judge 只能辅助语义检查，不能被视为事实验证器。保留“证据不足”结果，不强制所有输入生成 Memory。

### 10.4 分段与幂等

分段按对话所选路径、时间间隔和 token 上限确定，保存 `segment_id`、有序 revision 集合和边界规则版本。上下文重叠部分标记为 context，防止重复提炼。

提炼键包含：输入 revision 集合、segment 版本、prompt/schema 版本、provider/model 配置和策略版本。成功批次重试不重复激活；更换模型或规则视为新实验批次，不直接覆盖旧结论。

请求次数、输入 token、预算上限在批次开始前给出估算；上限由用户配置，不以无限重试消耗预算。外部 Provider 失败不影响已有 History 检索。

---

## 11. 生命周期、冲突与人工纠错

### 11.1 关系类型

`same | refines | supersedes | contradicts | coexists | unrelated`。

仅当主体、主题、适用条件和时间范围可比较，且有明确替代证据时，才建议 `supersedes`。例如“项目 A 用 SQLite”与“项目 B 用 PostgreSQL”应并存。

“更偏 AI 工程”不必然否定“仍关注安全”。含糊变化可以用 `refines` 或 `coexists`，不能只因消息更新就把旧观点标失效。

生命周期状态迁移带 actor、原因、时间和依据；supersedes 关系禁止自环和环路。冲突未解决时保留双方并标注 disputed，不让排序器悄悄替用户判定。

### 11.2 手工记忆与纠错

初期写入仅走本地 CLI 审核，不向通用 Agent 暴露写 MCP：

```text
brain review-memory
brain add-manual-memory
brain correct-memory <claim_id>
brain archive-memory <claim_id>
```

手工输入建立 `source_kind=manual` 的版本化事件，再建立主张与证据边。这样“所有 active memory 有来源”与“允许手工输入”不会冲突。

纠错建立 correction record 和新 claim revision，旧主张被撤回或替代；原始聊天保持不变。下游状态、投影、检索缓存立即失效。只做归档不表示事实错误，也不表示隐私删除。

---

## 12. CURRENT_STATE：证据驱动的当前视图

在 Memory 阶段通过后实施。它描述“截至某时，现有证据支持的状态”，不能被称为实时用户状态。

每个状态项保存：

- `state_item_id`、类型、内容、证据与 claim revisions。
- `as_of`、`last_confirmed_at` 可空、`review_due_at`。
- `status=current | needs_review | expired | disputed`。
- scope / sensitivity 标签、生成版本与来源 generation。
- `origin=user_stated | model_suggested`，模型建议分区显示。

生成规则：

- 时间窗口用于寻找近期证据，不自动删除窗口外的长期约束。
- “最近没提”不等于停止；超过复核期改为 needs_review。
- 有明确截止期的状态按截止期失效；无期限的状态按类型配置复核期。
- 下一步行动只有用户明确承诺才进入 user_stated；建议不冒充承诺。
- 原证据撤回、纠错或路径变化，受影响状态项立即不可作为 current 返回。
- 每次从有效证据集合构建候选快照，与旧快照 diff 后发布。可优化为 patch，但必须与完整重建结果可对照。
- 快照保存逐项证据，不能只保存无法逐项过滤的整段摘要。

权限不同的客户端只看到被授权的状态项。跨 scope 摘要需基于授权项重新生成，不先生成完整人生摘要再删几句话。

增加 `get_current_state` 时才注册该 MCP 工具；首版不提供空壳或假状态接口。

---

## 13. Obsidian 投影与编辑规则

Obsidian 后于当前状态实施。生成区可重建，手工区单独保存：

```markdown
---
generated_by: personal-brain
projection_version: 1
source_generation: 42
manual_edit_policy: preserve_notes
---

<!-- AUTO:START -->
带逐项来源的生成内容
<!-- AUTO:END -->

<!-- NOTES:START -->
用户独立笔记，不自动成为数据库事实
<!-- NOTES:END -->
```

- 修改生成区不作为数据库纠错；下次更新检测到差异时保存冲突副本并提示，禁止静默覆盖。
- 改变记忆通过 `correct-memory`；确需导入笔记时显式执行手工来源导入。
- 手工区不会自动参与提炼，也不能仅从 L1/L2 重建，需纳入独立备份。
- 投影带 manifest 和内容摘要；使用临时文件 + 原子替换。
- 每个 vault 绑定固定输出 profile；生成文件本身可能含隐私，放在 Agent 可读目录会绕过 MCP 控制。
- 用户复制到其他位置或云同步后的副本不在服务可删除范围；清除报告必须披露这一边界。

---

## 14. 撤回、删除与依赖传播

### 14.1 三种操作不能混淆

| 操作 | 含义 | 检索行为 |
|---|---|---|
| archive | 保留历史，退出默认当前视图 | 仅显式历史查询可返回 |
| withdraw | 从可服务的数据中撤回，等待清理 / 复核 | 所有普通查询不可返回 |
| purge | 清除系统控制范围内的正文与派生副本 | 不可返回；仅留不含正文的操作凭证 |

### 14.2 删除来源流程

1. 本地生成影响预览：快照、事件、记忆、状态、文件、备份范围；用户针对明确范围确认清除。
2. 在事务中撤回所选来源，更新 data / policy epoch，阻断全部查询路径和旧缓存。
3. 对多来源证据逐一重检。仅当剩余有效证据仍独立支持整个主张且策略允许保留，才保留；否则撤回主张并重新提炼。显式“忘记此内容”应覆盖重复来源，不能靠另一份导出保活。
4. 通过依赖边清理全文索引、语义索引、候选、状态快照、导出生成区、模型输入输出缓存。
5. 按选择移除 Raw。原始包含多个对话时，首版清除粒度为整个归档快照；细粒度请求若无法在保持原包不修改的前提下完成，应明确显示需扩大清除范围，不能声称只删索引已删除原文。
6. 重建允许保留的投影，并处理 SQLite 空闲页 / WAL 和备份保留策略。
7. 输出完成、失败和不可控副本列表；任务可恢复重试，不能因中途失败重新开放旧数据。

删除失效须覆盖多层依赖，不仅查一次 `source_event_ids`。`derived_dependencies` 记录 event revision → claim revision → state item / snapshot / projection 的边。

不承诺从 SSD、OS 快照、远程模型供应商或用户副本中实现可验证的物理擦除。数据库清理与磁盘加密各有作用，不能把 `DELETE`、VACUUM 或归档称为已完成所有副本擦除。

### 14.3 备份与恢复

- 本地备份包含 Raw、数据库一致性快照、人工修订、策略配置及投影手工区；密钥使用独立保护方式。
- 默认设计目标：每日快照、滚动保留 7 份；是否启用和存放位置由部署配置确定。
- 用户清除前展示备份影响；不可逐项清理的备份应整份销毁或明确列入保留例外，不能先报成功再等待自然过期。
- 恢复时先应用撤回 / 清除登记，再开放检索，防止旧备份使已删除内容复活。
- 首次真实使用前完成一次合成数据恢复演练。

---

## 15. Open Brain 可选评估

### 15.1 当前核对范围

2026-09-06 核对的项目 README 声明支持 MCP、混合排序、生命周期、矛盾检测和 Wiki 等功能；描述的主要处理链路包括 Supabase、Deno Edge Functions 和 OpenAI。尚未进行代码审计、测试或本地运行验证。

因此不能把“列有功能”直接等同于“可作为本地可替换模型的 MemoryBackend”。Python 外层的 Provider abstraction 无法自动控制上游内部模型调用。

### 15.2 Spike 范围与退出条件

建议时间上限一个工作日，与 History 开发解耦；固定 commit SHA 和许可证，记录运行环境。

必须用合成数据验证：

1. 无外网条件下所需读写功能是否可运行；实际需要哪些服务。
2. 能否保存已经审核的主张，跳过上游重复分类 / 提炼。
3. 是否能原样存取 revision 级证据、标签、适用时间和人工纠错。
4. superseded / archived 是否可按显式历史条件查询。
5. 所有模型、embedding、后台作业调用能否关闭或配置。
6. 撤回、清除和派生失效能否可靠传播。
7. 完整导出 / 恢复能力和迁移成本。
8. 与 SQLite 基线相比，新增部署与适配工作是否换来明确价值。

输出 `docs/open-brain-evaluation.md`：逐项证据、测试命令、失败、需修改接口、运维成本及 go/no-go。出现结构性 blocker 即结束评估，继续 SQLite；不默认 fork 重写上游。

`MemoryRepository` 定义主张及证据持久化契约；`RetrievalIndex` 定义可重建搜索索引契约；`LLMProvider` 与 `EmbeddingProvider` 定义推理契约。不要把它们混在一个不透明 `MemoryBackend` 里，使上游绕过权限和证据校验。

---

## 16. 目录与运行方式

建议代码仓库与私有数据分离。路径可配置，以下仅为示意：

```text
personal-brain/
  pyproject.toml
  uv.lock
  config/*.example.yaml
  src/personal_brain/
    importers/
    history/
    policy/
    retrieval/
    mcp/
    cli.py
    # 后续阶段再创建 memory/ state/ obsidian/
  tests/fixtures/          # 合成数据
  tests/integration/
  tests/evals/
  docs/

<private-data-root>/       # 不在 Git、默认云同步或 Agent 工作目录中
  raw/
  db/brain.sqlite
  cache/
  private_evals/
  projections/
  backups/
```

`.gitignore` 是补充保护，不是唯一措施。配置示例不得包含真实账号、密钥、聊天路径或正文。Secret 通过环境或 OS Keychain 提供；不需要远程模型时不要求配置 API key。

首版建议 CLI：

```text
brain import-chatgpt <zip-or-directory> --source <account-alias>
brain status
brain label-source <source-id> --scope <scope>       # 本地显式分类
brain search-history "职业" --from 2026-08-01 --to 2026-09-01
brain recent --days 7
brain show-event <event-id> --revision <revision-id>
brain eval --suite <suite-path>
brain backup
brain restore-check <backup-path>
personal-brain-mcp --config <trusted-config-path>
```

标签命令需显示作用范围及是否混合话题，不自动将所有资料授予普通 profile。上述命令是待实现接口，不是当前已可执行功能。

---

## 17. 实施阶段与交付门槛

### Phase A：档案与中文历史检索

实现来源 / revision Schema、导入器、图结构、时间处理、幂等、中文基线、来源定位、CLI、基础标签和删除失效机制。

验收：

- 相同导出重复导入不重复事件或版本。
- 相同 ID 修改、新旧导出乱序、共享分支祖先、未知活跃路径均有明确行为。
- “职业 / 求职 / 安全 / AI / AI工程 / 单汉字 / 标点”符合约定匹配语义。
- 所有结果能定位到原始版本；未知内容可报告。
- 导入中断恢复、备份恢复和源撤回测试通过。

### Phase B：权限与只读 MCP

实现 4 个首版工具、服务端 profile、输出限制、外发边界声明和逐结果授权。

验收：

- 越权查询、按 ID 查询、邻接上下文、计数 / 标题和旧缓存均不能绕过策略。
- 提供合成数据 MCP 集成测试与客户端配置说明。
- 授权真实样本可端到端引用；服务端不调用远程 LLM。
- 不暴露未实现的 current-state 或 Memory 工具。

### Phase C：真实使用基线

完成约 30 个问题的标注与评测，记录覆盖、失败类型、查询时延和 token 成本，达到第 9 节门槛。

若常见问题已被 History-only 良好解决，可以长期停留在这一版本。Memory 不作为为了“完成架构图”而必做的工作。

### Phase D：候选记忆与本地人工审核

实现小批量提炼、证据边、主张类别、复核队列、人工纠错、派生失效和受权限控制的 `search_memory` / `explain_memory`。

验收：全部候选人工审核后激活；通过第 9.3 节对照再考虑按类型自动激活。失败时保留 History fallback，不自动扩大提炼范围。

### Phase E：当前状态与 Obsidian

实现逐项证据、复核期限、完整重建 / diff、权限投影、编辑冲突检测和历史快照清除。

验收：长期目标不因最近没提自动消失；讨论不变成当前优先级；纠错 / 撤回能传播到状态和生成文件。

### Phase F：微信与持续维护

先验证用户实际平台和可获取的导出格式，再实现消费导出数据的 adapter；不把实时数据库逆向耦合进核心。正确映射 owner/contact、群聊和引用；第三方信息默认收紧。

WeChatMsg 公开说明列出 Windows 本地数据库支持；不能据此承诺用户的 macOS 数据提取路径已解决。数据获取是独立前置验证项。

最后再按真实使用需求增加定期导入、提炼、刷新和备份任务。显式记录覆盖时间，不能把周期性导出称为实时同步。

---

## 18. 可观测性与故障行为

结构化日志仅含任务 ID、耗时、计数、版本和错误分类；默认不含正文、query、人物名、绝对路径或完整 Provider 请求。

至少记录：解析失败 / 不支持记录、版本冲突、导入去重、查询回退、查询时延、拒绝访问、候选拒绝原因、人工审核量、陈旧派生对象、重建积压、外部 token / 成本。

| 故障 | 行为 |
|---|---|
| 部分格式无法解析 | 快照标 partial，报告覆盖缺口；不冒充完整导入 |
| 模型或 embedding 不可用 | History 正常；候选停留待处理，有限重试 |
| 派生重建失败 | 旧失效对象不返回；可返回授权原始证据并说明状态未刷新 |
| 权限配置无效 | 拒绝启动 / 拒绝正文读取，不回退 full access |
| 删除作业失败 | 数据维持 withdrawn；恢复清理，不恢复可见性 |
| 查询无结果 | 区分正常无匹配、覆盖未知、路径未知和资源超限 |

---

## 19. 首轮实现任务模板

以下仅用于用户之后明确要求实现时。本次修订文档不触发执行。

```text
根据 Personal Brain v0.2 技术设计实现 Phase A 与 Phase B。

先检查目标仓库及适用指令，核对实际导出样本结构和本地运行环境，
给出精确到模块与验收项的计划，再在该范围内实现。

范围：
- 版本化 ChatGPT 导入、原始档案、对话图与所选路径。
- SQLite、中文子串基线与可验证 FTS 优化、时间检索、来源定位。
- 来源与事件标签、受信任 profile、外发声明、撤回与缓存失效。
- 4 个只读 MCP 工具及本地 CLI。
- 合成 fixture、权限测试、导入恢复与备份恢复测试。

不实现 Memory 提炼、CURRENT_STATE、Obsidian、微信、前端或 Open Brain 集成。
没有稳定验证的格式不得猜测解析后静默接受；默认不读取未授权个人正文。
遇到文档未定细节，在本阶段范围内选择最简单、可逆且可测试的实现并记录决策。

完成后交付：使用说明、Schema / migration、测试结果、已知限制、
未通过验收项和 Phase C 真实样本评测的准备清单。
不因全部命令能启动就宣称已验证端到端答案可靠性。
```

---

## 20. 事实依据与待验证事项

### 已核对的公开资料

- [Open Brain README](https://github.com/dagonet/open-brain)：用于确认项目声明的功能与 Supabase / Deno / OpenAI 主要链路。核对时间 2026-09-06；未运行验证。
- [SQLite FTS5 tokenizer 文档](https://sqlite.org/fts5.html#tokenizers)：默认 unicode61 的连续 token 行为，以及 trigram 对短全文查询的限制。
- [WeChatMsg 仓库说明](https://github.com/little-KaoKao/WeChatMsg)：导出与平台说明；未验证用户具体版本或操作系统兼容性。

### 已做的最小本地实验

在默认 FTS5 表中插入“我最近正在考虑职业方向变化”，用 MATCH 查询“职业”，返回 0 条。此实验说明不能直接依赖默认 tokenizer 完成目标中文子串检索；不代表本设计的二元索引与回退策略已经实现或通过性能验收。

### 实施前需记录的输入

- 目标仓库、私有数据根目录、备份目录及是否云同步。
- 导出结构、数据规模、账号身份、时间缺失情况。
- 初始授权对话范围、scope、消费 Agent 的本地 / 远程边界。
- 目标机器与查询性能实测。
- 后续提炼阶段才确定的 Provider、预算、审核量和可接受延迟。

当前无需为这些信息阻塞文档修订。进入依赖这些输入的实现步骤时，再通过最小样本和配置验证，不默认为“全量数据、所有权限、全部上传”。

---

**v0.2 的优先级：可找回的原文 → 正确的证据解释 → 可执行的权限 → 可修复的数据链 → 经对照验证的自动记忆。**
