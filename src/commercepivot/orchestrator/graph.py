"""LangGraph 主控 Agent（架构文档 §3.2 编排层）。

图结构::

    START → intent_node → slot_node ─┬─(缺关键槽位)→ answer_node → END
                                     └─(槽位齐)      → planning_node
                                                     → a2a_route_node
                                                     → aggregate_node
                                                     → answer_node → END

为什么保留一个「顺序执行」的降级实现：
LangGraph 是编排层唯一的重量级依赖，若其导入/编译失败（版本冲突、被裁剪的环境），
整条问答链路不应直接 500。因此 ``Orchestrator`` 在启动时探测一次，
能编译就用图、否则退化为同一组节点的顺序调用 —— 对外行为完全一致，
只是少了 checkpoint / 可视化能力。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from commercepivot.core.context import ensure_trace, get_request_id, get_trace, request_context
from commercepivot.core.logging import get_logger
from commercepivot.orchestrator.nodes import (
    a2a_route_node,
    aggregate_node,
    answer_node,
    intent_node,
    planning_node,
    route_after_slot,
    slot_node,
)
from commercepivot.orchestrator.state import PivotState, initial_state

log = get_logger("commercepivot.orchestrator.graph")

NODE_ORDER = (
    "intent_node",
    "slot_node",
    "planning_node",
    "a2a_route_node",
    "aggregate_node",
    "answer_node",
)

_NODE_FUNCS = {
    "intent_node": intent_node,
    "slot_node": slot_node,
    "planning_node": planning_node,
    "a2a_route_node": a2a_route_node,
    "aggregate_node": aggregate_node,
    "answer_node": answer_node,
}


def build_graph() -> Optional[Any]:
    """尝试用 LangGraph 编译主图；失败返回 None。"""
    try:
        from langgraph.graph import END, START, StateGraph

        builder = StateGraph(PivotState)
        for name in NODE_ORDER:
            builder.add_node(name, _NODE_FUNCS[name])
        builder.add_edge(START, "intent_node")
        builder.add_edge("intent_node", "slot_node")
        builder.add_conditional_edges(
            "slot_node",
            route_after_slot,
            {"clarify": "answer_node", "chitchat": "answer_node", "plan": "planning_node"},
        )
        builder.add_edge("planning_node", "a2a_route_node")
        builder.add_edge("a2a_route_node", "aggregate_node")
        builder.add_edge("aggregate_node", "answer_node")
        builder.add_edge("answer_node", END)
        return builder.compile()
    except Exception as exc:  # noqa: BLE001
        log.warning("LangGraph 编译失败，改用顺序执行降级实现", error=f"{type(exc).__name__}: {exc}")
        return None


class Orchestrator:
    def __init__(self) -> None:
        self._graph: Optional[Any] = None
        self._graph_ready: Optional[bool] = None

    @property
    def mode(self) -> str:
        self._ensure()
        return "langgraph" if self._graph_ready else "sequential"

    def _ensure(self) -> None:
        if self._graph_ready is None:
            self._graph = build_graph()
            self._graph_ready = self._graph is not None
            log.info("编排器就绪", mode="langgraph" if self._graph_ready else "sequential")

    async def _run_sequential(self, state: PivotState) -> PivotState:
        merged: Dict[str, Any] = dict(state)
        for name in NODE_ORDER:
            if name == "planning_node" and route_after_slot(merged) == "clarify":
                merged.update(await answer_node(merged))  # type: ignore[arg-type]
                return merged  # type: ignore[return-value]
            merged.update(await _NODE_FUNCS[name](merged))  # type: ignore[arg-type]
        return merged  # type: ignore[return-value]

    async def ainvoke(self, state: PivotState) -> PivotState:
        self._ensure()
        # 兜底开启 trace：HTTP 路径由 RequestContextMiddleware 负责，直接调用
        # （CLI / 脚本 / 单测）时这里补一份，保证各节点 trace_add 有落点。
        ensure_trace()
        started = time.perf_counter()
        if self._graph is not None:
            result = await self._graph.ainvoke(state)
        else:
            result = await self._run_sequential(state)
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        result["orchestrator_mode"] = self.mode
        return result  # type: ignore[return-value]

    async def arun(
        self,
        question: str,
        session_id: str = "",
        user_id: str = "anonymous",
        role: str = "customer",
        top_k: int | None = None,
        request_id: str | None = None,
    ) -> PivotState:
        state = initial_state(
            question=question,
            session_id=session_id,
            user_id=user_id,
            role=role,
            top_k=top_k,
            request_id=request_id or get_request_id(),
        )
        return await self.ainvoke(state)

    def mermaid(self) -> str:
        """给文档 / 前端画图用的 Mermaid 定义。"""
        return "\n".join(
            [
                "graph LR",
                "  S((START)) --> I[intent_node]",
                "  I --> SL[slot_node]",
                "  SL -->|缺槽位| A[answer_node 追问]",
                "  SL -->|闲聊| A",
                "  SL -->|槽位齐| P[planning_node]",
                "  P --> R[a2a_route_node]",
                "  R --> AG[aggregate_node]",
                "  AG --> A",
                "  A --> E((END))",
            ]
        )

    # ---------------------------------------------------------------- 流式
    async def astream(
        self,
        question: str,
        session_id: str = "",
        user_id: str = "anonymous",
        role: str = "customer",
        top_k: int | None = None,
        request_id: str | None = None,
    ):
        """逐事件产出编排过程，供 /ask_stream 转成 SSE。

        与 ``ainvoke`` 的区别：这里**手工按节点顺序推进**，目的是在每个节点
        完成后立刻把事件推给前端（前端可以先渲染「意图卡片 → 规划 → 表格」，
        再等最终回答），而不是等整图跑完才吐结果。回答阶段如果 LLM 可用，
        走真正的 token 级流式（``llm.stream``）。
        """
        import asyncio

        from commercepivot.core.errors import LLMError
        from commercepivot.orchestrator import nodes
        from commercepivot.orchestrator.state import initial_state

        state = initial_state(
            question=question,
            session_id=session_id,
            user_id=user_id,
            role=role,
            top_k=top_k,
            request_id=request_id or get_request_id(),
        )
        # 与 ainvoke 同理：流式路径也要保证 trace 有落点（HTTP 下中间件已开，
        # 这里 ensure 只会复用，不会覆盖）。
        ensure_trace()
        started = time.perf_counter()
        yield "start", {"question": question, "session_id": session_id, "request_id": state["request_id"]}

        state.update(await nodes.intent_node(state))
        yield "intent", state.get("intent") or {}

        state.update(await nodes.slot_node(state))
        yield "slots", {
            "slots": state.get("slots") or {},
            "missing": state.get("missing_slots") or [],
            "defaults_applied": state.get("defaults_applied") or [],
        }

        route = route_after_slot(state)
        if route == "clarify":
            state.update(await nodes.answer_node(state))
            yield "answer_delta", {"text": state.get("answer", "")}
            state["answer_source"] = "clarification"
        elif route == "chitchat":
            # 闲聊短路：一条 plan 事件（空计划）+ 整段回答。
            # 不产生 agents / findings / table 事件 —— 因为根本没有任何 Agent 被执行。
            state.update(await nodes.answer_node(state))
            yield "plan", {"plan": [], "reason": state.get("plan_reason")}
            yield "answer_delta", {"text": state.get("answer", "")}
        else:
            state.update(await nodes.planning_node(state))
            yield "plan", {"plan": state.get("plan") or [], "reason": state.get("plan_reason")}

            state.update(await nodes.a2a_route_node(state))
            yield "agents", {
                "tasks": [
                    {
                        "agent": t.get("agent_name"),
                        "status": t.get("status"),
                        "elapsed_ms": t.get("elapsed_ms"),
                        "error": t.get("error"),
                    }
                    for t in (state.get("agent_tasks") or [])
                ]
            }

            state.update(await nodes.aggregate_node(state))
            yield "findings", {
                "findings": state.get("findings") or [],
                "metrics": state.get("metrics") or {},
                "citations": state.get("citations") or [],
                "degraded": state.get("degraded"),
                "degrade_reasons": state.get("degrade_reasons") or [],
            }
            if state.get("table"):
                yield "table", state["table"]

            # ---- 回答阶段
            from commercepivot.models.llm_client import get_llm

            llm = get_llm()
            produced = ""
            answer_source = "template"
            answer_meta: Dict[str, Any] = {}
            if llm.available:
                try:
                    messages = [
                        {"role": "system", "content": nodes.ANSWER_SYSTEM_PROMPT},
                        {"role": "user", "content": nodes.build_llm_context(state)},
                    ]
                    async for delta in llm.stream(messages, max_tokens=800):
                        produced += delta
                        yield "answer_delta", {"text": delta}
                    if produced.strip():
                        answer_source = "llm"
                        answer_meta = {"model": llm.model, "streamed": True}
                except LLMError as exc:
                    log.warning("流式回答降级为模板", error=exc.message)
                    answer_meta = {"llm_error": exc.message, "llm_degraded": True}
                except Exception as exc:  # noqa: BLE001
                    log.warning("流式回答异常，降级为模板", error=str(exc))
                    answer_meta = {"llm_error": f"{type(exc).__name__}: {exc}"}
            else:
                answer_meta = {"llm_degraded": True, "llm_error": "未配置 API_KEY（正常降级）"}

            if not produced.strip():
                produced = nodes.build_template_answer(state)
                answer_source = "template"
                yield "answer_delta", {"text": produced}

            state["answer"] = produced
            state["answer_source"] = answer_source
            state["answer_meta"] = answer_meta

        state["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        state["orchestrator_mode"] = self.mode
        # 流式路径不经过 answer_node，需在此显式快照全链路 trace（与 /ask 同源）。
        state["trace"] = get_trace()
        yield "done", {
            "answer": state.get("answer", ""),
            "answer_source": state.get("answer_source"),
            "answer_meta": state.get("answer_meta") or {},
            "elapsed_ms": state["elapsed_ms"],
            "orchestrator_mode": state["orchestrator_mode"],
            "node_timings": state.get("node_timings") or {},
            "orchestrator_state": _slim_state(state),
        }


def _slim_state(state: PivotState) -> Dict[str, Any]:
    """流式收尾事件里回传的状态摘要（去掉大对象，只留可解释的元信息）。"""
    return {
        "intent": state.get("intent"),
        "slots": state.get("slots"),
        "plan": state.get("plan"),
        "plan_reason": state.get("plan_reason"),
        "findings": state.get("findings"),
        "metrics": state.get("metrics"),
        "citations": state.get("citations"),
        "notes": state.get("notes"),
        "degraded": state.get("degraded"),
        "degrade_reasons": state.get("degrade_reasons"),
        "table": state.get("table"),
        "errors": state.get("errors"),
        # 与 /ask 的 include_trace 同源：共用同一份共享列表，因此此处也是全量。
        "trace": state.get("trace") or [],
    }


_ORCHESTRATOR: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        _ORCHESTRATOR = Orchestrator()
    return _ORCHESTRATOR


__all__ = ["NODE_ORDER", "Orchestrator", "build_graph", "get_orchestrator"]
