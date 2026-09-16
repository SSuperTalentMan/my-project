"""统一的 HTTP 异常 → JSON 响应映射。

原先这套 handler 只写在 ``main.create_app()`` 里，于是**独立 MCP Server
（:8001）完全没有** —— 它由 ``mcp_server.server.create_mcp_app()`` 单独构造
FastAPI 实例。后果是 `:8001` 上鉴权失败不返回 401，而是裸抛 ``AuthError``
被兜成 500：调用方看到的是「服务内部错误」，完全误导排查方向。

抽到这里之后主服务与独立 MCP Server 共用同一套语义。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from commercepivot.core.context import get_request_id
from commercepivot.core.errors import PivotError
from commercepivot.core.logging import get_logger

log = get_logger("commercepivot.api.errors")

STATUS_TEXT: Dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    422: "VALIDATION_FAILED",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}


def register_exception_handlers(app: FastAPI) -> None:
    """给 FastAPI 实例挂上统一异常映射（主服务与 MCP Server 共用）。"""

    @app.exception_handler(PivotError)
    async def _pivot_error(_: Request, exc: PivotError) -> JSONResponse:
        # PivotError 自带 http_status，鉴权/参数/权限类错误靠它保住正确的状态码
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(x) for x in e.get("loc", ())), "reason": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_FAILED",
                "message": "请求参数校验失败",
                "request_id": get_request_id(),
                "details": {"errors": errors},
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            body: Dict[str, Any] = {"request_id": get_request_id(), **exc.detail}
            return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": STATUS_TEXT.get(exc.status_code, "HTTP_ERROR"),
                "message": str(exc.detail),
                "request_id": get_request_id(),
                "details": {},
            },
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("未捕获异常", error=f"{type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误，请携带 request_id 联系管理员",
                "request_id": get_request_id(),
                "details": {"type": type(exc).__name__},
            },
        )


__all__ = ["STATUS_TEXT", "register_exception_handlers"]
