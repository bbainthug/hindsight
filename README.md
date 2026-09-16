# Hindsight

**可追溯的对话知识检索与受控记忆系统** —— 把 ChatGPT / Claude 等多源对话导出导入本地 SQLite，向编码 Agent 提供带权限、带证据引用的检索，并用自建评测门、性能门与长稳基准证明它"测透、评准、跑稳"。

> 本地优先、只读 MCP、无网络调用、不调远程模型。Python ≥ 3.12 · SQLite FTS5 · Pytest。

## 它解决什么

- **可追溯**：每条检索结果都能定位到原始对话的句级位置（归一化保留原文偏移），引用 100% 可解析。
- **受控**：权限优先的只读 MCP（stdio）——服务端 profile 绑定、逐结果再授权、"无权限 / 不存在"统一返回以收口探测、计数不泄露隐藏记录；版本化撤回让重复导入无法"复活"已撤回内容。
- **可评测**：内置评测运行器（Recall@K、证据覆盖率、引用可解析率、拒答 / 越权断言、失败分类），合成语料经真实导入器生成并分离开发 / 保留集。
- **可复现**：性能门、并发 / soak 基准与容器化复现全部一条命令重跑，报告为 JSON，每个数字有出处。

## 数据（全部可复跑）

| 项目 | 结果 | 出处 |
|---|---|---|
| 测试 | **280 passed**（宿主机与容器一致） | `pytest` / `docker compose run --rm test` |
| 性能门 | 10 万条语料预热后各查询 **P95 ≤ 1s**（实测 0.7–0.9s） | [`docs/evals/benchmark-100k.json`](docs/evals/benchmark-100k.json) |
| 导入优化 | FTS 写入由全表扫描改按 rowid 更新（O(n²)→O(log n)），10 万条导入 **15 分钟级 → 约 20 秒** | 决策 32 |
| 并发梯度 | 只读并发 1/2/4/8：P95 = **471 / 507 / 701 / 1171 ms**，0 错误 | [`docs/evals/soak-20260915-host.json`](docs/evals/soak-20260915-host.json) |
| 长稳 soak | 4 并发 × 30 分钟，17,814 次查询，P95 692 ms，漂移比 0.78，worker RSS 增长 1.2%，`integrity_check` ok，**8/8 门槛通过** | [`docs/evals/soak-20260915.md`](docs/evals/soak-20260915.md) |
| 并发写（加分项） | 读写同库时读侧 P95 抬升（漂移 4.41，**未过门槛**），如实披露并给出改进方向 | [`soak-20260915-host-writer.json`](docs/evals/soak-20260915-host-writer.json) |
| 容器复现 | 镜像 477MB、不含任何私有数据；容器内 bench 门槛通过（P95 128–414 ms） | [`docs/evals/docker-reproducibility.md`](docs/evals/docker-reproducibility.md) |

失败的运行（首轮受下载干扰的 soak、容器缩短口径冒烟）同样保留在 `docs/evals/`，没有挑数据。

## 快速开始

```bash
uv sync                                   # 或 pip install -e ".[dev]"
cp config/config.example.yaml config/config.yaml   # 指向你自己的私有数据目录（不要放进仓库）

brain import-chatgpt <chatgpt-export.zip>            # 幂等导入，事件-版本模型（修订 / 分支 / 冲突保留）
brain search-history "关键词" --from 2026-01-01      # FTS5 + 中文二元切分；--source / --conversation / --cursor 过滤与 keyset 分页
brain recent --limit 20                              # 最近事件
brain eval --suite evals/suites/dev-synthetic.json   # 评测运行器（Recall@10 / 引用解析 / 权限断言）
brain bench --conversations 500 --messages 200       # 10 万条性能门
brain soak --mode both --concurrency 4 --duration 1800   # 并发梯度 + 长稳（漂移 / RSS / 完整性门槛）
personal-brain-mcp                                   # 只读 MCP（stdio）
```

容器：

```bash
docker compose build
docker compose run --rm test    # 280 passed
docker compose run --rm bench   # 写 docs/evals/benchmark-docker.json
docker compose run --rm soak    # 全量约 1.5–2h
```

## 架构

```
导出 zip ──► importers（幂等 · 事件-版本 · ZIP 流式解析）──► SQLite（events / revisions / occurrences / FTS5）
                                                                   │
                     policy（profile · 逐结果授权 · 撤回纪元）◄──────┤
                                                                   ▼
                 retrieval（bigram 候选缩小 · 偏移溯源 · keyset 分页）──► CLI / 只读 MCP（stdio）
                                                                   │
                        evals（runner · synthgen · benchmark · soak）◄┘
```

- `src/personal_brain/importers/` 多源导入与事件-版本模型
- `src/personal_brain/retrieval/` FTS5、中文二元切分、检索与凭据遮蔽
- `src/personal_brain/policy/` 权限 profile 与撤回
- `src/personal_brain/mcp_server/` 只读 MCP
- `src/personal_brain/evals/` 评测运行器、合成语料、性能基准、soak

> Python 包名暂为 `personal_brain`（项目早期名称），命令为 `brain`；仓库改名为 Hindsight 后包名将在后续版本迁移。

## 设计记录

- [`docs/decisions-phase-a.md`](docs/decisions-phase-a.md) 数据层决策
- [`docs/phase-b-mcp.md`](docs/phase-b-mcp.md) 权限与只读 MCP
- [`docs/phase-c-evals.md`](docs/phase-c-evals.md) 评测与规模门槛
- [`docs/unified-retrieval.md`](docs/unified-retrieval.md) 统一检索

## 数据与隐私

真实对话、数据库、密钥一律放在仓库外的私有目录（`config.yaml` 指向），`.gitignore` 与 `.dockerignore` 双重排除；仓库与镜像中只有合成语料。
