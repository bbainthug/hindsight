# Phase A（数据层切片）实现决策记录

范围：v0.2 规格中 Phase A 的数据模型、ChatGPT 导入、事件版本、分支、幂等（批次 1），
以及**中文检索基线（§6.1–6.3）、时间/路径过滤、基础标签（§7.2）、源撤回（§14.1/14.2）、
备份与恢复校验（§14.3）、CLI（§16 Phase A 适用命令）**（批次 2）。
权限执行/MCP（§7–8 Phase B）、eval（§19 Phase C）、Memory 提炼、purge/archive 仍未实现。

## 文档未定细节的选择（最简、可逆、可测试）

| # | 决策 | 依据/影响 | 可逆性 |
|---|---|---|---|
| 1 | `event_id = "{source_kind}:{account_namespace}:{source_message_id}"` 直接拼接 | §4.2「由三者确定」的直读；可读、可调试 | 改派生规则 = 迁移重建 L1，可从 L0 重建 |
| 2 | `revision_id = "{event_id}:r{revision_hash 前 20 位}"` 确定性派生 | 幂等天然成立；同库重建（§3.2）产生相同 ID | 同上 |
| 3 | `revision_hash` 覆盖 content_type/raw_text/speaker/时间四元组/source_updated_at/附件引用；**排除** recorded_at、归档路径、locator、search_text | §4.2 明确排除导入时间与路径；search_text 归一化版本升级不应改写内容身份 | 哈希算法版本变化时可引入 hash_version 列 |
| 4 | 版本排序键 = `COALESCE(update_time, create_time)` 的 UTC 归一值；双方皆无时间则"先后不明" | §5.3「版本先后不明保留冲突」的可操作化 | `resolve_current()` 独立函数，可整体替换 |
| 5 | 冲突状态：`current_revision_id=NULL` + `resolution=unresolved`，双方版本保留；严格更新到达即恢复 resolved | §4.2 current 可空 + §5.3 保留冲突 | 规则集中、可改 |
| 6 | ChatGPT 无 conversation_id 时派生 `conv-fb-` ID（title+create_time+最小节点 ID） | 真实导出均含 conversation_id；此为容错兜底 | 已知限制，见下 |
| 7 | `source_locator = 成员路径#/conversations/{id}/mapping/{node_id}`，用稳定节点 ID 不用数组下标 | §4.2 允许「JSON pointer / 稳定节点 ID」；下标跨导出不稳定 | 格式可换 |
| 8 | 快照/来源 ID 为 uuid5 确定性派生；同（来源, sha256）必同快照 | 重复导入幂等在 ID 层即成立，DB 唯一约束兜底 | — |
| 9 | ZIP 成员不落盘，按上限流式读取解析；归档目录只保存**原始归档字节** | L0 权威=原始字节；避免解压面 | — |
| 10 | 目录输入的 `archive_sha256` = 按成员路径排序的 名称+内容哈希 规范摘要 | 相同内容目录与重复导入得到同摘要 | — |
| 11 | fallback 消息身份 = 来源+对话+结构位置(父节点|兄弟序)+说话者+时间原值+内容摘要 | §4.2 要求组合定位，防止不同时间相同发言合并 | — |
| 12 | 每个内容版本初始化一条 `revision_policy_state`（available/unclassified/policy v0）；标签变更留待标签阶段写入新版本行 | §4.2 标签物理隔离在版本化表 | 表结构已就绪 |
| 13 | 归一化 UTC 时间统一为**固定 6 位微秒**（`…T08:00:00.000000Z`），Python 与 SQL 排序键同构 | 字典序==时间序，消除整秒 `Z`(0x5A) > 亚秒 `.`(0x2E) 的排序反超（验收缺陷 1）；亚微秒差异截断为相等→保守判冲突 | 格式属 L1 归一化层，可从 L0 重建 |
| 14 | 已有版本全为未知时间 + 新版本带已知时间 → 统一判"先后不明"（unresolved） | 与"新版本无时间"对称，规格字面语义；保守禁止后续自动激活前提 | `resolve_current` 集中可改 |
| 15 | 父链悬空（父节点在 mapping 外）→ `active_path_unknown`，不把孤立节点当根 | §4.3「无法确定时标注并跳过」；验收建议采纳 | — |
| 16 | `source_assets` 在发布时写入成员级清单（路径/sha256/大小/类别），附件到事件的映射（event_id）留待附件引用解析 | §5.2 步骤 1 manifest；验收建议采纳 | event_id 列已预留 |
| 17 | `search_text` 归一化升级为**逐字符 NFKC+casefold 并保留偏移映射**（norm 版本 `nfkc-casefold-v2-charmap`）；FTS 索引、候选验证、snippet 三方共用同一归一化实现 | §6.2「snippet 必须能映射回原文」禁止用归一化索引切原文；偏移映射（含 ﬁ→fi 等展开情形多对一指回原字符）使三方自洽；纯 ASCII/常用中文文本 v1→v2 结果不变，版本号显式区分以便未来重建 | 归一化属派生层，可从 L0 重建 |
| 18 | selected 路径过滤 = 每**对话**取最近一次发布快照（插入序 rowid 最大者）的所选路径；`active_path_unknown` 对话不混入 selected 结果，改为显式报告 `path_unknown_conversations` | §4.3「selected 表示一次确定性切片」；对话级快照是分支一致性的自然单位 | 规则集中在 `_selected_path_events`，可整体替换 |
| 19 | 检索排序：chronological/reverse_chronological 未知时间一律排最后（不参与时间比较）；同时间戳内 revision_id 升序（保证全序确定）；relevance = 查询词出现次数和（同前 §6.1 tie-break 时间升序） | 排序必须稳定确定（§6.3 cursor 正确性前提）；未知时间"排最后"是最不惊讶的选择 | — |
| 20 | FTS 仅作候选缩小：查询词的 CJK 段全部 ≥2 字才走 FTS（超集证明：词在文本中连续 ⇒ 词的每个二元组必在索引 token 中）；单汉字、拉丁词内子串、标点结尾词回退参数化扫描；两路径差分测试强制一致 | §6.2「候选优化不得改变匹配集合」；FTS unicode61 对拉丁自带大小写折叠，索引保留原文大小写 | 索引可重建（rebuild_fts） |
| 21 | 撤回实现为版本化 policy_state 变更（available→withdrawn）+ `sources.status='withdrawn'`；检索/最近列表在 SQL 层强制 `availability='available'` 过滤；FTS 行保留但永不作为权威；purge/archive 不实现 | §14.1 撤回=从可服务数据移除但保留证据链；存储层天然满足 §14.3「恢复时先应用撤回登记」——撤回登记就在库内，恢复出的备份同样排除 | — |
| 22 | 备份 = SQLite 在线 backup API 一致性快照（正确处理 WAL，§3.1 禁止运行时裸拷主文件）+ Raw 归档目录副本 + manifest（sha256/计数）；`restore-check` 只做校验（integrity/外键/迁移版本/计数/manifest 摘要），不做恢复向导 | §14.3 范围内最小可验证实现；完整恢复流程属后续阶段 | — |
| 23 | CLI `show-event` 对已撤回事件**仍可显式查看**并标注 `availability: withdrawn`；检索/最近列表一律过滤。本地 CLI 定位为受信本地边界（§7.4 local_review 性质），MCP 端策略执行属 Phase B | §14.1"普通查询不可返回"中的"普通查询"=检索/列表/最近；按 ID 显式查看是人工核查通道，隐藏标记反而违背可追溯性 | Phase B 引入访问档案后收紧 |
| 24 | CLI 标签命令显式覆盖标签（不做继承合并），scope 标签输出强制附混合话题警示（§16）；`label-source` 覆盖来源下全部事件全部**当前可用**版本，`label-event` 提供混合话题的细粒度出口；撤回状态下的来源/事件**拒绝标注**，来源内个别撤回版本跳过并报告 `skipped_withdrawn` | §7.2 显式人工标注优先；§16 要求警示；§14.1 撤回不可被标注操作隐式解除（验收缺陷 A） | — |
| 25 | mypy 已配置并全绿（§3.1「类型检查」落地），严格度 pragmatic（check_untyped_defs, no_implicit_optional） | 消除"类型检查器未配置"已知限制 | — |
| 26 | **撤回状态延续**：新内容版本入库时，policy_state 初始 availability 继承事件/来源的 withdrawn 状态（`_event_or_source_withdrawn`）；检索基线 SQL 另加 `sources.status != 'withdrawn'` 防御层 | §14.2.3「显式忘记此内容应覆盖重复来源，不能靠另一份导出保活」（验收缺陷 B）；新版本照常入库保留证据链，但不可检索；防御层堵住 policy 行状态漂移时的旁路 | 恢复可见性须显式流程（本批不提供），继承规则集中一处可改 |
| 27 | **策略纪元**（迁移 3）：`policy_epoch` 单行计数器，标签/撤回变更在同一事务内递增；MCP 工具返回前核对，执行中变化 → 整体重跑（≤3 次），持续变化抛 `TRANSIENT_POLICY_CHANGE` 放弃结果 | §7.3「请求在返回前核对 policy epoch」；无缓存的 Phase B 下语义为"结果新鲜度保证"，缓存引入后键必须含 epoch | — |
| 28 | **profile 绑定与校验**：受信任启动配置绑定单一 profile；`local_cli_only` profile 拒绝作为 MCP 身份；`delivery_boundary` 缺失拒绝启动；`allow_unclassified` 缺省 False；工具白名单之外的工具在 tools/list 隐藏且调用返回 NOT_FOUND_OR_NOT_ALLOWED | §7.3/§7.4/§7.5；配置错误宁可拒绝启动也不猜测语义 | — |
| 29 | **MCP 计数不泄露**：total_matched/undated_excluded/brain_status 覆盖期与来源列表只在授权过滤后的集合上统计；不支持结构等解析警告只报存在性不报数量/位置 | §7.3「邻接、标题、计数也必须经过权限检查」「不泄露隐藏记录数量」 | — |
| 30 | **get_event 授权与上下文**：按事件逐次重新授权（无权限与不存在同为 NOT_FOUND_OR_NOT_ALLOWED）；context_radius 沿所选路径取邻接并逐条授权，未授权邻接静默略去（不计数量）；speaker_id 仅在事件已授权时返回；检索结果只给 snippet 不给正文（正文走 get_event）；定位符只含 snapshot_id/node_id | §7.3/§8.1 | — |
| 31 | **MCP SDK 与传输**：官方 python-sdk v2（低层 Server + on_list_tools/on_call_tool），stdio 传输；服务端无网络监听、无远程调用 | §7.1 首版本机 stdio；§7.5 无服务端 LLM | — |
| 32 | **FTS 行 rowid 化（规模门槛修复）**：FTS5 `DELETE FROM ... WHERE revision_id=?` 按非 rowid 列删除会全扫索引 → 导入 O(n²)（10 万条 15 分钟+）；迁移 4 改为 `rowid = sha256(revision_id) 前 8 字节 & 2^63-1` 作主键，`INSERT OR REPLACE` 按 rowid（O(log n)）；实测 10 万条导入 19.7s | §9.2 门槛 7 实测触发；L3 可从 L1 重建的性质保证迁移可逆（§3.2） | rowid 冲突概率 ~3×10⁻¹⁰ 可忽略；`fts_rowid` 注册于连接工厂，迁移与运行期同一实现 |
| 33 | **套件前置条件声明**：suite `requires`（来源数/事件数/已标注与未标注下界/必需标签），runner 跑用例前检查，不满足 fail-fast 并逐条说明 | 验收报告建议 2：裸库跑策略卷/双来源跑语义卷的失败形态与真实越权漏洞一模一样，红色失败脱敏比浪费时间更危险 | 检查只读元数据计数；requires 键白名单，未知键拒绝加载 |
| 34 | **基准语料物化覆盖**：生成器含连续 "AI工程选型" 与会话首条 "你好，世界"，recent 窗口锚定库内最大消息时间——每条常用查询命中 ≥10，snippet/偏移映射/citation/文本预算路径全覆盖；另将 search 池查询改为**正文懒加载**（池 SELECT 不含 raw_text，页确定后按 revision_id 补取；recent 路径仅 limit+1 行故保留正文列） | 验收报告建议 4：命中 0 的查询只测扫描路径，门槛数字偏乐观；正文懒加载降低每次查询 I/O 与对象物化成本，加大 1s 门槛余量 | 差分测试保护匹配集不变；页内正文补取由 snippet 测试覆盖 |
| 35 | **非 JSON 成员只留 manifest**：官方导出含 GB 级媒体 .dat/chat.html，导入器原样读入内存且记 MEMBER_NOT_JSON 伪错误；改为非 JSON 成员仅登记 source_assets（.dat→attachment），不读入、不算解析错误 | 真实导出核对（§5「核对实际导出样本结构」）；1.17GB 导出内存峰值从全量降到仅 JSON ~50MB | 媒体内容不入库仅留溯源行；后续媒体处理可扩展 kind |
| 36 | **发布与 mapping 键序解耦**：conversation_node_edges 的 FK 要求父节点行已存在，真实导出 mapping 子先于父 → FK 炸；改两遍写入（先全部节点行，后写边） | 真实 1540 对话导入触发；合成 fixture root-first 掩盖了顺序假设 | 发布语义不变：图=节点+边，顺序无关 |
| 37 | **容器化复现边界**：镜像 `python:3.12-slim`；依赖按 `uv.lock` 精确安装（`uv sync --frozen`，uv 版本与宿主机一致）；构建上下文只含 `pyproject.toml / uv.lock / src / tests / evals/suites / config / docs`——`evals/private`、`*.sqlite`、真实归档与密钥由 `.dockerignore` + 显式 COPY 列表**双重排除**；非 root（uid 1000）运行；tzdata 供 zoneinfo；`ENTRYPOINT ["brain"]`；compose 三 service（test/bench/soak）共用同一镜像，bench/soak 的库与归档显式放容器 `/tmp/pb`，报告写挂载的 `docs/evals` | 验收要求"每个数字都能用一条命令复现"且私有数据绝不进镜像；`.dockerignore` 与 `.gitignore` 相互独立，即使被 git 追踪也挡在构建上下文外 | 镜像配方纯声明式，可整体替换 |
| 38 | **soak 门槛口径**（`evals/soak.py` 顶部常量，改动即改口径）：ladder=并发 4 预热后 P95 ≤ 1000ms、并发 8 ≤ 2000ms（前提：该档错误数=0）；soak=漂移比（后 10% / 前 10% 查询 P95；样本对齐各 worker 首样本时刻后按全局时间排序）≤ 1.3、worker `ru_maxrss` 末/首 ≤ 1.2（高水位口径，**基线取预热 30s 后首个样本**——实测 worker RSS 在首 5-10s 完成解释器/页缓存预热后全程平稳，以冷启动首样本为基线会把预热误判为 4.5× 增长；冷启动比在报告 `rss_growth_cold` 单独披露）、错误数 0；`PRAGMA integrity_check` = ok 且行数（含 FTS）前后一致。单独披露查询的 `QUERY_TOO_BROAD` 是 §6.3 资源边界**预期拒绝**：计入 `expected_rejections` 不计入错误（与 bench 门槛把 `disclosed_separately` 排除在 gate 外同口径）；未运行档门槛 `passed=null` 不参与判定；`--writer` 模式下 counts 门槛改断言 sources/events 不变 + 三张版本侧表增量一致 | 门槛必须可解释、可复现且诚实：预期拒绝混入错误数会把资源边界保护误报为不稳定；未运行的档位不能假装达标 | 门槛常量集中一处；报告 `gates` 逐项含 expect/value/passed/note 可审计 |

