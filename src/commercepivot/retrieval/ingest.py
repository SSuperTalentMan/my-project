"""知识库建库/入库（架构文档 §6 Milvus Lite 集合）。

语料来源：``data/knowledge/*.json``
每条记录形如::

    {"id": "prod-001", "collection": "product_kb", "title": "云感记忆枕",
     "content": "...", "source": "商品详情", "keywords": "枕头,记忆棉,护颈"}

入库双写：
1. Milvus（product_kb / faq_kb）—— 稠密 + 稀疏向量，供 hybrid 检索；
2. MySQL ``knowledge_docs``     —— 文本镜像，供 §11 的 FULLTEXT / LIKE 降级。

长文按标题/段落切块，块间保留 overlap，避免答案被切断在边界上。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from commercepivot.core.config import KNOWLEDGE_DIR, get_settings
from commercepivot.core.logging import get_logger
from commercepivot.retrieval.milvus_client import get_milvus

log = get_logger("commercepivot.retrieval.ingest")

CHUNK_SIZE = 420
CHUNK_OVERLAP = 60


def load_corpus(directory: Path | None = None) -> List[Dict[str, Any]]:
    """读取 data/knowledge 下全部 json 语料文件。"""
    directory = directory or KNOWLEDGE_DIR
    docs: List[Dict[str, Any]] = []
    if not directory.exists():
        log.warning("知识语料目录不存在", directory=str(directory))
        return docs
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("语料文件解析失败", file=path.name, error=str(exc))
            continue
        items = payload.get("documents", payload) if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            item.setdefault("collection", payload.get("collection") if isinstance(payload, dict) else "")
            item.setdefault("source", path.stem)
            docs.append(item)
        log.info("已加载语料文件", file=path.name, count=len(items))
    return docs


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []

    # 先按空行/句号切成语义块，再按长度合并，比硬切更不容易切断语义
    import re

    units = [u.strip() for u in re.split(r"\n{2,}|(?<=[。！？；])", text) if u and u.strip()]
    chunks: List[str] = []
    buf = ""
    for unit in units:
        if len(buf) + len(unit) + 1 <= size:
            buf = f"{buf}{unit}" if not buf else f"{buf}{unit}"
        else:
            if buf:
                chunks.append(buf)
            if len(unit) <= size:
                buf = unit
            else:
                for i in range(0, len(unit), size - overlap):
                    chunks.append(unit[i : i + size])
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def expand_docs(docs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把长文档切成多个 chunk，id 加后缀保证唯一。"""
    out: List[Dict[str, Any]] = []
    for doc in docs:
        chunks = chunk_text(doc.get("content", ""))
        if len(chunks) <= 1:
            out.append({**doc, "content": doc.get("content", "")[:2000]})
            continue
        for idx, chunk in enumerate(chunks):
            out.append({**doc, "id": f"{doc['id']}#{idx + 1}", "content": chunk})
    return out


def build_index(rebuild: bool = False, only_mysql: bool = False) -> Dict[str, Any]:
    """建索引主入口。返回统计信息，供 CLI 打印。"""
    settings = get_settings()
    docs = expand_docs(load_corpus())
    stats: Dict[str, Any] = {
        "documents": len(docs),
        "collections": {},
        "mysql_mirror": 0,
        "milvus": {"available": False, "inserted": 0, "reason": None},
    }
    if not docs:
        stats["error"] = f"未找到语料，请检查 {KNOWLEDGE_DIR}"
        return stats

    by_collection: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs:
        by_collection.setdefault(doc.get("collection") or settings.milvus_collection_faq, []).append(doc)
    stats["collections"] = {k: len(v) for k, v in by_collection.items()}

    # ---------- 1) MySQL 文本镜像（降级检索的数据基础）
    try:
        from commercepivot.db import repository as repo

        stats["mysql_mirror"] = repo.upsert_knowledge_docs(docs)
    except Exception as exc:  # noqa: BLE001
        stats["mysql_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("知识语料写入 MySQL 镜像失败", error=stats["mysql_error"])

    if only_mysql:
        return stats

    # ---------- 2) Milvus 向量索引
    store = get_milvus()
    if not store.available:
        stats["milvus"] = {"available": False, "inserted": 0, "reason": store.reason}
        return stats

    if rebuild:
        store.recreate()

    from commercepivot.retrieval.embedder import encode_documents

    inserted = 0
    for collection, items in by_collection.items():
        texts = [f"{d.get('title', '')} {d.get('content', '')}" for d in items]
        dense, sparse = encode_documents(texts)
        inserted += store.upsert(collection, items, dense, sparse)
    stats["milvus"] = {
        "available": True,
        "inserted": inserted,
        "supports_sparse": store.supports_sparse,
        "mode": store.mode,
        "reason": None,
    }
    return stats


__all__ = ["build_index", "chunk_text", "expand_docs", "load_corpus"]
