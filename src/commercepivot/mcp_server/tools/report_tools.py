"""报表生成 MCP 工具（架构文档 §5 工具清单 7）。

``generate_report`` 支持 9 种报表类型，输出 Markdown 或 CSV，
可选落盘到 ``data/reports/``。

为什么 ``write_kind="file"`` 而不是 ``"db"``：
它只产生本地文件产物、不改业务数据，不应被 ``ENABLE_WRITE_OPS`` 拦截；
注册表里只有 ``write_kind="db"`` 的工具才受写门禁保护。
"""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from commercepivot.core.config import DATA_DIR, get_settings
from commercepivot.core.errors import ValidationFailedError
from commercepivot.db import repository as repo
from commercepivot.mcp_server.registry import get_registry

registry = get_registry()

REPORT_DIR = DATA_DIR / "reports"

DATE_DESC = "支持 2026-08-01 / 上个月 / 近7天 等中文口语"

# 报表类型 -> (中文名, 需要注入的查询函数)
REPORT_TYPES: Dict[str, str] = {
    "orders": "订单明细与汇总报表",
    "products": "商品信息报表",
    "after_sales": "售后与退款报表",
    "inventory": "库存与预警报表",
    "ad": "广告投放与 ROI 报表",
    "return_rate": "商品退货率排行报表",
    "sales_trend": "销售趋势报表",
    "top_products": "畅销商品排行报表",
    "audit": "MCP 工具调用审计报表",
    "agent_tasks": "A2A 任务执行报表",
}


class GenerateReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: str = Field(..., description="报表类型：" + " / ".join(REPORT_TYPES))
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "查询参数。通用键：start_date、end_date、platform、limit；"
            "return_rate 额外支持 order(desc/asc)、min_orders；"
            "sales_trend 支持 group_by(day/week/month/category/platform/region)；"
            "top_products 支持 metric(gmv/quantity/order_count)"
        ),
    )
    fmt: str = Field("markdown", description="输出格式：markdown 或 csv")
    save: bool = Field(True, description="是否落盘到 data/reports/")
    filename: Optional[str] = Field(None, max_length=120, description="自定义文件名（不含扩展名）")


class _Ctx:
    """把 params 里的键安全地取出并转成查询函数需要的类型。"""

    def __init__(self, params: Dict[str, Any]) -> None:
        self.p = params or {}

    def s(self, key: str, default: Any = None) -> Any:
        v = self.p.get(key, default)
        return v if v not in ("", None) else default

    def i(self, key: str, default: int) -> int:
        try:
            return int(self.p.get(key, default))
        except (TypeError, ValueError):
            return default

    def b(self, key: str, default: bool = False) -> bool:
        v = self.p.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("1", "true", "yes", "y", "是")


def _build_orders(ctx: _Ctx) -> Dict[str, Any]:
    return repo.query_orders(
        start_date=ctx.s("start_date"), end_date=ctx.s("end_date"),
        platform=ctx.s("platform"), status=ctx.s("status"),
        region=ctx.s("region"), keyword=ctx.s("keyword"), limit=ctx.i("limit", 200),
    )


def _build_products(ctx: _Ctx) -> Dict[str, Any]:
    return repo.query_products(
        keyword=ctx.s("keyword"), category=ctx.s("category"),
        platform=ctx.s("platform"), limit=ctx.i("limit", 200),
    )


def _build_after_sales(ctx: _Ctx) -> Dict[str, Any]:
    return repo.query_after_sales(
        order_no=ctx.s("order_no"), status=ctx.s("status"), type_=ctx.s("after_sale_type"),
        platform=ctx.s("platform"), start_date=ctx.s("start_date"),
        end_date=ctx.s("end_date"), limit=ctx.i("limit", 200),
    )


def _build_inventory(ctx: _Ctx) -> Dict[str, Any]:
    return repo.query_inventory(
        sku=ctx.s("sku"), warehouse=ctx.s("warehouse"),
        low_stock_only=ctx.b("low_stock_only"), limit=ctx.i("limit", 200),
    )


def _build_ad(ctx: _Ctx) -> Dict[str, Any]:
    return repo.query_ad_reports(
        platform=ctx.s("platform"), start_date=ctx.s("start_date"),
        end_date=ctx.s("end_date"), group_by=ctx.s("group_by", "platform"),
    )


def _build_return_rate(ctx: _Ctx) -> Dict[str, Any]:
    return repo.return_rate_by_product(
        start_date=ctx.s("start_date"), end_date=ctx.s("end_date"),
        platform=ctx.s("platform"), order=ctx.s("order", "desc"),
        limit=ctx.i("limit", 20), min_orders=ctx.i("min_orders", 1),
        category=ctx.s("category"),
    )


def _build_sales_trend(ctx: _Ctx) -> Dict[str, Any]:
    return repo.sales_stats(
        start_date=ctx.s("start_date"), end_date=ctx.s("end_date"),
        platform=ctx.s("platform"), group_by=ctx.s("group_by", "day"),
        limit=ctx.i("limit", 200),
    )


def _build_top_products(ctx: _Ctx) -> Dict[str, Any]:
    return repo.top_products(
        start_date=ctx.s("start_date"), end_date=ctx.s("end_date"),
        platform=ctx.s("platform"), metric=ctx.s("metric", "gmv"),
        limit=ctx.i("limit", 20),
    )


def _build_audit(ctx: _Ctx) -> Dict[str, Any]:
    rows = repo.list_audit(limit=ctx.i("limit", 200), tool_name=ctx.s("tool_name"))
    return {"rows": rows, "row_count": len(rows), "summary": {"report": "工具调用审计"}}


