"""导出档案读取：ZIP / 已解压目录，带安全限制（§5.1）。

- 解压前限制大小、文件数和压缩展开量；超限整档拒绝。
- 拒绝目录穿越（绝对路径、``..``、盘符、NUL）及符号链接成员。
- 成员内容按上限流式读取，不做全量解压落盘；原始归档字节另行原样暂存。
- 所有内容作为不可信数据处理，不执行任何导出内指令。
"""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ArchiveRejected(Exception):
    """归档被安全检查拒绝。"""


@dataclass(frozen=True)
class ImportLimits:
    max_total_uncompressed_bytes: int = 4 * 1024**3
    max_file_uncompressed_bytes: int = 1 * 1024**3
    max_members: int = 200_000
    max_compression_ratio: float = 500.0


@dataclass(frozen=True)
class ArchiveMember:
    member_path: str  # 归档内相对路径（正斜杠规范）
    size_bytes: int
    sha256: str

    def read_bytes_capped(self, limit: ImportLimits) -> bytes:
        raise NotImplementedError


class ArchiveReader:
    """统一 ZIP 与目录两种输入。"""

    def __init__(self, root: Path, limits: ImportLimits) -> None:
        self.root = root
        self.limits = limits

    def members(self) -> list[ArchiveMember]:
        raise NotImplementedError

    def open_member(self, member_path: str) -> bytes:
        """按上限读取成员内容；超限抛 ArchiveRejected。"""
        raise NotImplementedError


def _validate_member_name(name: str) -> None:
    if not name or "\x00" in name:
        raise ArchiveRejected(f"非法成员名: {name!r}")
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or name.startswith("/") or name.startswith("~"):
        raise ArchiveRejected(f"绝对路径成员: {name!r}")
    if ":" in name.split("/", 1)[0] and len(name.split("/", 1)[0]) == 2:
        raise ArchiveRejected(f"疑似盘符路径成员: {name!r}")
    for part in p.parts:
        if part in ("..", "."):
            raise ArchiveRejected(f"目录穿越成员: {name!r}")


def _total_size_guard(sizes: list[int], limits: ImportLimits) -> None:
    if len(sizes) > limits.max_members:
        raise ArchiveRejected(f"成员数超限: {len(sizes)} > {limits.max_members}")
    if sum(sizes) > limits.max_total_uncompressed_bytes:
        raise ArchiveRejected("解压总量超限")


class ZipArchiveReader(ArchiveReader):
    def __init__(self, root: Path, limits: ImportLimits) -> None:
        super().__init__(root, limits)
        self._zf = zipfile.ZipFile(root)
        self._infos: dict[str, zipfile.ZipInfo] = {}
        sizes: list[int] = []
        for info in self._zf.infolist():
            if info.is_dir():
                continue
            _validate_member_name(info.filename)
            # 拒绝符号链接成员（unix 外部属性高 16 位文件类型为 S_IFLNK）
            if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
                raise ArchiveRejected(f"符号链接成员: {info.filename!r}")
            if info.file_size > limits.max_file_uncompressed_bytes:
                raise ArchiveRejected(f"成员过大: {info.filename!r}")
            if info.compress_size > 0 and (
                info.file_size / info.compress_size > limits.max_compression_ratio
            ):
                raise ArchiveRejected(f"压缩比异常: {info.filename!r}")
            self._infos[info.filename] = info
            sizes.append(info.file_size)
        _total_size_guard(sizes, limits)

    def members(self) -> list[ArchiveMember]:
        result = []
        for name, info in self._infos.items():
            h = hashlib.sha256()
            with self._zf.open(info) as fh:
                remaining = self.limits.max_file_uncompressed_bytes
                while chunk := fh.read(min(1024 * 1024, remaining)):
                    h.update(chunk)
                    remaining -= len(chunk)
            result.append(ArchiveMember(name, info.file_size, h.hexdigest()))
        return result

    def open_member(self, member_path: str) -> bytes:
        info = self._infos.get(member_path)
        if info is None:
            raise KeyError(member_path)
        if info.file_size > self.limits.max_file_uncompressed_bytes:
            raise ArchiveRejected(f"成员过大: {member_path!r}")
        data = self._zf.read(info)
        if len(data) != info.file_size:
            raise ArchiveRejected(f"成员大小不一致: {member_path!r}")
        return data


class DirectoryArchiveReader(ArchiveReader):
    def __init__(self, root: Path, limits: ImportLimits) -> None:
        super().__init__(root, limits)
        root_real = root.resolve()
        self._files: dict[str, Path] = {}
        sizes: list[int] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # 拒绝逃逸根目录的符号链接目录
            dirnames[:] = [
                d
                for d in dirnames
                if not (Path(dirpath) / d).is_symlink()
            ]
            for fn in filenames:
                fp = Path(dirpath) / fn
                if fp.is_symlink():
                    target = fp.resolve()
                    if not str(target).startswith(str(root_real)):
                        raise ArchiveRejected(f"符号链接逃逸: {fn!r}")
                if not fp.is_file():
                    continue
                rel = fp.relative_to(root).as_posix()
                _validate_member_name(rel)
                size = fp.stat().st_size
                if size > limits.max_file_uncompressed_bytes:
                    raise ArchiveRejected(f"文件过大: {rel!r}")
                self._files[rel] = fp
                sizes.append(size)
        _total_size_guard(sizes, limits)

    def members(self) -> list[ArchiveMember]:
        result = []
        for rel, fp in self._files.items():
            h = hashlib.sha256()
            with fp.open("rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    h.update(chunk)
            result.append(ArchiveMember(rel, fp.stat().st_size, h.hexdigest()))
        return result

    def open_member(self, member_path: str) -> bytes:
        fp = self._files.get(member_path)
        if fp is None:
            raise KeyError(member_path)
        return fp.read_bytes()


def open_archive(path: Path, limits: ImportLimits | None = None) -> ArchiveReader:
    """按输入类型选择读取器（按结构处理，不按扩展名猜测）。"""
    limits = limits or ImportLimits()
    if not path.exists():
        raise FileNotFoundError(str(path))
    if zipfile.is_zipfile(path):
        return ZipArchiveReader(path, limits)
    if path.is_dir():
        return DirectoryArchiveReader(path, limits)
    raise ArchiveRejected(f"不支持的输入类型: {path}")


def compute_archive_sha256(path: Path) -> str:
    """原始归档字节摘要（ZIP 为文件字节；目录为排序后的 名称+内容 规范摘要）。"""
    if path.is_file():
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()
    # 目录输入：按成员路径排序做规范摘要，保证相同内容得到相同摘要
    reader = DirectoryArchiveReader(path, ImportLimits())
    h = hashlib.sha256()
    for member in sorted(reader.members(), key=lambda m: m.member_path):
        h.update(member.member_path.encode("utf-8"))
        h.update(b"\x00")
        h.update(bytes.fromhex(member.sha256))
        h.update(b"\x00")
    return h.hexdigest()
