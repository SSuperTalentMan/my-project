"""领域数据模型（架构文档 §6 数据模型）。

这些模型是**跨层契约**：MCP 工具的返回体、A2A Agent 的输出、
编排层聚合后的中间态都复用它们，保证字段名与语义在四层之间完全一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------- 基础实体
class Product(BaseModel):
    id: int
    name: str
    category: Optional[str] = None
    price: Optional[float] = None
    platform: Optional[str] = None
    created_at: Optional[str] = None


class Order(BaseModel):
    id: int
    order_no: str
    user_id: Optional[str] = None
    product_id: Optional[int] = None
    product_name: Optional[str] = None
    category: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None
    platform: Optional[str] = None
    order_date: Optional[str] = None
    region: Optional[str] = None


class OrderItem(BaseModel):
    id: int
    order_id: int
    product_id: Optional[int] = None
    quantity: Optional[int] = None
    unit_price: Optional[float] = None
    subtotal: Optional[float] = None


class AfterSale(BaseModel):
    id: int
    order_id: Optional[int] = None
    order_no: Optional[str] = None
    product_name: Optional[str] = None
    platform: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    refund_amount: Optional[float] = None
    created_at: Optional[str] = None


class InventoryItem(BaseModel):
    id: int
    sku: str
    product_id: Optional[int] = None
    product_name: Optional[str] = None
    warehouse: Optional[str] = None
    quantity: int = 0
    safety_stock: int = 10
    updated_at: Optional[str] = None
    low_stock: bool = False
    turnover_days: Optional[float] = None


class AdReportRow(BaseModel):
    platform: Optional[str] = None
    report_date: Optional[str] = None
    impressions: int = 0
    clicks: int = 0
    cost: float = 0.0
    revenue: float = 0.0
    ctr: Optional[float] = None
    roi: Optional[float] = None


# --------------------------------------------------------------- 聚合结果
class SummaryBlock(BaseModel):
    """所有工具统一携带的汇总块，便于前端直接渲染卡片。"""

    label: str = ""
    metrics: Dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = None


class ReturnRateRow(BaseModel):
    product_id: int
    product_name: str
    category: Optional[str] = None
    platform: Optional[str] = None
    order_count: int = 0
    return_count: int = 0
    return_rate: float = 0.0
    refund_amount: float = 0.0


class KnowledgeHit(BaseModel):
    id: str
    collection: str = ""
    title: str = ""
    content: str = ""
    source: Optional[str] = None
    score: Optional[float] = None
    dense_score: Optional[float] = None
    sparse_score: Optional[float] = None
    rerank_score: Optional[float] = None
    retrieval_mode: str = "hybrid"


# --------------------------------------------------------------- 会话与审计
class ChatSession(BaseModel):
    id: str
    user_id: Optional[str] = None
    role: str = "customer"
    created_at: Optional[str] = None


class ChatMessage(BaseModel):
    id: Optional[int] = None
    session_id: str
    role: str
    content: str
    created_at: Optional[str] = None


class McpAuditRecord(BaseModel):
    id: Optional[int] = None
    tool_name: str
    params: Dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    elapsed_ms: int = 0
    status: str = "ok"
    request_id: Optional[str] = None
    created_at: Optional[str] = None


class AgentTaskRecord(BaseModel):
    id: str
    agent_name: str
    status: str = "submitted"
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Optional[Dict[str, Any]] = None
    elapsed_ms: int = 0
    created_at: Optional[str] = None
    error: Optional[str] = None


# --------------------------------------------------------------- 工具输出
class ToolResult(BaseModel):
    """MCP 工具的统一返回结构。"""

    ok: bool = True
    tool: str = ""
    data: Any = None
    summary: Dict[str, Any] = Field(default_factory=dict)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    degraded: bool = False
    degrade_reason: Optional[str] = None
    elapsed_ms: int = 0
    request_id: Optional[str] = None


class Ticket(BaseModel):
    id: str
    summary: str
    priority: str = "P2"
    contact: Optional[str] = None
    status: str = "open"
    created_at: Optional[str] = None


__all__ = [
    "AdReportRow",
    "AfterSale",
    "AgentTaskRecord",
    "ChatMessage",
    "ChatSession",
    "InventoryItem",
    "KnowledgeHit",
    "McpAuditRecord",
    "Order",
    "OrderItem",
    "Product",
    "ReturnRateRow",
    "SummaryBlock",
    "Ticket",
    "ToolResult",
]
