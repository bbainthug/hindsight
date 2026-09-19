"""评测套件模式（§9.1）：每个用例记录完整判定依据，不只检查关键词。

字段语义（§9.1 要求逐项）：
- ``query``/``match_mode``/``branch_scope``/``time``：检索条件。
- ``profile``：执行 profile（受信任配置语义；评测在本地进程内加载）。
- ``acceptable_event_ids``：可接受证据的事件 ID 集合（Recall@10 分母来源）。
- ``must_distinguish``：必须区分的语义（人工复核用文本 + 机器可查的
  ``forbidden_event_ids``）。
- ``forbidden_event_ids``：禁止出现在结果中的事件（越权/排除断言）。
- ``allowed_refusal``：允许拒答（预期 0 命中且不算失败）。
- ``expected_access``：``allow`` | ``deny``（deny 时预期 NOT_FOUND_OR_NOT_ALLOWED
  或空结果）。
- ``attribution``：证据的说话者归属（assistant-only 证据不得当作用户事实；
  运行器输出归属表供人工复核 §9.2 门槛 4）。

公开仓库只放合成 fixture；真实标注保存在 Git 外私有评测目录
（建议 ``evals/private/``，已加入 .gitignore）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from personal_brain.policy.profiles import AccessProfile

CASE_TYPES = ("keyword", "multi_temporal", "insufficiency", "adversarial", "semantic")
# semantic 用例只在 mode=hybrid 下评分（exact 跑时跳过，不计入门槛）


@dataclass(frozen=True)
class TimeCondition:
    date_from: str | None = None  # 自然日期 YYYY-MM-DD
    date_to: str | None = None
    timezone: str = "UTC"


@dataclass(frozen=True)
class EvalCase:
    id: str
    type: str  # keyword | multi_temporal | insufficiency | adversarial
    mode: str = "search"  # search | get_event
    query: str | None = None
    match_mode: str = "literal"
    order: str = "chronological"
    branch_scope: str = "all"
    time: TimeCondition | None = None
    event_id: str | None = None  # get_event 用例
    context_radius: int = 0
    acceptable_event_ids: tuple[str, ...] = ()
    forbidden_event_ids: tuple[str, ...] = ()
    must_distinguish: str = ""
    forbidden_conclusions: tuple[str, ...] = ()
    allowed_refusal: bool = False
    expected_access: str = "allow"  # allow | deny
    attribution: dict[str, tuple[str, ...]] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class EvalSuite:
    name: str
    schema_version: str
    profile: AccessProfile
    cases: tuple[EvalCase, ...]
    source_path: str = ""
    requires: dict = field(default_factory=dict)


class SuiteValidationError(ValueError):
    """套件结构非法：拒绝执行而非猜测语义。"""


# 前置条件键（验收建议 2：套件环境不匹配必须 fail-fast，不能伪装成检索失败）
_REQUIRES_INT_KEYS = (
    "min_sources",
    "max_sources",
    "min_events",
    "min_labeled_events",
    "min_unlabeled_events",
)
_REQUIRES_LABEL_KEYS = ("required_scope_labels", "required_sensitivity_labels")


def _parse_requires(raw: object) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SuiteValidationError("requires 必须是映射")
    unknown = set(raw) - set(_REQUIRES_INT_KEYS) - set(_REQUIRES_LABEL_KEYS)
    if unknown:
        raise SuiteValidationError(
            f"requires 含未知键: {sorted(unknown)}；"
            f"允许 {_REQUIRES_INT_KEYS + _REQUIRES_LABEL_KEYS}"
        )
    out: dict = {}
    for key in _REQUIRES_INT_KEYS:
        if key in raw:
            value = raw[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SuiteValidationError(f"requires.{key} 必须是非负整数")
            out[key] = value
    for key in _REQUIRES_LABEL_KEYS:
        if key in raw:
            value = raw[key]
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(x, str) and x for x in value)
            ):
                raise SuiteValidationError(f"requires.{key} 必须是非空字符串列表")
            out[key] = list(value)
    return out


def profile_from_suite(raw: dict) -> AccessProfile:
    """套件内嵌 profile（评测本地运行；字段语义与 §7.4 一致）。"""
    if not isinstance(raw, dict):
        raise SuiteValidationError("profile 必须是映射")
    scopes = raw.get("allow_scopes", ["*"])
    if not isinstance(scopes, list) or not scopes:
        raise SuiteValidationError("allow_scopes 必须是非空列表")
    if "*" in scopes and len(scopes) > 1:
        raise SuiteValidationError("通配 '*' 不得与其他 scope 并用")
    return AccessProfile(
        name=str(raw.get("name", "eval")),
        allow_scopes=tuple(scopes),
        deny_sensitivity=tuple(raw.get("deny_sensitivity", ())),
        allow_unclassified=bool(raw.get("allow_unclassified", False)),
        tools=("brain_status", "search_history", "get_recent_events", "get_event"),
        delivery_boundary="local_only",
        transport="local_cli_only",  # 评测属本地人工验证通道
    )


def _parse_case(raw: dict, index: int) -> EvalCase:
    if not isinstance(raw, dict):
        raise SuiteValidationError(f"cases[{index}] 必须是映射")
    cid = raw.get("id")
    if not cid:
        raise SuiteValidationError(f"cases[{index}] 缺少 id")
    ctype = raw.get("type")
    if ctype not in CASE_TYPES:
        raise SuiteValidationError(f"case {cid}: type 必须是 {CASE_TYPES}")
    mode = raw.get("mode", "search")
    if mode not in ("search", "get_event"):
        raise SuiteValidationError(f"case {cid}: mode 必须是 search|get_event")
    if mode == "search" and not raw.get("query"):
        raise SuiteValidationError(f"case {cid}: search 用例需要 query")
    if mode == "get_event" and not raw.get("event_id"):
        raise SuiteValidationError(f"case {cid}: get_event 用例需要 event_id")
    expected_access = raw.get("expected_access", "allow")
    if expected_access not in ("allow", "deny"):
        raise SuiteValidationError(f"case {cid}: expected_access 必须是 allow|deny")
    time_raw = raw.get("time")
    time_cond = TimeCondition(
        date_from=time_raw.get("date_from"),
        date_to=time_raw.get("date_to"),
        timezone=str(time_raw.get("timezone", "UTC")),
    ) if isinstance(time_raw, dict) else None
    attribution = {
        k: tuple(v) for k, v in (raw.get("attribution") or {}).items()
    }
    return EvalCase(
        id=str(cid),
        type=ctype,
        mode=mode,
        query=raw.get("query"),
        match_mode=raw.get("match_mode", "literal"),
        order=raw.get("order", "chronological"),
        branch_scope=raw.get("branch_scope", "all"),
        time=time_cond,
        event_id=raw.get("event_id"),
        context_radius=int(raw.get("context_radius", 0)),
        acceptable_event_ids=tuple(raw.get("acceptable_event_ids", ())),
        forbidden_event_ids=tuple(raw.get("forbidden_event_ids", ())),
        must_distinguish=str(raw.get("must_distinguish", "")),
        forbidden_conclusions=tuple(raw.get("forbidden_conclusions", ())),
        allowed_refusal=bool(raw.get("allowed_refusal", False)),
        expected_access=expected_access,
        attribution=attribution,
        note=str(raw.get("note", "")),
    )


def load_suite(path: Path) -> EvalSuite:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SuiteValidationError("评测套件必须是 JSON 映射")
    if raw.get("schema_version") != "0.2":
        raise SuiteValidationError(
            f"schema_version 必须是 '0.2'，得到 {raw.get('schema_version')!r}"
        )
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise SuiteValidationError("cases 必须是非空列表")
    cases = tuple(_parse_case(c, i) for i, c in enumerate(cases_raw))
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise SuiteValidationError("case id 重复")
    profile = profile_from_suite(raw.get("profile", {}))
    return EvalSuite(
        name=str(raw.get("name", Path(path).stem)),
        schema_version="0.2",
        profile=profile,
        cases=cases,
        source_path=str(path),
        requires=_parse_requires(raw.get("requires")),
    )
