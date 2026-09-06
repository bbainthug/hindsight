"""时间语义（§4.4）。

- 统一以 UTC ISO-8601 字符串存储（毫秒精度，结尾 ``Z``）。
- 原始值保留在 ``original_time_value``，不覆盖。
- 无时区的时间不能静默当作 UTC：标记 ``timezone_status='naive'``，
  归一化结果为 ``None``，不得参与时间过滤。
- 无法确定的时间不以 ``imported_at`` 代替。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

__all__ = [
    "TimePrecision",
    "TimezoneStatus",
    "ParsedTime",
    "parse_unix_seconds",
    "parse_iso8601",
    "utc_now_iso",
    "TIME_PRECISION_UNKNOWN",
]

TimePrecision = str  # second | minute | day | month | year | unknown
TimezoneStatus = str  # utc | offset | naive | unknown

TIME_PRECISION_UNKNOWN: TimePrecision = "unknown"

_ISO_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:[T ](?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:\.(?P<frac>\d+))?)?)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True)
class ParsedTime:
    """解析后的时间：UTC 归一值 + 语义标注。"""

    utc_iso: str | None  # 归一化 UTC；无法确定时为 None
    original_time_value: str  # 原始字符串，永久保留
    time_precision: TimePrecision
    timezone_status: TimezoneStatus

    @property
    def is_known(self) -> bool:
        return self.utc_iso is not None


def utc_now_iso() -> str:
    """系统记录时间（recorded_at 等使用；不参与 revision_hash）。"""
    return _format(datetime.now(UTC))


def _format(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_unix_seconds(value: float | int | str) -> ParsedTime:
    """解析 ChatGPT 导出的 unix 秒（浮点）。导出中的 unix 时间语义为 UTC。"""
    original = str(value)
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ParsedTime(
            utc_iso=None,
            original_time_value=original,
            time_precision="unknown",
            timezone_status="unknown",
        )
    dt = datetime.fromtimestamp(ts, tz=UTC)
    return ParsedTime(
        utc_iso=_format(dt),
        original_time_value=original,
        time_precision="second",
        timezone_status="utc",
    )


def parse_iso8601(value: str) -> ParsedTime:
    """解析 ISO-8601 字符串。

    - 带 ``Z``/偏移 → 归一化 UTC，``timezone_status`` 为 ``utc``/``offset``。
    - 无时区 → ``naive``：**不**当作 UTC，``utc_iso=None``。
    - 精度按字段推断（秒/分/日/月/年）。
    """
    original = str(value)
    m = _ISO_RE.match(original.strip())
    if not m:
        return ParsedTime(None, original, "unknown", "unknown")

    parts = m.groupdict()
    tz = parts["tz"]
    if tz is None:
        timezone_status: TimezoneStatus = "naive"
    elif tz == "Z":
        timezone_status = "utc"
    else:
        timezone_status = "offset"

    # 精度：按出现的最小字段
    if parts["s"] is not None:
        precision: TimePrecision = "second"
    elif parts["mi"] is not None:
        precision = "minute"
    elif parts["h"] is not None:
        precision = "minute"
    elif parts["d"] is not None:
        precision = "day"
    elif parts["mo"] is not None:
        precision = "month"
    else:
        precision = "year"

    if timezone_status == "naive":
        return ParsedTime(None, original, precision, timezone_status)

    try:
        tz_offset = UTC if tz == "Z" else _parse_offset(tz)
        dt = datetime(
            int(parts["y"]),
            int(parts["mo"]),
            int(parts["d"]),
            int(parts["h"] or 0),
            int(parts["mi"] or 0),
            int(parts["s"] or 0),
            tzinfo=tz_offset,
        )
    except (ValueError, TypeError):
        return ParsedTime(None, original, precision, "unknown")
    return ParsedTime(_format(dt), original, precision, timezone_status)


def _parse_offset(tz: str):
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:4])

    return timezone(sign * timedelta(hours=hours, minutes=minutes))
