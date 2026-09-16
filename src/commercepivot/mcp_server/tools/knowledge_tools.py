"""知识库检索与工单类 MCP 工具（架构文档 §5 工具清单 6、8）。

- ``search_knowledge`` —— 检索商品知识 / FAQ / 平台规则。
  走 retrieval.service：BGE-M3 稠密+稀疏 → Milvus hybrid → Reranker 精排；
  Milvus 不可用时按 §11 自动降级为 MySQL FULLTEXT / LIKE，并在返回体里
  用 ``degraded / degrade_reason / retrieval_mode`` 如实标注。
- ``create_ticket``    —— 创建人工工单。**写操作**，受 ``ENABLE_WRITE_OPS`` 门禁保护。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from commercepivot.core.config import get_settings
from commercepivot.core.errors import ForbiddenError
from commercepivot.core.security import ensure_write_allowed
from commercepivot.db import repository as repo
from commercepivot.mcp_server.registry import get_registry

registry = get_registry()


class SearchKnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=500, description="检索问题，如「7天无理由退货的条件是什么」")
    top_k: int = Field(5, ge=1, le=20, description="返回条数")
    collection: Optional[str] = Field(
        None, description="限定集合：product_kb（商品知识）/ faq_kb（客服 FAQ 与平台规则）；留空则全部"
    )
    use_rerank: bool = Field(True, description="是否启用 BGE-Reranker 精排")
    min_score: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="最低相关性阈值，低于该值的结果会被过滤"
    )


class CreateTicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=2, max_length=200, description="问题摘要")
    priority: str = Field("P2", description="优先级：P0（紧急）/ P1 / P2 / P3")
    contact: Optional[str] = Field(None, max_length=120, description="联系方式（手机号或邮箱，落库前会被脱敏）")
    session_id: Optional[str] = Field(None, max_length=64, description="关联会话 ID")


@registry.tool(
    name="search_knowledge",
    description=(
        "检索商品知识、客服 FAQ、平台规则与售后政策。返回命中片段、来源与相关性分数。"
        "用于回答「怎么申请换货」「平台对七天无理由的规定是什么」等知识型问题。只读。"
    ),
    input_model=SearchKnowledgeInput,
    readonly=True,
    tags=["rag", "knowledge", "read"],
    owner_agent="knowledge_rag_agent",
    examples=[
        {"query": "七天无理由退货的条件", "top_k": 3},
        {"query": "记忆枕怎么清洗", "collection": "product_kb"},
    ],
)
async def search_knowledge(payload: SearchKnowledgeInput) -> Dict[str, Any]:
    from commercepivot.retrieval.service import search_knowledge as _search

    result = await _search(
        query=payload.query,
        top_k=payload.top_k,
        collection=payload.collection,
        use_rerank=payload.use_rerank,
        min_score=payload.min_score,
    )
    rows = result.get("rows", [])
    summary = result.get("summary", {})
    summary["hit_count"] = len(rows)
    summary["sources"] = list(dict.fromkeys([r.get("source") for r in rows if r.get("source")]))[:5]
    if not rows:
        summary.setdefault("note", "知识库未检索到相关内容，请确认语料已 build_index 入库")
    return {
        "rows": rows,
        "row_count": len(rows),
        "summary": summary,
        "degraded": result.get("degraded", False),
        "degrade_reason": result.get("degrade_reason"),
    }


@registry.tool(
    name="create_ticket",
    description=(
        "创建人工工单并转交人工客服，返回工单号。**写操作**，需要服务端开启 ENABLE_WRITE_OPS=true；"
        "默认只读模式下会直接拒绝并返回 FORBIDDEN。"
    ),
    input_model=CreateTicketInput,
    readonly=False,
    write_kind="db",
    tags=["ticket", "write", "human"],
    owner_agent="after_sales_agent",
    examples=[{"summary": "用户投诉物流延误要求赔付", "priority": "P1", "contact": "13800000000"}],
)
def create_ticket(payload: CreateTicketInput) -> Dict[str, Any]:
    ensure_write_allowed("create_ticket")
    settings = get_settings()
    prefix = "CP"
    ticket_id = f"{prefix}{time.strftime('%Y%m%d')}{uuid.uuid4().hex[:6].upper()}"
    priority = payload.priority.upper() if payload.priority else "P2"
    if priority not in ("P0", "P1", "P2", "P3"):
        priority = "P2"
    ticket = repo.create_ticket(
        ticket_id=ticket_id,
        summary=payload.summary,
        priority=priority,
        contact=payload.contact,
        session_id=payload.session_id or payload.__dict__.get("_session_id"),
    )
    return {
        "rows": [],
        "row_count": 0,
        "data": ticket,
        "summary": {
            "ticket_id": ticket_id,
            "priority": priority,
            "status": "open",
            "write_ops_enabled": settings.enable_write_ops,
            "next_action": "已转人工，客服会在 1 个工作小时内响应",
        },
    }


__all__ = ["CreateTicketInput", "SearchKnowledgeInput", "create_ticket", "search_knowledge"]
