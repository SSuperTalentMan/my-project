"""Redis 访问层（架构文档 §3.5 / §11）。

职责：会话上下文、问答缓存、限流计数、Agent Card 缓存。

降级策略是本模块的核心：Redis 是**可选加速件**，不是数据源。因此
``SafeRedis`` 把真正客户端包一层，任何连接/命令异常都吞掉并返回
「安全默认值」（get→None、incr→0、set→False ...），使限流 fail-open、
缓存 miss 走全链路、会话退化为进程内字典，业务不中断。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

from commercepivot.core.config import get_settings
from commercepivot.core.logging import get_logger

log = get_logger("commercepivot.db.redis")

try:  # pragma: no cover
    import redis as _redis

    _HAS_REDIS = True
except Exception:  # pragma: no cover
    _redis = None  # type: ignore[assignment]
    _HAS_REDIS = False

# 命令 -> 降级返回值
_DEFAULTS: Dict[str, Any] = {
    "get": None, "set": False, "setex": False, "setnx": False, "delete": 0,
    "exists": 0, "incr": 0, "incrby": 0, "expire": False, "ttl": -1,
    "hget": None, "hset": 0, "hgetall": {}, "hdel": 0, "lpush": 0,
    "lrange": [], "llen": 0, "sadd": 0, "smembers": set(), "ping": False,
    "flushdb": True, "keys": [], "mget": [], "type": "none",
}


class SafeRedis:
    """带 fail-open 语义的 Redis 门面。"""

    def __init__(self) -> None:
        s = get_settings()
        self._client: Any = None
        self.degraded = True
        self._error: Optional[str] = None
        self._local: Dict[str, Any] = {}  # 降级时的进程内 KV
        self._local_ttl: Dict[str, float] = {}
        self._lock = threading.Lock()
        if not _HAS_REDIS:
            self._error = "redis 包未安装"
            return
        try:
            client = _redis.Redis(
                host=s.redis_host,
                port=s.redis_port,
                password=s.redis_password or None,
                db=s.redis_db,
                decode_responses=True,
                socket_connect_timeout=s.redis_connect_timeout,
                socket_timeout=s.redis_connect_timeout,
                health_check_interval=30,
            )
            client.ping()
            self._client = client
            self.degraded = False
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("Redis 不可用，已切换为进程内降级实现", error=self._error)

    # ------------------------------------------------------------ 代理
    def __getattr__(self, item: str) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            fallback = _DEFAULTS.get(item, None)
            if self._client is None:
                return self._local_op(item, args, fallback)
            try:
                return getattr(self._client, item)(*args, **kwargs)
            except Exception as exc:
                self.degraded = True
                self._error = f"{type(exc).__name__}: {exc}"
                log.warning("Redis 命令降级", cmd=item, error=self._error)
                return self._local_op(item, args, fallback)

        return _call

    def _local_op(self, item: str, args: tuple, fallback: Any) -> Any:
        """仅在 Redis 不可用时使用的进程内近似实现（够用即可，不作强一致假设）。"""
        with self._lock:
            now = time.time()
            expired = [k for k, exp in self._local_ttl.items() if exp < now]
            for k in expired:
                self._local.pop(k, None)
                self._local_ttl.pop(k, None)

            key = args[0] if args else None
            if item == "get":
                return self._local.get(key)
            if item == "set":
                self._local[key] = args[1] if len(args) > 1 else None
                return True
            if item == "setex":
                # SETEX key ttl value
                if len(args) > 2:
                    self._local[key] = args[2]
                    self._local_ttl[key] = now + float(args[1])
                    return True
                return False
            if item == "setnx":
                if key in self._local:
                    return False
                self._local[key] = args[1] if len(args) > 1 else None
                return True
            if item == "incr":
                self._local[key] = int(self._local.get(key, 0)) + 1
                return self._local[key]
            if item == "expire":
                self._local_ttl[key] = now + float(args[1] if len(args) > 1 else 0)
                return True
            if item == "ttl":
                return int(self._local_ttl.get(key, now) - now)
            if item in ("delete", "hdel"):
                existed = key in self._local
                self._local.pop(key, None)
                return 1 if existed else 0
            if item == "exists":
                return 1 if key in self._local else 0
            if item == "ping":
                return True
            if item == "keys":
                return list(self._local.keys())
            return fallback

    # ------------------------------------------------------------ 业务便捷方法
    def health(self) -> Dict[str, Any]:
        s = get_settings()
        ok = False
        if self._client is not None:
            try:
                ok = bool(self._client.ping())
            except Exception:
                ok = False
        return {
            "component": "redis",
            "available": ok,
            "degraded": bool(self.degraded or not ok),
            "target": f"{s.redis_host}:{s.redis_port}/{s.redis_db}",
            "error": self._error if not ok else None,
        }

    def get_json(self, key: str) -> Optional[Any]:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None

    def set_json(self, key: str, value: Any, ttl: int = 300) -> bool:
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return False
        return bool(self.set(key, payload, ex=ttl))

    def store_session(self, session_id: str, payload: Dict[str, Any], ttl: int = 3600) -> bool:
        return self.set_json(f"session:{session_id}", payload, ttl)

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        data = self.get_json(f"session:{session_id}")
        return data if isinstance(data, dict) else None

    def push_session_turn(self, session_id: str, role: str, content: str,
                          ttl: int = 3600, max_turns: int = 20) -> Dict[str, Any]:
        """把一轮对话追加进会话上下文（同时写 Redis 与本地列表）。"""
        sess = self.load_session(session_id) or {"session_id": session_id, "turns": []}
        turns: List[Dict[str, str]] = list(sess.get("turns", []))
        turns.append({"role": role, "content": content, "at": _now()})
        sess["turns"] = turns[-max_turns:]
        self.store_session(session_id, sess, ttl)
        return sess

    def cache_key(self, skill: str, role: str, question: str) -> str:
        import hashlib

        h = hashlib.sha256(question.strip().encode("utf-8")).hexdigest()[:32]
        return f"cache:ask:{skill}:{role}:{h}"

    def cache_get(self, skill: str, role: str, question: str) -> Optional[Dict[str, Any]]:
        data = self.get_json(self.cache_key(skill, role, question))
        return data if isinstance(data, dict) else None

    def cache_set(self, skill: str, role: str, question: str, payload: Dict[str, Any],
                  ttl: int | None = None) -> bool:
        ttl = ttl or get_settings().cache_ttl_s
        return self.set_json(self.cache_key(skill, role, question), payload, ttl)

    def cache_agent_card(self, name: str, card: Dict[str, Any], ttl: int = 300) -> bool:
        return self.set_json(f"agent_card:{name}", card, ttl)

    def load_agent_card(self, name: str) -> Optional[Dict[str, Any]]:
        data = self.get_json(f"agent_card:{name}")
        return data if isinstance(data, dict) else None

    def flush_cache(self) -> int:
        """清掉 cache:ask:* 键，返回删除数量。"""
        try:
            if self._client is None:
                n = len([k for k in self._local if str(k).startswith("cache:ask:")])
                for k in [k for k in self._local if str(k).startswith("cache:ask:")]:
                    self._local.pop(k, None)
                return n
            keys = list(self._client.scan_iter(match="cache:ask:*", count=200))
            return int(self._client.delete(*keys)) if keys else 0
        except Exception:
            return 0


def _now() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_CLIENT: Optional[SafeRedis] = None
_LOCK = threading.Lock()


def get_redis() -> SafeRedis:
    global _CLIENT
    if _CLIENT is None:
        with _LOCK:
            if _CLIENT is None:
                _CLIENT = SafeRedis()
    return _CLIENT


def reset_redis() -> None:
    global _CLIENT
    with _LOCK:
        _CLIENT = None


__all__ = ["SafeRedis", "get_redis", "reset_redis"]
