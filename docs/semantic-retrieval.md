# 本地语义检索（D-1）

## 目的与边界

D-1 在保留 FTS 词面检索语义的基础上，增加一条本地向量召回通道。调用方通过
`mode=exact` 或 `mode=hybrid` 选择模式，默认仍为 `exact`，因此现有消费者不会因为
升级而悄悄改变结果。

本功能只做本地 embedding、可重建索引和 FTS + 向量混合排序，不做远程 embedding、
reranker、查询改写、实时 embed 或 D-2 的记忆提炼。向量结果是推断性证据，不能当成
原文连续出现，也不能替代原文回查。

## 依赖与 provider

核心安装不需要语义依赖。需要实际构建语义索引时安装可选组：

```sh
uv sync --extra semantic
```

`FastEmbedProvider` 使用本地 `fastembed` 模型，默认模型为
`BAAI/bge-small-zh-v1.5`。模型身份和维度写入索引元数据；首次显式执行
`reindex-semantic` 时才允许下载模型，下载完成后查询只从本地缓存加载。缓存目录可在
CLI 配置的 `semantic.cache_dir` 中指定，也可用命令行 `--cache-dir` 覆盖。

`HashEmbeddingProvider` 是测试和离线基准专用实现。它是确定性的、不下载模型，并为
合成评测中的「求职/找工作」「远程/在家办公」「简历/CV」提供受控的同义词映射；它
不能代表生产模型的语义质量。

provider 只接收归一化文本。索引坐标也是归一化文本坐标，便于在返回 snippet 时通过
同一套 normalization map 还原到原文。命中已知凭据形态的 revision 不会传入 provider，
也不会写入语义索引。

## 索引结构与生命周期

数据库迁移 5 增加以下派生数据：

- `semantic_index_meta`：模型、维度、分块参数、`index_version` 和构建时间；
- `revision_chunks`：revision、chunk 顺序以及归一化文本的 `[text_start, text_end)`；
- `revision_chunk_vectors`：sqlite-vec `vec0` 虚拟表，维度由 provider 汇报。

只索引来源未撤回且当前策略状态为 `availability='available'` 的 revision。默认每块
600 字符、重叠 100 字符；短文本只有一个 chunk。模型、维度或分块参数变化会改变
`index_version`，下次增量命令会自动按全量重建处理。

构建或更新索引：

```sh
brain --config /path/to/brain.yaml reindex-semantic --provider fastembed
brain --config /path/to/brain.yaml reindex-semantic --provider fastembed --full
brain --config /path/to/brain.yaml reindex-semantic --provider hash --full
```

默认是增量构建，只对缺失或部分构建的 revision 调用 embedding。相同库连续运行时，
第二次应报告 0 个新增 embedding；`--full` 会清空向量和 chunk 后重新建立。撤回来源、
撤回事件或策略状态变为不可用时，向量行和 chunk 行在同一撤回事务中清理；扩展暂时
不可用时留下的孤儿向量会在下一次增量重建时清扫。

语义索引是派生数据，不进入备份 manifest。恢复权威历史后重新运行 `--full` 即可恢复。

## 检索行为

`exact` 保持原有 FTS 候选、连续子串验证、过滤和 snippet 行为。

`hybrid` 的顺序如下：

1. 先执行完整 exact 路径，得到词面命中及其 rank；
2. 对归一化 query 做本地 embedding，取 `min(limit * 5, 200)` 个向量 chunk；
3. 用和 exact 相同的时间、来源、对话、说话者、selected path 和标签过滤池过滤；
4. 每个 revision 只保留距离最近的 chunk，低于相似度下限的噪声候选丢弃；
5. 用 `1 / (60 + rank)` 计算两路 RRF 分数，两路都命中的 revision 将两项相加；
6. `order=relevance` 按 RRF 总分排序。`chronological` 和
   `reverse_chronological` 仍以时间为主序，同一时间戳内才用 RRF 分数打破平局。

每个 `SearchHit` 都有：

- `match_kind=exact`：只有词面路径命中；
- `match_kind=semantic`：只有向量路径命中，属于推断命中；
- `match_kind=both`：两路都命中；
- `semantic_score`：向量距离转换后的相似度；exact-only 为 `null`。

