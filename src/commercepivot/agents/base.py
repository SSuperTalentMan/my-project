"""A2A Agent 基类（架构文档 §4 A2A 协议设计）。

包含四件事：
1. **Agent Card**：``/.well-known/agent-card.json`` 的内容载体（name / description /
   version / skills / endpoint / input_schema / output_schema）。
2. **任务状态机**：``submitted → working → completed | failed | canceled``，
   与文档 §4.3 完全一致，状态落库到 ``agent_tasks`` 表。
3. **JSON-RPC 2.0 方法**：``tasks/send``、``tasks/get``、``tasks/cancel``（§4.2）。
4. **稳定性护栏**：单任务超时 + 连续失败熔断，落地 §11 的
   「A2A 子 Agent 超时：熔断，返回部分结果」。

子 Agent 只需继承 ``BaseAgent`` 并实现 ``handle()``；取数一律通过 ``self.call_tool()``
走 MCP 工具层，保证「Agent 不直连数据库」这条分层约束不被破坏。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from commercepivot.core.config import get_settings
from commercepivot.core.context import get_request_id, trace_add
from commercepivot.core.errors import (
    AgentNotFoundError,
    AgentTimeoutError,
    CircuitOpenError,
    PivotError,
)
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import A2A_LATENCY, A2A_TASKS, DEGRADED
from commercepivot.mcp_server.client import MCPClient

log = get_logger("commercepivot.agents.base")

TASK_STATES = ("submitted", "working", "completed", "failed", "canceled")
TERMINAL_STATES = ("completed", "failed", "canceled")


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ Agent Card
@dataclass(slots=True)
class AgentCard:
    name: str
    description: str
    version: str = "1.0"
    skills: List[str] = field(default_factory=list)
    endpoint: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    tools: List[str] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(
        default_factory=lambda: {"streaming": True, "pushNotifications": False, "stateTransitionHistory": True}
    )
    provider: Dict[str, str] = field(default_factory=lambda: {"organization": "商枢 CommercePivot"})

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "skills": self.skills,
            "endpoint": self.endpoint,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "tools": self.tools,
            "capabilities": self.capabilities,
            "provider": self.provider,
        }


# ------------------------------------------------------------------ 任务记录
@dataclass(slots=True)
class TaskRecord:
    id: str
    agent_name: str
    status: str = "submitted"
    input: Dict[str, Any] = field(default_factory=dict)
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    elapsed_ms: int = 0
    request_id: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "status": self.status,
            "input": self.input,
            "output": self.output,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


TASK_STORE: Dict[str, TaskRecord] = {}
_STORE_LOCK = threading.Lock()


def get_task(task_id: str) -> Optional[TaskRecord]:
    with _STORE_LOCK:
        return TASK_STORE.get(task_id)


def list_tasks(limit: int = 50, agent_name: str | None = None) -> List[TaskRecord]:
    with _STORE_LOCK:
        items = list(TASK_STORE.values())
    if agent_name:
        items = [t for t in items if t.agent_name == agent_name]
    items.sort(key=lambda t: t.created_at, reverse=True)
    return items[:limit]


def _persist(record: TaskRecord) -> None:
    try:
        from commercepivot.db import repository as repo

        repo.save_agent_task(
            task_id=record.id,
            agent_name=record.agent_name,
            status=record.status,
            payload_in=record.input,
            payload_out=record.output or {"error": record.error},
            elapsed_ms=record.elapsed_ms,
            request_id=record.request_id,
        )
    except Exception as exc:  # noqa: BLE001 - 持久化失败不影响主流程
        log.warning("Agent 任务落库失败", task_id=record.id, error=str(exc))


# ------------------------------------------------------------------ 熔断器
class CircuitBreaker:
    """连续失败 N 次后打开，冷却期内直接拒绝调用。"""

    def __init__(self, name: str, fail_threshold: int = 3, reset_s: float = 30.0) -> None:
        self.name = name
        self.fail_threshold = max(1, fail_threshold)
        self.reset_s = reset_s
        self.failures = 0
        self.opened_at: Optional[float] = None
        self.state = "closed"  # closed | open | half-open
        self._lock = threading.Lock()

    def before_call(self) -> None:
        with self._lock:
            if self.state == "open" and self.opened_at is not None:
                if time.time() - self.opened_at >= self.reset_s:
                    self.state = "half-open"
                else:
                    remaining = self.reset_s - (time.time() - self.opened_at)
                    raise CircuitOpenError(
                        f"Agent {self.name} 熔断中，{remaining:.0f}s 后重试",
                        details={"agent": self.name, "failures": self.failures},
                    )

    def on_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.state = "closed"
            self.opened_at = None

    def on_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.fail_threshold:
                self.state = "open"
                self.opened_at = time.time()
                DEGRADED.inc({"component": f"agent:{self.name}"})

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "failures": self.failures,
                "threshold": self.fail_threshold,
                "reset_s": self.reset_s,
            }


# ------------------------------------------------------------------ Agent 基类
INPUT_SCHEMA_DEFAULT: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "用户原始问题"},
        "intent": {"type": "string", "description": "主意图"},
        "slots": {
            "type": "object",
            "description": "已抽取的槽位，如 platform / period / metric / order / limit",
        },
        "top_k": {"type": "integer", "description": "返回条数"},
    },
    "required": ["question"],
}

OUTPUT_SCHEMA_DEFAULT: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {"type": "string"},
        "status": {"type": "string"},
        "data": {"type": "object", "description": "结构化数据（rows / summary）"},
        "findings": {"type": "array", "items": {"type": "string"}, "description": "关键结论"},
        "metrics": {"type": "object", "description": "核心指标"},
        "citations": {"type": "array", "description": "知识来源"},
        "degraded": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}


class BaseAgent:
    name: str = "base_agent"
    description: str = ""
    version: str = "1.0"
    skills: List[str] = []
    tools: List[str] = []
    port: int = 0
    input_schema: Dict[str, Any] = INPUT_SCHEMA_DEFAULT
    output_schema: Dict[str, Any] = OUTPUT_SCHEMA_DEFAULT

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self.timeout_s = settings.a2a_timeout_s
        self.breaker = CircuitBreaker(
            self.name, settings.a2a_circuit_fail_threshold, settings.a2a_circuit_reset_s
        )
        self._mcp: Optional[MCPClient] = None

    # -------------------------------------------------------------- 元信息
    def endpoint(self, sync: bool = False) -> str:
        if self.port:
            return f"http://localhost:{self.port}/a2a"
        return f"http://localhost:{self._settings.api_port}/api/v1/a2a/{self.name}"

    def card(self) -> AgentCard:
        return AgentCard(
            name=self.name,
            description=self.description,
            version=self.version,
            skills=list(self.skills),
            endpoint=self.endpoint(),
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            tools=list(self.tools),
        )

    # -------------------------------------------------------------- MCP
    @property
    def mcp(self) -> MCPClient:
        if self._mcp is None:
            self._mcp = MCPClient()
        return self._mcp

    async def call_tool(self, tool: str, params: Dict[str, Any] | None = None,
                        session_id: str | None = None) -> Dict[str, Any]:
        """通过 MCP 工具层取数（Agent 不直连数据库）。"""
        trace_add("agent-tool", agent=self.name, tool=tool)
        return await self.mcp.call(tool, params or {}, role="operator", session_id=session_id)

    @staticmethod
    def fail_reason(result: Dict[str, Any]) -> Optional[str]:
        if not result.get("ok"):
            return result.get("message") or result.get("code") or "工具调用失败"
        return None

    # -------------------------------------------------------------- 执行
    async def handle(self, task_input: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def run(
        self,
        task_input: Dict[str, Any],
        task_id: str | None = None,
        persist: bool = True,
    ) -> TaskRecord:
        started = time.perf_counter()
        record = TaskRecord(
            id=task_id or f"{self.name}-{uuid.uuid4().hex[:12]}",
            agent_name=self.name,
            status="submitted",
            input=dict(task_input or {}),
            request_id=get_request_id(),
        )
        with _STORE_LOCK:
            TASK_STORE[record.id] = record
        if persist:
            _persist(record)

        try:
            self.breaker.before_call()
        except CircuitOpenError as exc:
            record.status = "failed"
            record.error = exc.message
            record.elapsed_ms = int((time.perf_counter() - started) * 1000)
            record.updated_at = _now()
            A2A_TASKS.inc({"agent": self.name, "status": "circuit_open"})
            if persist:
                _persist(record)
            return record

        record.status = "working"
        record.updated_at = _now()
        if persist:
            _persist(record)

        try:
            output = await asyncio.wait_for(self.handle(record.input), timeout=self.timeout_s)
            record.output = self._normalize(output)
            record.status = "completed"
            self.breaker.on_success()
        except asyncio.TimeoutError:
            record.status = "failed"
            record.error = f"Agent 超时（>{self.timeout_s}s），返回部分结果"
            self.breaker.on_failure()
        except Exception as exc:  # noqa: BLE001
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            self.breaker.on_failure()
            log.warning("Agent 执行失败", agent=self.name, error=record.error)

        record.elapsed_ms = int((time.perf_counter() - started) * 1000)
        record.updated_at = _now()
        A2A_TASKS.inc({"agent": self.name, "status": record.status})
        A2A_LATENCY.observe(record.elapsed_ms / 1000.0, {"agent": self.name})
        trace_add("a2a", agent=self.name, status=record.status, elapsed_ms=record.elapsed_ms)
        if persist:
            _persist(record)
        return record

    def _normalize(self, output: Dict[str, Any] | None) -> Dict[str, Any]:
        payload = dict(output or {})
        payload.setdefault("agent", self.name)
        payload.setdefault("agent_name", self.name)
        payload.setdefault("status", "completed")
        payload.setdefault("data", {})
        payload.setdefault("findings", [])
        payload.setdefault("metrics", {})
        payload.setdefault("citations", [])
        payload.setdefault("notes", [])
        payload.setdefault("degraded", False)
        payload.setdefault("degrade_reason", None)
        return payload

    def cancel(self, task_id: str) -> Dict[str, Any]:
        record = get_task(task_id)
        if record is None:
            raise AgentNotFoundError(f"任务不存在：{task_id}")
        if record.status in TERMINAL_STATES:
            return {"ok": False, "reason": f"任务已是终态 {record.status}", "task": record.as_dict()}
        record.status = "canceled"
        record.updated_at = _now()
        _persist(record)
        with _STORE_LOCK:
            TASK_STORE[record.id] = record
        return {"ok": True, "task": record.as_dict()}

    # -------------------------------------------------------------- JSON-RPC
    async def jsonrpc(self, method: str, params: Dict[str, Any] | None, req_id: Any = None) -> Dict[str, Any]:
        params = params or {}
        if method == "tasks/send":
            task_input = params.get("input") or params.get("message") or params
            if not isinstance(task_input, dict):
                task_input = {"question": str(task_input)}
            if str(task_input.get("role", "")).lower() == "user" and "parts" in task_input:
                # 兼容 A2A 原生 message 形态：{role, parts:[{text:...}]}
                texts = [p.get("text", "") for p in task_input.get("parts", []) if isinstance(p, dict)]
                task_input = {"question": " ".join(t for t in texts if t)}
            if params.get("blocking", True) is False:
                task_id = f"{self.name}-{uuid.uuid4().hex[:12]}"
                asyncio.create_task(self.run(task_input, task_id=task_id))
                await asyncio.sleep(0)
                record = get_task(task_id)
                return {"ok": True, "task": record.as_dict() if record else {"id": task_id, "status": "submitted"}}
            record = await self.run(task_input)
            return {"ok": record.status == "completed", "task": record.as_dict()}
        if method == "tasks/get":
            record = get_task(str(params.get("id") or params.get("task_id") or ""))
            if record is None:
                raise AgentNotFoundError(f"任务不存在：{params.get('id')}")
            return {"ok": True, "task": record.as_dict()}
        if method == "tasks/cancel":
            return self.cancel(str(params.get("id") or params.get("task_id") or ""))
        if method in ("agent/card", "agent/getCard"):
            return {"ok": True, "card": self.card().as_dict()}
        raise AgentNotFoundError(
            f"不支持的 JSON-RPC 方法：{method}",
            details={"supported": ["tasks/send", "tasks/get", "tasks/cancel", "agent/card"]},
        )

    # -------------------------------------------------------------- 观测
    def status(self) -> Dict[str, Any]:
        with _STORE_LOCK:
            mine = [t for t in TASK_STORE.values() if t.agent_name == self.name]
        counts: Dict[str, int] = {}
        for t in mine:
            counts[t.status] = counts.get(t.status, 0) + 1
        return {
            "agent": self.name,
            "description": self.description,
            "skills": self.skills,
            "tools": self.tools,
            "timeout_s": self.timeout_s,
            "breaker": self.breaker.snapshot(),
            "task_counts": counts,
        }


__all__ = [
    "INPUT_SCHEMA_DEFAULT",
    "OUTPUT_SCHEMA_DEFAULT",
    "TASK_STATES",
    "TERMINAL_STATES",
    "AgentCard",
    "BaseAgent",
    "CircuitBreaker",
    "TaskRecord",
    "get_task",
    "list_tasks",
]