## 已知限制（本切片内不做、不假装已做）

- 权限执行、MCP 未实现（Phase B）；CLI 检索默认不做标签强制过滤（`--scope/--deny` 显式启用，见决策 23）。
- purge/archive（§14.1 后半）未实现；`sources.status` 枚举无 archived 值。
- 撤回不可逆（不提供恢复可见性命令）；失败恢复向导未做，`restore-check` 仅校验。
- 备份调度（每日快照、滚动保留 7 份）未实现，属部署配置与调度事项；本批提供手工 `brain backup`。
- 合成数据恢复演练仅到 `restore-check`（校验级），完整"恢复→开放检索"演练待恢复向导实现。
- 内存中 `json.loads` 解析对话成员（上限内可接受）；超大导出的流式解析留待实测后再做。
- `supersedes_snapshot_id` 字段就绪但导入器从不自动设置（§4.1「不静默覆盖」）。
- 未做端到端答案可靠性验证（规格 §19 明确禁止此类宣称）。
- 重复导入快路径中暂存失败不留 failed 作业记录（仅影响可观测性，不影响数据正确性）。
- 成员内容被读两遍（members() 摘要 + open_member 解析），仅性能损耗；上限内可接受。
- 与全未知时间版本并存的混合排序（部分版本有时间、部分没有）仅比较可证明有序对，残余歧义保守留冲突。
- FTS 候选缩小只在"授权过滤后行集"内做；`max_scan` 上限约束回退路径的行集大小，不约束 FTS 命中数（决策 20 的推论）。
- selected 路径的"最近快照"以插入序（rowid）近似发布序；同事务并发多快照不存在（单进程 CLI）。

