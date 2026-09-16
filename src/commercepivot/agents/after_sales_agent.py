"""售后客服 Agent（架构文档 §3.3）。

职责：售后单进度、售后政策/FAQ 问答、工单创建。
这是一个**混合型 Agent**：既有结构化查询（售后单状态），
又有知识检索（政策解释），必要时还能落一张人工工单 —— 对应
文档所说的「售后单、FAQ、工单」。

注意：``create_ticket`` 是写操作，服务端默认只读时会返回 FORBIDDEN，
本 Agent 会把它转成「已生成工单草稿 + 提示开启写开关」的软失败，
而不是让整条链路报错。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from commercepivot.agents.base import BaseAgent


class AfterSalesAgent(BaseAgent):
    name = "after_sales_agent"
    description = "售后客服：售后单与退款进度查询、售后政策问答、人工工单"
    version = "1.0"
    skills = ["after_sale_status", "refund_progress", "faq_answer", "ticket_create"]
    tools = ["query_after_sales", "search_knowledge", "create_ticket"]
    port = 8004

    async def handle(self, task_input: Dict[str, Any]) -> Dict[str, Any]:
        slots = _slots(task_input)
        question = str(task_input.get("question") or "")
        focus = _focus(task_input, slots, question)
        notes: List[str] = []
        degraded = False
        reason: Optional[str] = None
        citations: List[Dict[str, Any]] = []
        findings: List[str] = []
        payload: Dict[str, Any] = {}
        metrics: Dict[str, Any] = {}

        # 1) 结构化部分：售后单 / 退款进度
        if focus in ("status", "both"):
            res = await self.call_tool(
                "query_after_sales",
                {
                    "order_id": slots.get("order_id"),
                    "order_no": slots.get("order_no"),
                    "status": slots.get("status"),
                    "after_sale_type": slots.get("after_sale_type"),
                    "platform": slots.get("platform"),
                    "start_date": slots.get("start_date"),
                    "end_date": slots.get("end_date"),
                    "breakdowns": ["type", "status"],
                    "limit": slots.get("limit", 100),
                },
            )
            payload = {
                "kind": "after_sales",
                "summary": res.get("summary") or {},
                "rows": res.get("rows") or [],
                "row_count": res.get("row_count") or 0,
                "source_tools": ["query_after_sales"],
            }
            metrics = payload["summary"]
            findings += _status_findings(payload)
            if not res.get("ok"):
                degraded = True
                reason = self.fail_reason(res)
                notes.append(f"售后单查询失败：{reason}")

        # 2) 知识部分：售后政策 / FAQ
        if focus in ("policy", "both"):
            query = slots.get("knowledge_query") or question
            res = await self.call_tool(
                "search_knowledge",
                {"query": query, "top_k": slots.get("top_k", 3), "collection": slots.get("collection")},
            )
            hits = res.get("rows") or []
            for hit in hits:
                citations.append(
                    {
                        "id": hit.get("id"),
                        "title": hit.get("title"),
                        "source": hit.get("source"),
                        "collection": hit.get("collection"),
                        "score": hit.get("rerank_score") or hit.get("score"),
                        "retrieval_mode": hit.get("retrieval_mode"),
                    }
                )
            if hits:
                findings.append(f"政策依据：{hits[0].get('title')} —— {_clip(hits[0].get('content'))}")
            payload.setdefault("knowledge", {})
            payload["knowledge"] = {
                "rows": hits,
                "summary": res.get("summary") or {},
                "retrieval_mode": (res.get("summary") or {}).get("retrieval_mode"),
            }
            payload.setdefault("source_tools", [])
            if "search_knowledge" not in payload["source_tools"]:
                payload["source_tools"].append("search_knowledge")
            if res.get("degraded"):
                degraded = True
                reason = res.get("degrade_reason") or reason
                notes.append(reason or "知识检索降级")

        # 3) 工单：仅在用户明确要求转人工/投诉时触发
        if slots.get("want_ticket") or focus == "ticket":
            ticket_res = await self.call_tool(
                "create_ticket",
                {
                    "summary": slots.get("ticket_summary") or (question[:120] or "用户请求人工介入"),
                    "priority": slots.get("priority", "P2"),
                    "contact": slots.get("contact"),
                },
            )
            if ticket_res.get("ok"):
                payload["ticket"] = ticket_res.get("data") or ticket_res.get("summary")
                tid = (ticket_res.get("summary") or {}).get("ticket_id")
                findings.append(f"已创建人工工单 {tid}，客服将在 1 个工作小时内响应。")
            else:
                payload["ticket"] = {
                    "ok": False,
                    "code": ticket_res.get("code"),
                    "message": ticket_res.get("message"),
                }
                notes.append(
                    "写操作被拒绝（服务端默认只读）："
                    f"{ticket_res.get('message')}。已改为生成工单草稿，建议人工在后台补录。"
                )
                findings.append("已将问题整理为工单草稿，但自动建单未开启，需人工处理。")

        return {
            "agent": self.name,
            "status": "completed" if payload else "empty",
            "focus": focus,
            "data": payload,
            "findings": findings,
            "metrics": metrics,
            "citations": citations,
            "notes": notes,
            "degraded": degraded,
            "degrade_reason": reason,
        }


def _status_findings(payload: Dict[str, Any]) -> List[str]:
    s = payload.get("summary") or {}
    rows = payload.get("rows") or []
    if not s and not rows:
        return ["未查询到匹配的售后单。"]
    if not rows:
        return ["该条件下没有售后记录，说明售后情况良好。"]
    out = [
        f"共 {s.get('after_sale_count', len(rows))} 笔售后，"
        f"退款金额合计 ¥{s.get('refund_total', 0):,.2f}，"
        f"售后率 {s.get('after_sale_rate', 0)}%。",
    ]
    out.append(
        f"类型构成：退货 {s.get('return_count', 0)} 笔、换货 {s.get('exchange_count', 0)} 笔、"
        f"仅退款 {s.get('refund_only_count', 0)} 笔。"
    )
    pending = [r for r in rows if str(r.get("status")) in ("待审核", "处理中")]
    if pending:
        out.append(f"其中 {len(pending)} 笔仍在处理中，需关注时效。")
    return out


def _clip(text: Any, limit: int = 90) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _slots(task_input: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(task_input.get("slots") or {})
    for key in (
        "order_id", "order_no", "status", "after_sale_type", "platform", "start_date",
        "end_date", "limit", "top_k", "collection", "knowledge_query", "contact",
        "priority", "want_ticket", "ticket_summary",
    ):
        if task_input.get(key) not in (None, ""):
            merged.setdefault(key, task_input.get(key))
    return merged


def _focus(task_input: Dict[str, Any], slots: Dict[str, Any], question: str) -> str:
    explicit = str(task_input.get("focus") or slots.get("focus") or "").lower()
    # 退货率排行的联合口径由 order_analysis_agent 承担；本 Agent 只负责
    # 「售后单进度 + 政策解释」，收到 return_rate 时按结构化查询处理。
    if explicit in ("return_rate", "refund", "after_sales"):
        explicit = "status"
    if explicit in ("status", "policy", "ticket", "both"):
        return explicit
    text = question.lower()
    if any(k in text for k in ("转人工", "人工客服", "找客服", "工单", "投诉", "举报")):
        return "ticket"
    asks_status = any(k in text for k in ("订单", "单号", "退款进度", "到哪", "处理到", "进度", "状态"))
    asks_policy = any(
        k in text for k in ("怎么", "如何", "规定", "规则", "政策", "条件", "能不能", "多久", "faq", "怎么办")
    )
    if asks_status and asks_policy:
        return "both"
    if asks_status:
        return "status"
    return "policy"


AGENT = AfterSalesAgent()

__all__ = ["AGENT", "AfterSalesAgent"]
