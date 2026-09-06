"""档案读取安全测试（§5.1）。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from synthetic import simple_conversation, write_directory, write_zip

from personal_brain.importers.archive import (
    ArchiveRejected,
    ImportLimits,
    compute_archive_sha256,
    open_archive,
)


def _tiny_limits(**kwargs) -> ImportLimits:
    defaults = dict(
        max_total_uncompressed_bytes=10_000,
        max_file_uncompressed_bytes=5_000,
        max_members=50,
        max_compression_ratio=100.0,
    )
    defaults.update(kwargs)
    return ImportLimits(**defaults)


def _conv_zip(path: Path, member: str = "conversations.json") -> Path:
    return write_zip([simple_conversation()], path, member)


class TestPathTraversal:
    def test_parent_directory_member_rejected(self, tmp_path: Path):
        zp = tmp_path / "evil.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("../evil.json", "[]")
        with pytest.raises(ArchiveRejected, match="穿越"):
            open_archive(zp, _tiny_limits())

    def test_absolute_path_member_rejected(self, tmp_path: Path):
        zp = tmp_path / "evil.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("/etc/passwd", "[]")
        with pytest.raises(ArchiveRejected, match="绝对路径"):
            open_archive(zp, _tiny_limits())

    def test_symlink_member_rejected(self, tmp_path: Path):
        zp = tmp_path / "evil.zip"
        info = zipfile.ZipInfo("link.json")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)  # S_IFLNK
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(info, "/etc/passwd")
        with pytest.raises(ArchiveRejected, match="符号链接"):
            open_archive(zp, _tiny_limits())

    def test_escaping_symlink_in_directory_rejected(self, tmp_path: Path):
        src = tmp_path / "export"
        src.mkdir()
        (src / "conversations.json").write_text(
            json.dumps([simple_conversation()]), encoding="utf-8"
        )
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret", encoding="utf-8")
        (src / "leak.json").symlink_to(secret)
        with pytest.raises(ArchiveRejected, match="逃逸|穿越"):
            open_archive(src, _tiny_limits())

    def test_prefix_sharing_sibling_symlink_rejected(self, tmp_path: Path):
        """回归（验收缺陷 2）：兄弟目录名共享前缀（export-evil vs export）
        时，字符串 startswith 前缀匹配曾把逃逸误判为根内。"""
        src = tmp_path / "export"
        src.mkdir()
        (src / "conversations.json").write_text(
            json.dumps([simple_conversation()]), encoding="utf-8"
        )
        evil = tmp_path / "export-evil"
        evil.mkdir()
        (evil / "secret-data.json").write_text("escaped content", encoding="utf-8")
        (src / "leak.json").symlink_to(evil / "secret-data.json")
        with pytest.raises(ArchiveRejected, match="逃逸"):
            open_archive(src, _tiny_limits())

    def test_inside_root_symlink_allowed(self, tmp_path: Path):
        """根内指向根内的符号链接是合法别名，不拒绝。"""
        src = tmp_path / "export"
        (src / "sub").mkdir(parents=True)
        real = src / "conversations.json"
        real.write_text(json.dumps([simple_conversation()]), encoding="utf-8")
        link = src / "sub" / "alias.json"
        link.symlink_to(real)
        reader = open_archive(src, _tiny_limits())
        names = {m.member_path for m in reader.members()}
        assert "conversations.json" in names
        assert "sub/alias.json" in names


class TestResourceLimits:
    def test_total_size_limit(self, tmp_path: Path):
        zp = tmp_path / "big.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("conversations.json", json.dumps([simple_conversation()]))
            # 近乎不可压缩的填充：确保触发总量守卫而非压缩比守卫
            import os

            zf.writestr("pad.json", os.urandom(10_000).hex())
        with pytest.raises(ArchiveRejected, match="总量"):
            open_archive(zp, _tiny_limits(max_file_uncompressed_bytes=50_000))

    def test_member_count_limit(self, tmp_path: Path):
        zp = tmp_path / "many.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            for i in range(60):
                zf.writestr(f"f{i:03}.json", "[]")
        with pytest.raises(ArchiveRejected, match="成员数"):
            open_archive(zp, _tiny_limits())

    def test_compression_ratio_limit(self, tmp_path: Path):
        zp = tmp_path / "bomb.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zero.json", "0" * 50_000)  # 压缩比远超 100
        with pytest.raises(ArchiveRejected, match="压缩比"):
            open_archive(zp, _tiny_limits(max_file_uncompressed_bytes=100_000))


class TestInputTypes:
    def test_zip_and_directory_same_content(self, tmp_path: Path):
        payload = [simple_conversation()]
        zp = write_zip(payload, tmp_path / "a.zip")
        dp = write_directory(payload, tmp_path / "dir")
        za, da = open_archive(zp, _tiny_limits()), open_archive(dp, _tiny_limits())
        assert len(za.members()) == len(da.members()) == 1

    def test_directory_content_hash_stable(self, tmp_path: Path):
        """相同内容的目录两次摘要一致（幂等导入的前提）。"""
        payload = [simple_conversation()]
        d1 = write_directory(payload, tmp_path / "d1")
        d2 = write_directory(payload, tmp_path / "d2")
        assert compute_archive_sha256(d1) == compute_archive_sha256(d2)

    def test_different_content_different_hash(self, tmp_path: Path):
        d1 = write_directory([simple_conversation()], tmp_path / "d1")
        d2 = write_directory([simple_conversation(), simple_conversation()], tmp_path / "d2")
        assert compute_archive_sha256(d1) != compute_archive_sha256(d2)

    def test_unsupported_input_rejected(self, tmp_path: Path):
        p = tmp_path / "not-archive.txt"
        p.write_text("hello", encoding="utf-8")
        with pytest.raises(ArchiveRejected, match="不支持的输入类型"):
            open_archive(p, _tiny_limits())