## 验收修复记录（2026-09-06 验收报告）

- **缺陷 1（已修复）**：亚秒时间戳版本排序反超——`_format` 改固定微秒等宽格式（决策 13），Python/SQL 两侧排序键同构；新增亚秒乱序、等毫秒冲突回归测试。
- **缺陷 2（已修复）**：目录符号链接逃逸被字符串前缀匹配放行——改 `Path.is_relative_to` 真包含判断；新增同前缀兄弟目录逃逸回归测试与根内链接放行正例。
- 同批收编非阻塞建议：决策 14（未知时间对称化）、15（悬空父链）、16（source_assets manifest）、unsupported 口径统一（含 UNSUPPORTED_NODE/CONVERSATION）。

## 批次 2 验收修复记录（2026-09-09 验收报告：撤回生命周期缺陷 A/B）

- **缺陷 A（已修复）**：标注操作复活已撤回内容——`_close_and_relabel` 改为仅重标当前 `availability='available'` 的状态行；撤回状态的来源/事件整体**拒绝标注**（ValueError）；来源内个别撤回版本跳过并以 `LabelSummary.skipped_withdrawn` 报告。回归：撤回后标注不复活、事件级撤回的版本不被来源级标注复活、全撤回事件标注被拒。
- **缺陷 B（已修复）**：撤回可被后续导入绕过——`_insert_revision` 初始化 policy_state 时经 `_event_or_source_withdrawn` 继承事件/来源的 withdrawn 状态（§14.2.3）；新版本照常入库（证据链保留、current 可指向它）但不可检索。检索基线 SQL 加 `sources.status != 'withdrawn'` 防御层（状态漂移兜底）。回归：撤回来源/事件后导入同事件身份的编辑导出，新版本入库且 availability=withdrawn、检索 0 命中、current 指向新版本仍排除；手工改 policy 行回 available 仍被 sources.status 过滤。
- 同批收编非阻塞建议：`undated_excluded` 口径已在结果说明标注；`recent_events` 翻页、SQL 侧预过滤留待 MCP/Phase C。

