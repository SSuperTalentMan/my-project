"""MySQL 访问层（架构文档 §10 代码连接方式）。

- 轻量连接池：``queue.LifoQueue`` + 按需创建，取用前 ``ping(reconnect=True)``，
  失效连接直接丢弃重建，避免长连接被 MySQL 掐断后的「假连接」错误。
- 全部 SQL 走参数化占位符（``%s``），杜绝拼接注入。
- MySQL 不可用时抛 ``MySQLUnavailable``，上层按 §11 降级返回结构化错误，
  **不会**把连接异常直接冒泡成 500。
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pymysql
from pymysql.cursors import DictCursor

from commercepivot.core.config import get_settings
from commercepivot.core.logging import get_logger

log = get_logger("commercepivot.db.mysql")


class MySQLUnavailable(RuntimeError):
    """MySQL 连接不可用。"""


class MySQLPool:
    def __init__(self) -> None:
        s = get_settings()
        self._cfg: Dict[str, Any] = dict(
            host=s.mysql_host,
            port=s.mysql_port,
            user=s.mysql_user,
            password=s.mysql_password,
            database=s.mysql_db,
            charset=s.mysql_charset,
            connect_timeout=s.mysql_connect_timeout,
            read_timeout=30,
            write_timeout=30,
            autocommit=True,
            cursorclass=DictCursor,
        )
        self._max = max(2, s.mysql_pool_size)
        self._idle: "queue.LifoQueue[pymysql.connections.Connection]" = queue.LifoQueue(self._max)
        self._created = 0
        self._lock = threading.Lock()
        self._last_error: Optional[str] = None
        self._checked_at: float = 0.0
        self._ok: Optional[bool] = None

    # ------------------------------------------------------------ 连接管理
    def _new_connection(self) -> pymysql.connections.Connection:
        conn = pymysql.connect(**self._cfg)
        with self._lock:
            self._created += 1
        return conn

    @contextmanager
    def acquire(self) -> Iterator[pymysql.connections.Connection]:
        conn: Optional[pymysql.connections.Connection] = None
        try:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = self._new_connection()
            try:
                conn.ping(reconnect=True)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = self._new_connection()
            yield conn
        except pymysql.MySQLError as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._ok = False
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
            raise MySQLUnavailable(self._last_error) from exc
        finally:
            if conn is not None:
                try:
                    self._idle.put_nowait(conn)
                except queue.Full:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    with self._lock:
                        self._created -= 1

    # ------------------------------------------------------------ 查询接口
    def query(self, sql: str, params: Sequence[Any] | Dict[str, Any] | None = None,
              limit: int | None = None) -> List[Dict[str, Any]]:
        with self.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                rows = list(cur.fetchall()) if cur.description else []
        if limit is not None:
            rows = rows[:limit]
        self._ok = True
        return [_normalize(r) for r in rows]

    def query_one(self, sql: str, params: Sequence[Any] | Dict[str, Any] | None = None
                  ) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params, limit=1)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | Dict[str, Any] | None = None,
               default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    def execute(self, sql: str, params: Sequence[Any] | Dict[str, Any] | None = None) -> int:
        with self.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                affected = cur.rowcount
        self._ok = True
        return int(affected)

    def execute_many(self, sql: str, seq: List[Sequence[Any]]) -> int:
        if not seq:
            return 0
        with self.acquire() as conn:
            with conn.cursor() as cur:
                affected = cur.executemany(sql, seq)
        self._ok = True
        return int(affected or 0)

    def insert(self, sql: str, params: Sequence[Any] | Dict[str, Any] | None = None) -> int:
        with self.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                new_id = cur.lastrowid
        self._ok = True
        return int(new_id or 0)

    def table_exists(self, name: str) -> bool:
        row = self.query_one(
            "SELECT COUNT(*) AS c FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (self._cfg["database"], name),
        )
        return bool(row and row.get("c"))

    # ------------------------------------------------------------ 健康检查
    def health(self, ttl: float = 5.0) -> Dict[str, Any]:
        now = time.time()
        if self._ok is not None and now - self._checked_at < ttl:
            return self._health_payload()
        try:
            self.query_one("SELECT 1 AS ok")
            self._ok = True
            self._last_error = None
        except Exception as exc:
            self._ok = False
            self._last_error = f"{type(exc).__name__}: {exc}"
        self._checked_at = now
        return self._health_payload()

    def _health_payload(self) -> Dict[str, Any]:
        return {
            "component": "mysql",
            "available": bool(self._ok),
            "target": f"{self._cfg['user']}@{self._cfg['host']}:{self._cfg['port']}/{self._cfg['database']}",
            "pool_created": self._created,
            "error": self._last_error,
        }

    @property
    def available(self) -> bool:
        return bool(self._ok)

    def close(self) -> None:
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1


def _normalize(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 datetime / Decimal 转成可 JSON 序列化的原生类型。"""
    import datetime as _dt
    from decimal import Decimal

    out: Dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, _dt.datetime):
            out[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, _dt.date):
            out[k] = v.strftime("%Y-%m-%d")
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (bytes, bytearray)):
            out[k] = v.decode("utf-8", "ignore")
        else:
            out[k] = v
    return out


_POOL: Optional[MySQLPool] = None
_POOL_LOCK = threading.Lock()


def get_mysql() -> MySQLPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = MySQLPool()
    return _POOL


def reset_pool() -> None:
    """单测/重载配置时使用。"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
        _POOL = None


__all__ = ["MySQLPool", "MySQLUnavailable", "get_mysql", "reset_pool"]
