"""A2A 协议接口（架构文档 §4 与 §3.1 的 /api/v1/a2a 路由）。

暴露三类入口：
1. **Agent Card 发现**
   - ``GET /.well-known/agent-card.json``            主控 Agent 的卡片
   - ``GET /.well-known/agents``                     全部子 Agent 目录
   - ``GET /.well-known/agents/{name}/agent-card.json`` 单个子 Agent 卡片
   - ``GET /api/v1/a2a/agents/{name}/.well-known/agent-card.json`` 等价别名
2. **JSON-RPC 2.0 调用**
   - ``POST /api/v1/a2a/{agent_name}``  指定 Agent
   - ``POST /api/v1/a2a``               由 params.agent 指定（网关式调用）
   方法：``tasks/send`` / ``tasks/get`` / ``tasks/cancel`` / ``agent/card``
3. **任务查询**
   - ``GET /api/v1/a2a/tasks/{task_id}``、``GET /api/v1/a2a/tasks``
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from commercepivot.agents.base import get_task, list_tasks
from commercepivot.agents.registry import get_agent_registry
from commercepivot.api.schemas import AgentRpcRequest
from commercepivot.core.config import get_settings
from commercepivot.core.context import get_request_id
from commercepivot.core.errors import AgentNotFoundError, PivotError
from commercepivot.core.logging import get_logger
from commercepivot.core.security import Principal, principal_from_request, require

log = get_logger("commercepivot.api.a2a")

router = APIRouter(prefix="/api/v1/a2a", tags=["a2a"])
well_known = APIRouter(tags=["a2a-discovery"])


# ------------------------------------------------------------------ 主控 Agent 卡
def _orchestrator_card() -> Dict[str, Any]:
    settings = get_settings()
    registry = get_agent_registry()
    return {
        "name": "commercepivot_orchestrator",
        "description": "商枢主控 Agent：意图识别、槽位填充、A2A 规划调度与结果聚合",
        "version": settings.app_version,
        "skills": ["intent_recognition", "slot_filling", "agent_planning", "result_aggregation"],
        "endpoint": f"http://localhost:{settings.api_port}/api/v1/chat/ask",
        "streaming_endpoint": f"http://localhost:{settings.api_port}/api/v1/chat/ask_stream",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户问题"},
                "session_id": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["question"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "table": {"type": "object"},
                "intent": {"type": "object"},
                "slots": {"type": "object"},
                "citations": {"type": "array"},
                "degraded": {"type": "boolean"},
            },
        },
        "sub_agents": [
            {"name": a["name"], "card": a["endpoint"]} for a in registry.directory()["agents"]
        ],
        "capabilities": {"streaming": True, "a2aProtocol": "1.0", "jsonrpc": "2.0"},
        "provider": {"organization": "商枢 CommercePivot"},
    }


@well_known.get("/.well-known/agent-card.json", summary="主控 Agent Card")
async def root_agent_card() -> Dict[str, Any]:
    return _orchestrator_card()


@well_known.get("/.well-known/agents", summary="全部子 Agent 目录")
async def agents_directory() -> Dict[str, Any]:
    return get_agent_registry().directory()


@well_known.get("/.well-known/agents/{name}/agent-card.json", summary="子 Agent Card")
async def agent_card(name: str) -> Any:
    registry = get_agent_registry()
    if not registry.has(name):
        return JSONResponse(
            status_code=404,
            content={"code": "AGENT_NOT_FOUND", "message": f"Agent 未注册：{name}",
                     "available": registry.names()},
        )
    return registry.card(name)


@router.get("/directory", summary="A2A 目录（含 skills 索引）")
async def directory() -> Dict[str, Any]:
    return get_agent_registry().directory()


@router.get("/agents/{name}/.well-known/agent-card.json", summary="子 Agent Card（带 /api/v1/a2a 前缀）")
async def agent_card_prefixed(name: str) -> Any:
    return await agent_card(name)


# ------------------------------------------------------------------ JSON-RPC
async def _dispatch(agent_name: Optional[str], rpc: AgentRpcRequest) -> Any:
    registry = get_agent_registry()
    params = dict(rpc.params or {})
    target = agent_name or params.get("agent") or params.get("agent_name")
    req_id = rpc.id

    try:
        if not target:
            # 未指定 Agent 时，允许用 skill 路由
            skill = params.get("skill")
            if skill:
                agent = registry.resolve_skill(str(skill))
            else:
                raise AgentNotFoundError(
                    "未指定 agent，请在路径 /api/v1/a2a/{agent} 或 params.agent 中给出",
                    details={"available": registry.names(), "skills": registry.all_skills()},
                )
        else:
            agent = registry.get(str(target))

        env = await agent.jsonrpc(rpc.method, params, req_id)
        return {"jsonrpc": "2.0", "id": req_id, "result": env}
    except PivotError as exc:
        return JSONResponse(
            status_code=200,
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "data": {"request_id": get_request_id(), **(exc.details or {})},
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("A2A 调用异常", agent=str(target), error=str(exc))
        return JSONResponse(
            status_code=200,
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": "A2A_INTERNAL_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                    "data": {"request_id": get_request_id()},
                },
            },
        )


@router.post("/{agent_name}", summary="JSON-RPC 调用指定 Agent")
async def rpc_to_agent(
    agent_name: str,
    request: Request,
    rpc: AgentRpcRequest = Body(...),
) -> Any:
    request.state.principal = principal_from_request(request)
    return await _dispatch(agent_name, rpc)


@router.post("", summary="JSON-RPC 网关调用（由 params.agent 指定）")
async def rpc_gateway(request: Request, rpc: AgentRpcRequest = Body(...)) -> Any:
    request.state.principal = principal_from_request(request)
    return await _dispatch(None, rpc)


# ------------------------------------------------------------------ 任务
@router.get("/tasks/{task_id}", summary="查询 A2A 任务状态")
async def task_detail(task_id: str, _: Principal = Depends(require("a2a"))) -> Dict[str, Any]:
    record = get_task(task_id)
    if record is None:
        from commercepivot.db import repository as repo
        from commercepivot.db.mysql import MySQLUnavailable

        try:
            row = repo.get_agent_task(task_id)
        except MySQLUnavailable:
            row = None
        if row:
            return {"ok": True, "source": "mysql", "task": row}
        return JSONResponse(
            status_code=404,
            content={"code": "TASK_NOT_FOUND", "message": f"任务不存在：{task_id}"},
        )
    return {"ok": True, "source": "memory", "task": record.as_dict()}


@router.get("/tasks", summary="列出最近的 A2A 任务")
async def task_list(
    limit: int = Query(20, ge=1, le=200),
    agent: Optional[str] = Query(None),
    _: Principal = Depends(require("a2a")),
) -> Dict[str, Any]:
    records = list_tasks(limit=limit, agent_name=agent)
    return {"ok": True, "count": len(records), "tasks": [r.as_dict() for r in records]}


@router.post("/tasks/{task_id}/cancel", summary="取消 A2A 任务")
async def task_cancel(task_id: str, _: Principal = Depends(require("a2a"))) -> Dict[str, Any]:
    record = get_task(task_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={"code": "TASK_NOT_FOUND", "message": f"任务不存在：{task_id}"},
        )
    agent = get_agent_registry().get(record.agent_name)
    return agent.cancel(task_id)


__all__ = ["router", "well_known"]