## 批次 2 交付记录（检索基线 + 时间/路径过滤 + 标签 + 撤回 + 备份 + CLI）

- `history/normalization.py`：逐字符 NFKC+casefold + 偏移映射（决策 17）；`identity.search_text` 生成切换到 v2。
- `history/schema.py` 迁移 2：FTS5 `event_revisions_fts(bigram_text, revision_id, unicode61)` + 2 个索引；发布事务内同步写 FTS 行。
- `retrieval/bigram.py`：CJK 连续段 ≥2 → 重叠二元组索引文本；`plan_term` 判定查询词可否安全预筛选（超集证明，决策 20）。
- `retrieval/fts.py`：索引写入/重建/参数化 MATCH。
- `retrieval/search.py`：`search_history`（literal/all_terms、授权过滤、时间 [from,to)、selected/all 路径、三种排序、keyset 游标、limit/truncated、snippet 原文偏移还原、QUERY_TOO_BROAD、undated_excluded 报告）+ `recent_events` 专用接口；`use_fts=False` 基线路径供差分测试。
- `policy/labels.py` / `policy/withdraw.py`：版本化策略状态变更，单事务（决策 21/24）。
- `backup.py`：backup API 快照 + Raw 归档副本 + manifest；`restore_check`（决策 22）。
- `history/status.py`：§16 status 汇总（含 index_lag）。
- `cli.py`：`brain` 入口（pyproject scripts），全命令 --json；测试配置样例 `config/config.example.yaml`（不含真实路径）。
- 差分验证：15 条查询语义电池 × FTS/基线两路径结果集一致（`test_retrieval.py::TestFtsBaselineDifferential`）。
- 已知修正：批次 1 的 `search_text_norm_version` 断言 v1→v2（归一化升级的正确反映，非放宽）。

