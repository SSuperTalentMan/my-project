"""Milvus 访问层（架构文档 §3.5 数据层）。

支持两种部署形态，由 ``MILVUS_URI`` 自动判断：

===============  ==============================  ==============================
MILVUS_URI       形态                            说明
===============  ==============================  ==============================
``./data/x.db``  Milvus Lite（本地文件内嵌）      架构文档默认形态；Win/macOS/Linux 通用
``http://host:19530``  Milvus Standalone（Docker）   多进程共享 / 大数据量时使用
===============  ==============================  ==============================

> Windows 说明：milvus-lite **3.x** 是纯 Python 重写版，提供 ``py3-none-any``
> wheel，因此在 Windows 上可以正常使用 Milvus Lite。需要注意版本配对：
> milvus-lite 3.x 的 gRPC adapter 会写 ``ShowCollectionsResponse.shards_num``，
> 而该字段到 **pymilvus 2.6.4** 才进入 proto —— 2.5.x 配 3.x 会报
> ``Protocol message ShowCollectionsResponse has no "shards_num" field``。
> 本项目锁定 ``pymilvus==2.6.17`` + ``milvus-lite==3.2.1``。
> 另：``MILVUS_URI`` 用相对路径时，pymilvus 在 import 阶段就会拒绝，
> 所以这里统一传 ``milvus_abs_uri``（绝对路径）。

能力探测
--------
不同形态对 sparse 向量的支持不一致（旧版 Milvus Lite 不支持），因此建集合时
先尝试「dense + sparse」双路 schema，失败则自动降级为 dense-only，
并把 ``supports_sparse`` 标记为 False —— 检索侧据此决定走 hybrid 还是纯向量。

全部不可用时 ``available=False``，检索链路按 §11 转 MySQL FULLTEXT / LIKE。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from commercepivot.core.config import get_settings
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import DEGRADED

log = get_logger("commercepivot.retrieval.milvus")

OUTPUT_FIELDS = ["id", "text", "collection", "title", "source", "keywords"]
MAX_TEXT_LEN = 2000  # VARCHAR 上限 65535 字节，中文 3 字节/字，留足余量


def import_pymilvus():
    """导入 pymilvus，并绕开它的一个自身缺陷。

    ``pymilvus/settings.py`` 在模块顶层执行 ``load_dotenv()``，把项目 ``.env``
    里的 ``MILVUS_URI`` 读进 ``os.environ``，随后在

        Config.MILVUS_URI = os.getenv("MILVUS_URI", LEGACY_URI)

    里当作**连接串**解析，并在 ``orm/connections.py`` 的模块级
    ``connections = Connections()`` 中立即校验。问题是该校验走
    ``urlparse`` 且要求 netloc 非空 —— 本地 ``.db`` 路径（无论相对还是绝对）
    都不满足，于是 ``import pymilvus`` 直接抛
    ``ConnectionConfigException: Illegal uri``。

    我们连接时始终显式传 ``uri=``，并不依赖 pymilvus 的「默认连接」，
    因此在其 import 期间把该变量临时置空即可。注意 pydantic-settings 的
    取值优先级是「环境变量 > .env 文件」，所以这里必须在 ``get_settings()``
    已经取过值之后再执行，避免把 settings 也读成空串。
    """
    import os

    previous = os.environ.get("MILVUS_URI")
    os.environ["MILVUS_URI"] = ""
    try:
        import pymilvus
        from pymilvus import MilvusClient  # noqa: F401
    finally:
        if previous is None:
            os.environ.pop("MILVUS_URI", None)
        else:
            os.environ["MILVUS_URI"] = previous
    return pymilvus


class MilvusStore:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Any = None
        self._lock = threading.Lock()
        self.available = False
        self.supports_sparse = False
        # 按集合分别记录 sparse 能力：两个集合是用同一套 schema 建的，
        # 但历史遗留的集合可能不一致，用单一标志会串味。
        self._sparse_flags: Dict[str, bool] = {}
        self.mode = "unavailable"
        self.reason: Optional[str] = None
        self.dim = self._settings.milvus_dense_dim
        self.collections = (
            self._settings.milvus_collection_product,
            self._settings.milvus_collection_faq,
        )
        self._connect()

    def _sparse_ready(self, collection: str) -> bool:
        """该集合是否可写入 / 可按 sparse 检索。"""
        if collection in self._sparse_flags:
            return self._sparse_flags[collection]
        return self.supports_sparse

    # ------------------------------------------------------------ 连接
    def _connect(self) -> None:
        uri = self._settings.milvus_uri.strip()
        try:
            pymilvus = import_pymilvus()
            MilvusClient = pymilvus.MilvusClient

            if uri.startswith(("http://", "https://")):
                self.mode = "remote"
                self._client = self._make_client(MilvusClient, uri)
            else:
                # Lite 形态的数据目录本身就是一个文件夹。若 milvus_lite 未安装，
                # 直接构造 MilvusClient 会先尝试拉起本地服务再失败（白等约 2.5s），
                # 这里提前 import 一次，让失败路径变成毫秒级。
                import importlib
                import os

                importlib.import_module("milvus_lite")

                self.mode = "lite"
                os.makedirs(os.path.dirname(self._settings.milvus_abs_uri) or ".", exist_ok=True)
                self._client = MilvusClient(uri=self._settings.milvus_abs_uri)
            # 探活：真正发一次请求，避免「建对象成功但连不上」
            self._client.list_collections()
            self.available = True
            self.reason = None
            for name in self.collections:
                self.ensure_collection(name)
            log.info("Milvus 已连接", mode=self.mode, uri=uri, sparse=self.supports_sparse)
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self._client = None
            self.reason = self._explain(exc, uri)
            DEGRADED.inc({"component": "milvus"})
            log.warning("Milvus 不可用，检索将降级为 MySQL FULLTEXT/LIKE", reason=self.reason)

    @staticmethod
    def _make_client(client_cls: Any, uri: str) -> Any:
        """构造远程客户端并传入连接超时（不同 pymilvus 小版本参数名有差异）。"""
        from commercepivot.core.config import get_settings

        timeout = get_settings().milvus_connect_timeout_s
        try:
            return client_cls(uri=uri, timeout=timeout)
        except TypeError:
            return client_cls(uri=uri)

    @staticmethod
    def _explain(exc: Exception, uri: str) -> str:
        text = f"{type(exc).__name__}: {exc}"
        if "milvus_lite" in text or "No module named 'milvus_lite'" in text:
            return (
                "未安装 milvus-lite（Milvus Lite 本地内嵌引擎）。"
                "执行 `uv pip install -r requirements.txt` 或 "
                "`uv pip install milvus-lite==3.2.1` 即可；"
                "也可以改用 Docker 版 Milvus Standalone：把 MILVUS_URI 设为 "
                "http://localhost:19530（docker compose --profile milvus up -d）。"
            )
        if "shards_num" in text:
            return (
                "pymilvus 与 milvus-lite 版本不配对：milvus-lite 3.x 需要 "
                "pymilvus>=2.6.4（proto 才含 ShowCollectionsResponse.shards_num）。"
                "请执行 `uv pip install 'pymilvus==2.6.17' 'milvus-lite==3.2.1'。"
            )
        if uri.startswith(("http://", "https://")):
            return f"无法连接 Milvus Standalone（{uri}）：{text}"
        return text

    # ------------------------------------------------------------ 集合
    def ensure_collection(self, name: str) -> bool:
        if not self.available or self._client is None:
            return False
        with self._lock:
            try:
                if self._client.has_collection(name):
                    self._detect_sparse(name)
                    self._ensure_loaded(name)
                    return True
            except Exception:
                return False
            created = self._create_collection(name, with_sparse=self.supports_sparse or None)
            if created:
                self._ensure_loaded(name)
            return created

    def _ensure_loaded(self, name: str) -> None:
        """确保集合处于 loaded 状态。

        Milvus 的集合必须先 load 才能 search/query。新进程打开一个已存在的
        集合时它是 ``released`` 状态，直接检索会报
        ``Collection 'xxx' is in state 'released'; call load() before search``
        —— 表现为「向量库明明可用，检索却总在走降级」。这里统一补一次 load，
        已加载时该调用是幂等的。
        """
        try:
            self._client.load_collection(name)
        except Exception as exc:  # noqa: BLE001
            log.debug("load_collection 未成功（通常表示已处于 loaded）", collection=name, error=str(exc))

    def _detect_sparse(self, name: str) -> None:
        """从已存在的集合 schema 里探测 sparse 字段。

        这个探测是必需的：sparse 能力只在**建集合**时才知道，而集合是跨进程
        持久化的。新进程遇到已存在的集合若沿用默认值 False，写入时就按
        dense-only 组装数据，会报
        ``Insert missed an field `sparse` to collection``。
        """
        try:
            DataType = import_pymilvus().DataType
            desc = self._client.describe_collection(name)
            fields = desc.get("fields") or []
            has_sparse = any(
                f.get("name") == "sparse" and f.get("type") == DataType.SPARSE_FLOAT_VECTOR
                for f in fields
            )
            self._sparse_flags[name] = has_sparse
            self.supports_sparse = self.supports_sparse or has_sparse
        except Exception as exc:  # noqa: BLE001
            log.warning("sparse 字段探测失败，按 dense-only 处理", collection=name, error=str(exc))

    def _create_collection(self, name: str, with_sparse: Optional[bool]) -> bool:
        DataType = import_pymilvus().DataType

        attempts = [True, False] if with_sparse is None else ([True, False] if with_sparse else [False])
        for use_sparse in attempts:
            try:
                schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
                schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=160)
                schema.add_field("text", DataType.VARCHAR, max_length=65535)
                schema.add_field("dense", DataType.FLOAT_VECTOR, dim=self.dim)
                if use_sparse:
                    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
                schema.add_field("collection", DataType.VARCHAR, max_length=64)
                schema.add_field("title", DataType.VARCHAR, max_length=1024)
                schema.add_field("source", DataType.VARCHAR, max_length=512)
                schema.add_field("keywords", DataType.VARCHAR, max_length=512)

                index_params = self._client.prepare_index_params()
                index_params.add_index(field_name="dense", index_type="FLAT", metric_type="COSINE")
                if use_sparse:
                    index_params.add_index(
                        field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP"
                    )
                self._client.create_collection(
                    collection_name=name, schema=schema, index_params=index_params
                )
                self.supports_sparse = use_sparse
                self._sparse_flags[name] = use_sparse
                log.info("Milvus 集合已创建", collection=name, sparse=use_sparse)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "创建集合失败，尝试降级形态",
                    collection=name, sparse=use_sparse, error=f"{type(exc).__name__}: {exc}",
                )
        return False

    def drop_collection(self, name: str) -> bool:
        if not self.available:
            return False
        try:
            self._client.drop_collection(name)
            self._sparse_flags.pop(name, None)
            return True
        except Exception:
            return False

    def recreate(self) -> None:
        """重建全部集合（build_index 强制刷新时用）。"""
        for name in self.collections:
            self.drop_collection(name)
            self.ensure_collection(name)

    # ------------------------------------------------------------ 写入
    def delete_ids(self, collection: str, ids: Sequence[str]) -> int:
        if not self.available or not ids:
            return 0
        try:
            total = 0
            for i in range(0, len(ids), 500):
                batch = list(ids[i : i + 500])
                self._client.delete(collection_name=collection, ids=batch)
                total += len(batch)
            return total
        except Exception as exc:  # noqa: BLE001
            log.warning("Milvus 删除失败", collection=collection, error=str(exc))
            return 0

    def upsert(
        self,
        collection: str,
        docs: Sequence[Dict[str, Any]],
        dense: Sequence[Sequence[float]],
        sparse: Sequence[Dict[int, float]] | None = None,
    ) -> int:
        """写入/更新文档。先按 id 删除再插入，避免依赖 upsert 的版本差异。"""
        if not self.available or not docs:
            return 0
        self.ensure_collection(collection)
        use_sparse = self._sparse_ready(collection)
        rows: List[Dict[str, Any]] = []
        for i, doc in enumerate(docs):
            row: Dict[str, Any] = {
                "id": str(doc["id"]),
                "text": str(doc.get("content") or doc.get("text") or "")[:MAX_TEXT_LEN],
                "dense": list(dense[i]),
                "collection": str(doc.get("collection", ""))[:64],
                "title": str(doc.get("title", ""))[:300],
                "source": str(doc.get("source", ""))[:200],
                "keywords": str(doc.get("keywords", ""))[:200],
            }
            if use_sparse and sparse is not None:
                row["sparse"] = sparse[i] or {}
            rows.append(row)
        try:
            self.delete_ids(collection, [r["id"] for r in rows])
            inserted = 0
            for i in range(0, len(rows), 200):
                self._client.insert(collection_name=collection, data=rows[i : i + 200])
                inserted += len(rows[i : i + 200])
            try:
                self._client.flush(collection)
            except Exception:
                pass
            return inserted
        except Exception as exc:  # noqa: BLE001
            log.warning("Milvus 写入失败", collection=collection, error=f"{type(exc).__name__}: {exc}")
            return 0

    # ------------------------------------------------------------ 检索
    def search(
        self,
        collection: str,
        dense: Sequence[float],
        sparse: Optional[Dict[int, float]] = None,
        top_k: int = 10,
        expr: Optional[str] = None,
    ) -> Dict[str, Any]:
        """hybrid（dense+sparse，RRF 融合）优先；不支持则纯稠密检索。"""
        if not self.available:
            return {"rows": [], "mode": "unavailable", "error": self.reason}
        self.ensure_collection(collection)

        if sparse and self._sparse_ready(collection):
            try:
                rows = self._hybrid(collection, dense, sparse, top_k, expr)
                return {"rows": rows, "mode": "hybrid"}
            except Exception as exc:  # noqa: BLE001
                log.warning("hybrid_search 失败，退回稠密检索", error=str(exc))

        try:
            rows = self._dense(collection, dense, top_k, expr)
            return {"rows": rows, "mode": "dense"}
        except Exception as exc:  # noqa: BLE001
            return {"rows": [], "mode": "error", "error": f"{type(exc).__name__}: {exc}"}

    def _hybrid(
        self,
        collection: str,
        dense: Sequence[float],
        sparse: Dict[int, float],
        top_k: int,
        expr: Optional[str],
    ) -> List[Dict[str, Any]]:
        pymilvus = import_pymilvus()
        AnnSearchRequest, RRFRanker = pymilvus.AnnSearchRequest, pymilvus.RRFRanker

        reqs = [
            AnnSearchRequest(
                data=[list(dense)], anns_field="dense",
                param={"metric_type": "COSINE"}, limit=max(top_k * 3, 20),
            ),
            AnnSearchRequest(
                data=[dict(sparse)], anns_field="sparse",
                param={"metric_type": "IP"}, limit=max(top_k * 3, 20),
            ),
        ]
        kwargs: Dict[str, Any] = dict(
            collection_name=collection,
            reqs=reqs,
            ranker=RRFRanker(60),
            limit=top_k,
            output_fields=OUTPUT_FIELDS,
        )
        if expr:
            kwargs["filter"] = expr
        raw = self._try_call(self._client.hybrid_search, kwargs)
        return self._normalize(raw, mode="hybrid")

    def _dense(
        self, collection: str, dense: Sequence[float], top_k: int, expr: Optional[str]
    ) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = dict(
            collection_name=collection,
            data=[list(dense)],
            limit=top_k,
            output_fields=OUTPUT_FIELDS,
            search_params={"metric_type": "COSINE"},
        )
        if expr:
            kwargs["filter"] = expr
        raw = self._try_call(self._client.search, kwargs)
        return self._normalize(raw, mode="dense")

    def _try_call(self, fn: Any, kwargs: Dict[str, Any]) -> Any:
        """兼容 pymilvus 各版本 `filter=` / `expr=` 参数差异。"""
        try:
            return fn(**kwargs)
        except TypeError:
            if "filter" in kwargs:
                alt = dict(kwargs)
                alt["expr"] = alt.pop("filter")
                return fn(**alt)
            raise

    @staticmethod
    def _normalize(raw: Any, mode: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        hits = raw[0] if raw and isinstance(raw[0], list) else (raw or [])
        for hit in hits or []:
            entity = hit.get("entity") or {}
            rows.append(
                {
                    "id": str(hit.get("id")),
                    "score": round(float(hit.get("distance") or hit.get("score") or 0.0), 6),
                    "title": entity.get("title") or "",
                    "content": entity.get("text") or "",
                    "collection": entity.get("collection") or "",
                    "source": entity.get("source") or "",
                    "keywords": entity.get("keywords") or "",
                    "retrieval_mode": f"milvus-{mode}",
                    "dense_score": round(float(hit.get("distance")), 6) if mode == "dense" and hit.get("distance") is not None else None,
                }
            )
        return rows

    # ------------------------------------------------------------ 元信息
    def count(self, collection: str) -> int:
        if not self.available:
            return 0
        try:
            stats = self._client.get_collection_stats(collection)
            return int(stats.get("row_count") or 0)
        except Exception:
            return 0

    def list_ids(self, collection: str, limit: int = 500) -> List[str]:
        if not self.available:
            return []
        try:
            res = self._try_call(
                self._client.query,
                dict(collection_name=collection, output_fields=["id"], limit=limit),
            )
            return [str(r.get("id")) for r in res or []]
        except Exception:
            return []

    def status(self) -> Dict[str, Any]:
        return {
            "component": "milvus",
            "available": self.available,
            "mode": self.mode,
            "uri": self._settings.milvus_uri,
            "supports_sparse": self.supports_sparse,
            "sparse_by_collection": dict(self._sparse_flags),
            "dim": self.dim,
            "collections": {
                name: self.count(name) for name in self.collections
            } if self.available else {},
            "degraded": not self.available,
            "reason": self.reason,
        }

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass


_STORE: Optional[MilvusStore] = None
_LOCK = threading.Lock()
_CONNECTED_AT: float = 0.0


def get_milvus() -> MilvusStore:
    global _STORE, _CONNECTED_AT
    if _STORE is None:
        with _LOCK:
            if _STORE is None:
                _STORE = MilvusStore()
                _CONNECTED_AT = time.time()
    return _STORE


def milvus_status_safe() -> Dict[str, Any]:
    """探活友好的状态查询：**已初始化才返回真实状态**，否则只报配置。

    这样 /health 不会因为第一次探活就去建连接（远程 Milvus 不可达时会阻塞
    到连接超时），把「连不连得上」推迟到真正要检索时再判定。
    """
    settings = get_settings()
    if _STORE is not None:
        return _STORE.status()
    uri = settings.milvus_uri
    return {
        "component": "milvus",
        "available": None,
        "mode": "remote" if uri.startswith(("http://", "https://")) else "lite",
        "uri": uri,
        "loaded": False,
        "degraded": None,
        "reason": "尚未初始化（首次知识库检索时建立连接）",
    }


def reset_milvus() -> None:
    global _STORE
    with _LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None


__all__ = ["MilvusStore", "get_milvus", "reset_milvus"]
