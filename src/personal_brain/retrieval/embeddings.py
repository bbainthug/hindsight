"""EmbeddingProvider 契约与本地实现（D-1 / 规格 §103）。

边界（不可妥协）：
- embedding 全程本地，不新增任何出网路径；FastEmbedProvider 首次构建
  会下载模型文件（缓存后离线使用），只在 ``reindex-semantic`` 显式运行时发生，
  查询路径永不下载。
- provider 只接收归一化后的文本（``normalize_search_text`` 产物）；
  命中已知凭据形态的 revision 由索引层拒绝，不进入 provider（§329）。
- HashEmbeddingProvider 仅用于测试与离线基准：确定性、不下载模型、
  由同义词表驱动使「求职/找工作」「远程/在家办公」「简历/CV」向量接近。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

DEFAULT_SEMANTIC_MODEL = "BAAI/bge-small-zh-v1.5"

# 同义词组（首个元素为规范词）。HashEmbeddingProvider 用它把同义变体
# 映射到同一规范成分，使合成评测的语义用例在无模型下载时保持确定性。
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("求职", "找工作", "应聘"),
    ("远程", "在家办公", "居家办公"),
    ("简历", "CV", "resume"),
)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """推理契约（规格 §103）：模型身份与维度由 provider 自行汇报。"""

    model_id: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# 词元化：与 FTS 索引侧同思路——拉丁整词 + CJK 二元组
# ---------------------------------------------------------------------------

_LATIN_WORD = re.compile(r"[a-zA-Z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _LATIN_WORD.findall(text):
        tokens.append(f"w:{word.lower()}")
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(f"c:{run}")
        else:
            for i in range(len(run) - 1):
                tokens.append(f"c:{run[i:i + 2]}")
    return tokens


def _canonical_tokens(text: str) -> list[str]:
    """同义词组规范注入：文本命中任一变体 → 注入规范词成分。"""
    injected: list[str] = []
    for group in SYNONYM_GROUPS:
        canonical = group[0]
        for variant in group:
            lowered = variant.lower()
            if variant in text or (
                not _CJK_RUN.search(variant) and lowered in text.lower()
            ):
                # 规范词整串 + 其二元组；拉丁变体同样注入其词元，保证
                # 「CV」与「简历」共享全部规范成分。
                injected.append(f"s:{canonical}")
                injected.extend(_tokens(canonical))
                break
    return injected


class HashEmbeddingProvider:
    """确定性测试实现：token 哈希桶累积 + L2 归一化，不下载任何模型。

    同一 token 摊到多个桶（hash 探测 3 次）提高密度；同义词组把变体
    映射到规范成分，使「找工作」与「求职进展顺利」共享规范向量分量。
    """

    def __init__(self, dimension: int = 256, probes: int = 3) -> None:
        if dimension < 16:
            raise ValueError("HashEmbeddingProvider dimension 必须 ≥ 16")
        self.model_id = "hash-embedding-test"
        self.dimension = dimension
        self._probes = probes

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dimension
            counts: dict[str, float] = {}
            for token in _tokens(text):
                counts[token] = counts.get(token, 0.0) + 1.0
            for token in _canonical_tokens(text):
                counts[token] = counts.get(token, 0.0) + 2.0
            for token, weight in counts.items():
                self._accumulate(vec, token, weight)
            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0:
                vec = [x / norm for x in vec]
            vectors.append(vec)
        return vectors

    def _accumulate(self, vec: list[float], token: str, weight: float) -> None:
        for probe in range(self._probes):
            digest = hashlib.sha1(f"{probe}:{token}".encode()).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign * weight


class FastEmbedProvider:
    """本地 fastembed 实现（可选依赖）。惰性导入：未安装时构建失败，
    由调用方转为 ``semantic_unavailable`` 退化路径，绝不在查询路径下载模型。
    """

    def __init__(
        self,
        model_id: str = DEFAULT_SEMANTIC_MODEL,
        cache_dir: str | None = None,
        *,
        allow_download: bool = True,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise RuntimeError(f"fastembed 未安装: {exc}") from exc
        self.model_id = model_id
        kwargs: dict = {"model_name": model_id}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        # fastembed resolves a missing model by downloading it.  Query-time
        # construction must never create that network path: only the explicit
        # reindex command may download a model.  fastembed delegates model
        # resolution to Hugging Face, whose offline switch is understood by
        # the downloader across supported versions.
        old_offline = os.environ.get("HF_HUB_OFFLINE")
        if not allow_download:
            os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            self._model = TextEmbedding(**kwargs)
        finally:
            if not allow_download:
                if old_offline is None:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = old_offline
        # 维度探测：以一次本地推理为准，不依赖 fastembed 内部 API 细节
        self.dimension = len(next(iter(self._model.embed(["维度探测"]))))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


def build_provider(
    provider_name: str,
    model_id: str = DEFAULT_SEMANTIC_MODEL,
    cache_dir: str | None = None,
) -> EmbeddingProvider:
    """按名称构建 provider；未知名称抛 ValueError。"""
    if provider_name == "hash":
        return HashEmbeddingProvider()
    if provider_name == "fastembed":
        return FastEmbedProvider(model_id, cache_dir)
    raise ValueError(f"未知 embedding provider: {provider_name}")


# 进程级缓存：MCP 服务常驻时同一模型只加载一次；构建失败也缓存，
# 保证同一进程内退化路径行为一致（不反复尝试）。
_PROVIDER_CACHE: dict[str, FastEmbedProvider | None] = {}


def get_or_build_fastembed(
    model_id: str = DEFAULT_SEMANTIC_MODEL,
    cache_dir: str | None = None,
) -> FastEmbedProvider | None:
    """构建（或取缓存）FastEmbed provider；不可用返回 None（调用方退化）。

    注意：查询路径必须先用 semantic_unavailable_reason 确认索引存在且
    模型一致后再调用本函数——索引由 reindex-semantic 建立时模型文件
    已在本地缓存，因此这里不会触发下载；若索引不存在则不会走到这里。
    """
    cache_key = f"{model_id}\x00{cache_dir or ''}"
    if cache_key in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[cache_key]
    try:
        provider = FastEmbedProvider(model_id, cache_dir, allow_download=False)
    except Exception:  # noqa: BLE001 - ImportError/RuntimeError/模型缺失
        _PROVIDER_CACHE[cache_key] = None
        return None
    _PROVIDER_CACHE[cache_key] = provider
    return provider