## Phase B 交付记录（权限与只读 MCP）

- `history/schema.py` 迁移 3：`policy_epoch` 单行计数器（决策 27）；标签/撤回事务内递增；顺带修复 `withdraw_source` 幂等早退导致事务悬挂的隐患。
- `policy/profiles.py`：受信任启动配置解析与校验（决策 28）——`local_cli_only` 拒绝作为 MCP 身份、`delivery_boundary` 必填、未知工具拒绝、`allow_unclassified` 缺省 False。
- `mcp_server/service.py`：4 个只读工具统一服务层——profile 强制过滤（请求只可收窄）、计数不泄露（决策 29）、NOT_FOUND_OR_NOT_ALLOWED 不可区分、context_radius 逐条授权（决策 30）、epoch 返回前核对 + 重跑。
- `mcp_server/server.py`：官方 python-sdk v2 低层 Server，stdio 传输，工具白名单生效于 tools/list 与 tools/call（决策 31）；`personal-brain-mcp` 入口。
- `retrieval/search.py`：`recent_events` 升级 keyset 游标 + 接受调用方过滤（收掉批次 2 非阻塞项；MCP `get_recent_events` 契约要求）。
- 客户端配置说明与安全须知：`docs/phase-b-mcp.md`。
- 测试：profile 校验 7 项、服务层契约 18 项（含计数不泄露、越权按 ID、邻接过滤、epoch 重跑/放弃、撤回 resolver）、stdio 子进程集成 5 项（initialize → tools/list → tools/call 全链路 + 工具面限制）。
- 已知限制：无服务端缓存（epoch 机制已就位）；`remote_model_allowed` 仅作为声明记录，Phase B 无远程 Provider 可配置。

