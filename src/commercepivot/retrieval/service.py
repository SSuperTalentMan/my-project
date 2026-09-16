"""RAG 检索编排（架构文档 §3.3 知识库 RAG Agent 的数据面）。

一次检索的完整链路：
    问题 → BGE-M3 稠密+稀疏 → Milvus hybrid 召回 top_k_recall
         → BGE-Reranker-v2-m3 精排 → top_k
    任一环节不可用 → 自动降级到 MySQL FULLTEXT / LIKE（§11）

``summary`` 里回传 ``stages``，把「召回数量、召回模式、重排器、耗时」
暴露给上层，这样 A2A Agent 与最终回答都能说明「这条结论是怎么来的」，
而不是只给一句不可解释的答案。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence

from commercepivot.core.config import get_settings
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import DEGRADED, RETRIEVAL_CALLS
from commercepivot.retrieval.embedder import embedder_status
from commercepivot.retrieval.milvus_client import get_milvus
from commercepivot.retrieval.reranker import rerank, reranker_status

log = get_logger("commercepivot.retrieval.service")

MYSQL_FALLBACK_HINT = (
    "向量检索不可用，已降级为 MySQL 全文/LIKE 检索；结果相关性会低于混合检索。"
)


async def search_knowledge(
    query: str,
    top_k: int = 5,
    collection: str | None = None,
    use_rerank: bool = True,
    min_score: float | None = None,
) -> Dict[str, Any]:
    settings = get_settings()
    started = time.perf_counter()
    query = (query or "").strip()
    if not query:
        return {"rows": [], "row_count": 0, "summary": {"query": query, "error": "查询为空"}}

    store = get_milvus()
    stages: Dict[str, Any] = {
        "vector_available": store.available,
        "embedder": getattr(_embedder_name_safe(), "name", None),
    }

    if store.available:
        try:
            recall_k = max(top_k * 3, settings.retrieval_top_k)
            candidates = await asyncio.to_thread(
                _vector_recall, query, store, recall_k, collection
            )
            stages["recall_count"] = len(candidates)
            stages["recall_mode"] = candidates[0].get("retrieval_mode") if candidates else "milvus-empty"

            if not candidates:
                raise RuntimeError("Milvus 召回为空，转入 MySQL 降级")

            if use_rerank and candidates:
                rows = await asyncio.to_thread(rerank, query, candidates, top_k)
                stages["reranker"] = reranker_status()["active"]
            else:
                rows = candidates[:top_k]
                stages["reranker"] = "disabled"

            if min_score is not None:
                rows = [r for r in rows if (r.get("rerank_score") or r.get("score") or 0) >= min_score]

            elapsed = int((time.perf_counter() - started) * 1000)
            RETRIEVAL_CALLS.inc({"mode": str(stages.get("recall_mode", "milvus"))})
            return {
                "rows": rows,
                "row_count": len(rows),
                "degraded": False,
                "summary": {
                    "query": query,
                    "top_k": top_k,
                    "retrieval_mode": stages.get("recall_mode"),
                    "degraded": False,
                    "elapsed_ms": elapsed,
                    "stages": stages,
                },
            }
        except Exception as exc:  # noqa: BLE001
            stages["vector_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("向量检索失败，降级为 MySQL 检索", error=stages["vector_error"])
            DEGRADED.inc({"component": "retrieval"})

    # ---------------------------------------------------------- MySQL 降级
    return await _mysql_fallback(query, top_k, collection, stages, started)


async def _mysql_fallback(
    query: str,
    top_k: int,
    collection: str | None,
    stages: Dict[str, Any],
    started: float,
) -> Dict[str, Any]:
    from commercepivot.db import repository as repo
    from commercepivot.db.mysql import MySQLUnavailable

    try:
        payload = await asyncio.to_thread(repo.search_knowledge_like, query, top_k, collection)
    except MySQLUnavailable as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        stages["mysql_error"] = str(exc)
        RETRIEVAL_CALLS.inc({"mode": "unavailable"})
        return {
            "rows": [],
            "row_count": 0,
            "degraded": True,
            "degrade_reason": "Milvus 与 MySQL 均不可用，无法检索知识库",
            "summary": {
                "query": query,
                "top_k": top_k,
                "retrieval_mode": "unavailable",
                "degraded": True,
                "elapsed_ms": elapsed,
                "stages": stages,
            },
        }

    rows = payload.get("rows", [])
    # 降级路径没有语义分，用词法重排补一层排序，避免 LIKE 命中顺序即结果顺序
    if rows:
        try:
            rows = await asyncio.to_thread(rerank, query, rows, top_k)
        except Exception:  # noqa: BLE001
            rows = rows[:top_k]
    elapsed = int((time.perf_counter() - started) * 1000)
    stages["recall_count"] = len(payload.get("rows", []))
    stages["recall_mode"] = payload.get("mode", "like")
    stages["reranker"] = "lexical"
    RETRIEVAL_CALLS.inc({"mode": f"mysql-{payload.get('mode', 'like')}"})
    return {
        "rows": rows,
        "row_count": len(rows),
        "degraded": True,
        "degrade_reason": MYSQL_FALLBACK_HINT,
        "summary": {
            "query": query,
            "top_k": top_k,
            "retrieval_mode": f"mysql-{payload.get('mode', 'like')}",
            "degraded": True,
            "elapsed_ms": elapsed,
            "stages": stages,
        },
    }


def _vector_recall(
    query: str, store: Any, recall_k: int, collection: str | None
) -> List[Dict[str, Any]]:
    from commercepivot.retrieval.embedder import encode_query

    dense, sparse = encode_query(query)
    targets = [collection] if collection else list(store.collections)
    merged: List[Dict[str, Any]] = []
    for name in targets:
        result = store.search(name, dense, sparse if store.supports_sparse else None, recall_k)
        if result.get("mode") in ("error", "unavailable"):
            raise RuntimeError(result.get("error") or "Milvus 检索失败")
        merged += result.get("rows", [])
    merged.sort(key=lambda r: r.get("score") or 0.0, reverse=True)
    return merged[:recall_k]


def _embedder_name_safe() -> Any:
    class _N:
        name = None

    try:
        from commercepivot.retrieval import embedder as emb

        return emb.get_embedder()
    except Exception:  # noqa: BLE001
        return _N()


def retrieval_status(load_models: bool = False) -> Dict[str, Any]:
    from commercepivot.retrieval.milvus_client import milvus_status_safe

    return {
        "embedder": embedder_status(load=load_models),
        "reranker": reranker_status(load=load_models),
        "milvus": milvus_status_safe(),
        "fallback": "mysql-fulltext/like",
    }


__all__ = ["MYSQL_FALLBACK_HINT", "retrieval_status", "search_knowledge"]
