# Phase C 评测与性能门槛（§9）

## 组件

| 组件 | 路径 | 说明 |
|---|---|---|
| 套件模式 | `src/personal_brain/evals/suite.py` | §9.1 全字段：query/profile/时间分支条件/可接受证据集/必须区分语义/禁止结论/允许拒答/预期访问结果；**不是 `expected_contains` 关键词检查** |
| 运行器 | `src/personal_brain/evals/runner.py` | Recall@10、证据覆盖率、citation 100% 解析、禁止访问断言、拒答/回答率（防全拒答作弊）、失败分类（recall_fail/citation_unresolved/forbidden_access/attribution_mismatch/unexpected_empty） |
| 引用解析 | `src/personal_brain/evals/citation.py` | `pb:{revision_id}` → 本地解析到正确版本；不可解析/不可访问统一 None（防探测） |
| 合成生成器 | `src/personal_brain/evals/synthgen.py` | 种子确定；每条消息含唯一编号 token；生成物必须通过真实导入器（契约测试） |
| 基准 | `src/personal_brain/evals/benchmark.py` | 预热 P95（产品服务层路径含 epoch 核对）；硬件/SQLite/Python 版本入档；短词扫描单独披露 |

## 命令

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"

# 运行评测套件（合成开发集随 Git 分发）
uv run brain --db <db> --json eval --suite evals/suites/dev-synthetic.json

# 合成数据基准（默认 500×200=10 万条；报告含环境与行数）
uv run brain --db /tmp/bench.sqlite --archive-dir /tmp/archives --json bench \
  --conversations 500 --messages 200 --repeats 20 --label \
  --out docs/evals/benchmark-100k.json
```

## 门槛映射（§9.2）

| 门槛 | 工具 | 状态 |
|---|---|---|
| 确定性测试全过 | `uv run pytest tests/` | 237 passed |
| Recall@10 ≥ 90% | 运行器 | 合成集 100%（真实集待标注） |
| citation 解析 100% | runner + citation.py | 合成集 100% |
| 证据支持人工复核 | 报告 human_review 段 | 输出归属表 + 必须区分语义（人工步骤） |
| 对抗样例：归属/越权 | 策略卷 `dev-synthetic-policy.json` | 通过（assistant 归属断言 + secret/unclassified 不可见） |
| 必须拒答全过 + 回答率 | 运行器 refusal/answer_rate | 全过 / 100% |
| 10 万条预热 P95 ≤ 1s | `brain bench` | 见 `docs/evals/benchmark-100k.json` |
| 短词扫描单独披露 | bench `disclosed_separately` | 见报告 |

## 真实标注问题集（用户制品，§9.1）

- 保存于 **Git 外**私有评测目录：`evals/private/`（已 .gitignore）。
- 建议配比（初始）：关键词/短词/混合/精确时间 10、多时间点观点与分支版本 6、信息不足/计划完成/引用假设 6、权限/第三方/删除/注入 8。
- 开发集与保留集**分开**，避免调参记住答案。
- 套件 JSON 字段见 `evals/suites/dev-synthetic.json` 的现成示例；真实集用同一 schema，`brain eval --suite evals/private/<name>.json` 直接运行。
- 人工复核（门槛 4）用 `--json` 输出的 `human_review` 段逐条核对证据支持关系。

## 已知边界

- 评测为检索级（证据列表），不含答案生成与 token 成本——token 成本属 §9.3 Memory 对照（Phase D+，Memory 未实现）。
- 人工复核无法自动化，报告只输出材料。
- **短词回退扫描在 10 万条规模下超过 §6.3 `max_scan` 资源上限时诚实拒绝（QUERY_TOO_BROAD）而非全表扫描**——基准记录该拒绝并单独披露；这不是缺陷而是资源边界保护。缩短时间范围或限定来源可缩小扫描集。
