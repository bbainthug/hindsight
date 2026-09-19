"""Federated, bounded retrieval. Neither source is copied into the other."""

from __future__ import annotations

from personal_brain.mcp_server.service import ToolError, tool_search_history
from personal_brain.policy.profiles import McpServerConfig
from personal_brain.retrieval.search import SearchLimits
from personal_brain.retrieval.semantic_index import SemanticConfig
from personal_brain.retrieval.vault import search_vault


def search_brain(
    conn,
    config: McpServerConfig,
    args: dict,
    limits: SearchLimits,
    *,
    semantic_config: SemanticConfig | None = None,
) -> dict:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ToolError("INVALID_PARAMS", "query 必须为 1..1000 字符")
    source = args.get("source", "all")
    if source not in ("all", "history", "vault"):
        raise ToolError("INVALID_PARAMS", "source 必须为 all/history/vault")
    count = args.get("limit", limits.default_limit)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ToolError("INVALID_PARAMS", "limit 必须为正整数")
    count = min(count, limits.max_limit, 50)
    # D-1：mode 透传（hybrid 只作用于聊天历史部分）；缺省走 profile 语义默认
    mode = args.get("mode")
    if mode is not None and mode not in ("exact", "hybrid"):
        raise ToolError("INVALID_PARAMS", "mode 必须为 exact|hybrid")
    allowed = set(config.profile.tools)
    buckets: dict[str, list[dict]] = {}
    status: dict[str, dict] = {}
    warnings: list[str] = []
    for name in ("vault", "history"):
        if source not in ("all", name):
            continue
        tool = "search_vault" if name == "vault" else "search_history"
        if tool not in allowed or (name == "vault" and config.vault is None):
            status[name] = {"status": "not_configured_or_not_allowed"}
            continue
        try:
            if name == "vault":
                assert config.vault is not None
                payload = search_vault(
                    config.vault, query, limit=count, max_text_chars=limits.max_text_chars
                )
            else:
                payload = tool_search_history(
                    conn, config.profile,
                    {
                        **args,
                        "limit": count,
                        "order": args.get("order", "reverse_chronological"),
                        "mode": mode or config.profile.semantic_default,
                    },
                    limits, config.timezone,
                    semantic_config=semantic_config or config.semantic,
                )
            if "error" in payload:
                status[name] = {"status": "error", "error": payload["error"]}
                continue
            buckets[name] = [{**hit, "source_kind": name} for hit in payload["results"]]
            status[name] = {
                "status": "ok", "truncated": payload.get("truncated", False),
                "coverage": payload.get("coverage"),
            }
            warnings.extend(payload.get("warnings", []))
        except ToolError as exc:
            status[name] = {"status": "error", "error": exc.code}
        except (ValueError, OSError):
            status[name] = {"status": "error", "error": "SOURCE_UNAVAILABLE"}
    if not buckets:
        return {
            "content_trust": "untrusted_archive", "results": [], "sources": status,
            "error": "NO_AVAILABLE_SOURCE", "warnings": warnings, "truncated": False,
        }
    # Round-robin prevents a large chat archive from crowding all Vault hits out.
    merged = []
    for i in range(count):
        for hits in buckets.values():
            if i < len(hits):
                merged.append(hits[i])
    results = []
    budget = min(limits.max_text_chars, 8000)
    for hit in merged[:count]:
        snippet = str(hit.get("snippet", ""))
        if budget <= 0:
            break
        clipped = snippet[:budget]
        results.append({
            **hit, "snippet": clipped, "snippet_truncated": len(clipped) < len(snippet),
        })
        budget -= len(clipped)
    return {
        "schema_version": "0.3", "content_trust": "untrusted_archive",
        "results": results, "sources": status,
        "truncated": any(x["snippet_truncated"] for x in results)
        or len(results) < len(merged) or any(
            x.get("truncated") for x in status.values()
        ),
        "warnings": list(dict.fromkeys(warnings)),
        "retrieval_note": (
            "词面检索为主；mode=hybrid 时叠加本地语义召回（语义命中为推断）。"
            "必要时换用同义词。按来源交替展示，不代表事实可靠性排名。"
            "Vault 修改时间不是事实时间；历史 is_current 仅表示消息版本。"
            "无结果不等于从未发生。用 get_event/get_vault_note 查看原文并核对时间。"
        ),
    }
