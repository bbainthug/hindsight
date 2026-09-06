"""二元索引变换与查询计划安全测试（§6.2）。"""

from __future__ import annotations

from personal_brain.retrieval.bigram import (
    bigrams,
    build_fts_query,
    cjk_runs,
    plan_term,
    to_index_text,
)


class TestIndexTransform:
    def test_cjk_bigram_overlap(self):
        assert to_index_text("职业方向") == "职业 业方 方向"

    def test_latin_whole_token(self):
        assert to_index_text("Learning AI daily") == "Learning AI daily"

    def test_mixed_runs(self):
        """拉丁 token 保留原样：FTS unicode61 分词器自带大小写折叠。"""
        assert to_index_text("AI工程") == "AI 工程"

    def test_punctuation_separates_runs(self):
        assert to_index_text("你好，世界") == "你好 世界"

    def test_single_hanzi_run_kept_as_token(self):
        assert to_index_text("安") == "安"

    def test_hanzi_inside_run_not_standalone(self):
        """「安全」的索引中没有独立 token「安」——单汉字查询必须回退。"""
        assert to_index_text("安全") == "安全"
        assert to_index_text("系统安全很重要") == "系统 统安 安全 全很 很重 重要"


class TestQueryPlan:
    def test_safe_pure_cjk(self):
        plan = plan_term("职业方向")
        assert plan.fts_tokens == ("职业", "业方", "方向")
        assert not plan.needs_fallback

    def test_single_hanzi_fallback(self):
        assert plan_term("安").needs_fallback

    def test_latin_fallback(self):
        assert plan_term("ai").needs_fallback
        assert plan_term("earn").needs_fallback  # 词内子串

    def test_mixed_uses_cjk_part_only(self):
        """契约：plan_term 接收已归一化词（search_history 先归一化）。"""
        plan = plan_term("ai工程")
        assert plan.fts_tokens == ("工程",)
        assert plan.term == "ai工程"

    def test_punctuated_cjk_safe(self):
        plan = plan_term("你好，世界")
        assert plan.fts_tokens == ("你好", "世界")

    def test_single_hanzi_inside_mixed_fallback(self):
        assert plan_term("AI安").needs_fallback


class TestBigramHelpers:
    def test_bigrams(self):
        assert bigrams("职业方向") == ["职业", "业方", "方向"]
        assert bigrams("安") == ["安"]

    def test_cjk_runs(self):
        assert cjk_runs("AI工程栈") == ["工程栈"]
        assert cjk_runs("你好，世界") == ["你好", "世界"]

    def test_fts_query_construction(self):
        assert build_fts_query(("职业", "业方")) == '"职业" AND "业方"'
