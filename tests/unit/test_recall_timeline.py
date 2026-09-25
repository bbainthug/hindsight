"""D-7 单元测试：片段合并、预算装填、超长截断（纯逻辑，不需数据库）。"""

from __future__ import annotations

from personal_brain.mcp_server.service import (
    SearchHit,
    _fit_fragment_messages,
    _merge_adjacent_hits,
)


def _hit(event_id: str, conv: str = "c1", kind: str = "exact") -> SearchHit:
    return SearchHit(
        event_id=event_id, revision_id=f"rev-{event_id}", is_current=True,
        conversation_id=conv, speaker_id=None, speaker_type="owner",
        content_type="text", source_created_at="2026-09-10T08:00:00.000000Z",
        time_known=True, score=1, match_kind=kind,  # type: ignore[arg-type]
    )


class TestMergeAdjacentHits:
    def test_adjacent_hits_merge(self):
        # 位置 3 与 5：中间隔 1 条 ≤ 2*radius（radius=2）→ 合并
        groups = _merge_adjacent_hits([_hit("a"), _hit("b")], [3, 5], 2)
        assert len(groups) == 1
        assert [h.event_id for h in groups[0]] == ["a", "b"]

    def test_far_apart_hits_split(self):
        # 位置 0 与 10：中间隔 9 条 > 2*radius → 两个片段
        groups = _merge_adjacent_hits([_hit("a"), _hit("b")], [0, 10], 2)
        assert len(groups) == 2

    def test_boundary_exactly_touching_windows_merge(self):
        # 半径 2：命中在 0 与 5，两窗口 [0,2] 与 [3,5] 相接（中间 2 条）→ 合并
        groups = _merge_adjacent_hits([_hit("a"), _hit("b")], [0, 5], 2)
        assert len(groups) == 1

    def test_radius_zero_merges_only_adjacent(self):
        # radius=0：窗口只含命中自身；相邻（隔 0 条）合并，隔 1 条不合并
        assert len(_merge_adjacent_hits([_hit("a"), _hit("b")], [0, 1], 0)) == 1
        assert len(_merge_adjacent_hits([_hit("a"), _hit("b")], [0, 2], 0)) == 2

    def test_preserves_order(self):
        groups = _merge_adjacent_hits(
            [_hit("a"), _hit("b"), _hit("c")], [1, 2, 9], 2
        )
        assert [[h.event_id for h in g] for g in groups] == [["a", "b"], ["c"]]


class TestFitFragmentMessages:
    def _msg(self, eid: str, text: str, hit: bool = False) -> dict:
        return {
            "event_id": eid, "revision_id": f"rev-{eid}", "speaker_type": "owner",
            "timestamp": "2026-09-10T08:00:00.000000Z", "text": text,
            "is_hit": hit, "truncated": False,
        }

    def test_everything_fits(self):
        msgs = [self._msg("a", "上下文"), self._msg("b", "命中", hit=True),
                self._msg("c", "后文")]
        fitted, used = _fit_fragment_messages(msgs, 10_000)
        assert [m["event_id"] for m in fitted] == ["a", "b", "c"]
        assert used == len("上下文命中后文")
        assert all(not m["truncated"] for m in fitted)

    def test_later_hit_truncated_rather_than_dropped(self):
        msgs = [self._msg("a", "x" * 50, hit=True), self._msg("b", "y" * 50),
                self._msg("c", "z" * 50, hit=True)]
        # 命中是片段的核心证据：预算尽时后面的命中截断装入而不是丢弃
        fitted, used = _fit_fragment_messages(msgs, 120)
        assert [m["event_id"] for m in fitted] == ["a", "b", "c"]
        assert used == 120
        assert fitted[2]["truncated"] is True
        assert fitted[2]["text_offset"] == 20

    def test_hit_truncated_with_text_offset(self):
        msgs = [self._msg("a", "x" * 500, hit=True)]
        fitted, used = _fit_fragment_messages(msgs, 200)
        assert len(fitted) == 1
        assert fitted[0]["truncated"] is True
        assert fitted[0]["text_offset"] == 200  # get_event(text_offset=200) 续读
        assert len(fitted[0]["text"]) == 200
        assert used == 200

    def test_hit_after_truncated_context_still_included(self):
        # 上下文放不下即止；其后的命中仍截断装入（命中优先于上下文）
        msgs = [self._msg("a", "x" * 100), self._msg("b", "y" * 100, hit=True)]
        fitted, used = _fit_fragment_messages(msgs, 150)
        assert [m["event_id"] for m in fitted] == ["a", "b"]
        assert fitted[1]["truncated"] is True
        assert fitted[1]["text_offset"] == 50
        assert used == 150

    def test_trailing_context_after_truncated_hit_dropped(self):
        msgs = [self._msg("a", "x" * 300, hit=True), self._msg("b", "y" * 10)]
        fitted, _ = _fit_fragment_messages(msgs, 200)
        assert [m["event_id"] for m in fitted] == ["a"]
        assert fitted[0]["truncated"] is True
