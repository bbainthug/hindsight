# Hindsight

把你和 AI 的对话——ChatGPT 导出、Codex / Claude Code / DSH / Hermes 的本地会话——自动收进一个
本地 SQLite，再以只读 MCP 提供给任何 agent 检索。每条结果能指回原话，权限按"这份数据会被送到
哪个模型"分级，撤回的内容不会因为重复导入而复活。

我做它是因为每个 agent 的记忆都是孤岛，而且都是"它觉得该记的"。我想要的是：全量原始历史在
自己手里，任何 agent 开口前能先查一下我。

> 本地优先 · 只读 MCP（stdio，可选 HTTP）· 不调远程模型 · Python ≥ 3.12 · SQLite FTS5 + sqlite-vec

## 它能做什么

- **采集**：`brain import-chatgpt` 导 ChatGPT 导出；`integrations/agent_sync` 每 15 分钟扫本机
  四家 agent 的会话文件增量入库（[docs/agent-sync.md](docs/agent-sync.md)）。
- **检索**：FTS5 精确匹配 + 中文二元切分，结果带原文偏移；`mode=hybrid` 叠加本地向量召回
  （bge-small-zh，RRF 融合），语义命中明确标为推断。
- **权限**：服务端绑定 profile；逐结果再授权；"无权限"和"不存在"返回一致；远程 profile 遮蔽
  凭据形态、拒绝敏感标签。撤回是版本化的，不是删行。
- **接入**：本机 agent 用 stdio MCP；远程用 `--transport http` + 反向代理（我用 Cloudflare Tunnel
  + Access，[docs/remote-access.md](docs/remote-access.md)）。
- **证明它能用**：评测运行器（Recall@10、引用可解析率、拒答/越权断言）、10 万条性能门、
  并发 soak、Docker 复现。失败的跑也留着，没挑数据。

## 数字

| 项目 | 结果 | 出处 |
|---|---|---|
| 测试 | 340 passed（宿主机与容器一致） | `pytest` / `docker compose run --rm test` |
| 性能门 | 10 万条语料预热后各查询 P95 ≤ 1s（实测 0.7–0.9s） | [`docs/evals/benchmark-100k.json`](docs/evals/benchmark-100k.json) |
| 导入优化 | FTS 写入由全表扫描改按 rowid 更新（O(n²)→O(log n)），10 万条 15 分钟级 → 约 20 秒 | 决策 32 |
| 并发梯度 | 只读并发 1/2/4/8：P95 = 471 / 507 / 701 / 1171 ms，0 错误 | [`docs/evals/soak-20260915-host.json`](docs/evals/soak-20260915-host.json) |
| 长稳 soak | 4 并发 × 30 分钟，17,814 次查询，P95 692 ms，RSS 增长 1.2%，8/8 门槛通过 | [`docs/evals/soak-20260915.md`](docs/evals/soak-20260915.md) |
| 读写同库 | 读侧 P95 抬升（漂移 4.41，未过门槛），如实披露 | [`soak-20260915-host-writer.json`](docs/evals/soak-20260915-host-writer.json) |
| 我自己的库 | 4 个来源，6.4 万条事件，2023-07 至今；香港一台免费 VM 上常驻副本，端到端同步 20–45 分钟 | — |

## 几个设计决定

- **存原话，不存提炼的"事实"。** Mem0 那类做法丢了原始对话，而"当时到底怎么说的"是我最常用的查询。
  提炼可以后做（规格里预留了），底座不能反过来。
- **FTS 只缩小候选，不决定结果。** 中文没有分词，二元切分后有超集保证，最后用 `instr` 逐条验证，
  结果集和暴力扫描完全一致（差分测试）。不安全的查询（单字、拉丁词内子串）回退参数化扫描，超限报错而不是假装没结果。
- **权限按投递边界切。** 同一个库，本机 agent 全开，给网页版模型的 profile 遮蔽密钥、屏蔽敏感——
  因为风险不在"谁在问"，在"答案会去哪"。
- **先写规格再写代码，分阶段交给 agent。** 一份技术规格是宪法，AGENTS.md 约束每个阶段只做当前范围、
  不删测试过关。我是项目经理，Codex 做架构和审查，DSH / Claude Code 写代码。

## 下次不会这么做的

- 早期两张 FTS 表并存（v1/v2 迁移遗留），库体积多了 170 MB，该早点清。
- 远程副本第一版走 Cloudflare 隧道 rsync 800 MB 的库，把 1 GB 内存的小机器堵死了；后来改走 Tailscale 直连
  并保留 rsync 基准文件，增量降到 35 秒。大文件同步不该和公网入口共用一条隧道。
- 查询页做早了。我自己从不看原话，都是让 agent 查了再分析；应该先接 agent，页面只留一个健康检查。

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
- `src/personal_brain/mcp_server/` 只读 MCP（stdio 默认；可选 HTTP transport，见"远程接入"）
- `src/personal_brain/evals/` 评测运行器、合成语料、性能基准、soak

> Python 包名暂为 `personal_brain`（项目早期名称），命令为 `brain`；仓库改名为 Hindsight 后包名将在后续版本迁移。

## 远程接入（可选）

在只绑回环的 HTTP transport 上加 Cloudflare Tunnel + Access，手机 / 网页版
claude.ai 也能只读查询；服务端仍不调用远程模型、不暴露任何写入端点。

```bash
uv sync --extra remote
export BRAIN_MCP_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
personal-brain-mcp --config config/config.yaml --transport http --port 8765
```

VM 部署一条命令：`deploy/vm/install.sh`。详见
[`docs/remote-access.md`](docs/remote-access.md)（架构图、威胁模型、token
轮换）与 [`deploy/vm/README.md`](deploy/vm/README.md)（Cloudflare Tunnel /
Access 配置步骤）。

## 设计记录

- [`docs/decisions-phase-a.md`](docs/decisions-phase-a.md) 数据层决策
- [`docs/phase-b-mcp.md`](docs/phase-b-mcp.md) 权限与只读 MCP
- [`docs/phase-c-evals.md`](docs/phase-c-evals.md) 评测与规模门槛
- [`docs/unified-retrieval.md`](docs/unified-retrieval.md) 统一检索
- [`docs/remote-access.md`](docs/remote-access.md) 远程接入（HTTP transport）

## 数据与隐私

真实对话、数据库、密钥一律放在仓库外的私有目录（`config.yaml` 指向），`.gitignore` 与 `.dockerignore` 双重排除；仓库与镜像中只有合成语料。
