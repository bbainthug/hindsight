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
| 24 | CLI 标签命令显式覆盖标签（不做继承合并），scope 标签输出强制附混合话题警示（§16）；`label-source` 覆盖来源下全部事件全部可用版本，`label-event` 提供混合话题的细粒度出口 | §7.2 显式人工标注优先；§16 要求警示 | — |
| 25 | mypy 已配置并全绿（§3.1「类型检查」落地），严格度 pragmatic（check_untyped_defs, no_implicit_optional） | 消除"类型检查器未配置"已知限制 | — |

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
