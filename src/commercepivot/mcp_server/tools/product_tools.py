"""商品与库存类 MCP 工具（架构文档 §5 工具清单 2、4）。

- ``query_products``  —— 商品信息、价格、类目分布
- ``query_inventory`` —— 库存与周转（含安全库存预警、可售天数）
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from commercepivot.db import repository as repo
from commercepivot.mcp_server.registry import get_registry

registry = get_registry()


class QueryProductsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: Optional[str] = Field(None, description="关键词，模糊匹配商品名与类目")
    category: Optional[str] = Field(None, description="类目精确匹配，如 家居 / 数码 / 服饰")
    platform: Optional[str] = Field(None, description="平台过滤")
    product_id: Optional[int] = Field(None, ge=1, description="商品 ID 精确查询")
    limit: int = Field(50, ge=1, le=500, description="最多返回条数")


class QueryInventoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: Optional[str] = Field(None, description="SKU 模糊匹配")
    warehouse: Optional[str] = Field(None, description="仓库名称，如 杭州仓 / 广州仓")
    product_id: Optional[int] = Field(None, ge=1, description="按商品 ID 查询其全部仓库存")
    low_stock_only: bool = Field(False, description="仅返回低于安全库存的 SKU")
    limit: int = Field(100, ge=1, le=500, description="最多返回条数")
    turnover_days: int = Field(30, ge=1, le=365, description="周转天数计算窗口（销量统计区间）")


@registry.tool(
    name="query_products",
    description=(
        "查询商品信息、价格、类目与销量排序，返回商品列表与类目分布（含各类目均价）。"
        "用于回答「有哪些家居类商品」「价格最高的商品」等问题。只读。"
    ),
    input_model=QueryProductsInput,
    readonly=True,
    tags=["product", "read"],
    owner_agent="product_analysis_agent",
    examples=[{"category": "家居", "limit": 20}, {"keyword": "记忆枕"}],
)
def query_products(payload: QueryProductsInput) -> Dict[str, Any]:
    return repo.query_products(
        keyword=payload.keyword,
        category=payload.category,
        platform=payload.platform,
        product_id=payload.product_id,
        limit=payload.limit,
    )


@registry.tool(
    name="query_inventory",
    description=(
        "查询库存与周转情况，返回 SKU 明细、库存总量、低于安全库存的预警清单，"
        "并按近期销量估算可售天数。用于回答「哪些 SKU 快断货了」「库存周转如何」等问题。只读。"
    ),
    input_model=QueryInventoryInput,
    readonly=True,
    tags=["inventory", "read", "alert"],
    owner_agent="product_analysis_agent",
    examples=[{"low_stock_only": True}, {"warehouse": "杭州仓"}],
)
def query_inventory(payload: QueryInventoryInput) -> Dict[str, Any]:
    return repo.query_inventory(
        sku=payload.sku,
        warehouse=payload.warehouse,
        product_id=payload.product_id,
        low_stock_only=payload.low_stock_only,
        limit=payload.limit,
        turnover_days=payload.turnover_days,
    )


__all__ = ["QueryInventoryInput", "QueryProductsInput", "query_inventory", "query_products"]
