"""BGE-Reranker-v2-m3 精排（架构文档 §3.6）。

流程：Milvus hybrid 召回 top_k=10 → Reranker 精排 → 取 rerank_top_k=3。
重排是 RAG 质量的关键一步：向量召回负责「不漏」，Reranker 负责「排序准」。

降级策略（§11）
--------------
``RERANKER_PATH`` 未配置或加载失败时，退化为 ``LexicalReranker``——
用字符 bigram 覆盖率 + 词交集 + 长度惩罚做重排。它没有 cross-encoder 的
语义能力，但能把「问句里的关键词在文档中越全 → 排名越前」这一单调性做对，
实际效果明显优于不做重排。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Sequence, Tuple

from commercepivot.core.config import get_settings
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import DEGRADED
from commercepivot.retrieval.embedder import _features, _tokens

log = get_logger("commercepivot.retrieval.reranker")


class LexicalReranker:
    """无模型降级重排器。"""

    name = "lexical-fallback"

    def rerank(self, query: str, docs: Sequence[Dict[str, Any]], top_k: int = 3) -> List[Dict[str, Any]]:
        q_tokens = set(_tokens(query))
        q_feats = set(_features(query))
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for doc in docs:
            text = f"{doc.get('title', '')} {doc.get('content', '')}"
            d_tokens = set(_tokens(text))
            d_feats = set(_features(text))
            overlap_tok = len(q_tokens & d_tokens) / (len(q_tokens) + 1e-6)
            overlap_feat = len(q_feats & d_feats) / (len(q_feats) + 1e-6)
            exact = 1.0 if query and query in text else 0.0
            # 长度惩罚：同样命中率下更短的文档更可能是答案
            penalty = min(1.0, 300.0 / (len(text) + 1.0)) * 0.1
            score = 0.55 * overlap_tok + 0.30 * overlap_feat + 0.10 * exact + penalty
            item = dict(doc)
            item["rerank_score"] = round(float(min(1.0, score)), 4)
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[: max(1, top_k)]]


class BgeReranker:
    """FlagEmbedding 的 BGE-Reranker-v2-m3 cross-encoder。"""

    name = "bge-reranker-v2-m3"

    def __init__(self, model_path: str, use_fp16: bool = False) -> None:
        from FlagEmbedding import FlagReranker

        self._model = FlagReranker(model_path, use_fp16=use_fp16)

    def rerank(self, query: str, docs: Sequence[Dict[str, Any]], top_k: int = 3) -> List[Dict[str, Any]]:
        if not docs:
            return []
        pairs = [[query, f"{d.get('title', '')} {d.get('content', '')}".strip()] for d in docs]
        scores = self._model.compute_score(pairs, normalize=True)
        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        decorated: List[Dict[str, Any]] = []
        for doc, score in zip(docs, scores):
            item = dict(doc)
            item["rerank_score"] = round(float(score), 4)
            decorated.append(item)
        decorated.sort(key=lambda d: d.get("rerank_score") or 0.0, reverse=True)
        return decorated[: max(1, top_k)]


_RERANKER: Any = None
_LOCK = threading.Lock()
_LOAD_ATTEMPTED = False


def get_reranker() -> Any:
    global _RERANKER, _LOAD_ATTEMPTED
    if _RERANKER is not None:
        return _RERANKER
    with _LOCK:
        if _RERANKER is not None:
            return _RERANKER
        settings = get_settings()
        path = settings.reranker_path.strip()
        if not _LOAD_ATTEMPTED:
            _LOAD_ATTEMPTED = True
            import os

            ready = bool(path) and (os.path.isdir(path) or (settings.model_autodownload and "/" in path))
            if ready:
                try:
                    if settings.hf_endpoint:
                        os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
                    log.info("正在加载 BGE-Reranker-v2-m3", path=path)
                    _RERANKER = BgeReranker(path)
                    log.info("Reranker 加载完成")
                    return _RERANKER
                except Exception as exc:  # noqa: BLE001
                    log.warning("Reranker 加载失败，降级为词法重排", error=f"{type(exc).__name__}: {exc}")
                    DEGRADED.inc({"component": "reranker"})
            else:
                log.info("未配置 RERANKER_PATH，使用词法重排降级实现")
                DEGRADED.inc({"component": "reranker"})
        _RERANKER = LexicalReranker()
        return _RERANKER


def rerank(query: str, docs: Sequence[Dict[str, Any]], top_k: int | None = None) -> List[Dict[str, Any]]:
    top_k = top_k or get_settings().rerank_top_k
    if not docs:
        return []
    return get_reranker().rerank(query, docs, top_k)


def reranker_status(load: bool = False) -> Dict[str, Any]:
    """精排器状态；``load=False`` 时不触发模型加载（同 embedder_status）。"""
    settings = get_settings()
    path = settings.reranker_path.strip()
    import os

    ready = bool(path) and (os.path.isdir(path) or (settings.model_autodownload and "/" in path))
    if _RERANKER is not None:
        active, loaded = getattr(_RERANKER, "name", "unknown"), True
    elif load:
        active, loaded = getattr(get_reranker(), "name", "unknown"), True
    else:
        active, loaded = ("bge-reranker-v2-m3" if ready else "lexical-fallback"), False
    return {
        "component": "reranker",
        "active": active,
        "loaded": loaded,
        "configured_path": path or None,
        "degraded": active != "bge-reranker-v2-m3",
    }


__all__ = [
    "BgeReranker",
    "LexicalReranker",
    "get_reranker",
    "rerank",
    "reranker_status",
]
