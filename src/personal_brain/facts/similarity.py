"""主张相近度：文本 + 向量（§10.3 duplicate / conflict checks）。

- 归一化后完全一致 → 1.0（确定性，不依赖任何模型）。
- 提供 embedding provider 时计算余弦；未提供（核心安装无可选依赖）
  退化为字符二元组 Jaccard——确定性、可解释，质量下限可接受。
- 阈值语义：``>= dedup_threshold`` 视为同一主张（合并证据）；
  ``>= conflict_threshold`` 且 ``< dedup_threshold`` 视为潜在相关
  （送 LLM 给关系建议，仅建议）。
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

from personal_brain.history.normalization import normalize_search_text


@runtime_checkable
class SimilarityEmbedder(Protocol):
    """最小嵌入契约（复用 retrieval.embeddings 的 provider 即满足）。"""

    model_id: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(max(0, len(text) - 1))} or {text}


def bigram_jaccard(a: str, b: str) -> float:
    """字符二元组 Jaccard：无外部依赖的确定性相近度回退。"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def _cosine(u: list[float], v: list[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v, strict=True))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return dot / (nu * nv)


class ClaimSimilarity:
    """文本精确一致 + 向量/Jaccard 兜底的相近度判定器。"""

    def __init__(self, embedder: SimilarityEmbedder | None = None) -> None:
        self._embedder = embedder

    @property
    def backend(self) -> str:
        return self._embedder.model_id if self._embedder else "bigram-jaccard"

    def similarity(self, a: str, b: str) -> float:
        na, nb = normalize_search_text(a), normalize_search_text(b)
        if na and na == nb:
            return 1.0
        score = bigram_jaccard(na, nb)
        if self._embedder is not None:
            try:
                va, vb = self._embedder.embed([na, nb])
                score = max(score, _cosine(va, vb))
            except Exception:  # noqa: BLE001 - 嵌入失败退回文本相近度
                pass
        return score


def build_local_claim_similarity(model_id: str, cache_dir: str | None) -> ClaimSimilarity:
    """优先用已缓存的本地 FastEmbed；未安装/未缓存时退回文本相似度。

    Facts 命令不触发模型下载，避免在提炼流程中隐式出网。
    """
    try:
        from personal_brain.retrieval.embeddings import FastEmbedProvider

        embedder = FastEmbedProvider(
            model_id=model_id, cache_dir=cache_dir, allow_download=False
        )
    except Exception:  # noqa: BLE001 - 无本地模型/可选依赖时安全退回文本
        embedder = None
    return ClaimSimilarity(embedder)
