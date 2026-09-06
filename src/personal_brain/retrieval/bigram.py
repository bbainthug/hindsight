"""中文二元索引变换与查询安全分析（§6.2 初始索引策略）。

索引变换（对 search_text）：
- 连续汉字段（长度 n≥2）→ 重叠二元，空格分隔：「职业方向」→「职业 业方 方向」
- 长度为 1 的汉字段 → 该汉字本身作为 token（仅当原文独立成段时存在）
- 拉丁字母/数字段 → 完整 token（大小写已由归一化处理）
- 其余（标点等）→ 丢弃（FTS 仅缩小候选范围，正文语义由 search_text 保留）

FTS 预筛选的**超集保证**：设查询词在原文 search_text 中连续出现（真匹配），
则查询词的每个长度≥2 汉字段在原文中同样连续出现，必落在原文某个更长的
汉字段内部，其全部二元 token 必出现在该段的索引 token 集合中。
因此「索引 token 全部 AND 匹配」的结果集 ⊇ 真匹配集——FTS 仅作
候选缩小，最终以 ``instr(search_text, term)`` 逐条验证，匹配集合不变。

**不安全**（必须走参数化 instr 回退，不得用 FTS）：
- 单个汉字查询（「安」在「安全」内部时索引中不存在独立 token「安」）
- 拉丁词内子串（「earn」≠索引 token「learning」）
- 仅标点/数字等查询
"""

from __future__ import annotations

from dataclasses import dataclass

_CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0x20000, 0x2A6DF),  # 扩展 B
    (0x2A700, 0x2EBEF),  # 扩展 C–F
)


def is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def cjk_runs(text: str) -> list[str]:
    """文本中的极大连续汉字段。"""
    runs: list[str] = []
    cur: list[str] = []
    for ch in text:
        if is_cjk_char(ch):
            cur.append(ch)
        elif cur:
            runs.append("".join(cur))
            cur = []
    if cur:
        runs.append("".join(cur))
    return runs


def bigrams(term: str) -> list[str]:
    """汉字段的重叠二元切分；长度 1 返回自身（调用方须避免用于预筛选）。"""
    if len(term) == 1:
        return [term]
    return [term[i : i + 2] for i in range(len(term) - 1)]


def to_index_text(search_text: str) -> str:
    """search_text → FTS 索引文本（二元以空格分隔；拉丁/数字整 token）。"""
    tokens: list[str] = []
    cur_latin: list[str] = []
    cur_cjk: list[str] = []

    def flush_latin() -> None:
        if cur_latin:
            tokens.append("".join(cur_latin))
            cur_latin.clear()

    def flush_cjk() -> None:
        if cur_cjk:
            run = "".join(cur_cjk)
            if len(run) >= 2:
                tokens.extend(bigrams(run))
            else:
                tokens.append(run)
            cur_cjk.clear()

    for ch in search_text:
        if ch.isspace():
            flush_latin()
            flush_cjk()
        elif is_cjk_char(ch):
            flush_latin()
            cur_cjk.append(ch)
        elif ch.isalnum():
            flush_cjk()
            cur_latin.append(ch)
        else:
            flush_latin()
            flush_cjk()
    flush_latin()
    flush_cjk()
    return " ".join(tokens)


@dataclass(frozen=True)
class TermPlan:
    """单个查询词的检索计划。"""

    term: str  # 归一化后的查询词（instr 验证以此为准）
    fts_tokens: tuple[str, ...]  # 空 = 不可安全预筛选，必须回退扫描

    @property
    def needs_fallback(self) -> bool:
        return not self.fts_tokens


def plan_term(normalized_term: str) -> TermPlan:
    """查询词 → 检索计划。

    只有当查询词的**每个**汉字段长度 ≥2 时才可 FTS 预筛选
    （见模块 docstring 的超集证明）；含单汉字段、拉丁、纯标点的词
    一律回退。
    """
    runs = cjk_runs(normalized_term)
    if not runs or any(len(r) < 2 for r in runs):
        return TermPlan(term=normalized_term, fts_tokens=())
    tokens: list[str] = []
    for r in runs:
        tokens.extend(bigrams(r))
    return TermPlan(term=normalized_term, fts_tokens=tuple(tokens))


def build_fts_query(tokens: tuple[str, ...]) -> str:
    """受控构造 FTS5 MATCH 表达式（全部 token AND；绑定参数传入）。"""
    quoted = ['"' + t.replace('"', "") + '"' for t in tokens]
    return " AND ".join(quoted)
