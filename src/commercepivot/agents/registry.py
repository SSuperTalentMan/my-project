"""A2A Agent 注册与调度（架构文档 §3.2 a2a_route_node 的底座）。

能力：
- **注册/发现**：按名称或 skill 找到 Agent，输出 Agent Card 列表；
- **Card 缓存**：Agent Card 写入 Redis ``agent_card:{name}``（§6），
  避免每次外部发现都重建 schema；
- **并行调度**：``dispatch_many`` 用 ``asyncio.gather`` 同时唤起多个子 Agent，
  对应「planning_node 判断需要调用哪些 A2A Agent」的多 Agent 场景；
- **部分结果**：任一 Agent 超时/熔断/失败都不影响其他 Agent，
  失败的那个产出一条 ``status=failed`` 的记录，由聚合节点决定如何呈现（§11）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from commercepivot.agents.after_sales_agent import AGENT as AFTER_SALES_AGENT
from commercepivot.agents.base import BaseAgent, TaskRecord
from commercepivot.agents.order_agent import AGENT as ORDER_AGENT
from commercepivot.agents.product_agent import AGENT as PRODUCT_AGENT
from commercepivot.agents.rag_agent import AGENT as RAG_AGENT
from commercepivot.agents.report_agent import AGENT as REPORT_AGENT
from commercepivot.core.context import get_request_id, trace_add
from commercepivot.core.errors import AgentNotFoundError, CircuitOpenError
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import A2A_TASKS

log = get_logger("commercepivot.agents.registry")


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: Dict[str, BaseAgent] = {}
        self._by_skill: Dict[str, str] = {}

    # ---------------------------------------------------------- 注册
    def register(self, agent: BaseAgent) -> BaseAgent:
        if agent.name in self._agents:
            raise ValueError(f"Agent 重复注册：{agent.name}")
        self._agents[agent.name] = agent
        for skill in agent.skills:
            self._by_skill.setdefault(skill, agent.name)
        return agent

    def register_all(self, agents: Iterable[BaseAgent]) -> None:
        for agent in agents:
            if agent.name not in self._agents:
                self.register(agent)

    # ---------------------------------------------------------- 查询
    def get(self, name: str) -> BaseAgent:
        agent = self._agents.get(name)
        if agent is None:
            raise AgentNotFoundError(
                f"Agent 未注册：{name}", details={"available": self.names()}
            )
        return agent

    def has(self, name: str) -> bool:
        return name in self._agents

    def names(self) -> List[str]:
        return sorted(self._agents)

    def list_agents(self) -> List[BaseAgent]:
        return [self._agents[n] for n in self.names()]

    def resolve_skill(self, skill: str) -> BaseAgent:
        name = self._by_skill.get(skill)
        if not name:
            raise AgentNotFoundError(f"没有 Agent 提供 skill：{skill}", details={"skills": sorted(self._by_skill)})
        return self.get(name)

    def all_skills(self) -> Dict[str, str]:
        return dict(self._by_skill)

    # ---------------------------------------------------------- Agent Card
    def card(self, name: str, use_cache: bool = True) -> Dict[str, Any]:
        agent = self.get(name)
        if use_cache:
            try:
                from commercepivot.db.redis import get_redis

                cached = get_redis().load_agent_card(name)
                if cached:
                    cached["_cached"] = True
                    return cached
            except Exception:  # Redis 不可用时静默跳过
                pass
        card = agent.card().as_dict()
        if use_cache:
            try:
                from commercepivot.db.redis import get_redis

                get_redis().cache_agent_card(name, card)
            except Exception:
                pass
        return card

    def cards(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        return [self.card(n, use_cache=use_cache) for n in self.names()]

    def directory(self) -> Dict[str, Any]:
        """A2A 发现入口：把全部 Agent Card 打包返回。"""
        return {
            "ok": True,
            "count": len(self._agents),
            "skills_index": self.all_skills(),
            "agents": [
                {
                    "name": a.name,
                    "description": a.description,
                    "skills": a.skills,
                    "endpoint": a.endpoint(),
                    "tools": a.tools,
                }
                for a in self.list_agents()
            ],
        }

    # ---------------------------------------------------------- 调度
    async def dispatch(
        self,
        name: str,
        task_input: Dict[str, Any],
        task_id: str | None = None,
    ) -> TaskRecord:
        agent = self.get(name)
        trace_add("a2a-dispatch", agent=name)
        return await agent.run(task_input, task_id=task_id)

    async def dispatch_many(
        self,
        plan: Sequence[Tuple[str, Dict[str, Any]]],
        task_ids: Optional[Dict[str, str]] = None,
    ) -> List[TaskRecord]:
        """并行唤起多个 Agent；单点失败不影响其他 Agent。"""
        if not plan:
            return []
        task_ids = task_ids or {}

        async def _one(name: str, payload: Dict[str, Any]) -> TaskRecord:
            try:
                return await self.dispatch(name, payload, task_ids.get(name))
            except (AgentNotFoundError, CircuitOpenError) as exc:
                record = TaskRecord(
                    id=task_ids.get(name) or f"{name}-rejected",
                    agent_name=name,
                    status="failed",
                    input=dict(payload or {}),
                    error=exc.message,
                    request_id=get_request_id(),
                )
                A2A_TASKS.inc({"agent": name, "status": "rejected"})
                log.warning("Agent 调度被拒", agent=name, error=exc.message)
                return record
            except Exception as exc:  # noqa: BLE001
                record = TaskRecord(
                    id=task_ids.get(name) or f"{name}-error",
                    agent_name=name,
                    status="failed",
                    input=dict(payload or {}),
                    error=f"{type(exc).__name__}: {exc}",
                    request_id=get_request_id(),
                )
                A2A_TASKS.inc({"agent": name, "status": "error"})
                return record

        results = await asyncio.gather(*[_one(n, p) for n, p in plan])
        return list(results)

    # ---------------------------------------------------------- 观测
    def status(self) -> Dict[str, Any]:
        return {
            "component": "a2a_agents",
            "count": len(self._agents),
            "skills": self.all_skills(),
            "agents": [a.status() for a in self.list_agents()],
        }


REGISTRY = AgentRegistry()
REGISTRY.register_all(
    [ORDER_AGENT, PRODUCT_AGENT, AFTER_SALES_AGENT, RAG_AGENT, REPORT_AGENT]
)


def get_agent_registry() -> AgentRegistry:
    return REGISTRY


__all__ = ["REGISTRY", "AgentRegistry", "get_agent_registry"]
