"""统一错误体系（架构文档 §11 降级与异常）。

所有错误都带机器可读的 ``code``、可选 ``details``，并由 ``main.py`` 的
异常处理器统一渲染成： ``{"code":..., "message":..., "request_id":..., "details":...}``
这样前端 / 外部 Agent 可以用 code 做分支，不用解析中文文案。
"""

from __future__ import annotations

from typing import Any, Dict

from commercepivot.core.context import get_request_id


class PivotError(Exception):
    """业务异常基类。"""

    code = "INTERNAL_ERROR"
    http_status = 500
    message = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        details: Dict[str, Any] | None = None,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        super().__init__(self.message)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "request_id": get_request_id(),
            "details": self.details,
        }


class AuthError(PivotError):
    code = "UNAUTHORIZED"
    http_status = 401
    message = "身份校验失败"


class ForbiddenError(PivotError):
    code = "FORBIDDEN"
    http_status = 403
    message = "无权访问"


class NotFoundError(PivotError):
    code = "NOT_FOUND"
    http_status = 404
    message = "资源不存在"


class ValidationFailedError(PivotError):
    code = "VALIDATION_FAILED"
    http_status = 422
    message = "参数校验失败"


class ToolNotFoundError(PivotError):
    code = "TOOL_NOT_FOUND"
    http_status = 404
    message = "工具未注册"


class ToolExecutionError(PivotError):
    code = "TOOL_EXECUTION_FAILED"
    http_status = 502
    message = "工具执行失败"


class AgentNotFoundError(PivotError):
    code = "AGENT_NOT_FOUND"
    http_status = 404
    message = "Agent 未注册"


class AgentTimeoutError(PivotError):
    code = "AGENT_TIMEOUT"
    http_status = 504
    message = "Agent 调用超时"


class CircuitOpenError(PivotError):
    code = "CIRCUIT_OPEN"
    http_status = 503
    message = "熔断器已打开，暂时拒绝调用"


class UpstreamError(PivotError):
    code = "UPSTREAM_ERROR"
    http_status = 502
    message = "上游依赖异常"


class DegradedError(PivotError):
    """可降级的软失败：调用方应拿到 this.details['fallback'] 继续。"""

    code = "DEGRADED"
    http_status = 200
    message = "已降级返回"


class LLMError(UpstreamError):
    code = "LLM_ERROR"
    message = "大模型调用失败"


__all__ = [
    "AgentNotFoundError",
    "AgentTimeoutError",
    "AuthError",
    "CircuitOpenError",
    "DegradedError",
    "ForbiddenError",
    "LLMError",
    "NotFoundError",
    "PivotError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "UpstreamError",
    "ValidationFailedError",
]
