"""FastAPI 主服务入口（架构文档 §3.1 接入层 / §8 部署架构 / §12 可观测性）。

挂载内容：
- ``/api/v1/chat/*``  问答（一次性 + SSE 流式）
- ``/api/v1/mcp/*``   内嵌 MCP 工具层（``/mcp/*`` 为别名）
- ``/api/v1/a2a/*``   A2A JSON-RPC 与任务查询
- ``/.well-known/*``  Agent Card 发现
- ``/health``、``/metrics``、``/version``、``/config``  运维与可观测

中间件注入 ``X-Request-ID`` 并统一计量，异常处理器把
``PivotError`` 渲染成 ``{code, message, request_id, details}``。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from commercepivot.api import a2a as a2a_api
from commercepivot.api import branding
from commercepivot.api import chat as chat_api
from commercepivot.api import mcp as mcp_api
from commercepivot.api.errors import register_exception_handlers
from commercepivot.core.config import get_settings
from commercepivot.core.context import (
    begin_trace,
    get_request_id,
    new_request_id,
    set_request_id,
)
from commercepivot.core.errors import PivotError
from commercepivot.core.logging import get_logger, setup_logging
from commercepivot.core.metrics import (
    HTTP_LATENCY,
    HTTP_REQUESTS,
    render_prometheus,
    snapshot,
)

log = get_logger("commercepivot.main")


def _normalize_path(path: str) -> str:
    """压掉路径里的动态段，避免 Prometheus 标签基数爆炸。"""
    parts = []
    for seg in path.strip("/").split("/"):
        if not seg:
            continue
        if len(seg) >= 16 or seg.isdigit() or seg.startswith(("sess-", "task-", "SO", "CP")):
            parts.append("{id}")
        else:
            parts.append(seg)
    return "/" + "/".join(parts)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """X-Request-ID 贯穿 + 访问日志 + Prometheus 计量。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        rid = request.headers.get("x-request-id") or new_request_id()
        set_request_id(rid)
        # trace 必须在一进入口就开一份新列表：它靠「共享引用」跨 LangGraph 节点与
        # 线程池累积（见 core/context 的模块注释），不开就没有可累积的对象。
        begin_trace()
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:  # noqa: BLE001 - 兜底，交给全局异常处理器
            log.warning("请求处理异常", path=request.url.path, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            cost = time.perf_counter() - started
            HTTP_REQUESTS.inc(
                {
                    "method": request.method,
                    "path": _normalize_path(request.url.path),
                    "status": str(status_code),
                }
            )
            HTTP_LATENCY.observe(cost, {"path": _normalize_path(request.url.path)})
        response.headers["X-Request-ID"] = rid
        response.headers["X-Process-Time"] = f"{cost * 1000:.1f}ms"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging()
    from commercepivot.mcp_server.tools import load_all

    tools = load_all()
    log.info(
        "商枢服务启动",
        version=settings.app_version,
        tools=len(tools),
        llm_enabled=settings.llm_enabled,
        write_ops=settings.enable_write_ops,
    )
    # 探活（不加载模型、不建 Milvus 连接，避免启动阻塞）
    try:
        from commercepivot.db.mysql import get_mysql

        log.info("MySQL 探活", **get_mysql().health())
    except Exception as exc:  # noqa: BLE001
        log.warning("MySQL 探活失败", error=str(exc))
    try:
        from commercepivot.db.redis import get_redis

        log.info("Redis 探活", **get_redis().health())
    except Exception as exc:  # noqa: BLE001
        log.warning("Redis 探活失败", error=str(exc))

    app.state.started_at = time.time()
    yield
    log.info("商枢服务关闭")
    try:
        from commercepivot.models.llm_client import LLMClient

        _ = LLMClient  # 保持引用，便于未来扩展优雅关闭
    except Exception:  # noqa: BLE001
        pass


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "电商智能经营分析与客服助手平台。\n\n"
            "分层：接入层(FastAPI+JWT+限流) → 编排层(LangGraph 主控 Agent) → "
            "A2A Agent 层(5 个专职 Agent) → MCP 工具层(8 个工具) → "
            "数据层(MySQL/Redis/Milvus) → 模型层(百炼 qwen + BERT + BGE-M3 + Reranker)"
        ),
        lifespan=lifespan,
    )

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Process-Time"],
    )

    # ---------------------------------------------------------- 路由
    app.include_router(chat_api.router, prefix="/api/v1")
    app.include_router(a2a_api.router)
    app.include_router(a2a_api.well_known)
    app.include_router(mcp_api.router)
    app.include_router(mcp_api.legacy_router)

    # ---------------------------------------------------------- 异常处理
    # 与独立 MCP Server（:8001）共用同一套映射，避免两边状态码语义漂移
    register_exception_handlers(app)

    # ---------------------------------------------------------- 运维接口
    # ``GET /`` 双形态：浏览器打开是导航落地页，程序调用仍是原来的 JSON
    # （按 Accept 头分流，curl/httpx 默认 ``*/*`` 走的还是 JSON 分支）。
    branding.register_landing(
        app,
        settings.app_name,
        settings.app_version,
        {
            "app": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "endpoints": {
                "ask": "POST /api/v1/chat/ask",
                "ask_stream": "POST /api/v1/chat/ask_stream",
                "token": "POST /api/v1/auth/token",
                "mcp_tools_list": "GET /api/v1/mcp/tools/list",
                "mcp_tools_call": "POST /api/v1/mcp/tools/call",
                "mcp_sse": "GET /api/v1/mcp/sse?tool=...&params={...}",
                "agent_card": "GET /.well-known/agent-card.json",
                "agents": "GET /.well-known/agents",
                "a2a_rpc": "POST /api/v1/a2a/{agent_name}",
                "health": "GET /health",
                "metrics": "GET /metrics",
            },
        },
    )
    # 浏览器每次打开页面都会自动再要一次 /favicon.ico，不提供就会在控制台
    # 反复刷 404 噪音（表现为"看起来像报错"），这里补上。
    branding.register_favicon(app)

    @app.get("/health", summary="健康检查（含各组件降级状态）")
    async def health(deep: bool = Query(False, description="是否深度检查（会做一次数据计数）")):
        components: Dict[str, Any] = {}
        try:
            from commercepivot.db.mysql import get_mysql

            components["mysql"] = get_mysql().health()
        except Exception as exc:  # noqa: BLE001
            components["mysql"] = {"component": "mysql", "available": False, "error": str(exc)}
        try:
            from commercepivot.db.redis import get_redis

            components["redis"] = get_redis().health()
        except Exception as exc:  # noqa: BLE001
            components["redis"] = {"component": "redis", "available": False, "error": str(exc)}

        from commercepivot.retrieval.service import retrieval_status

        retrieval = retrieval_status(load_models=deep)
        components["llm"] = _safe_status("llm")
        components["intent"] = _safe_status("intent")

        from commercepivot.agents.registry import get_agent_registry
        from commercepivot.mcp_server.registry import get_registry

        payload: Dict[str, Any] = {
            "ok": bool(components["mysql"].get("available")),
            "app": settings.app_name,
            "version": settings.app_version,
            "request_id": get_request_id(),
            "uptime_s": int(time.time() - getattr(app.state, "started_at", time.time())),
            "components": {**components, "retrieval": retrieval},
            "mcp": {"tool_count": len(get_registry().names()), "tools": get_registry().names()},
            "a2a": {"agent_count": len(get_agent_registry().names()), "agents": get_agent_registry().names()},
            "degraded": [
                name
                for name, info in components.items()
                if isinstance(info, dict) and info.get("degraded")
            ] + (["retrieval"] if retrieval and retrieval.get("milvus", {}).get("degraded") else []),
            "metrics": snapshot(),
        }
        if deep:
            from commercepivot.db import repository as repo
            from commercepivot.db.mysql import MySQLUnavailable

            try:
                payload["tables"] = repo.table_stats()
            except MySQLUnavailable as exc:
                payload["tables"] = {"available": False, "error": str(exc)}
        return payload

    @app.get("/metrics", summary="Prometheus 指标", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/version", summary="版本与运行模式")
    async def version() -> Dict[str, Any]:
        from commercepivot.orchestrator.graph import get_orchestrator

        return {
            "app": settings.app_name,
            "version": settings.app_version,
            "orchestrator_mode": get_orchestrator().mode,
            "orchestrator_graph": get_orchestrator().mermaid(),
            "mcp_transport": settings.mcp_transport,
            "enable_write_ops": settings.enable_write_ops,
        }

    @app.get("/config", summary="配置快照（密钥脱敏）")
    async def config_snapshot(
        request: Request,
    ) -> Any:
        from commercepivot.core.security import principal_from_request

        try:
            principal = principal_from_request(request)
        except PivotError as exc:
            return JSONResponse(status_code=exc.http_status, content=exc.to_payload())
        if principal.role != "admin":
            return JSONResponse(
                status_code=403,
                content={"code": "FORBIDDEN", "message": "仅 admin 可查看配置",
                         "request_id": get_request_id(), "details": {}},
            )
        return {"ok": True, "config": settings.public_dict()}

    return app


def _safe_status(component: str) -> Dict[str, Any]:
    """组件状态探针，任何异常都降级为 available=False，不抛给 /health。"""
    try:
        if component == "llm":
            from commercepivot.models.llm_client import get_llm

            return get_llm().status()
        if component == "intent":
            from commercepivot.models.intent_bert import intent_status

            return intent_status()
    except Exception as exc:  # noqa: BLE001
        return {"component": component, "available": False, "degraded": True,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"component": component, "available": False}


app = create_app()


__all__ = ["app", "create_app"]
