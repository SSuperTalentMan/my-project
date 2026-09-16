"""报表生成 Agent（架构文档 §3.3）。

职责：汇总、导出、模板。把编排层抽到的槽位翻译成 ``generate_report``
的 ``report_type`` + ``params``，产出 Markdown / CSV 内容与落盘路径。

设计上刻意保持「薄」：报表口径集中在 MCP 工具的 BUILDERS 里，
Agent 只负责**选类型、传参数、报结果**，避免同一套口径在两处实现。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from commercepivot.agents.base import BaseAgent

# 问句关键词 -> 报表类型（顺序即优先级）
KEYWORD_TO_REPORT: List[tuple[str, tuple[str, ...]]] = [
    ("return_rate", ("退货率", "退款率", "售后率", "退货排行", "退货榜")),
    ("top_products", ("畅销", "销量排行", "卖得最好", "爆款", "热销", "商品排行")),
    ("inventory", ("库存", "缺货", "补货", "周转", "安全库存", "滞销")),
    ("ad", ("广告", "投放", "roi", "投产比", "千川", "直通车", "曝光")),
    ("after_sales", ("售后", "退款", "退货", "换货", "工单")),
    ("sales_trend", ("趋势", "走势", "环比", "同比", "每日", "每月", "日报", "月报", "周报")),
    ("products", ("商品", "类目", "品类", "sku", "选品")),
    ("orders", ("订单", "单量", "成交", "发货", "地区", "区域")),
]

# 报表类型 -> 需要从槽位透传的 params 键
PARAM_KEYS = (
    "start_date", "end_date", "platform", "status", "region", "category",
    "keyword", "order", "min_orders", "group_by", "metric", "warehouse",
    "low_stock_only", "after_sale_type", "limit", "tool_name", "agent_name",
)


class ReportAgent(BaseAgent):
    name = "report_agent"
    description = "报表生成与导出：订单、商品、售后、库存、广告、退货率排行、销售趋势"
    version = "1.0"
    skills = ["report_generate", "data_export", "report_template"]
    tools = ["generate_report"]
    port = 8006
    timeout_s_default = 30.0

    def __init__(self) -> None:
        super().__init__()
        self.timeout_s = max(self.timeout_s, 30.0)

    async def handle(self, task_input: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(task_input.get("slots") or {})
        for key in PARAM_KEYS:
            if task_input.get(key) not in (None, ""):
                slots.setdefault(key, task_input.get(key))

        question = str(task_input.get("question") or "")
        report_type = _pick_report_type(task_input, slots, question)
        fmt = str(slots.get("fmt") or slots.get("format") or ("csv" if "csv" in question.lower() else "markdown"))
        params = {k: slots[k] for k in PARAM_KEYS if slots.get(k) not in (None, "")}
        params.setdefault("limit", 200)

        res = await self.call_tool(
            "generate_report",
            {
                "report_type": report_type,
                "params": params,
                "fmt": "csv" if fmt.startswith("csv") else "markdown",
                "save": True,
            },
        )
        data = res.get("data") or {}
        summary = res.get("summary") or {}
        findings: List[str] = []
        notes: List[str] = []
        degraded = not res.get("ok")
        reason = None if res.get("ok") else self.fail_reason(res)

        if res.get("ok"):
            findings.append(
                f"已生成《{summary.get('report_name', report_type)}》，共 {summary.get('row_count', 0)} 行，"
                f"格式 {summary.get('format')}。"
            )
            if data.get("saved_path"):
                findings.append(f"文件已保存：{data['saved_path']}")
            content = str(data.get("content") or "")
            if summary.get("row_count", 0) == 0:
                notes.append("报表内容为空，可能是筛选条件过窄（平台或时间区间）导致的。")
            notes.append(f"内容体积 {len(content.encode('utf-8'))} 字节")
        else:
            findings.append(f"报表生成失败：{reason}")
            notes.append("可尝试放宽时间范围或去掉平台过滤后重试。")

        return {
            "agent": self.name,
            "status": "completed" if res.get("ok") else "failed",
            "focus": report_type,
            "data": {
                "kind": "report",
                "report_type": report_type,
                "format": summary.get("format"),
                "saved_path": data.get("saved_path"),
                "content": data.get("content"),
                "preview": _preview(data.get("content")),
                "query_params": params,
                "source_tools": ["generate_report"],
            },
            "findings": findings,
            "metrics": {
                "row_count": summary.get("row_count", 0),
                "format": summary.get("format"),
                "bytes": data.get("bytes"),
            },
            "citations": [],
            "notes": notes,
            "degraded": degraded,
            "degrade_reason": reason,
        }


def _preview(content: Any, max_lines: int = 12) -> str:
    lines = str(content or "").splitlines()
    return "\n".join(lines[:max_lines])


def _pick_report_type(task_input: Dict[str, Any], slots: Dict[str, Any], question: str) -> str:
    explicit = str(task_input.get("report_type") or slots.get("report_type") or "").lower()
    if explicit:
        return explicit
    text = question.lower()
    for report_type, keywords in KEYWORD_TO_REPORT:
        if any(k in text for k in keywords):
            return report_type
    # 兜底：有明确时间范围就出订单报表，否则出销售趋势
    if slots.get("start_date") or slots.get("end_date"):
        return "orders"
    if re.search(r"(报表|报告|导出|汇总)", text):
        return "sales_trend"
    return "orders"


AGENT = ReportAgent()

__all__ = ["AGENT", "KEYWORD_TO_REPORT", "PARAM_KEYS", "ReportAgent"]
