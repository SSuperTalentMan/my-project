"""BGE-M3 向量化（架构文档 §3.6 模型层）。

产出**双路向量**：
- ``dense``：1024 维稠密向量，用于语义召回；
- ``sparse``：lexical weights（词权重稀疏向量），用于关键词精确召回。
两者在 Milvus 里做 hybrid search，再交给 Reranker 精排 —— 这是 BGE-M3 的
标准用法，也是本平台「商品知识 / FAQ / 平台规则」检索质量的主要来源。

降级策略（§11）
--------------
BGE-M3 权重约 2GB，常见于离线/内网环境。因此：
- ``BGE_M3_PATH`` 为空或路径不存在且 ``MODEL_AUTODOWNLOAD=false`` → 直接用
  ``HashingEmbedder``（字符 n-gram 哈希 + TF 次线性缩放 + L2 归一化）。
- 维度固定为 ``MILVUS_DENSE_DIM``（默认 1024，与 BGE-M3 一致），
  保证「有模型/无模型」两种状态下 Milvus 集合 schema 完全一致，
  切换时不需重建索引。
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence

from commercepivot.core.config import get_settings
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import DEGRADED

log = get_logger("commercepivot.retrieval.embedder")

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_SEED = b"commercepivot-bge-fallback-v1"


def _tokens(text: str) -> List[str]:
    """中英混合分词：英文数字按词，中文按单字（后续再做 bigram 组合）。"""
    return _TOKEN_RE.findall(text or "")


def _features(text: str) -> List[str]:
    """特征集合：词 + 中文 bigram + 英文 trigram。"""
    toks = _tokens(text)
    feats = list(toks)
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i + 1]
        if "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff":
            feats.append(a + b)
    low = (text or "").lower()
    for i in range(len(low) - 2):
        chunk = low[i : i + 3]
        if chunk.isascii() and chunk.strip():
            feats.append(chunk)
    return feats


def _hash_index(token: str, dim: int) -> int:
    """确定性哈希：blake2b 而非内置 hash()，保证跨进程/跨重启稳定。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8, key=_SEED).digest()
    return int.from_bytes(digest, "big") % dim