def _build_agent_tasks(ctx: _Ctx) -> Dict[str, Any]:
    rows = repo.list_agent_tasks(limit=ctx.i("limit", 200), agent_name=ctx.s("agent_name"))
    return {"rows": rows, "row_count": len(rows), "summary": {"report": "A2A 任务执行"}}



BUILDERS: Dict[str, Callable[[_Ctx], Dict[str, Any]]] = {
    "orders": _build_orders,
    "products": _build_products,
    "after_sales": _build_after_sales,
    "inventory": _build_inventory,
    "ad": _build_ad,
    "return_rate": _build_return_rate,
    "sales_trend": _build_sales_trend,
    "top_products": _build_top_products,
    "audit": _build_audit,
    "agent_tasks": _build_agent_tasks,
}

# 每类报表优先展示的列（存在才输出，保证不同数据形态都能渲染）
PREFERRED_COLUMNS: Dict[str, List[str]] = {
    "orders": ["id", "order_no", "product_name", "category", "amount", "status", "platform", "order_date", "region"],
    "products": ["id", "name", "category", "price", "platform", "order_count"],
    "after_sales": ["id", "order_id", "order_no", "product_name", "type", "status", "refund_amount", "created_at"],
    "inventory": ["sku", "product_name", "warehouse", "quantity", "safety_stock", "turnover_days", "low_stock"],
    "ad": ["dim", "impressions", "clicks", "ctr", "cost", "revenue", "roi"],
    "return_rate": ["product_id", "product_name", "category", "platform", "order_count", "return_count", "return_rate", "refund_amount"],
    "sales_trend": ["day", "week", "month", "category", "platform", "region", "order_count", "quantity", "gmv"],
    "top_products": ["product_id", "product_name", "category", "gmv", "quantity", "order_count"],
    "audit": ["id", "tool_name", "status", "elapsed_ms", "request_id", "created_at"],
    "agent_tasks": ["id", "agent_name", "status", "elapsed_ms", "request_id", "created_at"],
}


def _columns(report_type: str, rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return []
    preferred = [c for c in PREFERRED_COLUMNS.get(report_type, []) if c in rows[0]]
    extra = [c for c in rows[0] if c not in preferred]
    return preferred + extra


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _render_markdown(title: str, summary: Dict[str, Any], rows: List[Dict[str, Any]], report_type: str) -> str:
    lines = [f"# {title}", "", f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    if summary:
        lines += ["## 汇总", "", "| 指标 | 数值 |", "| --- | --- |"]
        for k, v in summary.items():
            if isinstance(v, (dict, list)):
                continue
            lines.append(f"| {k} | {_cell(v)} |")
        lines.append("")
    lines += [f"## 明细（{len(rows)} 行）", ""]
    cols = _columns(report_type, rows)
    if not cols:
        lines.append("_没有匹配的数据_")
        return "\n".join(lines)
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join("---" for _ in cols) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def _render_csv(rows: List[Dict[str, Any]], report_type: str) -> str:
    if not rows:
        return ""
    cols = _columns(report_type, rows)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _cell(row.get(c)) for c in cols})
    return buf.getvalue()


@registry.tool(
    name="generate_report",
    description=(
        "生成经营报表，支持 " + "、".join(REPORT_TYPES) + " 等类型，输出 Markdown 或 CSV 内容"
        "（可选落盘到 data/reports/）。用于回答「导出一份上个月的退货率排行」「生成销售日报」等需求。"
    ),
    input_model=GenerateReportInput,
    readonly=False,
    write_kind="file",
    timeout_s=30.0,
    tags=["report", "export"],
    owner_agent="report_agent",
    examples=[
        {"report_type": "return_rate", "params": {"start_date": "上个月", "platform": "抖店", "limit": 10}},
        {"report_type": "sales_trend", "params": {"group_by": "month", "start_date": "今年"}, "fmt": "csv"},
    ],
)
def generate_report(payload: GenerateReportInput) -> Dict[str, Any]:
    report_type = (payload.report_type or "").strip().lower()
    if report_type not in BUILDERS:
        raise ValidationFailedError(
            f"不支持的报表类型：{payload.report_type}",
            details={"supported": list(BUILDERS)},
        )
    fmt = (payload.fmt or "markdown").strip().lower()
    if fmt not in ("markdown", "md", "csv"):
        raise ValidationFailedError(f"不支持的输出格式：{payload.fmt}", details={"supported": ["markdown", "csv"]})
    fmt = "csv" if fmt == "csv" else "markdown"

    ctx = _Ctx(payload.params or {})
    raw = BUILDERS[report_type](ctx)
    rows = raw.get("rows", []) or []
    summary = dict(raw.get("summary") or {})
    title = f"{REPORT_TYPES[report_type]}"

    content = _render_csv(rows, report_type) if fmt == "csv" else _render_markdown(
        title, summary, rows, report_type
    )

    result: Dict[str, Any] = {
        "rows": rows,
        "row_count": len(rows),
        "summary": {
            "report_type": report_type,
            "report_name": title,
            "format": fmt,
            "row_count": len(rows),
            "query_summary": summary,
        },
        "data": {
            "report_type": report_type,
            "format": fmt,
            "content": content,
            "saved_path": None,
            "bytes": len(content.encode("utf-8")),
        },
    }

    if payload.save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ext = "csv" if fmt == "csv" else "md"
        name = payload.filename or f"{report_type}_{time.strftime('%Y%m%d_%H%M%S')}"
        name = "".join(c for c in name if c.isalnum() or c in "-_") or f"report_{int(time.time())}"
        path = REPORT_DIR / f"{name}.{ext}"
        path.write_text(content, encoding="utf-8")
        result["data"]["saved_path"] = str(path)
        result["summary"]["saved_path"] = str(path)
    result["summary"]["degrade_note"] = None
    return result


__all__ = ["GenerateReportInput", "REPORT_TYPES", "generate_report"]
