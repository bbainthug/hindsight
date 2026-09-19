# 任务 D-1：本地语义检索（混合召回）

> 交付给实现 agent 的任务书。遵守 AGENTS.md 工作约定。

## 背景

现有检索只有 FTS 精确/连续子串匹配（`src/personal_brain/retrieval/search.py`），
"求职"命不中"找工作"，规格 §6.3/§262 把它写成了已知边界，`SearchResult.notes`
也在每次返回里声明这一点。规格 §103 预留了 `EmbeddingProvider`，§659 预留了可重建的
`RetrievalIndex` 契约。本任务把这两个预留项激活：在不改变 FTS 语义、不改变权限
边界的前提下，增加一条**本地**向量召回通道，并与 FTS 做混合排序。

规格文件：`Personal_Brain_技术设计与Codex实施规格_v0.2.md`（AGENTS.md 里写的
`docs/技术设计_v0.2.md` 是旧路径，以根目录这份为准）。按需读 §4.3、§6.1–6.3、
§9、§10.3 的 delivery boundary 部分、§14 撤回依赖边。

## 范围（只做这些）

1. `EmbeddingProvider` 协议 + 一个本地实现 + 一个测试用假实现
2. 向量索引表（sqlite-vec）+ 重建/增量命令
3. `search_history` 增加 `mode="exact"|"hybrid"`，hybrid 用 RRF 融合 FTS 与向量候选
4. CLI / MCP / `search_brain` 透传 `mode`，默认 `exact`（向后兼容）
5. 撤回与策略变更时清理向量行（§615 依赖边）
6. evals：新增语义用例；现有套件 Recall@10 不得回退
7. 文档：`docs/semantic-retrieval.md`；更新 `docs/unified-retrieval.md` 消费端约定第 1 条

## 非目标（明确不做）

- 不做记忆提炼 / 事实卡（那是 D-2）
- 不做远程 embedding、不做 reranker、不做查询改写
- 不改 FTS 的匹配语义、bigram 计划、`_verify` 逻辑
- 不把向量命中当作精确命中：语义命中必须可区分、可关闭

## 设计要求

### 依赖与降级

- 新增可选依赖组 `semantic = ["sqlite-vec", "fastembed"]`，核心安装保持零外部服务。
- 默认模型 `BAAI/bge-small-zh-v1.5`（中文为主、体积小），模型名与维度写入配置
  `semantic.model` / 由 provider 汇报维度；模型文件缓存目录可配置，默认走 fastembed 默认缓存。
- 未安装可选依赖、或索引不存在、或索引模型与配置不一致时：`mode=hybrid` **不报错**，
  退化为 exact 并在 `notes` 里说明原因（`semantic_unavailable: <reason>`）。
  `doctor` 要能报告语义索引状态（是否存在、模型、维度、已索引 revision 数、缺口数）。

### EmbeddingProvider

```python
class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
```

- `FastEmbedProvider`（本地）与 `HashEmbeddingProvider`（测试用：确定性、不下载、
  同义词表驱动，使"求职/找工作"向量接近）。**测试不得下载模型。**
- provider 只收归一化后的文本；命中已知凭据形态的 revision（复用
  `retrieval/credentials.py` 的检测）不 embed、不入索引（§329）。

### 索引存储

- `schema_migrations` 新增一版：
  - `semantic_index_meta(model_id, dimension, chunk_chars, chunk_overlap, built_at, index_version)`
  - `revision_chunks(chunk_id PK, revision_id, chunk_index, text_start, text_end)`
    （归一化坐标，便于 snippet 还原）
  - `vec0` 虚拟表 `revision_chunk_vectors(chunk_id, embedding float[dim])`
- 只索引：`sources.status != 'withdrawn'` 且 `revision_policy_state` 当前
  `availability='available'` 的 revision，与 `_BASE_FROM_WHERE` 保持同一判定。
- 分块：`chunk_chars` 默认 600、overlap 100，短文本单块。
- 命令：
  - `brain reindex-semantic [--full]`：默认增量（只 embed 缺失/失效的 revision），
    `--full` 重建。幂等：同一库连跑两次第二次 0 新增。
  - 撤回来源/事件、策略状态变为不可用时，同事务删除对应 chunks/vectors。
    模型或 chunk 参数变化 → `index_version` 变化 → 视为全部失效。
