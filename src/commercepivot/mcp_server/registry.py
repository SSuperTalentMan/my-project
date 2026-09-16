"""MCP 工具注册表（架构文档 §3.4 MCP 工具层）。

职责
----
1. **注册**：每个工具声明 名称 / 描述 / Pydantic 参数模型 / 超时 / 是否只读，
   参数 schema 由 Pydantic 自动导出成 JSON Schema，避免手写 schema 漂移。
2. **校验**：调用前 ``model_validate``，失败返回 ``VALIDATION_FAILED`` + 字段级错误。
3. **执行**：同步 handler 走 ``asyncio.to_thread``（不阻塞事件循环），
   统一 ``wait_for`` 超时；失败按 §11 重试 1 次。
4. **审计**：成功/失败/超时/降级都落审计（JSONL + MySQL），参数脱敏。
5. **权限**：``write_kind == "db"`` 的工具受 ``ENABLE_WRITE_OPS`` 保护，
   默认只读，直接拒绝写操作。

设计取舍：``call()`` **不抛异常**，而是返回结构化 ``ToolResult``。
理由——MCP 的调用方既有 HTTP 客户端也有 LLM，把错误变成「可读的错误码
+ 提示」比抛异常更容易让上层 Agent 决定是否降级，也天然满足 §11
「仍失败返回错误码」。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

from commercepivot.core.config import get_settings
from commercepivot.core.context import get_request_id, trace_add
from commercepivot.core.errors import ForbiddenError, PivotError
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import DEGRADED, MCP_CALLS, MCP_LATENCY
from commercepivot.core.security import ensure_write_allowed
from commercepivot.db.mysql import MySQLUnavailable
from commercepivot.mcp_server.audit import write_audit

log = get_logger("commercepivot.mcp.registry")

ToolHandler = Callable[[Any], Any]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: Type[BaseModel]
    handler: ToolHandler
    readonly: bool = True
    write_kind: str = "none"  # none | file | db
    timeout_s: float = 10.0
    retries: int = 1
    tags: List[str] = field(default_factory=list)
    owner_agent: str = ""
    examples: List[Dict[str, Any]] = field(default_factory=list)

    def input_schema(self) -> Dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.setdefault("title", f"{self.name} 参数")
        return schema

    def to_payload(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema(),
            "readonly": self.readonly,
            "write_kind": self.write_kind,
            "timeout_s": self.timeout_s,
            "retries": self.retries,
            "tags": self.tags,
            "owner_agent": self.owner_agent,
            "examples": self.examples,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    # ---------------------------------------------------------- 注册
    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._tools[spec.name] = spec
        return spec

    def tool(
        self,
        name: str,
        description: str,
        input_model: Type[BaseModel],
        readonly: bool = True,
        write_kind: str = "none",
        timeout_s: float | None = None,
        retries: int | None = None,
        tags: Optional[List[str]] = None,
        owner_agent: str = "",
        examples: Optional[List[Dict[str, Any]]] = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        settings = get_settings()

        def deco(fn: ToolHandler) -> ToolHandler:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    input_model=input_model,
                    handler=fn,
                    readonly=readonly,
                    write_kind=write_kind,
                    timeout_s=settings.mcp_tool_timeout_s if timeout_s is None else timeout_s,
                    retries=settings.mcp_tool_retries if retries is None else retries,
                    tags=tags or [],
                    owner_agent=owner_agent,
                    examples=examples or [],
                )
            )
            return fn

        return deco

    # ---------------------------------------------------------- 查询
    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def list_specs(self) -> List[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def describe(self, name: str | None = None) -> Dict[str, Any]:
        if name:
            spec = self.get(name)
            if spec is None:
                return {"ok": False, "code": "TOOL_NOT_FOUND", "message": f"工具不存在：{name}",
                        "available": self.names()}
            return {"ok": True, "tool": spec.to_payload()}
        return {
            "ok": True,
            "count": len(self._tools),
            "tools": [spec.to_payload() for spec in self.list_specs()],
        }

    def readonly_names(self) -> List[str]:
        return [s.name for s in self.list_specs() if s.readonly]

    # ---------------------------------------------------------- 调用
    async def call(
        self,
        name: str,
        params: Dict[str, Any] | None = None,
        *,
        role: str = "admin",
        session_id: str | None = None,
        raise_on_error: bool = False,
        audit: bool = True,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        spec = self.get(name)
        params = dict(params or {})
        if session_id:
            params.setdefault("_session_id", session_id)

        if spec is None:
            result = self._error_payload(
                name, "TOOL_NOT_FOUND", f"工具不存在：{name}",
                {"available": self.names()}, started,
            )
            if audit:
                write_audit(name, params, result, result["elapsed_ms"], "not_found", result["message"])
            return self._finish(result, raise_on_error)

        # ---- 写操作门禁（§11 默认只读）
        if spec.write_kind == "db":
            try:
                ensure_write_allowed(spec.name)
            except ForbiddenError as exc:
                result = self._error_payload(spec.name, exc.code, exc.message, exc.details, started)
                if audit:
                    write_audit(spec.name, params, result, result["elapsed_ms"], "forbidden", exc.message)
                return self._finish(result, raise_on_error)

        # ---- 参数校验
        try:
            clean = {k: v for k, v in params.items() if not str(k).startswith("_")}
            validated = spec.input_model.model_validate(clean)
        except ValidationError as exc:
            details = {
                "errors": [
                    {"field": ".".join(str(x) for x in e.get("loc", ())), "reason": e.get("msg", "")}
                    for e in exc.errors()
                ]
            }
            result = self._error_payload(spec.name, "VALIDATION_FAILED", "参数校验失败", details, started)
            if audit:
                write_audit(spec.name, params, result, result["elapsed_ms"], "invalid", result["message"])
            return self._finish(result, raise_on_error)

        # ---- 执行（超时 + 重试 1 次）
        attempt = 0
        last_error: Optional[BaseException] = None
        while attempt <= max(0, spec.retries):
            attempt += 1
            try:
                payload = await self._invoke(spec, validated, session_id)
                elapsed = int((time.perf_counter() - started) * 1000)
                result = self._ok_payload(spec, payload, elapsed)
                MCP_CALLS.inc({"tool": spec.name, "status": "ok"})
                MCP_LATENCY.observe(elapsed / 1000.0, {"tool": spec.name})
                trace_add("mcp", tool=spec.name, elapsed_ms=elapsed, attempt=attempt)
                if audit:
                    write_audit(spec.name, params, result, elapsed, "ok")
                return self._finish(result, raise_on_error)
            except asyncio.TimeoutError as exc:
                last_error = exc
                status, code, msg = "timeout", "TOOL_TIMEOUT", (
                    f"工具 {spec.name} 调用超时（>{spec.timeout_s}s）"
                )
            except MySQLUnavailable as exc:
                last_error = exc
                status, code, msg = "degraded", "DATA_SOURCE_UNAVAILABLE", (
                    f"MySQL 不可用，{spec.name} 已降级：{exc}"
                )
                DEGRADED.inc({"component": "mysql"})
            except PivotError as exc:
                last_error = exc
                status, code, msg = "error", exc.code, exc.message
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                status, code, msg = "error", "TOOL_EXECUTION_FAILED", f"{type(exc).__name__}: {exc}"

            if attempt <= max(0, spec.retries):
                log.warning("工具调用失败，准备重试", tool=spec.name, attempt=attempt, error=str(last_error))
                await asyncio.sleep(0.15 * attempt)

        elapsed = int((time.perf_counter() - started) * 1000)
        MCP_CALLS.inc({"tool": spec.name, "status": status})
        result = self._error_payload(spec.name, code, msg, {"attempts": attempt}, started)
        result["degraded"] = status == "degraded"
        if audit:
            write_audit(spec.name, params, result, elapsed, status, msg)
        return self._finish(result, raise_on_error)

    async def _invoke(self, spec: ToolSpec, validated: BaseModel, session_id: str | None) -> Any:
        fn = spec.handler
        if inspect.iscoroutinefunction(fn):
            coro = fn(validated)
        else:
            coro = asyncio.to_thread(fn, validated)
        return await asyncio.wait_for(coro, timeout=spec.timeout_s)

    # ---------------------------------------------------------- 结果封装
    def _ok_payload(self, spec: ToolSpec, payload: Any, elapsed_ms: int) -> Dict[str, Any]:
        data = payload if isinstance(payload, dict) else {"value": payload}
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        return {
            "ok": True,
            "tool": spec.name,
            "data": {k: v for k, v in data.items() if k not in ("rows", "summary")},
            "summary": data.get("summary") or {},
            "rows": rows,
            "row_count": int(data.get("row_count") or len(rows)),
            "truncated": bool(data.get("truncated", False)),
            "degraded": bool(data.get("degraded", False)),
            "degrade_reason": data.get("degrade_reason"),
            "elapsed_ms": elapsed_ms,
            "request_id": get_request_id(),
        }

    @staticmethod
    def _error_payload(
        name: str, code: str, message: str, details: Any, started: float
    ) -> Dict[str, Any]:
        return {
            "ok": False,
            "tool": name,
            "code": code,
            "message": message,
            "details": details if isinstance(details, (dict, list)) else {"info": details},
            "data": None,
            "summary": {},
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "degraded": code == "DATA_SOURCE_UNAVAILABLE",
            "degrade_reason": message if code == "DATA_SOURCE_UNAVAILABLE" else None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "request_id": get_request_id(),
        }

    @staticmethod
    def _finish(result: Dict[str, Any], raise_on_error: bool) -> Dict[str, Any]:
        if raise_on_error and not result.get("ok"):
            raise PivotError(
                result.get("message") or "工具调用失败",
                details={"code": result.get("code"), **(result.get("details") or {})},
                code=result.get("code") or "TOOL_EXECUTION_FAILED",
            )
        return result


# 全局单例
REGISTRY = ToolRegistry()


def get_registry() -> ToolRegistry:
    return REGISTRY


__all__ = ["REGISTRY", "ToolRegistry", "ToolSpec", "get_registry"]
