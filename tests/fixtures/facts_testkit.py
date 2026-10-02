"""D-2 事实层测试工具（tests/fixtures 已在 sys.path）。

全部内容为合成数据；FakeLLMClient 不出网。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from synthetic import build_conversation, node, text_message


def linear_conversation(
    conv_id: str,
    title: str,
    msgs: list[tuple[str, str, str | None, float | None]],
    current_node: str | None = None,
) -> dict:
    """线性单链对话。msgs: (node_id, role, text, create_time)。"""
    mapping: dict[str, dict] = dict([node("root", None, None, [msgs[0][0]])])
    prev = "root"
    for node_id, role, text, t in msgs:
        m = text_message(node_id, role, text, t) if text is not None else None
        mapping[node_id] = node(node_id, m, prev, [])[1]
        mapping[prev]["children"] = [node_id]
        prev = node_id
    return build_conversation(conv_id, title, 1785571200.0, mapping,
                              current_node if current_node is not None else prev)


def import_conversations(importer, conversations: list[dict], namespace="factsuser") -> None:
    """打包为 ChatGPT 导出 ZIP 并经真实导入器发布。"""
    zip_path = Path(importer.archive_dir) / "facts-synth.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", _dumps(conversations))
    importer.import_archive(zip_path, namespace)


def _dumps(payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def revision_id_of(conn, text: str) -> str:
    """按原文找 revision_id（合成文本唯一）。"""
    rows = conn.execute(
        "SELECT revision_id FROM event_revisions WHERE raw_text = ?", (text,)
    ).fetchall()
    assert len(rows) == 1, f"期望唯一 revision，得到 {len(rows)}: {text[:20]}"
    return rows[0]["revision_id"]


def make_claim(
    conn,
    content: str,
    memory_type: str = "goal",
    *,
    review_status: str = "pending",
    lifecycle_status: str = "candidate",
    run_id: str | None = None,
    now: str = "2026-10-01T00:00:00.000000Z",
) -> str:
    """直接落一条主张（测试关系/审核/导出用）。返回 claim_id。"""
    import uuid

    claim_id = uuid.uuid4().hex
    rev_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO claims (claim_id, memory_type, lifecycle_status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (claim_id, memory_type, lifecycle_status, now, now),
    )
    conn.execute(
        """
        INSERT INTO claim_revisions (
            claim_revision_id, claim_id, content, memory_type, assertion_kind,
            verification_status, review_status, lifecycle_status, recorded_at,
            extraction_run_id, rule_version
        ) VALUES (?, ?, ?, ?, 'self_report', 'unverified', ?, ?, ?, ?, 'facts-rule-v1')
        """,
        (rev_id, claim_id, content, memory_type, review_status,
         lifecycle_status, now, run_id),
    )
    conn.execute(
        "UPDATE claims SET current_revision_id = ? WHERE claim_id = ?",
        (rev_id, claim_id),
    )
    conn.commit()
    return claim_id


def make_relation(
    conn,
    from_claim: str,
    to_claim: str,
    relation_type: str,
    *,
    origin: str = "llm",
    status: str = "suggested",
    now: str = "2026-10-01T00:00:00.000000Z",
) -> str:
    import uuid

    relation_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO claim_relations (
            relation_id, from_claim_id, to_claim_id, relation_type,
            status, origin, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (relation_id, from_claim, to_claim, relation_type, status, origin, now),
    )
    conn.commit()
    return relation_id


class FakeLLMClient:
    """假提炼客户端：按请求类型返回预置 JSON；不出网。

    提炼请求与关系建议请求分开排队（用户 prompt 含「配对」的是
    关系建议请求，见 prompting.build_relation_prompt）。
    """

    provider_name = "fake"
    model_id = "fake-facts-model"

    def __init__(
        self,
        responses: list[dict] | None = None,
        relation_responses: list[dict] | None = None,
    ) -> None:
        self._responses: list[dict] = list(responses or [])
        self._relation_responses: list[dict] = list(relation_responses or [])
        self.calls: list[tuple[str, str]] = []
        self._prompt_tokens = 0
        self._completion_tokens = 0

    def queue(self, response: dict) -> None:
        self._responses.append(response)

    def queue_relation(self, response: dict) -> None:
        self._relation_responses.append(response)

    def complete_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        self._prompt_tokens += (len(system) + len(user)) // 4
        self._completion_tokens += 20
        if "配对" in user:  # 关系建议请求
            return (
                self._relation_responses.pop(0)
                if self._relation_responses else {"relations": []}
            )
        return self._responses.pop(0) if self._responses else {"claims": []}

    @property
    def usage(self) -> tuple[int, int]:
        return self._prompt_tokens, self._completion_tokens


def claim_of(content: str, memory_type: str, assertion_kind: str,
             revision_id: str, quote: str) -> dict:
    """构造一个 LLM 候选（引用真实 revision 的原话）。"""
    return {
        "content": content,
        "memory_type": memory_type,
        "assertion_kind": assertion_kind,
        "evidence": [{"revision_id": revision_id, "quote": quote}],
    }
