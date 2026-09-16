"""商品分析 Agent（架构文档 §3.3）。

职责：商品、库存、销量、评论。两个分支：
- 库存视角：``query_inventory`` —— 安全库存预警、可售天数；
- 商品视角：``query_products`` —— 类目分布、价格区间、销量排行。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from commercepivot.agents.base import BaseAgent


class ProductAnalysisAgent(BaseAgent):
    name = "product_analysis_agent"
    description = "商品与库存分析：商品信息、类目分布、库存周转与缺货预警"
    version = "1.0"
    skills = ["product_query", "inventory_check", "sales_rank", "category_stats"]
    tools = ["query_products", "query_inventory"]
    port = 8003

    async def handle(self, task_input: Dict[str, Any]) -> Dict[str, Any]:
        slots = _slots(task_input)
        focus = _focus(task_input, slots)
        notes: List[str] = []
        degraded = False
        reason: Optional[str] = None

        if focus == "inventory":
            res = await self.call_tool(
                "query_inventory",
                {
                    "sku": slots.get("sku"),
                    "warehouse": slots.get("warehouse"),
                    "low_stock_only": bool(slots.get("low_stock_only")),
                    "limit": slots.get("limit", 100),
                },
            )
            payload = {
                "kind": "inventory",
                "summary": res.get("summary") or {},
                "rows": res.get("rows") or [],
                "row_count": res.get("row_count") or 0,
                "source_tools": ["query_inventory"],
            }
            findings = _inventory_findings(payload)
        else:
            res = await self.call_tool(
                "query_products",
                {
                    "keyword": slots.get("keyword"),
                    "category": slots.get("category"),
                    "platform": slots.get("platform"),
                    "limit": slots.get("limit", 50),
                },
            )
            payload = {
                "kind": "products",
                "summary": res.get("summary") or {},
                "rows": res.get("rows") or [],
                "row_count": res.get("row_count") or 0,
                "source_tools": ["query_products"],
            }
            findings = _product_findings(payload, slots)

        if not res.get("ok"):
            degraded = True
            reason = self.fail_reason(res)
            notes.append(f"数据获取失败：{reason}")
        elif res.get("degraded"):
            degraded = True
            reason = res.get("degrade_reason")
            notes.append(reason or "数据源降级")

        return {
            "agent": self.name,
            "status": "completed" if payload.get("rows") is not None else "empty",
            "focus": focus,
            "data": payload,
            "findings": findings,
            "metrics": payload.get("summary") or {},
            "notes": notes,
            "degraded": degraded,
            "degrade_reason": reason,
        }


def _inventory_findings(payload: Dict[str, Any]) -> List[str]:
    s = payload.get("summary") or {}
    if not s:
        return ["未查询到库存数据。"]
    out = [
        f"共 {s.get('sku_count', 0)} 个 SKU，库存合计 {s.get('total_quantity', 0)} 件，"
        f"其中 {s.get('low_stock_count', 0)} 个低于安全库存。",
    ]
    low = [r for r in payload.get("rows", []) if r.get("low_stock")][:5]
    if low:
        detail = "、".join(
            f"{r.get('sku')}({r.get('quantity')}/{r.get('safety_stock')})" for r in low
        )
        out.append(f"预警 SKU（当前/安全库存）：{detail}。")
    fast = [r for r in payload.get("rows", []) if r.get("turnover_days") is not None]
    if fast:
        fastest = min(fast, key=lambda r: r["turnover_days"] or 9999)
        out.append(
            f"周转最快的是 {fastest.get('sku')}，按近期销量约 {fastest.get('turnover_days')} 天售罄，需关注补货节奏。"
        )
    return out


def _product_findings(payload: Dict[str, Any], slots: Dict[str, Any]) -> List[str]:
    s = payload.get("summary") or {}
    if not s:
        return ["未查询到商品数据。"]
    out = [
        f"命中 {s.get('product_count', 0)} 个商品（类目：{s.get('category')}，平台：{s.get('platform')}）。"
    ]
    dist = s.get("category_distribution") or []
    if dist:
        detail = "、".join(
            f"{r.get('category')} {r.get('cnt')} 个（均价 ¥{r.get('avg_price')}）" for r in dist[:5]
        )
        out.append(f"类目分布：{detail}。")
    rows = payload.get("rows") or []
    if rows:
        top = max(rows, key=lambda r: int(r.get("order_count") or 0))
        if int(top.get("order_count") or 0) > 0:
            out.append(f"销量最好的商品是「{top.get('name')}」（{top.get('order_count')} 单）。")
    return out


def _slots(task_input: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(task_input.get("slots") or {})
    for key in ("keyword", "category", "platform", "sku", "warehouse", "low_stock_only", "limit"):
        if task_input.get(key) not in (None, ""):
            merged.setdefault(key, task_input.get(key))
    return merged


def _focus(task_input: Dict[str, Any], slots: Dict[str, Any]) -> str:
    explicit = str(task_input.get("focus") or slots.get("focus") or "").lower()
    if explicit in ("inventory", "stock", "库存"):
        return "inventory"
    if explicit in ("products", "product", "商品"):
        return "products"
    question = str(task_input.get("question") or "")
    if any(k in question for k in ("库存", "缺货", "断货", "补货", "周转", "安全库存", "仓", "滞销")):
        return "inventory"
    return "products"


AGENT = ProductAnalysisAgent()

__all__ = ["AGENT", "ProductAnalysisAgent"]
