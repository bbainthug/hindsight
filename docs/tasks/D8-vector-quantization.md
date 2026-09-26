# 任务 D-8：语义检索在 1 GB VM 上可用——向量量化 + 增量索引提速

> 交付给实现 agent 的任务书。遵守 AGENTS.md 工作约定。技术背景见 `docs/unified-retrieval.md`
> 与 D-1 任务书（`docs/tasks/D1-semantic-retrieval.md`）。

## 背景

D-1 的语义索引已经在 VM（Azure B2ats v2：2 vCPU / 约 900 MB 内存 / 2 GB swap，突发型 CPU）上全量建完：
6.8 万 revision → 22 万 chunk，bge-small-zh（512 维 float32）。`revision_chunk_vectors`（vec0）占 **457 MB**，
MCP 进程常驻约 430 MB。VM 空闲时实测（2026-09-26，热态，n=15）：

| 接口 | P50 | P95 | 目标 |
|---|---|---|---|
| `/api/search` hybrid | 4.5 s | 5.9 s | ≤ 1 s |
| `/api/recall` hybrid | 5.2 s | 5.9 s | ≤ 2 s |
| `/api/search` exact | 20 ms | 21 ms | — |
| `/api/timeline` 30 天 | 0.31 s | 0.35 s | ≤ 1 s |

推测主因：vec0 全量暴力 KNN 每次要扫 457 MB，放不进页缓存，每次查询都在读盘。
**这只是推测**，先测量再动手（见范围 1）。

另一个问题：VM 上每轮导入后跑的增量 `reindex-semantic`，即使只新增 8 个事件也要约 **2 分钟**。
从代码看，增量路径会对**全部**可索引 revision 重新归一化 + 分块来算期望 chunk 数，做 22 万行的
`LEFT JOIN` 计数，并用 `NOT IN` 清扫孤儿向量（会扫整张 vec0 表），另外还要加载模型。

目前 VM 的 `semantic_default` 已临时改回 `exact`（显式传 `mode=hybrid` 仍可用）。本任务完成后切回 `hybrid`。

## 目标

1. VM 上 hybrid 检索热态 **P95 ≤ 1 s**（search）/ **≤ 2 s**（recall 默认参数），语义召回质量基本不降；
2. 增量 reindex 在"无新数据"时 **≤ 10 s**，"新增几十个事件"时 **≤ 60 s**（含模型加载）；
3. 从现有 float 向量迁移，**不重新 embed**——VM 上全量 embed 花了约 7 小时，不能再来一次。

## 范围

### 1. 先测量（必须先做，结论写进交付）

在合成 10 万事件库（`brain bench` 已有生成器）上构建语义索引，并用 `ulimit`/cgroup 或更小的
`PRAGMA cache_size` 模拟"向量表放不进内存"，把一次 hybrid 查询拆开计时：query embed、`knn_chunks`、
授权过滤、`semantic_unavailable_reason` 等状态检查、候选组装。确认瓶颈确实在 KNN 扫描；如果不是，
按实际瓶颈调整方案，并在交付里写明。

### 2. 向量量化（预期主方案，按测量结果定）

- 候选方案：sqlite-vec 的 `bit[512]`（二值量化，约 14 MB）或 `int8[512]`（约 114 MB）做粗排，
  再对前 `k × oversample` 个候选用 float 向量**重排**。float 向量移到普通表（按 `chunk_id` 主键，BLOB），
  只在重排时按主键随机读几百行，不再全表扫；
- 选哪个、oversample 取多少，用范围 4 的召回对比数据决定，并写明理由；
- `distance_to_cosine` 的换算只对 float L2 成立，重排后的分数必须仍是 float 余弦，返回字段语义不变；
- 量化参数（类型、oversample）进入 `index_version`，这样参数一变，已有索引就会被判定为不匹配；
- `semantic_status` / `brain doctor` 能显示当前索引的量化方式与各表大小。

### 3. 迁移：从现有 float 向量就地转换

- 新 migration（schema v7）或 `brain reindex-semantic --convert`：流式读出现有 `revision_chunk_vectors`
  的 float 向量，写入新结构（量化表 + float 表），分批提交，峰值内存可控（VM 上 `MemoryMax=450M` 能跑完）；
- 中断后能续跑，不丢失已转换的部分；转换完成前查询路径视为 `index_building`（沿用现有语义），
  不返回半截结果；
- 转换完成后删除旧 vec0 表并 `VACUUM`（或说明为什么不做，比如 VACUUM 需要 2 倍磁盘）。

### 4. 召回质量对比

- 合成库：固定 50 个查询，比较 float 全量 KNN 与量化 + 重排的 top-10 语义候选重合率，报平均值与最低值；
  目标平均 ≥ 0.9；
- VM 真实库：同样的比较脚本只输出**聚合数字**（重合率、延迟），不输出任何消息正文或标题；
  查询集用 10 个泛化词（如"工作""项目""学习"），不要用真实聊天里的句子。

### 5. 增量 reindex 提速

- 期望 chunk 数不要每次对全量 revision 重新分块算：把每个 revision 的 `(text_hash, chunk_params) → chunk 数`
  持久化（或用已有字段判定未变化），只处理新增/变化/撤回的 revision；
- 孤儿清扫改成只针对本轮撤回/变化的 revision（撤回路径已有 `purge_revision_semantic`），
  全表清扫移到 `--full` 或 `brain doctor --fix`；
- 没有待 embed 的内容时**不加载模型**；
- `deploy/vm/import_inbox.sh` 的增量调用保持不变即可，只要单次耗时达标。

### 6. 文档与部署说明

- `docs/unified-retrieval.md` 语义部分更新：量化方式、重排、召回对比数字；
- `deploy/vm/README.md` 写明 VM 上的转换步骤（停 import timer → 转换 → 起 timer）、预计耗时、回滚方法；
- **不要部署到 VM**，也不要改 VM 配置：部署、转换、切回 hybrid 由我来做。

## 非目标

- 不换 embedding 模型、不改分块参数（换了就得重新 embed）；
- 不改 `search_history` / `recall` 的返回格式和授权逻辑，KNN 之后"同一套 `_filter_sql` 过滤"的约束不变；
- 不做 ANN 索引（HNSW 等），22 万规模量化后暴力扫描已足够。

## 测试

- 单元：量化/反量化；重排后分数与 float 余弦一致（误差容限写明）；`index_version` 随量化参数变化；
  增量路径只处理变化的 revision（用计数断言 embed 调用次数，无新数据时 embed 调用为 0、模型不加载）；
- 集成（合成库）：float → 量化转换前后，同一查询的最终 `search_history` 结果（exact 部分）完全一致，
  语义部分 top-10 重合率达标；转换中断后续跑结果与一次跑完相同；转换进行中查询返回 `index_building`；
- 撤回回归：撤回 revision 后，其 chunk、float 向量、量化向量三处都被清除，语义检索不再返回它；
- 全量 pytest、ruff、mypy 干净。

## 交付

改动摘要、范围 1 的测量结论、召回对比表、合成库上 P50/P95（hybrid search / recall，放不进内存的模拟条件下）、
增量 reindex 耗时（无新数据 / 新增 50 事件）、VM 转换的预计耗时与峰值内存、可复现命令、未解决问题。
