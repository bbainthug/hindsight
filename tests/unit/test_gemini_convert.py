"""D-12 C：convert_gemini 转换器对“你说+重复副本”的归一化。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CONVERTER_PATH = (
    Path(__file__).resolve().parents[2] / "integrations/gemini_export/convert_gemini.py"
)
_SPEC = importlib.util.spec_from_file_location("convert_gemini", _CONVERTER_PATH)
convert_gemini = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("convert_gemini", convert_gemini)
_SPEC.loader.exec_module(convert_gemini)


def _export(turns: list[dict]) -> dict:
    return {
        "exported_at": "2026-10-03T00:00:00Z",
        "conversations": [
            {"id": "c_1", "title": "合成对话", "created_at": None, "turns": turns}
        ],
    }


def _messages(convs: list[dict], role: str) -> list[str]:
    return [
        node["message"]["content"]["parts"][0]
        for conv in convs
        for node in conv["mapping"].values()
        if node.get("message") and node["message"]["author"]["role"] == role
    ]


def test_convert_strips_label_and_duplicate_copy_from_user_turn():
    convs = convert_gemini.convert(
        _export(
            [
                {"role": "user", "text": "你说\n我偏好夜间工作\n我偏好夜间工作"},
                {"role": "assistant", "text": "合成答复"},
            ]
        )
    )
    assert _messages(convs, "user") == ["我偏好夜间工作"]
    assert _messages(convs, "assistant") == ["合成答复"]


def test_convert_keeps_user_text_without_defect():
    turns = [
        {"role": "user", "text": "你说得对，我偏好夜间工作"},
        {"role": "assistant", "text": "你说\n这是 assistant 的回复（不受影响）"},
    ]
    convs = convert_gemini.convert(_export(turns))
    assert _messages(convs, "user") == ["你说得对，我偏好夜间工作"]
    assert _messages(convs, "assistant") == ["你说\n这是 assistant 的回复（不受影响）"]


def test_message_id_uses_normalized_text():
    # 归一化发生在稳定 ID 计算之前：同一内容的不同缺陷形态收敛到同一 ID
    a = convert_gemini.convert(_export([{"role": "user", "text": "你说\nX\nX"}]))
    b = convert_gemini.convert(_export([{"role": "user", "text": "你说 X\nX"}]))
    node_ids = lambda convs: [  # noqa: E731
        nid
        for conv in convs
        for nid, node in conv["mapping"].items()
        if node.get("message")
    ]
    assert _messages(a, "user") == ["X"]
    assert _messages(b, "user") == ["X"]
    assert node_ids(a) == node_ids(b)  # 同一 digest → 同一稳定 ID，重复导入幂等
