"""编排状态（架构文档 §3.2 主控 Agent 的共享状态）。

LangGraph 的 State 是一个 ``TypedDict``：每个节点只返回**自己写的那部分键**，
框架负责合并。这里把所有键集中声明，好处是：
1. 节点函数签名统一 ``(state) -> dict``，可单独单测；
2. 状态字段即对外可观测的中间产物，``/api/v1/chat/ask`` 可选择性回传，
   便于排查「为什么给出这个答案」；
3. 与 ``orchestrator/nodes.py`` 的节点一一对应，阅读顺序 = 执行顺序。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class PivotState(TypedDict, total=False):
    # ---------- 输入 ----------
    question: str
    session_id: str
    user_id: str
    role: str
    top_k: int
    skill: str  # 缓存键里的技能维度，默认取主意图
    request_id: str

    # ---------- intent_node ----------
    intent: Dict[str, Any]

    # ---------- slot_node ----------
    slots: Dict[str, Any]
    missing_slots: List[str]
    clarification: Optional[str]
    slot_notes: List[str]
    defaults_applied: List[str]

    # ---------- planning_node ----------
    plan: List[Dict[str, Any]]
    plan_reason: str

    # ---------- a2a_route_node ----------
    agent_tasks: List[Dict[str, Any]]

    # ---------- aggregate_node ----------
    aggregated: Dict[str, Any]
    findings: List[str]
    metrics: Dict[str, Any]
    citations: List[Dict[str, Any]]
    table: Optional[Dict[str, Any]]
    degraded: bool
    degrade_reasons: List[str]
    notes: List[str]

    # ---------- answer_node ----------
    answer: str
    answer_source: str  # llm | template | clarification | chitchat | error
    answer_meta: Dict[str, Any]

    # ---------- 全链路 ----------
    errors: List[str]
    trace: List[Dict[str, Any]]
    node_timings: Dict[str, int]
    elapsed_ms: int


def initial_state(
    question: str,
    session_id: str = "",
    user_id: str = "anonymous",
    role: str = "customer",
    top_k: int | None = None,
    request_id: str = "",
    skill: str = "",
) -> PivotState:
    return PivotState(
        question=question,
        session_id=session_id,
        user_id=user_id,
        role=role,
        top_k=top_k or 0,
        request_id=request_id,
        skill=skill,
        errors=[],
        notes=[],
        degrade_reasons=[],
        trace=[],
        node_timings={},
    )


__all__ = ["PivotState", "initial_state"]