## Phase C 交付记录（真实样本评测与性能门槛）

- `evals/suite.py`：§9.1 全字段套件模式（query/profile/时间分支条件/可接受证据 revision 集/必须区分语义/禁止结论/允许拒答/预期访问结果/归属断言）——**不是 expected_contains 关键词检查**；结构非法拒绝执行。
- **验收修复（建议 2/4）**：套件 `requires` 前置条件 fail-fast（决策 33）；基准语料每条常用查询命中 ≥10 覆盖物化路径（决策 34）；`eval --json` 改 asdict 真 JSON；ruff 4 错修复。
- `evals/runner.py`：Recall@10、多证据覆盖、citation 100% 解析、禁止访问断言、拒答全过 + 正常回答率（防全拒答作弊）、失败分类（recall_fail/citation_unresolved/forbidden_access/attribution_mismatch/unexpected_empty）、人工复核输出段（归属表 + 区分语义）。
- `evals/citation.py`：`pb:{revision_id}` 解析到正确版本（revision_id 自含 event_id）；不可解析/不可访问统一 None（防探测）。
- `evals/synthgen.py`：种子确定合成生成器，消息 ID 全源唯一（事件 ID 含 message_id）；生成物必须通过真实导入器（契约测试）。
- `evals/benchmark.py`：产品服务层路径预热 P95（含 epoch 核对）；硬件/SQLite/Python/行数入档；短词扫描单独披露；QUERY_TOO_BROAD 诚实拒绝被记录而非整体崩溃。
- `history/schema.py` 迁移 4 + `retrieval/fts.py`：FTS 行 rowid 化（决策 32）——导入从 O(n²)（10 万条 15 分钟+）修复到 O(n log n)（19.7s）。
- CLI：`brain eval --suite <path>`（§16 落地）、`brain bench`（规模/重复/标注/报告）。
- 合成开发集：`evals/suites/dev-synthetic.json`（语义卷，本地审核 profile）25 题 + `dev-synthetic-policy.json`（策略卷，agent profile）6 题；期望全部机械推导，随 Git 分发（§9.1 公开仓库只放合成 fixture）。
- 真实标注问题集：用户制品，保存于 Git 外 `evals/private/`（已 .gitignore），开发/保留集分开，同 schema 直接 `brain eval`。
- 测试：237 passed（新增 19：套件校验 6 + 引用解析 3 + 双开发套件 4 + 生成器 3 + 基准冒烟 2）。
- 已知边界：评测为检索级（证据列表），token 成本与答案正确性属 §9.3 Memory 对照（Memory 未实现）；人工复核（§9.2 门槛 4）不可自动化，报告只输出材料。
