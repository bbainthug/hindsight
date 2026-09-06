"""归一化与偏移映射测试（§6.2）。"""

from __future__ import annotations

from personal_brain.history.normalization import (
    NORM_VERSION,
    normalize_search_text,
    normalize_with_map,
    raw_span,
)


class TestNormalizeWithMap:
    def test_ascii_identity(self):
        norm, offsets = normalize_with_map("abc")
        assert norm == "abc"
        assert offsets == [0, 1, 2, 3]

    def test_casefold_fullwidth(self):
        """全角→半角 + 小写化，偏移指向原字符位置。"""
        raw = "ＡＩ工程"
        norm, offsets = normalize_with_map(raw)
        assert norm == "ai工程"
        assert len(offsets) == len(norm) + 1
        assert offsets[0] == 0  # 'a' ← 'Ａ'
        assert offsets[1] == 1  # 'i' ← 'Ｉ'
        assert offsets[2] == 2  # '工' 原位

    def test_expansion_maps_to_same_raw_index(self):
        """单字符展开（ﬁ → fi）：两个归一化字符都指向同一原文位置。"""
        raw = "aﬁne"
        norm, offsets = normalize_with_map(raw)
        assert norm[1:3] == "fi"
        assert offsets[1] == offsets[2] == 1

    def test_roundtrip_span(self):
        raw = "ＡＩ工程选型"
        norm, offsets = normalize_with_map(raw)
        rs, re_ = raw_span(norm, offsets, 0, 4)
        assert raw[rs:re_] == "ＡＩ工程"

    def test_stored_value_matches_map_recompute(self):
        """存储值与映射重算一致（snippet 正确性前提）。"""
        for text in ["我最近正在考虑职业方向变化", "ＡＩ工程", "Hello，世界", "ﬁx"]:
            norm, _ = normalize_with_map(text)
            assert normalize_search_text(text) == norm


class TestVersionConstant:
    def test_version_is_v2(self):
        assert NORM_VERSION == "nfkc-casefold-v2-charmap"
