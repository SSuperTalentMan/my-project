"""问答接口（架构文档 §3.1 接入层 + §7 核心流程）。

``POST /api/v1/chat/ask``        一次性返回完整答案
``POST /api/v1/chat/ask_stream`` SSE 逐事件返回（意图 → 规划 → 子 Agent → 表格 → 回答增量）
``POST /api/v1/auth/token``      签发 JWT
``GET  /api/v1/chat/sessions/{id}`` 会话历史（MySQL + Redis）

接入层做四件事，顺序与 §7 完全对应：**鉴权 → 限流 → 缓存 → 会话落库**。

关于缓存键：文档 §6 规定 ``cache:ask:{skill}:{role}:{hash}`` 带 ``skill`` 维度，
而 skill 来自意图识别。因此在入口处先做一次轻量意图分类来构造缓存键（规则分支
耗时在微秒级；BERT 分支复用同一个单例），命中即返回，避免整条 A2A 链路空跑。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError as PydanticValidationError
from sse_starlette.sse import EventSourceResponse

from commercepivot.core.config import get_settings
from commercepivot.core.context import get_request_id
from commercepivot.core.errors import ValidationFailedError
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import CACHE_CALLS, HTTP_LATENCY
from commercepivot.core.security import (
    Principal,
    create_access_token,
    principal_from_request,
    rate_limit_dependency,
    require,
)
from commercepivot.orchestrator.graph import get_orchestrator
from commercepivot.orchestrator.state import PivotState
from commercepivot.api.schemas import AskRequest, AskResponse, TokenRequest, TokenResponse

log = get_logger("commercepivot.api.chat")

router = APIRouter(tags=["chat"])


# ------------------------------------------------------------------ 工具函数
def _new_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:16]}"


def _state_to_payload(state: PivotState, session_id: str, cached: bool = False,
                      include_trace: bool = False) -> Dict[str, Any]:
    aggregated = state.get("aggregated") or {}
    payload = {
        "answer": state.get("answer", ""),
        "session_id": session_id,
        "request_id": state.get("request_id") or get_request_id(),
        "answer_source": state.get("answer_source", "template"),
        "answer_meta": state.get("answer_meta") or {},
        "cached": cached,
        "intent": state.get("intent") or {},
        "slots": state.get("slots") or {},
        "missing_slots": state.get("missing_slots") or [],
        "defaults_applied": state.get("defaults_applied") or [],
        "plan": [
            {"agent": p.get("agent"), "intent": p.get("intent"), "focus": p.get("focus"), "role": p.get("role")}
            for p in (state.get("plan") or [])
        ],
        "plan_reason": state.get("plan_reason", ""),
        "agent_tasks": [
            {
                "id": t.get("id"),
                "agent": t.get("agent_name"),
                "status": t.get("status"),
                "elapsed_ms": t.get("elapsed_ms"),
                "error": t.get("error"),
            }
            for t in (state.get("agent_tasks") or [])
        ],
        "table": state.get("table"),
        "findings": state.get("findings") or [],
        "metrics": state.get("metrics") or {},
        "citations": state.get("citations") or [],
        "sources": [
            {
                "agent": s.get("agent"),
                "kind": s.get("kind"),
                "row_count": s.get("row_count"),
                "source_tools": s.get("source_tools"),
                "saved_path": s.get("saved_path"),
                "summary": s.get("summary"),
            }
            for s in (aggregated.get("sources") or [])
        ],
        "degraded": bool(state.get("degraded")),
        "degrade_reasons": state.get("degrade_reasons") or [],
        "notes": state.get("notes") or [],
        "errors": state.get("errors") or [],
        "node_timings": state.get("node_timings") or {},
        "elapsed_ms": state.get("elapsed_ms", 0),
        "orchestrator_mode": state.get("orchestrator_mode", ""),
    }
    if include_trace:
        payload["trace"] = state.get("trace") or []
    return payload


def _persist_turn(session_id: str, user_id: str, role: str, question: str, answer: str) -> None:
    """会话落库（MySQL chat_messages）+ 上下文写 Redis。Redis 失败不影响主流程。"""
    try:
        from commercepivot.db import repository as repo
        from commercepivot.db.redis import get_redis

        repo.ensure_session(session_id, user_id=user_id, role=role)
        repo.append_message(session_id, "user", question)
        repo.append_message(session_id, "assistant", answer)
        redis = get_redis()
        redis.push_session_turn(session_id, "user", question)
        redis.push_session_turn(session_id, "assistant", answer)
    except Exception as exc:  # noqa: BLE001
        log.warning("会话落库失败（不影响回答）", session_id=session_id, error=str(exc))


def _load_history(session_id: str) -> List[Dict[str, Any]]:
    from commercepivot.db import repository as repo

    return repo.list_messages(session_id, limit=30)


# ------------------------------------------------------------------ 鉴权
@router.post("/auth/token", response_model=TokenResponse, summary="签发 JWT")
async def issue_token(payload: TokenRequest) -> TokenResponse:
    settings = get_settings()
    expires = payload.expires_minutes or settings.jwt_expire_minutes
    token = create_access_token(payload.subject, payload.role, expires_minutes=expires)
    return TokenResponse(
        access_token=token, role=payload.role, expires_minutes=expires
    )


# ------------------------------------------------------------------ 一次性问答
@router.post(
    "/chat/ask",
    summary="经营分析问答（完整链路：意图→槽位→A2A→MCP→聚合→LLM）",
    dependencies=[Depends(rate_limit_dependency)],
)
async def ask(request: Request, payload: AskRequest) -> Dict[str, Any]:
    principal = principal_from_request(request)
    if not principal.can("chat"):
        from commercepivot.core.errors import ForbiddenError

        raise ForbiddenError(f"角色 {principal.role} 无权提问")
    request.state.principal = principal

    session_id = payload.session_id or _new_session_id()
    settings = get_settings()

    # ---- 缓存：先做一次轻量意图分类构造 skill 维度
    from commercepivot.models.intent_bert import classify_intent

    skill = "unknown"
    try:
        skill = classify_intent(payload.question).primary
    except Exception:  # noqa: BLE001
        pass

    redis_client = None
    if payload.use_cache:
        try:
            from commercepivot.db.redis import get_redis

            redis_client = get_redis()
            cached = redis_client.cache_get(skill, principal.role, payload.question)
            if cached:
                CACHE_CALLS.inc({"result": "hit"})
                cached["cached"] = True
                cached["request_id"] = get_request_id()
                cached["session_id"] = session_id
                return cached
            CACHE_CALLS.inc({"result": "miss"})
        except Exception as exc:  # noqa: BLE001
            CACHE_CALLS.inc({"result": "bypass"})
            log.warning("缓存不可用，直接走全链路", error=str(exc))

    orchestrator = get_orchestrator()
    state = await orchestrator.arun(
        question=payload.question,
        session_id=session_id,
        user_id=payload.user_id or principal.subject,
        role=principal.role,
        top_k=payload.top_k,
        request_id=get_request_id(),
    )
    result = _state_to_payload(state, session_id, include_trace=payload.include_trace)

    _persist_turn(
        session_id, payload.user_id or principal.subject, principal.role,
        payload.question, result.get("answer", ""),
    )

    if redis_client is not None and not result.get("degraded"):
        try:
            redis_client.cache_set(skill, principal.role, payload.question, result)
        except Exception:  # noqa: BLE001
            pass
    return result


# ------------------------------------------------------------------ 流式问答
@router.post(
    "/chat/ask_stream",
    summary="经营分析问答（SSE 逐事件流式返回）",
    dependencies=[Depends(rate_limit_dependency)],
)
async def ask_stream(request: Request, payload: AskRequest) -> EventSourceResponse:
    principal = principal_from_request(request)
    request.state.principal = principal
    session_id = payload.session_id or _new_session_id()
    orchestrator = get_orchestrator()

    async def event_stream() -> AsyncIterator[Dict[str, str]]:
        try:
            final_state: Dict[str, Any] = {}
            async for event, data in orchestrator.astream(
                question=payload.question,
                session_id=session_id,
                user_id=payload.user_id or principal.subject,
                role=principal.role,
                top_k=payload.top_k,
                request_id=get_request_id(),
            ):
                if event == "done":
                    final_state = data
                    payload_out = {
                        "answer": data.get("answer"),
                        "answer_source": data.get("answer_source"),
                        "answer_meta": data.get("answer_meta"),
                        "elapsed_ms": data.get("elapsed_ms"),
                        "node_timings": data.get("node_timings"),
                        "orchestrator_mode": data.get("orchestrator_mode"),
                        "session_id": session_id,
                        "request_id": get_request_id(),
                        **(data.get("orchestrator_state") or {}),
                    }
                    yield {"event": "done", "data": json.dumps(payload_out, ensure_ascii=False, default=str)}
                else:
                    yield {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}

            answer = (final_state.get("answer") or "")
            if answer:
                _persist_turn(
                    session_id, payload.user_id or principal.subject, principal.role,
                    payload.question, answer,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("流式问答异常", error=f"{type(exc).__name__}: {exc}")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"code": "STREAM_ERROR", "message": f"{type(exc).__name__}: {exc}",
                     "request_id": get_request_id()},
                    ensure_ascii=False,
                ),
            }

    return EventSourceResponse(event_stream(), ping=15)


# ------------------------------------------------------------------ 会话
@router.get("/chat/sessions/{session_id}", summary="查询会话历史")
async def session_detail(
    session_id: str,
    limit: int = Query(30, ge=1, le=200),
    _: Principal = Depends(require("chat")),
) -> Dict[str, Any]:
    from commercepivot.db.mysql import MySQLUnavailable

    out: Dict[str, Any] = {"session_id": session_id, "role": "customer", "messages": [], "turns": []}
    try:
        from commercepivot.db import repository as repo

        session = repo.get_session(session_id)
        if session:
            out.update(session)
        out["messages"] = repo.list_messages(session_id, limit=limit)
    except MySQLUnavailable as exc:
        out["degraded"] = True
        out["reason"] = f"MySQL 不可用：{exc}"
    try:
        from commercepivot.db.redis import get_redis

        cached = get_redis().load_session(session_id)
        if cached:
            out["turns"] = cached.get("turns", [])
    except Exception:  # noqa: BLE001
        pass
    return out


@router.post("/chat/cache/flush", summary="清空问答缓存（仅 admin）")
async def flush_cache(_: Principal = Depends(require("chat", "*"))) -> Dict[str, Any]:
    from commercepivot.db.redis import get_redis

    deleted = get_redis().flush_cache()
    return {"ok": True, "deleted": deleted}


__all__ = ["router"]
