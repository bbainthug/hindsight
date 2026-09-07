"""ChatGPT 导入编排（§5.2 可恢复导入）。

流程：
1. 校验输入与账号别名；读取归档（安全限制）并计算原始字节摘要。
2. 相同摘要快照已存在 → 幂等快路径：0 新增，直接返回。
3. 原始归档暂存到归档目录（临时文件 + 原子改名），失败不发布成功状态。
4. 在暂存数据上解析，统计支持与不支持的记录。
5. 单一数据库事务内发布：快照、事件、版本、出现位置、图、所选路径。
6. 任何一步失败：作业标记 failed，不发布；重新导入同一归档可完整恢复。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from personal_brain.history.store import derive_snapshot_id, derive_source_id, publish_snapshot
from personal_brain.history.timeutil import ParsedTime, utc_now_iso
from personal_brain.importers.archive import (
    ArchiveReader,
    ImportLimits,
    compute_archive_sha256,
    open_archive,
)
from personal_brain.importers.chatgpt import (
    PARSER_VERSION,
    find_conversation_members,
    parse_conversation,
)
from personal_brain.importers.models import (
    IncomingConversation,
    JobError,
    MemberManifest,
)

SOURCE_KIND = "chatgpt"
_NAMESPACE_ALPHABET = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


@dataclass
class ImportResult:
    job_kind: str  # published | duplicate | failed
    snapshot_id: str
    source_id: str
    conversations_seen: int
    events_new: int
    events_updated: int
    revisions_new: int
    occurrences_new: int
    nodes_new: int
    unsupported_count: int
    parse_status: str
    errors: list[JobError]
    archive_relative_path: str


def validate_namespace(account_namespace: str) -> str:
    """账号别名：稳定、非敏感、不依赖文件名（§4.1）。"""
    ns = account_namespace.strip()
    if not ns or not set(ns) <= _NAMESPACE_ALPHABET or len(ns) > 64:
        raise ValueError(
            "account_namespace 仅允许字母、数字、下划线、连字符，长度 1-64"
        )
    return ns


class ChatGPTImporter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        archive_dir: Path,
        limits: ImportLimits | None = None,
    ) -> None:
        self.conn = conn
        self.archive_dir = Path(archive_dir)
        self.limits = limits or ImportLimits()

    def import_archive(self, input_path: Path, account_namespace: str) -> ImportResult:
        ns = validate_namespace(account_namespace)
        input_path = Path(input_path)
        archive_sha256 = compute_archive_sha256(input_path)
        source_id = derive_source_id(SOURCE_KIND, ns)
        snapshot_id = derive_snapshot_id(source_id, archive_sha256)
        archive_relative_path = f"{SOURCE_KIND}/{ns}/{archive_sha256}"

        existing = self.conn.execute(
            "SELECT 1 FROM source_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if existing is not None:
            # 相同快照重复导入：0 新增（§5.3），不重复触发提炼；
            # 仍记录零计数作业以满足导入去重的可观测性（§18）。
            self._stage_archive(input_path, archive_relative_path)
            recorded_at = utc_now_iso()
            import uuid as _uuid

            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """
                INSERT INTO import_jobs (
                    job_id, source_id, archive_sha256, status, started_at, finished_at,
                    conversations_seen, events_new, events_updated, revisions_new,
                    occurrences_new, nodes_new, unsupported_count
                ) VALUES (?, ?, ?, 'published', ?, ?, 0, 0, 0, 0, 0, 0, 0)
                """,
                (_uuid.uuid4().hex, source_id, archive_sha256, recorded_at, recorded_at),
            )
            self.conn.commit()
            return ImportResult(
                job_kind="duplicate",
                snapshot_id=snapshot_id,
                source_id=source_id,
                conversations_seen=0,
                events_new=0,
                events_updated=0,
                revisions_new=0,
                occurrences_new=0,
                nodes_new=0,
                unsupported_count=0,
                parse_status="complete",
                errors=[],
                archive_relative_path=archive_relative_path,
            )

        reader = open_archive(input_path, self.limits)
        self._stage_archive(input_path, archive_relative_path)

        recorded_at = utc_now_iso()
        errors: list[JobError] = []
        try:
            (
                conversations,
                parse_status,
                coverage_start,
                coverage_end,
                coverage_notes,
                member_manifest,
            ) = self._parse(reader, errors)
        except Exception:
            self._mark_failed(source_id, ns, archive_sha256, recorded_at)
            raise

        try:
            result = publish_snapshot(
                self.conn,
                source_kind=SOURCE_KIND,
                account_namespace=ns,
                archive_sha256=archive_sha256,
                archive_relative_path=archive_relative_path,
                parser_version=PARSER_VERSION,
                recorded_at=recorded_at,
                conversations=conversations,
                errors=errors,
                parse_status=parse_status,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                coverage_notes=coverage_notes,
                member_manifest=member_manifest,
            )
        except BaseException:
            # 发布阶段失败同样不发布成功状态（§5.2 步骤 2）
            self._mark_failed(source_id, ns, archive_sha256, recorded_at)
            raise
        counts = result.counts
        return ImportResult(
            job_kind="published",
            snapshot_id=result.snapshot_id,
            source_id=result.source_id,
            conversations_seen=counts.conversations_seen,
            events_new=counts.events_new,
            events_updated=counts.events_updated,
            revisions_new=counts.revisions_new,
            occurrences_new=counts.occurrences_new,
            nodes_new=counts.nodes_new,
            unsupported_count=counts.unsupported_count,
            parse_status=parse_status,
            errors=errors,
            archive_relative_path=archive_relative_path,
        )

    # ------------------------------------------------------------------

    def _parse(
        self, reader: ArchiveReader, errors: list[JobError]
    ) -> tuple[
        list[IncomingConversation],
        str,
        str | None,
        str | None,
        str | None,
        list[MemberManifest],
    ]:
        """在暂存数据上解析；返回 (对话, parse_status, coverage, notes, 成员清单)。"""
        manifest: list[MemberManifest] = []
        raw_members: list[tuple[str, bytes]] = []
        for member in reader.members():
            is_json = member.member_path.lower().endswith(".json")
            kind = "other"
            if not is_json:
                # 非 JSON 成员（官方导出的媒体 .dat / chat.html 等）：只留
                # manifest 溯源，不读入内存——真实导出媒体可达 GB 级（决策 35）。
                if member.member_path.lower().endswith(".dat"):
                    kind = "attachment"
                manifest.append(
                    MemberManifest(
                        member_path=member.member_path,
                        sha256=member.sha256,
                        size_bytes=member.size_bytes,
                        kind=kind,
                    )
                )
                continue
            manifest.append(
                MemberManifest(
                    member_path=member.member_path,
                    sha256=member.sha256,
                    size_bytes=member.size_bytes,
                    kind="other",
                )
            )
            try:
                data = reader.open_member(member.member_path)
            except Exception as exc:  # 单成员读取失败 → partial，不中断
                errors.append(
                    JobError(
                        code="MEMBER_READ_FAILED",
                        locator=member.member_path,
                        message=str(exc),
                    )
                )
                continue
            raw_members.append((member.member_path, data))

        conv_members, detect_errors = find_conversation_members(raw_members)
        errors.extend(detect_errors)
        if not conv_members:
            raise ValueError("未找到对话数据（NO_CONVERSATION_DATA）")
        conv_paths = {p for p, _ in conv_members}
        for m in manifest:
            if m.member_path in conv_paths:
                m.kind = "conversation_data"

        conversations: list[IncomingConversation] = []
        conversation_failures = 0
        for member_path, data in conv_members:
            try:
                value = json.loads(data)
            except json.JSONDecodeError as exc:
                errors.append(
                    JobError(code="MEMBER_NOT_JSON", locator=member_path, message=str(exc))
                )
                conversation_failures += 1
                continue
            items = value if isinstance(value, list) else value.get("conversations", [])
            for idx, raw in enumerate(items):
                locator = f"{member_path}#[{idx}]"
                if not isinstance(raw, dict):
                    errors.append(
                        JobError(
                            code="UNSUPPORTED_CONVERSATION",
                            locator=locator,
                            message="对话条目不是对象",
                        )
                    )
                    conversation_failures += 1
                    continue
                try:
                    conv = parse_conversation(raw, member_path, errors)
                except Exception as exc:  # 单对话失败 → partial
                    errors.append(
                        JobError(
                            code="CONVERSATION_PARSE_FAILED",
                            locator=locator,
                            message=str(exc),
                        )
                    )
                    conversation_failures += 1
                    continue
                if conv is not None:
                    conversations.append(conv)
                else:
                    conversation_failures += 1

        parse_status = "complete"
        notes: str | None = None
        _partial_codes = (
            "UNSUPPORTED_CONTENT",
            "UNSUPPORTED_NODE",
            "MEMBER_READ_FAILED",
            "MEMBER_NOT_JSON",
        )
        if conversation_failures or any(e.code in _partial_codes for e in errors):
            parse_status = "partial"
            notes = f"存在未完全解析的结构；对话失败数={conversation_failures}"

        coverage_start, coverage_end = self._coverage(conversations)
        return conversations, parse_status, coverage_start, coverage_end, notes, manifest

    @staticmethod
    def _coverage(
        conversations: list[IncomingConversation],
    ) -> tuple[str | None, str | None]:
        """已知消息时间边界；未知时间不参与，也不冒充覆盖（§4.1/§4.4）。"""
        known = [
            t.utc_iso
            for conv in conversations
            for node in conv.nodes
            for t in (node.created,)
            if t is not None and t.is_known and t.utc_iso is not None
        ]
        if not known:
            return None, None
        return min(known), max(known)

    def _stage_archive(self, input_path: Path, archive_relative_path: str) -> None:
        """原始归档暂存：临时文件 + 原子改名（§5.2 步骤 2）。"""
        dest = self.archive_dir / archive_relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f".tmp-{id(self)}")
        try:
            if input_path.is_dir():
                if tmp.exists():
                    shutil.rmtree(tmp)
                shutil.copytree(input_path, tmp)
            else:
                shutil.copy2(input_path, tmp)
            if dest.exists():
                # 幂等：同摘要归档已存在，清理临时副本即可
                if dest.is_dir():
                    shutil.rmtree(tmp)
                else:
                    tmp.unlink()
            else:
                tmp.replace(dest)  # 同一文件系统上原子
        finally:
            if tmp.exists():
                if tmp.is_dir():
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    tmp.unlink(missing_ok=True)

    def _mark_failed(
        self, source_id: str, account_namespace: str, archive_sha256: str, recorded_at: str
    ) -> None:
        """失败不发布成功状态：仅留下 failed 作业记录，不产生可见快照。"""
        import uuid as _uuid

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # 作业行引用来源；失败导入可能发生在来源行创建前，先补身份行
            self.conn.execute(
                """
                INSERT INTO sources (source_id, source_kind, account_namespace, status, created_at)
                VALUES (?, ?, ?, 'active', ?)
                ON CONFLICT (source_id) DO NOTHING
                """,
                (source_id, SOURCE_KIND, account_namespace, recorded_at),
            )
            self.conn.execute(
                """
                INSERT INTO import_jobs (
                    job_id, source_id, archive_sha256, status, started_at, finished_at
                ) VALUES (?, ?, ?, 'failed', ?, ?)
                """,
                (_uuid.uuid4().hex, source_id, archive_sha256, recorded_at, utc_now_iso()),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return
        self.conn.commit()


def staged_time(value: object) -> ParsedTime | None:
    """测试与调试辅助：暴露 unix 时间解析。"""
    from personal_brain.history.timeutil import parse_unix_seconds

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        return parse_unix_seconds(value)
    return None
