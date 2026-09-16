"""订单分析 Agent（架构文档 §3.3 / §4.1）。

职责：订单、退款、发货、地区与销售统计；也是架构文档 §7 示例问句
「上个月抖店退货率最高的商品是什么」的**主要执行者**。

关键设计：退货率**不是**一个现成的 SQL 聚合，而是由本 Agent 分两步完成 ——
1. 通过 MCP 调 ``query_orders(breakdowns=['product'])`` 拿到每个商品的订单量；
2. 通过 MCP 调 ``query_after_sales(breakdowns=['product'])`` 拿到每个商品的退货单数；
3. 在 Agent 内做 join、算比率、排序、取 TopN。
这正是文档 §7 第 4~6 步描述的链路，把「取数」留在工具层、
把「业务口径与排序」留在 Agent 层。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from commercepivot.agents.base import BaseAgent
from commercepivot.core.logging import get_logger

log = get_logger("commercepivot.agents.order")

AGENT_NAME = "order_analysis_agent"


class OrderAnalysisAgent(BaseAgent):
    name = AGENT_NAME
    description = "订单与售后数据分析：订单量、GMV、退款、发货、地区统计与销售趋势"
    version = "1.0"
    skills = ["order_query", "refund_analysis", "sales_stats", "ad_roi", "region_stats"]
    tools = ["query_orders", "query_after_sales", "query_ad_reports"]
    port = 8002

    # ------------------------------------------------------------ 主体
    async def handle(self, task_input: Dict[str, Any]) -> Dict[str, Any]:
        slots = _slots(task_input)
        focus = _focus(task_input, slots)
        notes: List[str] = []
        findings: List[str] = []
        metrics: Dict[str, Any] = {}
        payload: Dict[str, Any] = {}
        degraded = False
        reason: Optional[str] = None

        if focus == "return_rate":
            payload, findings, metrics, degraded, reason = await self._return_rate(slots)
        elif focus == "ad":
            payload = await self._ad(slots)
            metrics = payload.get("summary", {})
        else:
            payload = await self._orders(slots)
            metrics = payload.get("summary", {})

        if degraded:
            notes.append(reason or "数据源降级")
        # 三类 payload 的口径完全不同，findings 必须各用各的：
        # 广告汇总里没有 order_count/gmv，若复用订单口径会输出「共成交 0 单」这种
        # 与问题无关的假结论。
        if payload.get("kind") == "ad":
            findings = _ad_findings(payload, slots)
        elif payload.get("kind") != "return_rate":
            findings = _order_findings(payload, slots)

        return {
            "agent": self.name,
            "status": "completed" if payload else "empty",
            "focus": focus,
            "data": payload,
            "findings": findings,
            "metrics": metrics,
            "notes": notes,
            "degraded": degraded,
            "degrade_reason": reason,
        }

    # ------------------------------------------------------------ 分支：退货率
    async def _return_rate(self, slots: Dict[str, Any]):
        metric_word = "退货率"
        order_res = await self.call_tool(
            "query_orders",
            {
                "start_date": slots.get("start_date"),
                "end_date": slots.get("end_date"),
                "platform": slots.get("platform"),
                "breakdowns": ["product"],
                "breakdown_limit": slots.get("limit", 200),
                "limit": 1,
            },
        )
        # 售后侧：口径固定为「退货」，与订单量同维度
        sale_res = await self.call_tool(
            "query_after_sales",
            {
                "start_date": slots.get("start_date"),
                "end_date": slots.get("end_date"),
                "platform": slots.get("platform"),
                "after_sale_type": "退货",
                "breakdowns": ["product"],
                "breakdown_limit": slots.get("limit", 200),
                "limit": 1,
            },
        )

        degraded = not (order_res.get("ok") and sale_res.get("ok"))
        reason = None
        if degraded:
            reason = self.fail_reason(order_res) or self.fail_reason(sale_res)
            if order_res.get("degraded") or sale_res.get("degraded"):
                reason = order_res.get("degrade_reason") or sale_res.get("degrade_reason") or reason

        # 工具侧的 product 切片是按 (product_id, platform) 分组的（同款商品在不同
        # 平台各占一行）。这里必须**按商品累加**而不是直接赋值：早期版本用
        # product_id 做 key 直接覆盖，导致全平台口径下同一商品的多个平台行互相
        # 顶掉，订单量与退货量大量丢失（表现为「全平台退货率 0%」这种明显错误）。
        by_product: Dict[Any, Dict[str, Any]] = {}
        platform_sets: Dict[Any, set] = {}

        def _ensure(key: Any, row: Dict[str, Any]) -> Dict[str, Any]:
            item = by_product.get(key)
            if item is None:
                item = {
                    "product_id": key,
                    "product_name": row.get("product_name"),
                    "category": row.get("category"),
                    "platform": row.get("platform"),
                    "order_count": 0,
                    "gmv": 0.0,
                    "return_count": 0,
                    "refund_amount": 0.0,
                }
                by_product[key] = item
            return item

        for row in (order_res.get("summary") or {}).get("by_product", []) or []:
            key = row.get("product_id")
            if key is None:
                continue
            item = _ensure(key, row)
            item["order_count"] += int(row.get("order_count") or 0)
            item["gmv"] = round(item["gmv"] + float(row.get("gmv") or 0), 2)
            if row.get("platform"):
                platform_sets.setdefault(key, set()).add(row["platform"])
        for row in (sale_res.get("summary") or {}).get("by_product", []) or []:
            key = row.get("product_id")
            if key is None:
                continue
            item = _ensure(key, row)
            item["return_count"] += int(row.get("return_count") or 0)
            item["refund_amount"] = round(item["refund_amount"] + float(row.get("refund_amount") or 0), 2)
            if row.get("platform"):
                platform_sets.setdefault(key, set()).add(row["platform"])

        # 指定了平台时该列就是那个平台；全平台口径下跨平台商品标注为「多平台」，
        # 避免用某一行平台的名称以偏概全。
        for key, item in by_product.items():
            plats = platform_sets.get(key) or set()
            if len(plats) > 1 and not slots.get("platform"):
                item["platform"] = "多平台"
            elif plats and not item.get("platform"):
                item["platform"] = sorted(plats)[0]

        rows: List[Dict[str, Any]] = []
        # 样本量下限：1/2 = 50% 这种低基数比率会给出误导性结论。
        # 默认要求至少 5 单成交才进入排行，除非调用方显式指定 min_orders。
        min_orders = int(slots.get("min_orders") or 5)
        for item in by_product.values():
            orders = item["order_count"]
            returns = item["return_count"]
            item["return_rate"] = round(returns * 100.0 / orders, 2) if orders else 0.0
            if orders >= min_orders:
                rows.append(item)

        reverse = str(slots.get("order", "desc")).lower() not in ("asc", "最低", "升序")
        rows.sort(key=lambda r: (r["return_rate"], r["return_count"]), reverse=reverse)
        top_n = int(slots.get("top_k") or slots.get("limit") or 5)
        top_n = max(1, min(top_n, len(rows) or 1))
        top = rows[:top_n]

        total_orders = sum(r["order_count"] for r in rows)
        total_returns = sum(r["return_count"] for r in rows)
        overall = round(total_returns * 100.0 / total_orders, 2) if total_orders else 0.0

        findings = self._return_findings(top, overall, slots, reverse, min_orders)
        metrics = {
            "overall_return_rate": overall,
            "order_count": total_orders,
            "return_count": total_returns,
            "product_count": len(rows),
            "min_orders": min_orders,
            "top_product": top[0]["product_name"] if top else None,
            "top_return_rate": top[0]["return_rate"] if top else None,
        }
        payload = {
            "kind": "return_rate",
            "label": (order_res.get("summary") or {}).get("label") or (sale_res.get("summary") or {}).get("label"),
            "platform": slots.get("platform") or "全平台",
            "metric": metric_word,
            "order": "降序" if reverse else "升序",
            "min_orders": min_orders,
            "rows": top,
            "all_count": len(rows),
            "summary": metrics,
            "source_tools": ["query_orders", "query_after_sales"],
        }
        return payload, findings, metrics, degraded, reason

    @staticmethod
    def _return_findings(
        top: List[Dict[str, Any]],
        overall: float,
        slots: Dict[str, Any],
        reverse: bool,
        min_orders: int = 1,
    ) -> List[str]:
        if not top:
            return [
                f"该条件下没有商品同时满足「成交 ≥ {min_orders} 单」的样本量要求，"
                "建议放宽时间范围或降低样本量阈值后重试。"
            ]
        label = slots.get("period_label") or slots.get("start_date") or "所选区间"
        platform = slots.get("platform") or "全平台"
        direction = "最高" if reverse else "最低"
        out = [
            f"{label} {platform}整体退货率为 {overall}%（退货单数 ÷ 订单数，口径为「退货」类售后）。",
            f"退货率{direction}的商品是「{top[0]['product_name']}」"
            f"（{top[0]['return_rate']}%，{top[0]['return_count']} 单退货 / {top[0]['order_count']} 单成交）。",
        ]
        if len(top) > 1:
            rest = [r for r in top[1:] if r["return_count"] > 0]
            if rest:
                detail = "、".join(f"{r['product_name']}（{r['return_rate']}%）" for r in rest[:4])
                out.append(f"其后依次为：{detail}。")
            else:
                out.append("其余入榜商品在统计周期内均无退货记录，说明问题集中在头部商品上。")
        high_rate = [r for r in top if r["return_count"] > 0 and r["return_rate"] >= overall * 1.5 and overall > 0]
        if high_rate:
            out.append(
                f"其中 {len(high_rate)} 个商品退货率显著高于整体均值（≥1.5 倍），建议优先排查质量或描述不符问题。"
            )
        elif top and top[0]["return_count"] == 0:
            out.append("全部入榜商品均无退货记录，本周期该平台的退货情况良好。")
        out.append(f"（排行已过滤成交少于 {min_orders} 单的商品，避免小样本比率失真。）")
        return out

    # ------------------------------------------------------------ 分支：订单
    async def _orders(self, slots: Dict[str, Any]) -> Dict[str, Any]:
        want_trend = bool(slots.get("want_trend"))
        breakdowns = list(slots.get("breakdowns") or [])
        group_by = str(slots.get("group_by") or "")
        if group_by in ("category", "region", "platform", "status") and group_by not in breakdowns:
            breakdowns.append(group_by)
        if want_trend and "date" not in breakdowns:
            breakdowns.append("date")
        res = await self.call_tool(
            "query_orders",
            {
                "start_date": slots.get("start_date"),
                "end_date": slots.get("end_date"),
                "platform": slots.get("platform"),
                "status": slots.get("status"),
                "keyword": slots.get("keyword"),
                "region": slots.get("region"),
                "breakdowns": breakdowns,
                "limit": slots.get("limit", 100),
            },
        )
        summary = res.get("summary") or {}
        rows = res.get("rows") or []
        kind = "orders"
        # 「各平台的销售额」这类问题问的是聚合事实，表格要给维度切片而不是订单明细；
        # 只有识别不出维度时才回落到明细行。
        dim = _primary_dimension(breakdowns)
        if dim:
            agg_rows = summary.get(f"by_{dim}") or []
            if agg_rows:
                rows = agg_rows
                kind = f"breakdown_{dim}"
        return {
            "kind": kind,
            "label": summary.get("label"),
            "summary": summary,
            "rows": rows,
            "row_count": len(rows),
            "source_tools": ["query_orders"],
            "degraded": res.get("degraded", False),
            "degrade_reason": res.get("degrade_reason"),
        }

    # ------------------------------------------------------------ 分支：广告
    async def _ad(self, slots: Dict[str, Any]) -> Dict[str, Any]:
        res = await self.call_tool(
            "query_ad_reports",
            {
                "platform": slots.get("ad_platform") or slots.get("platform"),
                "start_date": slots.get("start_date"),
                "end_date": slots.get("end_date"),
                "group_by": slots.get("group_by", "platform"),
            },
        )
        return {
            "kind": "ad",
            "summary": res.get("summary") or {},
            "rows": res.get("rows") or [],
            "row_count": res.get("row_count") or 0,
            "source_tools": ["query_ad_reports"],
            "degraded": res.get("degraded", False),
            "degrade_reason": res.get("degrade_reason"),
        }


def _row_count(row: Dict[str, Any]) -> int:
    """取切片行的「单数」，兼容 cnt / order_count 两种列名（见 repository 注释）。"""
    cnt = row.get("cnt")
    if cnt is None:
        cnt = row.get("order_count")
    try:
        return int(cnt or 0)
    except (TypeError, ValueError):
        return 0


def _order_findings(payload: Dict[str, Any], slots: Dict[str, Any]) -> List[str]:
    summary = payload.get("summary") or {}
    if not summary:
        return ["未查询到匹配的订单数据。"]
    label = summary.get("label") or slots.get("period_label") or "所选区间"
    platform = slots.get("platform") or "全平台"
    out = [
        f"{label} {platform}共成交 {summary.get('order_count', 0)} 单，"
        f"GMV ¥{summary.get('gmv', 0):,.2f}，客单价 ¥{summary.get('avg_order_amount', 0):,.2f}，"
        f"买家数 {summary.get('buyer_count', 0)}。",
    ]
    # 「概览型」结论要短，所以默认只列前几名；但**问题本身就是该维度拆分**时
    # （slots.breakdowns 命中）必须逐行给全 —— 否则结论只列前 4 个平台，
    # 却同时报了「全平台合计」，读的人一加就发现对不上账。
    # 实测：问「各平台销售额」时天猫被 [:4] 截掉，下游大模型还忠实地照抄了这个残缺结论。
    asked = set(slots.get("breakdowns") or [])

    by_status = summary.get("by_status") or []
    if by_status:
        rows = by_status if "status" in asked else by_status[:5]
        dist = "、".join(f"{r.get('status')} {_row_count(r)} 单" for r in rows)
        tail = "" if len(rows) >= len(by_status) else f"（共 {len(by_status)} 种状态）"
        out.append(f"订单状态分布：{dist}。{tail}")

    by_platform = summary.get("by_platform") or []
    if by_platform and len(by_platform) > 1:
        if "platform" in asked:
            rows = by_platform
            dist = "、".join(f"{r.get('platform')} {_row_count(r)} 单 / ¥{float(r.get('gmv') or 0):,.2f}" for r in rows)
        else:
            rows = by_platform[:4]
            dist = "、".join(f"{r.get('platform')} ¥{float(r.get('gmv') or 0):,.0f}" for r in rows)
        tail = "" if len(rows) >= len(by_platform) else f"（共 {len(by_platform)} 个平台）"
        out.append(f"平台 GMV 分布：{dist}。{tail}")
    by_product = summary.get("by_product") or []
    if by_product:
        top = by_product[0]
        out.append(
            f"订单量最高的商品是「{top.get('product_name')}」（{top.get('order_count')} 单，"
            f"GMV ¥{float(top.get('gmv') or 0):,.2f}）。"
        )
    by_date = summary.get("by_date") or []
    if len(by_date) >= 2:
        first, last = by_date[0], by_date[-1]
        out.append(
            f"趋势上从 {first.get('date')} 的 {first.get('order_count')} 单"
            f"变化到 {last.get('date')} 的 {last.get('order_count')} 单。"
        )
    return out


def _ad_findings(payload: Dict[str, Any], slots: Dict[str, Any]) -> List[str]:
    """广告投放结论：只看曝光/点击/花费/成交/ROI，绝不套用订单口径。"""
    summary = payload.get("summary") or {}
    rows = payload.get("rows") or []
    asked = slots.get("ad_platform") or slots.get("platform")
    label = summary.get("label") or slots.get("period_label") or "所选区间"
    if not rows:
        return [
            f"{label} 未查询到"
            + (f"「{asked}」" if asked else "")
            + "的广告投放数据，请确认广告平台名称或放宽时间范围。"
        ]

    # 指定了广告平台时，结论聚焦该平台；否则给出全部投放渠道的对比。
    if asked:
        target = next((r for r in rows if str(r.get("dim")) == str(asked)), None)
        if target is None:
            avail = "、".join(str(r.get("dim")) for r in rows)
            return [
                f"{label} 没有找到广告平台「{asked}」的投放记录，"
                f"当前可用的投放渠道有：{avail}。请确认平台名称。"
            ]
        return [
            f"{label}「{asked}」花费 ¥{_f(target.get('cost')):,.2f}，"
            f"带来成交额 ¥{_f(target.get('revenue')):,.2f}，"
            f"投产比 ROI 为 {_f(target.get('roi'))}。",
            f"同期曝光 {int(target.get('impressions') or 0):,} 次、点击 {int(target.get('clicks') or 0):,} 次，"
            f"点击率 {_f(target.get('ctr'))}%，单次点击成本 ¥{_f(target.get('cpc'))}。",
            f"全渠道同期总花费 ¥{_f(summary.get('cost')):,.2f}、总成交额 ¥{_f(summary.get('revenue')):,.2f}、"
            f"整体 ROI {_f(summary.get('roi'))}。",
        ]

    out = [
        f"{label} 全渠道广告花费 ¥{_f(summary.get('cost')):,.2f}，"
        f"带来成交额 ¥{_f(summary.get('revenue')):,.2f}，整体投产比 ROI 为 {_f(summary.get('roi'))}。",
    ]
    ranked = sorted(rows, key=lambda r: _f(r.get("roi")), reverse=True)
    if ranked:
        best = ranked[0]
        out.append(
            f"投产比最高的是「{best.get('dim')}」（ROI {_f(best.get('roi'))}，"
            f"花费 ¥{_f(best.get('cost')):,.2f}）；"
            f"最低的是「{ranked[-1].get('dim')}」（ROI {_f(ranked[-1].get('roi'))}）。"
        )
    weak = [r for r in ranked if _f(r.get("roi")) < 2]
    if weak:
        names = "、".join(str(r.get("dim")) for r in weak[:3])
        out.append(f"其中 {names} 的 ROI 低于 2，建议复核投放结构或降低出价。")
    return out


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _slots(task_input: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(task_input.get("slots") or {})
    for key in (
        "start_date", "end_date", "platform", "status", "keyword", "region", "limit",
        "top_k", "order", "min_orders", "group_by", "want_trend", "breakdowns",
        "period_label", "ad_platform",
    ):
        if task_input.get(key) not in (None, ""):
            merged.setdefault(key, task_input.get(key))
    return merged


def _focus(task_input: Dict[str, Any], slots: Dict[str, Any]) -> str:
    explicit = str(task_input.get("focus") or slots.get("focus") or "").lower()
    if explicit in ("return_rate", "refund", "after_sales", "退货率"):
        return "return_rate"
    if explicit in ("ad", "ad_roi", "广告"):
        return "ad"
    if explicit in ("orders", "sales", "order"):
        return "orders"
    question = str(task_input.get("question") or "")
    if any(k in question for k in ("退货率", "退款率", "售后率")):
        return "return_rate"
    if any(k in question for k in ("roi", "投产比", "广告", "投放", "roas", "曝光", "千川", "直通车")):
        return "ad"
    if any(k in question for k in ("趋势", "走势", "环比", "对比", "每天", "每日", "月度")):
        slots["want_trend"] = True
        return "orders"
    return "orders"


def _primary_dimension(breakdowns: List[str]) -> Optional[str]:
    """挑一个维度作为表格主视图：优先业务维度，时间维度只作兜底。

    理由：问「各平台的销售额」时时间只是筛选条件，表格应展示平台切片；
    问「销售趋势」时才轮到日期切片。
    """
    for dim in breakdowns:
        if dim != "date":
            return dim
    return breakdowns[0] if breakdowns else None


AGENT = OrderAnalysisAgent()

__all__ = ["AGENT", "AGENT_NAME", "OrderAnalysisAgent"]
