"""订单与售后类 MCP 工具（架构文档 §5 工具清单 1、3）。

覆盖：
- ``query_orders``      —— 按时间、平台、状态查询订单（返回列表 + 汇总）
- ``query_after_sales`` —— 查询售后单与退款进度

两个工具的入参都同时接受 **ISO 日期** 与 **中文时间口语**（上个月 / 近7天），
这样 LLM 或外部 Agent 不必先把时间算成日期再调用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from commercepivot.db import repository as repo
from commercepivot.mcp_server.registry import get_registry

registry = get_registry()

_DATE_DESC = "支持 2026-08-01 / 2026-08 / 上个月 / 近7天 / 昨天 等中文口语"


class QueryOrdersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: Optional[str] = Field(None, description=f"起始日期。{_DATE_DESC}")
    end_date: Optional[str] = Field(None, description=f"结束日期（含当天）。{_DATE_DESC}")
    platform: Optional[str] = Field(None, description="平台，如 抖店 / 京东 / 淘宝 / 拼多多 / 天猫")
    status: Optional[str] = Field(None, description="订单状态：已付款 / 已发货 / 已完成 / 已退款 / 已取消")
    keyword: Optional[str] = Field(None, description="模糊匹配订单号、商品名或地区")
    product_id: Optional[int] = Field(None, ge=1, description="按商品 ID 精确过滤")
    region: Optional[str] = Field(None, description="地区模糊匹配，如 广东")
    breakdowns: List[str] = Field(
        default_factory=list,
        description=(
            "附加聚合切片，可选 product / platform / status / date / region / category，"
            "结果位于 summary.by_<切片>。做排行、趋势、分布类分析时请传入，"
            "例如 breakdowns=['product'] 可直接拿到按商品聚合的订单量与 GMV。"
        ),
    )
    breakdown_limit: int = Field(200, ge=1, le=500, description="每个切片的商品/维度数上限")
    limit: int = Field(100, ge=1, le=500, description="明细最多返回条数")


class QueryAfterSalesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: Optional[int] = Field(None, ge=1, description="订单主键 ID")
    order_no: Optional[str] = Field(None, description="订单编号，如 SO20260801000001")
    status: Optional[str] = Field(None, description="售后状态：待审核 / 处理中 / 已完成 / 已拒绝")
    after_sale_type: Optional[str] = Field(None, description="售后类型：退货 / 换货 / 仅退款")
    platform: Optional[str] = Field(None, description="平台过滤")
    start_date: Optional[str] = Field(None, description=f"售后创建起始时间。{_DATE_DESC}")
    end_date: Optional[str] = Field(None, description=f"售后创建结束时间（含当天）。{_DATE_DESC}")
    breakdowns: List[str] = Field(
        default_factory=list,
        description=(
            "附加聚合切片，可选 product / platform / status / type / date，结果位于 summary.by_<切片>。"
            "注意 product 切片固定为「退货」口径，可与 query_orders 的 by_product 相除得到退货率。"
        ),
    )
    breakdown_limit: int = Field(200, ge=1, le=500, description="每个切片的商品/维度数上限")
    limit: int = Field(100, ge=1, le=500, description="明细最多返回条数")


@registry.tool(
    name="query_orders",
    description=(
        "按时间、平台、状态查询电商订单，返回订单明细列表与汇总（订单量、GMV、客单价、"
        "买家数、状态分布、平台分布）。用于回答「上个月抖店卖了多少」「未发货订单有哪些」等问题。只读。"
    ),
    input_model=QueryOrdersInput,
    readonly=True,
    tags=["order", "read", "analysis"],
    owner_agent="order_analysis_agent",
    examples=[
        {"start_date": "上个月", "platform": "抖店", "status": "已完成"},
        {"start_date": "2026-08-01", "end_date": "2026-08-31", "limit": 50},
    ],
)
def query_orders(payload: QueryOrdersInput) -> Dict[str, Any]:
    return repo.query_orders(
        start_date=payload.start_date,
        end_date=payload.end_date,
        platform=payload.platform,
        status=payload.status,
        keyword=payload.keyword,
        product_id=payload.product_id,
        region=payload.region,
        limit=payload.limit,
        breakdowns=payload.breakdowns,
        breakdown_limit=payload.breakdown_limit,
    )


@registry.tool(
    name="query_after_sales",
    description=(
        "查询售后单与退款进度，返回售后明细、类型分布（退货/换货/仅退款）、状态分布、"
        "退款金额合计与售后率。用于回答「退款处理到哪一步了」「上个月退货多少单」等问题。只读。"
    ),
    input_model=QueryAfterSalesInput,
    readonly=True,
    tags=["after_sales", "read", "analysis"],
    owner_agent="after_sales_agent",
    examples=[
        {"start_date": "上个月", "after_sale_type": "退货"},
        {"order_no": "SO20260801000001"},
    ],
)
def query_after_sales(payload: QueryAfterSalesInput) -> Dict[str, Any]:
    return repo.query_after_sales(
        order_id=payload.order_id,
        order_no=payload.order_no,
        status=payload.status,
        type_=payload.after_sale_type,
        platform=payload.platform,
        start_date=payload.start_date,
        end_date=payload.end_date,
        limit=payload.limit,
        breakdowns=payload.breakdowns,
        breakdown_limit=payload.breakdown_limit,
    )


__all__ = ["QueryAfterSalesInput", "QueryOrdersInput", "query_after_sales", "query_orders"]
