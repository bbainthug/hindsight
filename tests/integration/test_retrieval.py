"""历史检索集成测试：验收语义电池 + FTS/基线差分 + 过滤 + 限额（§6）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from corpus import retrieval_corpus_conversations
from synthetic import write_zip

from personal_brain.retrieval.search import (
    QueryTooBroad,
    SearchFilters,
    parse_date_bound,
    recent_events,
    search_history,
)

QUERIES_BATTERY = [
    ("职业", "literal"),
    ("求职", "literal"),
    ("安全", "literal"),
    ("安", "literal"),
    ("AI", "literal"),
    ("AI工程", "literal"),
    ("你好，世界", "literal"),
    ("职业方向", "literal"),
    ("职业 求职", "all_terms"),
    ("职业 求职", "literal"),
    ("Learning", "literal"),
    ("earn", "literal"),
    ("职业方向变化", "literal"),
    ("时间未知的职业话题", "literal"),
    ("", "literal"),  # 空 query → 异常
]


@pytest.fixture()
def corpus_db(importer, tmp_path: Path):
    zp = write_zip(retrieval_corpus_conversations(), tmp_path / "corpus.zip")
    importer.import_archive(zp, "testuser")
    return importer.conn


def _rids(result):
    return {h.revision_id for h in result.hits}


def _query_text(corpus_db, q, mode, **kw):
    return search_history(corpus_db, q, match_mode=mode, **kw)


class TestAcceptanceSemantics:
    """Phase A 验收 3：职业/求职/安全/AI/AI工程/单汉字/标点匹配语义。"""

    def test_two_char_cjk(self, corpus_db):
        r = _query_text(corpus_db, "职业", "literal")
        assert len(r.hits) == 5  # job01/rel01/both/early/undat
        r2 = _query_text(corpus_db, "求职", "literal")
        assert len(r2.hits) == 2  # seek01/both

    def test_safety_word(self, corpus_db):
        r = _query_text(corpus_db, "安全", "literal")
        assert len(r.hits) == 1  # 仅 sec01；「安」不含「安全」

    def test_latin_substring(self, corpus_db):
        r = _query_text(corpus_db, "AI", "literal")
        # 子串语义：ai01（独立+词内）、mix01/wide（AI工程中的 ai）都命中
        assert len(r.hits) == 3
        scores = {h.event_id.rsplit(":", 1)[-1]: h.score for h in r.hits}
        assert scores["ai01"] == 2  # daily 词内 + 独立 token
        assert scores["mix01"] == 1 and scores["wide"] == 1

    def test_mixed_latin_cjk(self, corpus_db):
        r = _query_text(corpus_db, "AI工程", "literal")
        assert len(r.hits) == 2  # mix01 / wide
        plans = r.query_plans[0]
        assert plans["fts_tokens"] == ["工程"]
        assert plans["fallback"] is False

    def test_single_hanzi_fallback(self, corpus_db):
        r = _query_text(corpus_db, "安", "literal")
        assert len(r.hits) == 2  # sec01（词内）+ hanzi（独立）
        assert r.query_plans[0]["fallback"] is True

    def test_punctuation_literal(self, corpus_db):
        r = _query_text(corpus_db, "你好，世界", "literal")
        assert len(r.hits) == 1

    def test_literal_space_vs_all_terms(self, corpus_db):
        r_lit = _query_text(corpus_db, "职业 求职", "literal")
        assert len(r_lit.hits) == 0  # 无连续子串「职业 求职」
        r_all = _query_text(corpus_db, "职业 求职", "all_terms")
        assert len(r_all.hits) == 1  # both 唯一同时含两词

    def test_earliest_requires_time_order_not_relevance(self, corpus_db):
        """「最早提到」必须用时间排序（§6.1），relevance 不得冒充。"""
        r = _query_text(corpus_db, "职业", "literal", order="chronological")
        known = [h for h in r.hits if h.time_known]
        assert known[0].source_created_at == min(
            h.source_created_at for h in known
        )
        r_rel = _query_text(corpus_db, "职业", "literal", order="relevance")
        assert r_rel.hits[0].score >= max(h.score for h in r.hits)

    def test_relevance_scores_occurrences(self, corpus_db):
        r = _query_text(corpus_db, "职业", "literal", order="relevance")
        top = r.hits[0]
        assert top.raw_text is not None and top.raw_text.count("职业") == 3  # rel01

    def test_empty_query_rejected(self, corpus_db):
        with pytest.raises(ValueError, match="空查询"):
            _query_text(corpus_db, "", "literal")
        with pytest.raises(ValueError, match="空查询"):
            _query_text(corpus_db, "   ", "all_terms")


class TestFtsBaselineDifferential:
    """§6.2：候选优化不得改变匹配集合——两条路径结果逐项一致。"""

    @pytest.mark.parametrize(("query", "mode"), QUERIES_BATTERY[:-1])
    def test_paths_agree(self, corpus_db, query: str, mode: str):
        via_fts = _query_text(corpus_db, query, mode, use_fts=True)
        baseline = _query_text(corpus_db, query, mode, use_fts=False)
        assert {(h.revision_id, h.score) for h in via_fts.hits} == {
            (h.revision_id, h.score) for h in baseline.hits
        }
        assert via_fts.total_matched == baseline.total_matched

    def test_fts_actually_used_for_safe_terms(self, corpus_db):
        r = _query_text(corpus_db, "职业方向", "literal")
        assert r.query_plans[0]["fallback"] is False
        assert r.query_plans[0]["fts_tokens"]


class TestSnippetOriginalText:
    def test_snippet_is_original_fragment(self, corpus_db):
        r = _query_text(corpus_db, "职业方向", "literal")
        assert r.hits[0].snippet is not None
        assert "职业方向" in r.hits[0].snippet
        assert r.hits[0].snippet.find("职业方向") >= 0

    def test_fullwidth_snippet_via_offset_map(self, corpus_db):
        """全角原文命中半角查询：snippet 必须还原全角原文（偏移映射）。"""
        r = _query_text(corpus_db, "AI工程", "literal")
        snippets = [h.snippet for h in r.hits if "ＡＩ" in (h.raw_text or "")]
        assert snippets
        assert any("ＡＩ工程" in (s or "") for s in snippets)


class TestTimeFilters:
    def test_date_range_utc(self, corpus_db):
        f = SearchFilters(
            date_from=parse_date_bound("2026-08-01", "UTC", end_of_day=False),
            date_to=parse_date_bound("2026-08-01", "UTC", end_of_day=True),
        )
        r = search_history(corpus_db, "职业", match_mode="literal", filters=f)
        ids_text = {h.raw_text for h in r.hits}
        assert "早期职业思考" not in ids_text  # 07-31T20:00Z 不在 [08-01, 08-02)
        assert r.undated_excluded == 1  # 未知时间记录被排除并报告

    def test_timezone_shifts_boundary(self, corpus_db):
        f = SearchFilters(
            date_from=parse_date_bound("2026-08-01", "Asia/Shanghai", end_of_day=False),
            date_to=parse_date_bound("2026-08-01", "Asia/Shanghai", end_of_day=True),
        )
        r = search_history(corpus_db, "职业", match_mode="literal", filters=f)
        ids_text = {h.raw_text for h in r.hits}
        # 上海时区 [07-31T16:00Z, 08-01T16:00Z) 覆盖 early(20:00Z)
        assert "早期职业思考" in ids_text

    def test_interpreted_interval_reported(self, corpus_db):
        f = SearchFilters(
            date_from=parse_date_bound("2026-08-01", "UTC", end_of_day=False),
            date_to=parse_date_bound("2026-08-01", "UTC", end_of_day=True),
        )
        r = search_history(corpus_db, "职业", match_mode="literal", filters=f)
        assert r.interpreted["date_from"] == "2026-08-01T00:00:00.000000Z"
        assert r.interpreted["date_to"] == "2026-08-02T00:00:00.000000Z"


class TestFilters:
    def test_speaker_filter(self, corpus_db):
        f = SearchFilters(speaker_type="assistant")
        r = search_history(corpus_db, "求职", match_mode="literal", filters=f)
        assert len(r.hits) == 1  # seek01

    def test_conversation_filter(self, corpus_db):
        f = SearchFilters(conversation_id="conv-branch2")
        r = search_history(corpus_db, "偏理论", match_mode="literal", filters=f)
        assert len(r.hits) == 1


class TestPathModes:
    def test_all_includes_off_path(self, corpus_db):
        r = search_history(
            corpus_db, "偏理论", match_mode="literal",
            filters=SearchFilters(path_mode="all"),
        )
        assert len(r.hits) == 1  # 左支版本可查（历史检索 all）

    def test_selected_excludes_off_path(self, corpus_db):
        r = search_history(
            corpus_db, "偏理论", match_mode="literal",
            filters=SearchFilters(path_mode="selected"),
        )
        assert len(r.hits) == 0  # 左支不在所选路径

    def test_selected_includes_on_path(self, corpus_db):
        r = search_history(
            corpus_db, "偏工程", match_mode="literal",
            filters=SearchFilters(path_mode="selected"),
        )
        assert len(r.hits) == 1  # 右支在所选路径


class TestLimits:
    def test_limit_and_truncation(self, corpus_db):
        r = search_history(corpus_db, "职业", match_mode="literal", limit=2)
        assert len(r.hits) == 2
        assert r.truncated is True
        assert r.next_cursor is not None

    def test_cursor_pagination_no_overlap(self, corpus_db):
        page1 = search_history(corpus_db, "职业", match_mode="literal", limit=2)
        assert page1.next_cursor is not None
        page2 = search_history(
            corpus_db, "职业", match_mode="literal", limit=2, cursor=page1.next_cursor
        )
        ids1 = {h.revision_id for h in page1.hits}
        ids2 = {h.revision_id for h in page2.hits}
        assert not (ids1 & ids2)
        assert page1.total_matched == page2.total_matched

    def test_invalid_cursor_rejected(self, corpus_db):
        with pytest.raises(ValueError, match="无效游标"):
            search_history(corpus_db, "职业", match_mode="literal", cursor="broken!!")

    def test_max_limit_clamp(self, corpus_db):
        r = search_history(corpus_db, "职业", match_mode="literal", limit=1000)
        assert len(r.hits) <= 50

    def test_hard_caps_in_limits(self):
        """§6.3：配置不得超过规格数值（服务端硬上限）。"""
        from personal_brain.retrieval.search import SearchLimits

        sl = SearchLimits(
            default_limit=99, max_limit=999, snippet_chars=9999, max_text_chars=99999
        )
        assert sl.default_limit == 10
        assert sl.max_limit == 50
        assert sl.snippet_chars == 400
        assert sl.max_text_chars == 8000

    def test_capability_boundary_note_in_result(self, corpus_db):
        """§6.3：不自动扩写语义查询的边界须写入结果说明。"""
        r = search_history(corpus_db, "职业", match_mode="literal")
        assert any("语义扩展" in n for n in r.notes)

    def test_query_too_broad(self, corpus_db):
        from personal_brain.retrieval.search import SearchLimits

        small = SearchLimits(max_scan=3)
        with pytest.raises(QueryTooBroad, match="QUERY_TOO_BROAD"):
            search_history(corpus_db, "安", match_mode="literal", limits=small)

    def test_fts_path_not_subject_to_fallback_cap(self, corpus_db):
        """纯 FTS 词不触发回退上限（仅在授权过滤范围内预筛选）。"""
        from personal_brain.retrieval.search import SearchLimits

        small = SearchLimits(max_scan=3)
        r = search_history(corpus_db, "职业方向", match_mode="literal", limits=small)
        assert len(r.hits) == 1

    def test_text_budget(self, corpus_db):
        from personal_brain.retrieval.search import SearchLimits

        sl = SearchLimits(max_text_chars=10)
        r = search_history(corpus_db, "职业", match_mode="literal", limits=sl)
        assert sum(len(h.raw_text or "") for h in r.hits) <= 10


class TestRecent:
    def test_recent_excludes_old_and_undated(self, corpus_db):
        from datetime import UTC, datetime

        now = datetime(2026, 8, 2, 8, 0, 0, tzinfo=UTC)
        r = recent_events(corpus_db, days=7, limit=50, now=now)
        texts = {h.raw_text for h in r.hits}
        assert "我最近正在考虑职业方向变化" in texts
        assert "早期职业思考" in texts  # 7 天内（07-31T20:00Z > 07-26T08:00Z）
        assert "时间未知的职业话题" not in texts  # 未知时间不入最近列表

    def test_recent_days_window(self, corpus_db):
        from datetime import UTC, datetime

        now = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)
        r1 = recent_events(corpus_db, days=1, limit=50, now=now)
        assert "早期职业思考" in {h.raw_text for h in r1.hits}  # 13h 前 < 1 天
        r0 = recent_events(corpus_db, days=0, limit=50, now=now)
        texts0 = {h.raw_text for h in r0.hits}
        assert "早期职业思考" not in texts0  # 07-31T20:00Z < 08-01T09:00Z 窗口起点
        assert "右支偏工程实践" not in texts0  # 08:18Z 也在 09:00Z 窗口起点之前
        assert r0.total_matched == 0  # 窗口内确无记录（验证过滤真实生效）

    def test_recent_limit(self, corpus_db):
        from datetime import UTC, datetime

        now = datetime(2026, 8, 2, 8, 0, 0, tzinfo=UTC)
        r = recent_events(corpus_db, days=7, limit=3, now=now)
        assert len(r.hits) <= 3
