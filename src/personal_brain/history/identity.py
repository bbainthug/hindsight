"""事件身份与内容版本哈希（§4.2）。

- ``event_id = source_kind + ":" + account_namespace + ":" + stable_source_message_id``
  确定性派生；同一消息在多次导入中必然得到同一身份。
- 无原生稳定消息 ID 时使用 fallback：组合来源、对话、说话者、时间、
  结构位置和内容摘要。仅凭文本 hash 不得把不同时间的相同发言合并
  （时间与结构位置参与派生）。
- ``revision_hash`` 覆盖影响解释的内容与元数据：
  content_type、raw_text、speaker、原始时间值、精度、时区状态、归一化 UTC、
  附件引用。**不**包括导入时间（recorded_at）与磁盘绝对路径/归档路径。
- ``search_text``：NFKC + casefold 归一化（§6.2），只用于检索，不参与哈希
  （归一化版本变化不影响内容版本身份）。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from personal_brain.history.timeutil import ParsedTime

__all__ = [
    "compute_event_id",
    "compute_fallback_source_message_id",
    "compute_revision_hash",
    "normalize_search_text",
    "SEARCH_TEXT_NORM_VERSION",
]

SEARCH_TEXT_NORM_VERSION = "nfkc-casefold-v1"

_HASH_NS_EVENT = "personal-brain:event:v1"
_HASH_NS_FALLBACK = "personal-brain:fallback-msg:v1"
_HASH_NS_REVISION = "personal-brain:revision:v1"


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def compute_event_id(
    source_kind: str, account_namespace: str, source_message_id: str
) -> str:
    """确定性事件 ID：``{source_kind}:{namespace}:{source_message_id}``。"""
    return f"{source_kind}:{account_namespace}:{source_message_id}"


def compute_fallback_source_message_id(
    *,
    source_kind: str,
    account_namespace: str,
    conversation_id: str,
    structural_position: str,
    speaker_id: str | None,
    original_time_value: str | None,
    content_digest: str,
) -> str:
    """无原生消息 ID 时的 fallback 身份。

    结构位置 + 时间 + 说话者 + 内容摘要组合，确保“同一对话中
    不同时间的相同发言”不会合并为同一事件。
    """
    payload = "|".join(
        [
            source_kind,
            account_namespace,
            conversation_id,
            structural_position,
            speaker_id or "",
            original_time_value or "",
            content_digest,
        ]
    )
    return "fb-" + hashlib.sha256(
        f"{_HASH_NS_FALLBACK}|{payload}".encode()
    ).hexdigest()[:24]


def compute_revision_hash(
    *,
    content_type: str,
    raw_text: str | None,
    speaker_id: str | None,
    speaker_type: str,
    parsed_time: ParsedTime | None,
    source_updated_at: str | None,
    attachment_refs: list[dict[str, str | None]],
) -> str:
    """内容版本哈希。

    参与字段：影响解释的内容与元数据。
    排除字段：recorded_at（导入时间）、归档相对路径、磁盘绝对路径、
    source_locator、search_text（归一化版本变化不影响身份）。
    """
    time_payload = (
        {
            "utc": parsed_time.utc_iso,
            "original": parsed_time.original_time_value,
            "precision": parsed_time.time_precision,
            "tz": parsed_time.timezone_status,
        }
        if parsed_time is not None
        else None
    )
    canonical = {
        "content_type": content_type,
        "raw_text": raw_text,
        "speaker_id": speaker_id,
        "speaker_type": speaker_type,
        "time": time_payload,
        "source_updated_at": source_updated_at,
        "attachments": attachment_refs,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_hex(f"{_HASH_NS_REVISION}|{encoded}")


def revision_id_for(event_id: str, revision_hash: str) -> str:
    """确定性 revision_id：``{event_id}:r{hash 前 20 位}``，天然幂等。"""
    return f"{event_id}:r{revision_hash[:20]}"


def normalize_search_text(text: str) -> str:
    """NFKC + casefold 归一化（§6.2 初始索引策略，本轮仅生成不建索引）。"""
    return unicodedata.normalize("NFKC", text).casefold()


def content_digest(text: str | None) -> str:
    """内容摘要（供 fallback 身份使用）。"""
    return _sha256_hex(f"{_HASH_NS_EVENT}|{text or ''}")