class HashingEmbedder:
    """无模型依赖的降级向量器：哈希词袋 + 次线性 TF + L2 归一化。

    语义能力弱于 BGE-M3，但**确定性、零下载、毫秒级**，
    足以支撑关键词型检索（商品名、SKU、平台规则条款）。
    """

    name = "hashing-fallback"

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def encode_dense(self, texts: Sequence[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            counts = Counter(_features(text))
            for token, cnt in counts.items():
                idx = _hash_index(token, self.dim)
                # 次线性 TF：抑制高频词主导
                vec[idx] += 1.0 + math.log1p(cnt)
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]
            out.append(vec)
        return out

    def encode_sparse(self, texts: Sequence[str]) -> List[Dict[int, float]]:
        out: List[Dict[int, float]] = []
        for text in texts:
            counts = Counter(_tokens(text))
            total = sum(counts.values()) or 1
            sparse: Dict[int, float] = {}
            for token, cnt in counts.items():
                idx = _hash_index(token, 30000)
                sparse[idx] = sparse.get(idx, 0.0) + cnt / total
            out.append(sparse)
        return out


class BgeM3Embedder:
    """基于 FlagEmbedding 的 BGE-M3，稠密 + 稀疏一次前向同时产出。"""

    name = "bge-m3"

    def __init__(self, model_path: str, dim: int = 1024, use_fp16: bool = False) -> None:
        from FlagEmbedding import BGEM3FlagModel  # 延迟导入，避免无谓的 torch 启动开销

        self.dim = dim
        self._model = BGEM3FlagModel(model_path, use_fp16=use_fp16)

    def _encode(self, texts: Sequence[str]) -> Dict[str, Any]:
        return self._model.encode(
            list(texts),
            batch_size=8,
            max_length=1024,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )

    def encode_dense(self, texts: Sequence[str]) -> List[List[float]]:
        result = self._encode(texts)
        return [list(map(float, v)) for v in result["dense_vecs"]]

    def encode_sparse(self, texts: Sequence[str]) -> List[Dict[int, float]]:
        result = self._encode(texts)
        weights = result.get("lexical_weights") or []
        out: List[Dict[int, float]] = []
        for item in weights:
            out.append({int(k): float(v) for k, v in dict(item).items()})
        return out

    def encode_both(self, texts: Sequence[str]) -> tuple[List[List[float]], List[Dict[int, float]]]:
        result = self._encode(texts)
        dense = [list(map(float, v)) for v in result["dense_vecs"]]
        sparse = [
            {int(k): float(v) for k, v in dict(item).items()}
            for item in (result.get("lexical_weights") or [])
        ]
        return dense, sparse


_EMBEDDER: Any = None
_LOCK = threading.Lock()
_LOAD_ATTEMPTED = False


def _path_ready(path: str) -> bool:
    import os

    if not path:
        return False
    if os.path.isdir(path):
        return True
    # 允许传 HF repo id（如 BAAI/bge-m3），此时需要显式允许下载
    return bool(get_settings().model_autodownload and "/" in path)


def get_embedder() -> Any:
    """获取向量化器单例。优先 BGE-M3，不可用则哈希降级。"""
    global _EMBEDDER, _LOAD_ATTEMPTED
    if _EMBEDDER is not None:
        return _EMBEDDER
    with _LOCK:
        if _EMBEDDER is not None:
            return _EMBEDDER
        settings = get_settings()
        path = settings.bge_m3_path.strip()
        if _LOAD_ATTEMPTED:
            _EMBEDDER = HashingEmbedder(settings.milvus_dense_dim)
            return _EMBEDDER
        _LOAD_ATTEMPTED = True
        if _path_ready(path):
            try:
                if settings.hf_endpoint:
                    import os

                    os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
                log.info("正在加载 BGE-M3 向量模型", path=path)
                _EMBEDDER = BgeM3Embedder(path, settings.milvus_dense_dim)
                log.info("BGE-M3 加载完成", dim=_EMBEDDER.dim)
                return _EMBEDDER
            except Exception as exc:  # noqa: BLE001
                log.warning("BGE-M3 加载失败，降级为哈希向量", error=f"{type(exc).__name__}: {exc}")
                DEGRADED.inc({"component": "embedder"})
        else:
            log.info("未配置 BGE_M3_PATH，使用哈希向量降级实现（检索能力受限但可用）")
            DEGRADED.inc({"component": "embedder"})
        _EMBEDDER = HashingEmbedder(settings.milvus_dense_dim)
        return _EMBEDDER


def embedder_status(load: bool = False) -> Dict[str, Any]:
    """向量器状态。

    ``load=False``（默认）时**不触发模型加载** —— /health 这类探活接口不应
    因为一次健康检查就把 2GB 权重量进显存/内存；``load=True`` 时才真正加载。
    """
    settings = get_settings()
    path = settings.bge_m3_path.strip()
    ready = _path_ready(path)
    if _EMBEDDER is not None:
        active, loaded = getattr(_EMBEDDER, "name", "unknown"), True
    elif load:
        active, loaded = getattr(get_embedder(), "name", "unknown"), True
    else:
        active, loaded = ("bge-m3" if ready else "hashing-fallback"), False
    return {
        "component": "embedder",
        "active": active,
        "loaded": loaded,
        "dim": settings.milvus_dense_dim,
        "configured_path": path or None,
        "degraded": active != "bge-m3",
    }


def encode_documents(texts: Sequence[str]) -> tuple[List[List[float]], List[Dict[int, float]]]:
    emb = get_embedder()
    if hasattr(emb, "encode_both"):
        return emb.encode_both(texts)
    return emb.encode_dense(texts), emb.encode_sparse(texts)


def encode_query(text: str) -> tuple[List[float], Dict[int, float]]:
    dense, sparse = encode_documents([text])
    return dense[0], sparse[0]


__all__ = [
    "BgeM3Embedder",
    "HashingEmbedder",
    "embedder_status",
    "encode_documents",
    "encode_query",
    "get_embedder",
]
