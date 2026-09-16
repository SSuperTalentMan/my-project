"""结构化日志（架构文档 §12）。

优先使用 structlog 输出 JSON/彩色控制台日志；未安装时自动退回标准库 logging。
所有日志自动携带 ``request_id``，因此一次请求的 route / A2A / MCP / LLM
四层日志可以按 request_id 串成一条完整链路。
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict

from commercepivot.core.config import LOG_DIR, get_settings
from commercepivot.core.context import get_request_id

try:  # pragma: no cover - 取决于环境
    import structlog

    _HAS_STRUCTLOG = True
except Exception:  # pragma: no cover
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False

_CONFIGURED = False
_STREAM: Any = None


def _inject_request_id(_logger: Any, _method: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    event_dict.setdefault("request_id", get_request_id())
    return event_dict


class _DropGrpcNotImplemented(logging.Filter):
    """过滤 milvus-lite 的已知噪音。

    ``pymilvus`` 建连后会调一次 ``AllocTimestamp``，而 milvus-lite 的 gRPC
    adapter 未实现该 RPC（pymilvus 侧会正常捕获并继续）。但 grpc 会先在服务端
    打一条 ``Exception calling application: Method not implemented!`` 的 ERROR
    堆栈 —— 容易被误读成故障，这里只丢弃这一条，其余错误照常输出。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "Method not implemented" not in record.getMessage()


def _resolve_stream(stream: Any) -> Any:
    """决定日志写到哪个流。

    **为什么需要这个**：MCP 的 stdio 传输把 ``stdout`` 当作 JSON-RPC 专用通道，
    往里写任何一行日志，宿主都会当成非法报文而握手失败（实测启动即吐 1032 字节脏数据）。
    但坑在于**配置时机**：``stdio_server`` 通过 ``python -m`` 启动时，Python 会先导入
    父包 ``commercepivot.mcp_server``，而它的 ``__init__`` 又会导入 ``registry``，
    后者在模块级就调 ``get_logger()`` —— 于是日志在 ``stdio_server`` 自己执行之前
    就已经被锁到 stdout 了。

    所以这里接受一个显式的 ``stream`` 参数并**允许切换**（而不是只认第一次调用），
    让 ``stdio_server.main()`` 能可靠地把日志改道到 stderr。
    """
    if stream is not None:
        return stream
    name = str(getattr(get_settings(), "log_stream", "stdout")).strip().lower()
    return sys.stderr if name == "stderr" else sys.stdout


def setup_logging(level: str | None = None, as_json: bool | None = None, stream: Any = None) -> None:
    """进程启动时调用一次；同一目标流重复调用是幂等的。

    ``stream`` 显式改变目标流时会**重新配置**（见 :func:`_resolve_stream`）。
    """
    global _CONFIGURED, _STREAM

    target = _resolve_stream(stream)
    if _CONFIGURED and target is _STREAM:
        return

    settings = get_settings()
    level = (level or settings.log_level).upper()
    as_json = settings.log_json if as_json is None else as_json

    # 标准库根 logger：让 uvicorn / sqlalchemy 的日志也走同一格式
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(target)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    logging.getLogger("grpc._server").addFilter(_DropGrpcNotImplemented())

    if _HAS_STRUCTLOG:
        processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _inject_request_id,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
        ]
        processors.append(
            structlog.processors.JSONRenderer(ensure_ascii=False)
            if as_json
            else structlog.dev.ConsoleRenderer(colors=False)
        )
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level, logging.INFO)
            ),
            logger_factory=structlog.PrintLoggerFactory(file=target),
            # 必须关掉缓存：stdio_server 会在 registry 模块级 get_logger()（此时还是
            # stdout）之后才把流切到 stderr。若缓存了首次绑定的 logger，那个已创建的
            # logger 对象会一直写 stdout，切流对它无效。
            cache_logger_on_first_use=False,
        )
    _CONFIGURED = True
    _STREAM = target


class _StdlibAdapter:
    """structlog 缺失时的最小替代品，接口保持 info(msg, **kv) / bind()。"""

    def __init__(self, name: str, extra: Dict[str, Any] | None = None) -> None:
        self._log = logging.getLogger(name)
        self._extra = extra or {}

    def bind(self, **kv: Any) -> "_StdlibAdapter":
        return _StdlibAdapter(self._log.name, {**self._extra, **kv})

    unbind = bind

    def _emit(self, level: int, msg: str, **kv: Any) -> None:
        payload = {**self._extra, **kv, "request_id": get_request_id()}
        tail = " ".join(f"{k}={v!r}" for k, v in payload.items() if v is not None)
        self._log.log(level, "%s | %s" % (msg, tail) if tail else msg)

    def debug(self, msg: str, **kv: Any) -> None:
        self._emit(logging.DEBUG, msg, **kv)

    def info(self, msg: str, **kv: Any) -> None:
        self._emit(logging.INFO, msg, **kv)

    def warning(self, msg: str, **kv: Any) -> None:
        self._emit(logging.WARNING, msg, **kv)

    warn = warning

    def error(self, msg: str, **kv: Any) -> None:
        self._emit(logging.ERROR, msg, **kv)

    def exception(self, msg: str, **kv: Any) -> None:
        self._log.exception(msg, **kv)


def get_logger(name: str = "commercepivot") -> Any:
    setup_logging()
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return _StdlibAdapter(name)


def audit_file() -> str:
    """审计 JSONL 文件路径，确保父目录存在。"""
    path = get_settings().audit_abs_path
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return str(path)
