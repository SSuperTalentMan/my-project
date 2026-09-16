"""MCP Server HTTP 入口（架构文档 §3.4 / §3.1）。

同一套 handler 挂载两份，兼容文档中出现的两种前缀：
- ``/api/v1/mcp/tools/list``、``/api/v1/mcp/tools/call``  —— §3.1 接入层路由
- ``/mcp/tools/list``、``/mcp/tools/call``               —— §3.4 MCP 接口（自定义 REST）

另外提供：
- ``POST /mcp``         **标准 MCP Streamable HTTP** 端点，供标准宿主
                        （Claude Desktop / Cursor / MCP Inspector）直接接入，
                        走官方 initialize / tools/list / tools/call 协议
- ``GET  /sse``         单次调用的 SSE 流式返回（§3.4「支持 SSE」）
- ``GET  /tools/{name}`` 单个工具的完整 schema
- ``GET  /audit``       最近审计记录（仅 admin）
- ``GET  /health``      工具层健康与工具数量
- ``GET  /transports``  当前可用的三种接入方式说明

请求体同时兼容两种风格：
1. 原生风格 ``{"tool": "query_orders", "params": {...}}``
2. MCP JSON-RPC 风格 ``{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":..,"arguments":{..}}}``
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from commercepivot.api import branding
from commercepivot.core.context import get_request_id, new_request_id, set_request_id, trace_add
from commercepivot.core.errors import ValidationFailedError
from commercepivot.core.logging import get_logger
from commercepivot.core.security import Principal, principal_from_request, rate_limit_dependency, require
from commercepivot.mcp_server.registry import get_registry
from commercepivot.mcp_server.tools import load_all

log = get_logger("commercepivot.mcp.server")


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: Optional[str] = Field(None, description="工具名（原生风格）")
    params: Dict[str, Any] = Field(default_factory=dict, description="工具参数")
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    # MCP JSON-RPC 风格字段
    jsonrpc: Optional[str] = None
    id: Optional[Any] = None
    method: Optional[str] = None


def _normalize(payload: ToolCallRequest) -> tuple[str, Dict[str, Any]]:
    """把两种请求风格统一成 (tool_name, arguments)。"""
    if payload.method:
        if payload.method not in ("tools/call", "tools/call_stream"):
            raise ValidationFailedError(
                f"不支持的 JSON-RPC 方法：{payload.method}",
                details={"supported": ["tools/call"]},
            )
        inner = payload.params or {}
        tool = str(inner.get("name") or inner.get("tool") or "").strip()
        args = inner.get("arguments") or inner.get("params") or {}
        if not isinstance(args, dict):
            raise ValidationFailedError("arguments 必须是对象")
        return tool, args
    tool = str(payload.tool or "").strip()
    return tool, payload.params or {}


def _jsonrpc_ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_err(req_id: Any, code: str, message: str, details: Any = None) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message, "data": details or {}},
    }


def build_router(prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["mcp"])

    @router.get("/tools/list", summary="列出全部 MCP 工具及其 JSON Schema")
    async def tools_list(
        detail: bool = Query(True, description="是否返回完整 inputSchema"),
        _: Principal = Depends(require("mcp")),
    ) -> Dict[str, Any]:
        load_all()
        registry = get_registry()
        if not detail:
            return {"ok": True, "count": len(registry.names()), "tools": registry.names()}
        return registry.describe()

    @router.get("/tools/{name}", summary="查看单个工具定义")
    async def tool_detail(name: str, _: Principal = Depends(require("mcp"))) -> Dict[str, Any]:
        load_all()
        return get_registry().describe(name)

    @router.post(
        "/tools/call",
        summary="调用 MCP 工具",
        dependencies=[Depends(rate_limit_dependency)],
    )
    async def tools_call(payload: ToolCallRequest, request: Request) -> Any:
        load_all()
        is_jsonrpc = bool(payload.jsonrpc) or bool(payload.method)
        if payload.request_id:
            set_request_id(payload.request_id)
        principal = principal_from_request(request)
        request.state.principal = principal

        try:
            tool, args = _normalize(payload)
        except ValidationFailedError as exc:
            return JSONResponse(
                status_code=exc.http_status,
                content=_jsonrpc_err(payload.id, exc.code, exc.message, exc.details)
                if is_jsonrpc
                else exc.to_payload(),
            )

        if not tool:
            detail = {"code": "VALIDATION_FAILED", "message": "缺少 tool / params.name 字段",
                      "request_id": get_request_id(),
                      "available": get_registry().names()}
            return JSONResponse(
                status_code=422,
                content=_jsonrpc_err(payload.id, "VALIDATION_FAILED", "缺少工具名", detail)
                if is_jsonrpc
                else detail,
            )

        trace_add("mcp-http", tool=tool, transport="http")
        result = await get_registry().call(
            tool, args, role=principal.role, session_id=payload.session_id
        )
        if is_jsonrpc:
            if result.get("ok"):
                return _jsonrpc_ok(payload.id, result)
            return JSONResponse(
                status_code=200,
                content=_jsonrpc_err(
                    payload.id, result.get("code", "TOOL_EXECUTION_FAILED"),
                    result.get("message", "工具调用失败"), result,
                ),
            )
        return result

    @router.get("/sse", summary="以 SSE 流式返回一次工具调用结果")
    async def tools_sse(
        request: Request,
        tool: str = Query(..., description="工具名"),
        params: str = Query("{}", description="JSON 字符串形式的参数"),
        session_id: Optional[str] = Query(None),
    ) -> EventSourceResponse:
        load_all()
        principal = principal_from_request(request)

        async def event_stream():
            yield {"event": "start", "data": json.dumps(
                {"tool": tool, "request_id": get_request_id()}, ensure_ascii=False)}

            try:
                args = json.loads(params or "{}")
                if not isinstance(args, dict):
                    raise ValueError("params 必须是 JSON 对象")
            except ValueError as exc:
                yield {"event": "error", "data": json.dumps(
                    {"code": "INVALID_PARAMS", "message": f"params 解析失败：{exc}"},
                    ensure_ascii=False)}
                return

            yield {"event": "progress", "data": json.dumps(
                {"stage": "calling", "tool": tool}, ensure_ascii=False)}

            result = await get_registry().call(
                tool, args, role=principal.role, session_id=session_id
            )
            for row in (result.get("rows") or [])[:50]:
                yield {"event": "row", "data": json.dumps(row, ensure_ascii=False, default=str)}
            yield {"event": "result", "data": json.dumps(result, ensure_ascii=False, default=str)}
            yield {"event": "done", "data": json.dumps(
                {"ok": result.get("ok"), "elapsed_ms": result.get("elapsed_ms")},
                ensure_ascii=False)}

        return EventSourceResponse(event_stream())

    @router.get("/audit", summary="最近的 MCP 工具调用审计记录（仅 admin）")
    async def audit_list(
        limit: int = Query(20, ge=1, le=200),
        tool: Optional[str] = Query(None),
        _: Principal = Depends(require("mcp", "*")),
    ) -> Dict[str, Any]:
        from commercepivot.db import repository as repo

        try:
            rows = repo.list_audit(limit=limit, tool_name=tool)
            return {"ok": True, "count": len(rows), "rows": rows}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": "AUDIT_UNAVAILABLE",
                    "message": f"审计表不可用：{exc}", "rows": []}

    @router.get("/health", summary="MCP 工具层健康检查")
    async def mcp_health() -> Dict[str, Any]:
        load_all()
        registry = get_registry()
        return {
            "ok": True,
            "component": "mcp_server",
            "request_id": get_request_id(),
            "tool_count": len(registry.names()),
            "readonly_count": len(registry.readonly_names()),
            "tools": registry.names(),
        }

    @router.get("/transports", summary="当前可用的 MCP 接入方式")
    async def transports() -> Dict[str, Any]:
        from commercepivot.mcp_server.protocol_server import describe_transports

        return {"ok": True, "transports": describe_transports()}

    return router


def create_mcp_app():  # pragma: no cover - 独立启动入口
    """独立 MCP Server（:8001）。"""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from commercepivot.api.errors import register_exception_handlers
    from commercepivot.core.config import get_settings
    from commercepivot.core.logging import setup_logging
    from commercepivot.mcp_server.protocol_server import build_server

    settings = get_settings()
    setup_logging()
    load_all()

    # 先构造协议 server 并生成 ASGI 子应用：``streamable_http_app()`` 会**惰性创建**
    # StreamableHTTPSessionManager，而下面的 lifespan 需要访问它，顺序不能反。
    protocol = build_server()
    streamable_app = protocol.streamable_http_app()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # 必须显式跑 session_manager：FastAPI 的默认 lifespan 只调 Starlette 的
        # ``startup()``，**不会进入被 mount 子应用的 lifespan**，于是 session
        # manager 的 task group 永远不启动，任何 /mcp 请求都会 500：
        # ``RuntimeError: Task group is not initialized. Make sure to use run().``
        async with protocol.session_manager.run():
            yield

    app = FastAPI(
        title=f"{settings.app_name} · MCP Server",
        version=settings.app_version,
        description="MCP 工具层独立服务（标准 Streamable HTTP + REST + SSE）",
        lifespan=_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 与主服务共用同一套异常映射。不加这一步的话，:8001 上鉴权失败会裸抛
    # AuthError 被兜成 500（HTTP 500 + "服务内部错误"），而正确语义是 401 ——
    # 调用方会顺着 500 去查服务端故障，排查方向被完全带偏（实测踩到）。
    register_exception_handlers(app)

    app.include_router(build_router("/api/v1/mcp"))
    app.include_router(build_router("/mcp"))

    # 浏览器打开 :8001 时同样会自动要图标。必须注册在下面 ``mount("/")``
    # **之前** —— 那个 catch-all 会吞掉所有未匹配路径，注册晚了就轮不到它。
    branding.register_favicon(app)

    # 挂载标准 MCP Streamable HTTP 端点（对外路径 ``/mcp``，即子应用内的默认路径）。
    #
    # 这里挂到 "/" 而不是 "/mcp"：挂到 "/mcp" 会让外层 Mount 剥掉前缀、把剩余路径
    # 交给子应用，可偏偏 ``/mcp`` 结尾没有内容，Starlette 会先回一个 307 跳到
    # ``/mcp/``，多一次往返；挂到 "/" 则 ``/mcp`` 直接命中子应用的精确路由。
    # 顺序上先注册的两个 include_router 仍优先，所以 ``POST /mcp/tools/call``
    # 这类显式路由不会被这个 catch-all 抢走。
    #
    # 注意该端点**不带 JWT**：标准宿主难以携带自定义鉴权头，故仅适合本机使用，
    # 与上面带 ``require("mcp")`` 的 REST 路由刻意保持区分。
    app.mount("/", streamable_app)
    log.info(
        "标准 MCP 端点已挂载",
        streamable_http=f"http://127.0.0.1:{settings.mcp_port}/mcp",
        auth="none（仅本机使用）",
    )
    return app


__all__ = ["ToolCallRequest", "build_router", "create_mcp_app"]
