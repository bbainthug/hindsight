"""时间语义测试（§4.4）。"""

from __future__ import annotations

from personal_brain.history.timeutil import parse_iso8601, parse_unix_seconds


class TestUnixSeconds:
    def test_utc_normalization_and_original_preserved(self):
        t = parse_unix_seconds(1785571200.0)
        assert t.utc_iso == "2026-08-01T08:00:00Z"
        assert t.original_time_value == "1785571200.0"
        assert t.time_precision == "second"
        assert t.timezone_status == "utc"
        assert t.is_known

    def test_sub_second(self):
        t = parse_unix_seconds(1785571200.5)
        assert t.utc_iso == "2026-08-01T08:00:00.500Z"

    def test_invalid_value_is_unknown_not_dropped(self):
        t = parse_unix_seconds("not-a-number")
        assert t.utc_iso is None
        assert t.time_precision == "unknown"
        assert t.timezone_status == "unknown"
        assert t.original_time_value == "not-a-number"


class TestIso8601:
    def test_z_suffix(self):
        t = parse_iso8601("2026-08-01T08:00:00Z")
        assert t.utc_iso == "2026-08-01T08:00:00Z"
        assert t.timezone_status == "utc"

    def test_offset_normalized_to_utc(self):
        t = parse_iso8601("2026-08-01T16:00:00+08:00")
        assert t.utc_iso == "2026-08-01T08:00:00Z"
        assert t.timezone_status == "offset"

    def test_naive_not_silently_utc(self):
        """无时区不能静默当作 UTC（§4.4 硬约束）。"""
        t = parse_iso8601("2026-08-01 08:00:00")
        assert t.utc_iso is None
        assert t.timezone_status == "naive"
        assert t.time_precision == "second"
        assert t.is_known is False

    def test_date_only_precision(self):
        t = parse_iso8601("2026-08-01Z")
        assert t.time_precision == "day"
        assert t.utc_iso == "2026-08-01T00:00:00Z"

    def test_minute_precision(self):
        t = parse_iso8601("2026-08-01T08:00Z")
        assert t.time_precision == "minute"

    def test_garbage_unknown(self):
        t = parse_iso8601("???")
        assert t.utc_iso is None
        assert t.time_precision == "unknown"