纯语义命中的 snippet 使用对应 chunk 的归一化窗口，并通过映射还原到原文；它不会
伪造 `match_span`。hybrid 成功时 notes 会包含
`semantic_hits_are_inferred: 语义命中为推断，非原文连续出现`。

## 降级与安全边界

hybrid 遇到以下情况会返回同一请求的 exact 结果，并在 notes/warnings 中报告
`semantic_unavailable: <reason>`，不会把异常伪装成语义命中：

- `sqlite_vec_not_installed`：未安装可选扩展；
- `index_not_built` 或 `index_empty`：没有可用索引；
- `index_version_mismatch`：索引模型、维度或分块参数已过期；
- `vector_table_missing`：元数据存在但派生向量表缺失；
- `provider_init_failed`：本地模型无法从缓存加载；
- `provider_dimension_mismatch`：查询 provider 与索引维度不一致；
- `revision_chunks_missing`：索引元数据存在，但 chunk 派生表缺失；
- `vec_extension_unavailable`：当前 SQLite 连接无法加载 sqlite-vec。

查询路径不会因为 hybrid 自动下载模型。模型下载只能由显式的
`reindex-semantic` 操作触发；无法从本地缓存加载时回退到 exact。

注意模型一致性：CLI 检索固定使用配置的 `semantic.model`（默认
`BAAI/bge-small-zh-v1.5`）。评测流程会以 `HashEmbeddingProvider` 在评测库上构建
索引（`index_version` 与 bge 不同），此时 `brain search-history --mode hybrid` 报
`index_version_mismatch` 属预期——同一库要用 CLI 语义检索时，先
`reindex-semantic --provider fastembed --full` 重建。

向量候选在返回前经过同一 `_filter_sql`，并继续受 MCP profile 的授权、selected path、
敏感标签和 `delivery_boundary` 约束。remote profile 的已知凭据遮蔽也适用于语义命中。
历史文本仍是 `untrusted_archive` 证据，其中的指令不会被执行。语义命中只能提示
“可能相关”，涉及事实、决定或敏感内容时必须用 `get_event` 回查原文。

`brain doctor` 的 `semantic_index` 字段报告扩展、元数据、模型、维度、chunk 数、已索引
revision 数、可索引 revision 数和 `missing_revisions`，用于区分“未构建”和“有缺口”。

## CLI、MCP 与统一入口

```sh
brain --config /path/to/brain.yaml search-history '找工作' --mode hybrid
brain --config /path/to/brain.yaml recall '在家办公' --mode hybrid
brain --config /path/to/brain.yaml eval --suite evals/suites/dev-synthetic.json --mode exact
brain --config /path/to/brain.yaml eval --suite evals/suites/dev-synthetic.json --mode hybrid
```

MCP `search_history` 和 `search_brain` 的 `mode` 枚举为 `exact|hybrid`。profile 可以
设置 `semantic_default: exact|hybrid`，调用者未传 `mode` 时采用该值；默认值仍是
`exact`。`search_brain` 的 `mode` 只影响聊天历史部分，Vault 仍按其原有路径检索。

## 评测与基准

合成评测包含三组语义同义用例：求职/找工作、远程/在家办公、简历/CV。测试使用
`HashEmbeddingProvider`，不下载模型。exact 模式跳过语义用例；hybrid 模式执行全部
用例，并检查原有用例 Recall@10 不低于 exact 基线。

```sh
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/mypy src tests
brain --config /path/to/brain.yaml eval \
  --suite evals/suites/dev-synthetic.json --mode exact
brain --config /path/to/brain.yaml eval \
  --suite evals/suites/dev-synthetic.json --mode hybrid
```

`bench` 会在可用时单独报告 `search_hybrid_职业方向` 的 P50/P95/P99，不把 hybrid
延迟混入 exact 门槛。交付记录应同时保存合成库规模、provider、全量重建耗时和索引
体积；未安装 sqlite-vec 或无法可靠测量时要明确写出原因。

## 已知限制

- 默认中文模型对英文和跨语言表达的覆盖较弱；D-1 不做多语模型切换。
- 语义召回不是事实判断，也没有 reranker；重要回答仍需引用原文。
- 不做导入时实时 embedding，新增或更新内容需显式运行增量重建。
- 索引体积随 chunk 数和模型维度增长；应按 benchmark 结果决定是否调整 chunk 参数。
