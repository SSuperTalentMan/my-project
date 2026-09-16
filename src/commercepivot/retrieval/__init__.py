"""检索层：BGE-M3 向量化 + Milvus 混合检索 + BGE-Reranker 精排。"""

from commercepivot.retrieval.embedder import encode_documents, encode_query, get_embedder
from commercepivot.retrieval.ingest import build_index, load_corpus
from commercepivot.retrieval.milvus_client import get_milvus
from commercepivot.retrieval.reranker import get_reranker, rerank
from commercepivot.retrieval.service import retrieval_status, search_knowledge

__all__ = [
    "build_index",
    "encode_documents",
    "encode_query",
    "get_embedder",
    "get_milvus",
    "get_reranker",
    "load_corpus",
    "rerank",
    "retrieval_status",
    "search_knowledge",
]
