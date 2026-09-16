"""A2A Agent 层（架构文档 §3.3 / §4）。

五个专职 Agent，各自暴露 Agent Card 并支持 JSON-RPC 2.0 调用：
- ``order_analysis_agent``   订单与销售分析（含退货率计算）
- ``product_analysis_agent`` 商品与库存分析
- ``after_sales_agent``      售后客服（售后单 / FAQ / 工单）
- ``knowledge_rag_agent``    知识库 RAG 检索问答
- ``report_agent``           报表生成与导出
"""

from commercepivot.agents.after_sales_agent import AfterSalesAgent
from commercepivot.agents.base import AgentCard, BaseAgent, TaskRecord, get_task, list_tasks
from commercepivot.agents.order_agent import OrderAnalysisAgent
from commercepivot.agents.product_agent import ProductAnalysisAgent
from commercepivot.agents.rag_agent import KnowledgeRagAgent
from commercepivot.agents.registry import REGISTRY, AgentRegistry, get_agent_registry
from commercepivot.agents.report_agent import ReportAgent

__all__ = [
    "REGISTRY",
    "AfterSalesAgent",
    "AgentCard",
    "AgentRegistry",
    "BaseAgent",
    "KnowledgeRagAgent",
    "OrderAnalysisAgent",
    "ProductAnalysisAgent",
    "ReportAgent",
    "TaskRecord",
    "get_agent_registry",
    "get_task",
    "list_tasks",
]
