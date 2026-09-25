"""CLI 导入结果带结构化错误时，人类可读与 JSON 两种输出都不能崩溃。

回归：ChatGPT 网页对话含导入器不认识的内容结构 → 结果里有 JobError（pydantic 模型），
dataclasses.asdict 不展开它，_emit_human 按 dict 取值时 TypeError，导入已提交却以非零退出，
批次被 D-5 的 import_inbox.sh 误判为失败。
"""

from __future__ import annotations

import json

from personal_brain.cli import main


def _conversations_with_unknown_content() -> list[dict]:
    return [
        {
            "conversation_id": "conv-cli-err-1",
            "title": "t",
            "create_time": 1790000000.0,
            "update_time": 1790000100.0,
            "current_node": "n2",
            "mapping": {
                "root": {"id": "root", "message": None, "parent": None, "children": ["n1"]},
                "n1": {
                    "id": "n1",
                    "parent": "root",
                    "children": ["n2"],
                    "message": {
                        "id": "m1",
                        "author": {"role": "user"},
                        "create_time": 1790000000.0,
                        "content": {"content_type": "text", "parts": ["hello"]},
                    },
                },
                "n2": {
                    "id": "n2",
                    "parent": "n1",
                    "children": [],
                    "message": {
                        "id": "m2",
                        "author": {"role": "assistant"},
                        "create_time": 1790000050.0,
                        "content": {"content_type": "some_future_type", "blob": {"x": 1}},
                    },
                },
            },
        }
    ]


def _write_batch(tmp_path):
    d = tmp_path / "batch"
    d.mkdir()
    (d / "conversations.json").write_text(
        json.dumps(_conversations_with_unknown_content()), encoding="utf-8"
    )
    return d


def test_human_output_with_job_errors(tmp_path, capsys):
    batch = _write_batch(tmp_path)
    rc = main([
        "--db", str(tmp_path / "b.sqlite"), "--archive-dir", str(tmp_path / "arch"),
        "import-chatgpt", str(batch), "--source", "clitest",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "新事件" in out
    assert "  ! " in out  # 错误行被正常打印


def test_json_output_with_job_errors(tmp_path, capsys):
    batch = _write_batch(tmp_path)
    rc = main([
        "--json", "--db", str(tmp_path / "b.sqlite"), "--archive-dir", str(tmp_path / "arch"),
        "import-chatgpt", str(batch), "--source", "clitest",
    ])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["result"]["errors"] and "code" in data["result"]["errors"][0]
