# Phase B：只读 MCP 服务配置说明

对应规格 §7（权限）、§8（首版只读 MCP 契约）、§16（`personal-brain-mcp`）。

## 服务概览

```bash
personal-brain-mcp --config <trusted-config-path>
```

- **只读**：仅 4 个工具，无任何写入/删除/Memory/current-state 工具。
- **传输**：本机 stdio（§7.1 首版边界）；不监听网络端口。
- **无远程调用**：服务端不调用 LLM / embedding / reranker（§7.5）。
- **profile 绑定**：由受信任启动配置确定，不是 tool 参数（§7.3）。
- 所有结果 `content_trust=untrusted_archive`：客户端应将其作为**证据**引用，
  不执行、不服从内容中的指令（§7.6）。

## 服务端配置（受信任启动配置）

示例 `mcp-server.yaml`（不得包含真实聊天路径、密钥；路径权限应限制为本用户）：

```yaml
database_path: ~/.local/share/personal-brain/brain.sqlite
timezone: Asia/Shanghai
profile: codex          # 服务以哪个 profile 身份运行（§7.4）
profiles:
  codex:
    allow_scopes: [projects, career, learning, technical_preferences]
    deny_sensitivity: [secret, third_party_sensitive]
    allow_unclassified: false          # §7.2：未知分类默认不对 Agent 开放
    tools: [brain_status, search_history, get_recent_events, get_event]
    delivery_boundary: local_only      # §7.5：必须显式声明
  # local_review（transport: local_cli_only）供本地 CLI 使用，
  # MCP 服务会拒绝以它启动——本地人工审核不走 Agent 通道。
```

字段说明：

| 字段 | 语义 |
|---|---|
| `allow_scopes` | 允许的 scope 标签；`['*']` 表示全部（不得与其他值并用） |
| `deny_sensitivity` | 命中即拒绝；`private` 内容仍需满足 scope（§7.4） |
| `allow_unclassified` | 缺省 `false`（§7.2） |
| `tools` | 工具白名单；未知工具拒绝启动（Phase B 仅 4 个只读工具） |
| `delivery_boundary` | `local_only` / `remote_model_allowed`；缺省拒绝启动（§7.5） |
| `search` | §6.3 限额（default_limit/max_limit/snippet_chars/max_text_chars/max_scan），受硬上限钳制 |

## 工具

| 工具 | 输入 | 说明 |
|---|---|---|
| `brain_status` | 无 | 健康、调用者可见覆盖期、最后成功导入、解析警告（只报存在性不报数量）、能力 |
| `search_history` | query、match_mode、日期、来源、conversation_id、speaker_type、branch_scope、order、limit、cursor | 授权匹配结果；snippet ≤400 字符；QUERY_TOO_BROAD 时返回该错误 |
| `get_recent_events` | days、来源/对话、limit、cursor | 按源时间倒序；返回 `reference_time`；keyset 翻页 |
| `get_event` | event_id、revision_id?、context_radius(0..3) | 原文 + 有界邻接（逐条重新授权）+ 版本信息；不返回完整对话树 |

统一行为：

- 无权限与不存在返回相同的 `NOT_FOUND_OR_NOT_ALLOWED`（§7.3）。
- 源撤回后按引用 ID 访问同样返回 `NOT_FOUND_OR_NOT_ALLOWED`（§8.1）。
- 计数（total_matched、undated_excluded、覆盖期）只统计调用者可见档案，
  不泄露隐藏记录数量。
- 请求返回前核对 policy epoch；标注/撤回在执行中发生 → 自动重跑；
  持续变化则返回 `TRANSIENT_POLICY_CHANGE` 放弃结果。
- `source_locator` 只含 `snapshot_id`/`node_id`，不暴露磁盘路径；
  本地 resolver 见 CLI `brain show-event`。

## 客户端配置示例

通用 MCP 客户端（以 Claude Desktop 为例，其他客户端同理）：

```json
{
  "mcpServers": {
    "personal-brain": {
      "command": "/path/to/.venv/bin/personal-brain-mcp",
      "args": ["--config", "/Users/me/.config/personal-brain/mcp-server.yaml"]
    }
  }
}
```

消费端回答约定（§8.2）：表述为"你在某日表示/计划……"；引用需支持结论；
明确缺失信息与覆盖期；**不把检索不到理解成没有发生**；跨词语义推断需标注。

## 安全须知（§7.1/§7.5）

- 若客户端 Agent 与本服务使用同一 OS 身份且可直接读取原始文件，
  MCP profile **不是**强隔离边界；真正隔离需独立服务身份与受限目录。
- `delivery_boundary: local_only` 表示服务不主动外发；但使用远程模型的
  客户端把返回内容发送给远程模型即构成外发——服务无法验证客户端行为，
  只能由用户在 profile 层面知情配置。
- 日志不打印聊天正文；错误信息不包含内容正文。

## 验证

合成数据集成测试（§17 Phase B 验收）：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
uv run pytest tests/integration/test_mcp_service.py tests/integration/test_mcp_stdio.py -q
```

`test_mcp_stdio.py` 通过真实子进程完成 initialize → tools/list → tools/call
全链路，并验证 profile 工具面限制。