- 索引是派生数据：备份 manifest 不必包含，`restore-check` 后可 `--full` 重建。

### 混合检索

- `search_history(..., mode: str = "exact")`。
- hybrid 流程：
  1. exact 路径照旧得到 `exact_hits`。
  2. 向量路径：query embed → vec 取 top `k_sem`（默认 `limit*5`，上限 200）→
     按 `SearchFilters` 用**同一套** `_filter_sql` 过滤（时间/来源/对话/说话者/
     selected path/标签），过滤后再按 revision 去重（取最优 chunk）。
  3. RRF 融合（k=60），两路都命中的分数相加。
  4. 分页 cursor 需要能编码融合后排序；`order` 为时间序时融合分数只做同刻 tie-break。
- `SearchHit` 新增字段：`match_kind: Literal["exact","semantic","both"]`、
  `semantic_score: float | None`。`score` 字段语义不变。
- 语义命中的 snippet 用对应 chunk 的窗口（无 `match_span`）。
- `SearchResult.notes` 改为动态：hybrid 生效时不再输出"语义扩展未实现"，
  改为 `semantic_hits_are_inferred: 语义命中为推断，非原文连续出现`（§390）。
- `query_plans` 追加一条 `{"path": "semantic", "model": ..., "k": ..., "filtered": n}`。

### 权限与边界（不可妥协）

- MCP `tool_search_history` / `search_brain` 中，向量候选必须和 exact 候选一样经过
  `_authorized_revision_ids` / profile 过滤；写一个测试证明：同一 query 下，
  hybrid 返回集合 ⊆ profile 授权集合，且 exact 命中集合 ⊆ hybrid 命中集合。
- `delivery_boundary=remote_model_allowed` 的凭据遮蔽逻辑对语义命中同样生效。
- embedding 全程本地，不新增任何出网路径；模型下载只在 `reindex-semantic`
  首次运行时发生，且要在命令输出里明说。

### CLI / MCP

- `brain search-history --mode hybrid`、`brain recall --mode hybrid`
- MCP 工具 schema 增加 `mode` 枚举参数，默认 `exact`；工具描述里说明语义命中是推断。
- profile 可配置 `semantic_default: exact|hybrid`（默认 exact），未传 `mode` 时采用。

### evals

- `evals/suites/dev-synthetic.json` 增加语义用例组（合成数据）：至少覆盖
  求职/找工作、远程/在家办公、简历/CV 三组同义，每条给出可接受 revision 集合、
  必须区分的语义、禁止结论（沿用 §411 格式）。
- `brain eval --mode hybrid` 与 `--mode exact` 都要能跑；
  回归门：hybrid 在**原有**用例上的 Recall@10 ≥ exact；新用例仅 hybrid 计分。
- `bench`：增加 hybrid 一行 P95，单独披露（不与 exact 混算）。

### 测试（`tests/unit` / `tests/integration`）

- 分块边界与坐标还原
- RRF 融合顺序与去重
- `reindex-semantic` 幂等；`--full` 后 `index_version` 变化
- 撤回后向量行消失；策略状态翻转后消失/恢复
- 缺依赖 / 无索引 / 模型不一致三种退化路径都返回 exact 结果 + 对应 note
- 凭据命中 revision 不入索引
- 过滤一致性：对随机过滤条件，hybrid 语义候选集合 ⊆ exact 路径同过滤下的全库集合
- 全部用 `HashEmbeddingProvider`，CI 无网络

## 交付

按 AGENTS.md：改动摘要、可复现命令（`uv run pytest`、`uv run ruff check`、
`uv run mypy`、`brain eval --mode hybrid`）、结果、未解决问题。
另附：在合成库上 `reindex-semantic --full` 的耗时与索引体积。

## 已知取舍（不用来回问）

- 模型选 bge-small-zh 是为体积和中文；英文弱可接受，D-1 不做多语切换。
- 默认 `exact` 而不是 `hybrid`，是为了现有消费者行为不变；切默认值是后续决定。
- 不做实时 embed（导入时不触发），先靠 `reindex-semantic` 增量；接入 launchd 作为后续项。
