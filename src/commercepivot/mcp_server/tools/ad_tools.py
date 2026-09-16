"""广告投放类 MCP 工具（架构文档 §5 工具清单 5）。

``query_ad_reports`` —— 查询广告投放与 ROI，返回按平台或按日期的
曝光 / 点击 / 花费 / 成交额，并派生 CTR、CPC、ROI（ROAS）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from commercepivot.db import repository as repo
from commercepivot.mcp_server.registry import get_registry

registry = get_registry()

_DATE_DESC = "支持 2026-08-01 / 上个月 / 近7天 等中文口语"


class QueryAdReportsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Optional[str] = Field(None, description="投放平台，如 巨量引擎 / 抖店 / 京东 / 淘宝")
    start_date: Optional[str] = Field(None, description=f"开始日期。{_DATE_DESC}")
    end_date: Optional[str] = Field(None, description=f"结束日期（含当天）。{_DATE_DESC}")
    group_by: str = Field("platform", description="分组维度：platform（按平台）或 date（按日期）")


@registry.tool(
    name="query_ad_reports",
    description=(
        "查询广告投放效果与 ROI，返回曝光、点击、花费、成交额及派生的 CTR / CPC / ROI(ROAS)。"
        "用于回答「上个月千川的投产比是多少」「哪个平台投放最划算」等问题。只读。"
    ),
    input_model=QueryAdReportsInput,
    readonly=True,
    tags=["ad", "read", "roi"],
    owner_agent="order_analysis_agent",
    examples=[
        {"platform": "巨量引擎", "start_date": "上个月", "group_by": "date"},
        {"group_by": "platform"},
    ],
)
def query_ad_reports(payload: QueryAdReportsInput) -> Dict[str, Any]:
    return repo.query_ad_reports(
        platform=payload.platform,
        start_date=payload.start_date,
        end_date=payload.end_date,
        group_by=payload.group_by,
    )


__all__ = ["QueryAdReportsInput", "query_ad_reports"]
