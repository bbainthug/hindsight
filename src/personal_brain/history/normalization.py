"""search_text 归一化与原文偏移映射（§6.2）。

- 归一化 = NFKC + casefold，**逐字符**应用并记录每个归一化字符
  在原文中的起止偏移。逐字符（而非整串）保证映射可精确对齐；
  索引、匹配、snippet 三者使用同一函数，语义自洽。
  与整串 NFKC 的差异仅在跨字符组合（如分解重音序列），属可接受
  的已知权衡（决策记录 17）。
- snippet 不得用归一化索引直接切原文（§6.2 硬约束）：匹配区间通过
  偏移映射还原为原文片段。
"""

from __future__ import annotations

import unicodedata

NORM_VERSION = "nfkc-casefold-v2-charmap"


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回 (归一化文本, 偏移映射)。

    ``map[j]`` = 归一化文本第 j 个字符在原文中的起始索引；
    第 j 个字符的原文结束索引 = ``map[j+1]``（末尾为 ``len(text)``）。
    """
    norm_chars: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        piece = unicodedata.normalize("NFKC", ch).casefold()
        for _ in piece:
            offsets.append(i)
        norm_chars.append(piece)
    offsets.append(len(text))  # 哨兵：最后一个归一化字符的结束位置
    return "".join(norm_chars), offsets


def normalize_search_text(text: str) -> str:
    """存储用归一化文本（与 normalize_with_map 同一实现）。"""
    return normalize_with_map(text)[0]


def raw_span(norm: str, offsets: list[int], start: int, end: int) -> tuple[int, int]:
    """归一化匹配区间 [start, end) → 原文区间 [raw_start, raw_end)。"""
    if not (0 <= start < end <= len(norm)):
        raise ValueError("非法匹配区间")
    return offsets[start], offsets[end]
