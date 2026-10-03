# 可溯源的个人事实层（D-2 v0）

Facts 在本地 SQLite 中保存经过人工审核的主张及其原话证据。提炼结果默认是候选；只有用户审核通过后才进入 active 视图。D-2 只提供本地 CLI，不注册写入或记忆检索 MCP 工具。

## 模型与边界

- v0 类型：`preference`、`goal`、`decision`、`project_fact`、`open_question`。`behavior_pattern` 暂不支持。
- `claims` 保存稳定主张身份和生命周期；`claim_revisions` 保存不可变措辞、类型、审核状态和时间；`claim_evidence` 指向确切的事件 revision 与原文跨度；`claim_relations` 保存建议或人工确认的关系；`extraction_runs` 保存输入摘要、配置版本、预算统计；`review_log` 记录人工审核与派生失效。数据库 migration 为 schema v9（D-12：原编号 8 让位给 D-11 的 withdrawal_audit，v8→v9 升级由回归测试覆盖）。
- 证据跨度以原文字符位置为准，并保存原文片段 SHA-256。发给模型前复用凭据遮蔽；遮蔽保持文本长度，所以模型看到的引用仍可映射回原文。CLI 展示原话时也会再次遮蔽已识别的凭据形态。
- 提炼输入遵循所选对话路径和当前可用策略状态。owner 发言是唯一可作为 support 的证据；assistant 内容只能帮助理解上下文。assistant-only、无有效证据或引用跨度无法核对的候选会被拒绝。
- 同类型相近主张达到去重阈值时，新证据合并到现有主张。CLI 优先使用已缓存的本地 FastEmbed 向量模型；模型不可用时退回字符二元组 Jaccard，不下载模型。与 active/candidate 主张相关但未达到去重阈值时，模型可以建议 `same`、`refines`、`supersedes`、`contradicts` 或 `coexists`；建议不会改变任何主张状态，必须由用户裁决。
- 撤回被引用的事件 revision 后，依赖主张进入 `needs_review` 并保留审计记录。撤回不自动删除主张历史。

## 提炼流程与预算

`brain facts extract` 默认处理最近 30 天。`--since`、`--until` 和 `--source` 可缩小窗口；`--source` 匹配数据库中的账号别名。系统先按所选路径分段、加入有限上下文并遮蔽已识别的凭据，再做估算和预算检查。

`--dry-run` 通过只读连接读取本地库，分段和估算，不应用待执行的 schema migration、不创建批次、不写 Facts 表、不创建 LLM 客户端，也不读取 API key。估算包括每段提炼请求和最多一批关系建议，输入侧为字符数启发式估算并为最多 20 对关系预留空间，输出侧按每请求配置的最大输出 token 数计算。它用于判断量级，不等同于提供方计费 tokenizer 的精确用量。

默认配置示例使用 OpenAI 兼容的 DeepSeek 接口。模型 URL、模型名、温度、超时、分段上限、输出上限、预算、`context_messages`（切分时携带的上下文条数）、`max_owner_chars`（跳过超过该长度的 owner 发言，如粘贴的长日志；预算收窄用，默认关闭）与 `include_assistant_context`（分段是否携带 assistant 消息，关闭可大幅省预算，默认开启）可在 `config/config.yaml` 的 `facts:` 段设置；`api_key_env` 只指定环境变量名，密钥仅从该环境变量读取。也可在命令行用 `--base-url` 和 `--model` 覆盖接口与模型。正式提炼会把已遮蔽的 owner/assistant 上下文发送到配置的外部模型；执行前应由用户选定提供方和预算。

成功批次以实际输入 revision 集合、分段规则、提示词/schema/规则版本及模型配置组成幂等键。完成批次不会重复请求；相同键的失败批次可重试；相同键已有运行中的批次会拒绝并发启动。

## 命令

```bash
# 只估算最近 30 天；不调用模型、不写库
brain facts extract --dry-run

# 指定窗口、来源与预算；正式提炼会调用配置的模型
brain facts extract --since 2026-09-02 --until 2026-10-02 \
  --source codex --budget 500000

# 交互式审核；支持通过、拒绝、改写、跳过和关系裁决
brain facts review

# 非交互审核示例
brain facts review --claim <claim-id-prefix> --action approve
brain facts review --relation <relation-id> --decision coexists

# 浏览、回查证据与生成决策时间线
brain facts list --type decision --status pending
brain facts explain <claim-id-prefix>
brain facts timeline --type decision

# 默认只显示 diff；只有显式 --write 才写入
brain facts export-profile --out ~/me/profile.md
brain facts export-profile --out ~/me/profile.md --write
```

`review` 跳过的主张继续留在审核队列。`timeline` 默认仅列出 approved 且 active 的主张；`--include-superseded` 可查看被替代的决策。`explain` 显示当前修订的全部证据定位、时间、来源与引用。

`export-profile` 仅替换 `<!-- facts:auto:start -->` 与 `<!-- facts:auto:end -->` 之间的区域。现有目标文件没有唯一且顺序正确的一对标记时会拒绝；目标不存在时会生成带标记的草稿。默认只打印 unified diff，只有 `--write` 才落盘。标记区外的文本（包括原有换行）保持原样。

## 已知限制

- 本地库中的 ChatGPT 数据只到 **2026-09-06**；之后的 ChatGPT 数据走 VM，本阶段不从 VM 拉取。真实自用验收需要等用户选择提供方与预算并审核结果。
- 本机没有缓存的 FastEmbed 模型时，相似度退回字符二元组 Jaccard，中文同义表达的判断能力有限。阈值只负责去重或触发关系建议，不代表事实置信度。
- 代码核对跨度与作者角色，但不自动证明引用在语义上支持整条主张；候选必须由用户阅读证据后审核。
- token 预算是字符启发式估算，不保证与模型实际 tokenizer 一致；单条超长消息可能仍超过模型上下文限制。预算门会按估算拒绝超限批次，但不会估算网络费用。
- 凭据遮蔽基于已知凭据形态，不是完整 DLP。其他个人信息仍可能出现在提交给模型的上下文中；必须由用户确认模型提供方和数据外发范围。
- 本阶段只在提炼输入时检查来源可用性，Facts 没有独立的 MCP 授权或按 scope/sensitivity 的投影过滤；profile 导出是本地文件操作，生成文件应由用户按自己的访问边界保管。
- 未实现自动激活、跨项目 `STATUS.md` 更新、Web 审核、Facts MCP 搜索、VM 提炼和 9.3 对照评测。任何候选都需要人工审核。
