"""MCP 接口挂载（架构文档 §3.1 的 /api/v1/mcp 路由）。

主服务内嵌一份 MCP 路由，同时保留 ``/mcp/*`` 前缀别名（§3.4 的写法），
这样外部 MCP 客户端无论按哪一份文档接都能通。
"""

from __future__ import annotations

from fastapi import APIRouter

from commercepivot.mcp_server.server import build_router

router: APIRouter = build_router("/api/v1/mcp")
legacy_router: APIRouter = build_router("/mcp")

__all__ = ["legacy_router", "router"]
