"""主控 Agent 的六个节点（架构文档 §3.2）。

    intent_node → slot_node ─┬─(缺槽位)→ answer_node（追问）
                             ├─(闲聊)  → answer_node（寒暄直答）
                             └─(齐)→ planning_node → a2a_route_node
                                       → aggregate_node → answer_node

每个节点只返回自己负责的状态片段；「失败回退固定规则」体现在两处：
1. ``slot_node`` 的默认值填充（未给时间 → 近 30 天）；
2. ``answer_node`` 在 LLM 不可用时用已查到的真实数据拼装模板回答
   —— 宁可用模板说清事实，也不用模型编数字。

闲聊为什么要单独一条路：闲聊在 ``INTENT_TAXONOMY`` 里没有对应 Agent，
若照常进入规划，会被兜底规则「未匹配到专属 Agent → 退回知识库问答」捞走，
于是一句「你好」也要跑一次向量检索（实测 ~2.6s），返回三条退货政策 —— 既慢又错答。
因此闲聊在 ``route_after_slot`` 就短路到 ``answer_node``，**不规划、不调 Agent、不检索**。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from commercepivot.core.context import get_trace, trace_add
from commercepivot.core.errors import LLMError
from commercepivot.core.logging import get_logger
from commercepivot.core.metrics import GRAPH_NODES
from commercepivot.models.intent_bert import INTENT_TAXONOMY, classify_intent, is_smalltalk_text
from commercepivot.orchestrator.slots import SlotResult, extract_slots
from commercepivot.orchestrator.state import PivotState

log = get_logger("commercepivot.orchestrator.nodes")

# 聚合时的 Agent 展示优先级（报表/分析的结论排在知识问答之前）
AGENT_PRIORITY = (
    "report_agent",
    "order_analysis_agent",
    "product_analysis_agent",
    "after_sales_agent",
    "knowledge_rag_agent",
)

# 表格列偏好（复用报表口径，保证前端渲染与导出一致）
TABLE_COLUMNS: Dict[str, List[str]] = {
    "return_rate": ["product_name", "category", "platform", "order_count", "return_count", "return_rate", "refund_amount"],
    "orders": ["order_no", "product_name", "amount", "status", "platform", "order_date", "region"],
    "ad": ["dim", "impressions", "clicks", "ctr", "cost", "revenue", "roi"],
    "inventory": ["sku", "product_name", "warehouse", "quantity", "safety_stock", "turnover_days", "low_stock"],
    "products": ["name", "category", "price", "platform", "order_count"],
    "after_sales": ["order_no", "product_name", "type", "status", "refund_amount", "created_at"],
    "sales_trend": ["day", "month", "category", "platform", "order_count", "quantity", "gmv"],
    # 「各 X 的 Y」这类问题的聚合视图（order_analysis_agent 返回的维度切片）
    "breakdown_platform": ["platform", "order_count", "gmv", "buyer_count"],
    "breakdown_category": ["category", "order_count", "gmv", "buyer_count"],
    "breakdown_region": ["region", "order_count", "gmv", "buyer_count"],
    "breakdown_status": ["status", "order_count", "gmv", "buyer_count"],
    "breakdown_date": ["date", "order_count", "gmv", "buyer_count"],
    "breakdown_product": ["product_name", "category", "platform", "order_count", "gmv", "buyer_count"],
}

COLUMN_LABELS: Dict[str, str] = {
    "product_name": "商品", "category": "类目", "platform": "平台",
    "order_count": "订单量", "return_count": "退货单数", "return_rate": "退货率(%)",
    "refund_amount": "退款金额", "order_no": "订单号", "amount": "金额",
    "status": "状态", "order_date": "下单时间", "region": "地区", "gmv": "GMV",
    "quantity": "销量", "name": "商品", "price": "价格", "sku": "SKU",
    "warehouse": "仓库", "safety_stock": "安全库存", "turnover_days": "可售天数",
    "low_stock": "库存预警", "type": "售后类型", "created_at": "创建时间",
    "impressions": "曝光", "clicks": "点击", "ctr": "CTR(%)", "cost": "花费",
    "revenue": "成交额", "roi": "ROI", "dim": "维度", "date": "日期",
    "day": "日期", "month": "月份", "week": "周", "return_count_": "退货单数",
    "buyer_count": "买家数",
}


def _tick(node: str, state: PivotState, started: float) -> Dict[str, Any]:
    cost = int((time.perf_counter() - started) * 1000)
    GRAPH_NODES.inc({"node": node, "status": "ok"})
    timings = dict(state.get("node_timings") or {})
    timings[node] = cost
    trace_add("node", node=node, elapsed_ms=cost)
    return timings


# =============================================================== intent_node
async def intent_node(state: PivotState) -> Dict[str, Any]:
    started = time.perf_counter()
    question = state.get("question", "")
    try:
        result = classify_intent(question)
    except Exception as exc:  # noqa: BLE001 - 分类失败不允许阻断链路
        log.warning("意图识别异常，按知识问答兜底", error=str(exc))
        from commercepivot.models.intent_bert import IntentResult

        result = IntentResult(primary="knowledge_qa", primary_name="知识问答", domain="知识问答", confidence=0.2, source="fallback")
        GRAPH_NODES.inc({"node": "intent_node", "status": "degraded"})

    skill = result.primary
    payload = result.as_dict()
    payload["question"] = question
    trace_add("intent", primary=result.primary, confidence=round(result.confidence, 3), source=result.source)
    return {
        "intent": payload,
        "skill": skill,
        "node_timings": _tick("intent_node", state, started),
    }


# =============================================================== slot_node
async def slot_node(state: PivotState) -> Dict[str, Any]:
    started = time.perf_counter()
    from commercepivot.models.intent_bert import IntentResult

    intent_payload = state.get("intent") or {}
    intent = IntentResult(
        primary=intent_payload.get("primary", "chitchat"),
        primary_name=intent_payload.get("primary_name", ""),
        domain=intent_payload.get("domain", ""),
        confidence=float(intent_payload.get("confidence") or 0.0),
        source=intent_payload.get("source", "rule"),
        matched=list(intent_payload.get("matched") or []),
        agents=list(intent_payload.get("agents") or []),
    )
    if state.get("top_k"):
        intent_payload["_top_k"] = state["top_k"]

    result: SlotResult = extract_slots(state.get("question", ""), intent)
    if state.get("top_k"):
        result.slots["top_k"] = state["top_k"]

    return {
        "slots": result.slots,
        "missing_slots": result.missing,
        "clarification": result.clarification,
        "defaults_applied": result.defaults_applied,
        "slot_notes": list(result.notes),
        "node_timings": _tick("slot_node", state, started),
    }


def _is_smalltalk(state: PivotState) -> bool:
    """是否是「纯闲聊」——主意图为闲聊且没有任何业务 Agent 被命中。

    ``INTENT_TAXONOMY["chitchat"]["agent"]`` 是空串，所以 ``agents`` 里只要还有
    元素就说明这句同时带着业务诉求（例：「你好，帮我看下上个月销售额」判为 sales），
    此时**不能**短路，必须正常走规划。
    """
    intent = state.get("intent") or {}
    if intent.get("primary") != "chitchat":
        return False
    return not [a for a in (intent.get("agents") or []) if a]


def route_after_slot(state: PivotState) -> str:
    """条件边：闲聊直接回话，缺关键槽位就去问清楚，否则进入规划。"""
    if _is_smalltalk(state):
        return "chitchat"
    if state.get("clarification") and state.get("missing_slots"):
        return "clarify"
    return "plan"


# =============================================================== planning_node
async def planning_node(state: PivotState) -> Dict[str, Any]:
    started = time.perf_counter()
    intent = state.get("intent") or {}
    slots = dict(state.get("slots") or {})
    question = state.get("question", "")
    primary = intent.get("primary", "knowledge_qa")
    matched = list(intent.get("matched") or [primary])

    plan: List[Dict[str, Any]] = []
    reasons: List[str] = []

    # 1) 主意图对应的 Agent
    for intent_key in matched:
        agent = INTENT_TAXONOMY.get(intent_key, {}).get("agent")
        if not agent:
            continue
        focus = _focus_for(intent_key, question, slots)
        plan.append(
            {
                "agent": agent,
                "intent": intent_key,
                "focus": focus,
                "role": "primary" if intent_key == primary else "secondary",
                "task_input": {"question": question, "intent": intent, "slots": slots, "focus": focus},
            }
        )
        reasons.append(f"{INTENT_TAXONOMY[intent_key]['name']} → {agent}")

    # 2) 补充规则：分析类问题若同时问到政策/规则，并行拉一次知识库
    asks_policy = any(
        k in question for k in ("规定", "规则", "政策", "怎么处理", "能不能", "允许", "条件")
    )
    if asks_policy and primary != "knowledge_qa" and "knowledge_rag_agent" not in [p["agent"] for p in plan]:
        plan.append(
            {
                "agent": "knowledge_rag_agent",
                "intent": "knowledge_qa",
                "focus": "policy",
                "role": "support",
                "task_input": {
                    "question": question,
                    "intent": intent,
                    "slots": {**slots, "knowledge_query": question},
                    "focus": "policy",
                },
            }
        )
        reasons.append("问题同时涉及规则/政策 → 并行检索知识库")

    # 2.5) 政策/规则类知识问答的唯一执行者是 knowledge_rag_agent。
    #     「七天无理由退货的条件是什么」这类问句会让意图引擎顺带命中 after_sales
    #     （因为出现了「退货」二字），但用户要的是**解释**而不是**退货数据**；
    #     此时拉业务 Agent 只会把无关的售后流水灌进表格与结论。
    #     判定标准：问题里有没有明确的取数诉求 —— 有则保留业务 Agent。
    if primary == "knowledge_qa" and not _asks_for_data(question):
        plan = [p for p in plan if p["agent"] in ("knowledge_rag_agent", "report_agent")]
        if not any(p["agent"] == "knowledge_rag_agent" for p in plan):
            plan.insert(
                0,
                {
                    "agent": "knowledge_rag_agent",
                    "intent": "knowledge_qa",
                    "focus": "knowledge",
                    "role": "primary",
                    "task_input": {
                        "question": question,
                        "intent": intent,
                        "slots": {**slots, "knowledge_query": slots.get("knowledge_query") or question},
                        "focus": "knowledge",
                    },
                },
            )
        reasons = ["知识问答 → knowledge_rag_agent（政策/规则咨询只检索知识库，不叠加业务数据）"]

    # 3) 退货率排行是「订单量 × 退货单数」的**联合口径**，唯一执行者是
    #    order_analysis_agent；此时把 after_sales_agent 移出计划，避免同一口径
    #    被两个 Agent 重复计算 —— 这也是 planning_node 存在的意义（去重而非叠加）。
    ranking_return = any(k in question for k in ("退货率", "退款率")) or slots.get("metric") in ("退货率", "退款率")
    if ranking_return:
        # 退货率排行只需要一个数据源（订单量 × 退货单数），把「商品/售后」这类
        # 仅因问句里出现了「商品」二字而命中的次要 Agent 剔除，避免答案里混入
        # 无关的类目分布、售后进度等内容；报表/知识库作为明确的附加意图保留。
        keep = {"order_analysis_agent", "report_agent", "knowledge_rag_agent"}
        plan = [p for p in plan if p["agent"] in keep]
        target = next((p for p in plan if p["agent"] == "order_analysis_agent"), None)
        if target is None:
            target = {
                "agent": "order_analysis_agent",
                "intent": "after_sales",
                "focus": "return_rate",
                "role": "primary",
                "task_input": {"question": question, "intent": intent, "slots": dict(slots)},
            }
            plan.insert(0, target)
        else:
            plan.remove(target)
            plan.insert(0, target)
        target["focus"] = "return_rate"
        target["role"] = "primary"
        target["task_input"]["focus"] = "return_rate"
        target["task_input"]["slots"]["metric"] = "退货率"
        target["task_input"]["slots"]["order"] = slots.get("order", "desc")
        # 依据文案按**最终计划**重写，避免出现「计划里没有、说明里还有」的自相矛盾。
        # 名称一律取自计划项里**已经确定的** ``intent``，不要拿 agent 反查意图表：
        # sales / order / after_sales / ad 四个意图共用 order_analysis_agent，反查会
        # 恒命中字典序最靠前的 sales，把「售后分析（退货率）」显示成「销售分析」
        # —— 这是实测跑出来的缺陷（问句与意图都是退货，原因里却写着销售）。
        final_desc = []
        for item in plan:
            key = item.get("intent") or ""
            name = (INTENT_TAXONOMY.get(key) or {}).get("name") or item["agent"]
            final_desc.append(f"{name} → {item['agent']}")
        reasons = final_desc + [
            "命中退货率排行口径 → 收敛为 order_analysis_agent(focus=return_rate)，"
            "避免仅因问句出现「商品」二字就并行调用无关 Agent 污染结论"
        ]

    # 去重（同一 Agent 只调一次，多意图时合并到主次角色里）
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in plan:
        if item["agent"] in seen:
            continue
        seen.add(item["agent"])
        deduped.append(item)
    if not deduped:
        # 闲聊：没有 Agent 可调度，也**不该**退回知识库检索。
        # 正常情况下 route_after_slot 已经短路，这里只是防御性兜底
        # （例如直接调用本节点的场景），保证不会给「你好」返回退货政策。
        if _is_smalltalk(state):
            trace_add("plan", agents=[])
            return {
                "plan": [],
                "plan_reason": "闲聊寒暄 → 直接回应（不调用 Agent、不检索知识库）",
                "node_timings": _tick("planning_node", state, started),
            }
        deduped.append(
            {
                "agent": "knowledge_rag_agent",
                "intent": "knowledge_qa",
                "focus": "knowledge",
                "role": "primary",
                "task_input": {
                    "question": question, "intent": intent,
                    "slots": {**slots, "knowledge_query": question}, "focus": "knowledge",
                },
            }
        )
        reasons.append("未匹配到专属 Agent → 退回知识库问答")

    trace_add("plan", agents=[p["agent"] for p in deduped])
    return {
        "plan": deduped,
        "plan_reason": "；".join(reasons) or "默认规划",
        "node_timings": _tick("planning_node", state, started),
    }


# 「问题里是否含明确取数诉求」——用于区分「退货政策是什么」（要解释）
# 与「上个月退货率是多少」（要数据）。含这些词就认为用户想要真实业务数据。
_DATA_ASK_HINTS = (
    "多少", "几单", "几笔", "几个", "几条", "排行", "排名", "最高", "最低", "最多", "最少",
    "最好", "最差", "统计", "数据", "报表", "趋势", "走势", "对比", "环比", "同比",
    "占比", "分布", "销量", "销售额", "退货率", "退款率", "客单价", "库存", "roi", "投产比",
)


def _asks_for_data(question: str) -> bool:
    low = (question or "").lower()
    return any(h in low for h in _DATA_ASK_HINTS)


def _focus_for(intent_key: str, question: str, slots: Dict[str, Any]) -> str:
    mapping = {
        "sales": "orders",
        "order": "orders",
        "after_sales": "return_rate" if ("退货率" in question or "退款率" in question) else "after_sales",
        "product": "products",
        "inventory": "inventory",
        "ad": "ad",
        "knowledge_qa": "knowledge",
        "report": slots.get("report_type") or "report",
        "ticket": "ticket",
    }
    return mapping.get(intent_key, "knowledge")


# =============================================================== a2a_route_node
async def a2a_route_node(state: PivotState) -> Dict[str, Any]:
    started = time.perf_counter()
    from commercepivot.agents.registry import get_agent_registry

    plan = state.get("plan") or []
    registry = get_agent_registry()
    pairs: List[Tuple[str, Dict[str, Any]]] = [
        (item["agent"], item.get("task_input") or {}) for item in plan
    ]
    records = await registry.dispatch_many(pairs)
    tasks = [r.as_dict() for r in records]
    failed = [t for t in tasks if t.get("status") != "completed"]
    timings = _tick("a2a_route_node", state, started)
    if failed:
        GRAPH_NODES.inc({"node": "a2a_route_node", "status": "partial"})
    return {
        "agent_tasks": tasks,
        "errors": list(state.get("errors") or []) + [
            f"{t['agent_name']}: {t.get('error')}" for t in failed if t.get("error")
        ],
        "node_timings": timings,
    }


# =============================================================== aggregate_node
async def aggregate_node(state: PivotState) -> Dict[str, Any]:
    started = time.perf_counter()
    tasks = state.get("agent_tasks") or []
    slots = state.get("slots") or {}

    findings: List[str] = []
    notes: List[str] = []
    metrics: Dict[str, Any] = {}
    citations: List[Dict[str, Any]] = []
    degrade_reasons: List[str] = list(state.get("degrade_reasons") or [])
    degraded = False
    sources: List[Dict[str, Any]] = []

    ordered = sorted(
        tasks,
        key=lambda t: AGENT_PRIORITY.index(t["agent_name"])
        if t.get("agent_name") in AGENT_PRIORITY else len(AGENT_PRIORITY),
    )
    for task in ordered:
        output = task.get("output") or {}
        agent = task.get("agent_name", "")
        if task.get("status") != "completed":
            notes.append(f"「{agent}」未能完成：{task.get('error') or task.get('status')}，已按部分结果继续汇总。")
            degraded = True
            degrade_reasons.append(f"{agent}: {task.get('error') or task.get('status')}")
            continue

        agent_findings = [f for f in (output.get("findings") or []) if f]
        findings.extend(agent_findings)
        notes.extend([f"[{agent}] {n}" for n in (output.get("notes") or []) if n])
        if output.get("metrics"):
            metrics.update({f"{agent.split('_')[0]}_{k}": v for k, v in output["metrics"].items() if not isinstance(v, (dict, list))})
        if output.get("citations"):
            citations.extend(output["citations"])
        if output.get("degraded"):
            degraded = True
            if output.get("degrade_reason"):
                degrade_reasons.append(output["degrade_reason"])

        data = output.get("data") or {}
        if isinstance(data, dict) and (data.get("rows") or data.get("summary")):
            sources.append(
                {
                    "agent": agent,
                    "kind": data.get("kind"),
                    "row_count": data.get("row_count") or len(data.get("rows") or []),
                    "summary": data.get("summary") or {},
                    "rows": data.get("rows") or [],
                    "source_tools": data.get("source_tools") or [],
                    "saved_path": data.get("saved_path"),
                }
            )

    primary = sources[0] if sources else {}
    table = _build_table(primary, slots)
    aggregated = {
        "primary_agent": primary.get("agent"),
        "kind": primary.get("kind"),
        "sources": sources,
        "summary": primary.get("summary") or {},
    }

    timings = _tick("aggregate_node", state, started)
    return {
        "aggregated": aggregated,
        "findings": findings,
        "metrics": metrics,
        "citations": citations,
        "table": table,
        "degraded": degraded,
        "degrade_reasons": list(dict.fromkeys(degrade_reasons)),
        "notes": notes,
        "node_timings": timings,
    }


def _build_table(source: Dict[str, Any], slots: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = source.get("rows") or []
    if not rows:
        return None
    kind = source.get("kind") or "orders"
    columns = [c for c in TABLE_COLUMNS.get(kind, []) if c in rows[0]]
    if not columns:
        columns = [k for k in rows[0] if k != "id"][:8]
    columns = columns[:10]
    return {
        "title": f"{source.get('agent', '')} · {kind}",
        "kind": kind,
        "columns": [{"key": c, "label": COLUMN_LABELS.get(c, c)} for c in columns],
        "rows": [{c: row.get(c) for c in columns} for row in rows[:50]],
        "row_count": len(rows),
        "truncated": len(rows) > 50,
    }


# =============================================================== answer_node
ANSWER_SYSTEM_PROMPT = """你是「商枢」电商智能经营分析平台的助手，服务于店铺运营人员。

回答要求：
1. **只依据给定的查询结果作答**，不得编造数字、商品名或政策条款；数据缺失就直说。
2. 结论先行：第一句直接回答问题，再补关键数据支撑。
3. 查询结果里若有**明细表/分组结果**（按平台、类目、地区、时间等拆分），必须**逐行完整列举**，
   不得只挑金额大的前几名、也不得省略数值较小的行；行数超过 8 行时才可归纳，
   但要说明「共 N 行」。凡是引用了全量合计，就必须把所有行都列出来，别让读者对不上账。
4. 涉及金额用 ¥ 千分位，比率保留两位小数并带 %。
5. **仅当结果中确实出现「降级/部分结果」标记时**，才在末尾加一句「数据说明」如实交代；
   没有标记就不要写「数据说明」，也不要替上游解释格式或口径。
6. 纯文字回答，不要输出 JSON、不要输出 Markdown 表格（表格由前端单独渲染）。
7. 控制在 400 字以内。"""

SMALLTALK_SYSTEM_PROMPT = """你是「商枢」电商智能经营分析平台的助手。

用户这一句只是寒暄、致谢、告别，或者在问你能做什么 —— **不需要查询任何数据**，
也没有任何业务指标可以回答。

回答要求：
1. 1-3 句话，简洁友好，不要客套过头，也不要反复追问。
2. 顺带说清你能做的四类事：经营数据分析（销售额 / 订单量 / 退货率 / 广告投产比，
   可按平台、类目、地区、时间拆分与排行）、商品与库存（热销滞销、低库存预警）、
   售后与平台规则（退款退货流程、政策原文检索）、报表导出。
3. 若用户的话与电商经营无关或看不懂，礼貌说明一句，并给出 1 个示例问句引导。
4. 不得编造任何数字、订单、商品或政策条款。纯文字，不要 Markdown 表格。"""

# 闲聊的确定性回答（LLM 不可用时使用）。这里只描述**真实存在**的能力，
# 不涉及任何业务数字，因此不需要查库，也不存在编数据的风险。
SMALLTALK_CAPABILITIES = (
    "- 经营分析：销售额 / 订单量 / 退货率 / 广告投产比，可按平台、类目、地区、时间拆分与排行\n"
    "- 商品与库存：热销与滞销商品、类目结构、低库存预警\n"
    "- 售后与规则：退款退货流程、七天无理由等平台政策原文检索\n"
    "- 报表：按需导出销售 / 售后 / 广告报表"
)

SMALLTALK_SAMPLES = (
    "上个月各平台的销售额分别是多少？",
    "上个月抖店退货率最高的商品是什么？",
    "哪些 SKU 快断货了？",
    "七天无理由退货的条件是什么？",
)


def _smalltalk_answer(greeting: bool) -> str:
    """闲聊的模板回答。``greeting`` 为 False 表示没看懂用户在说什么。"""
    samples = "\n".join(f"  · {s}" for s in SMALLTALK_SAMPLES[:2 if greeting else 4])
    if greeting:
        return (
            "你好，我是「商枢」电商智能经营助手，可以帮你做这些事：\n"
            f"{SMALLTALK_CAPABILITIES}\n\n"
            f"直接说想了解什么就行，比如：\n{samples}"
        )
    return (
        "抱歉，我没太理解这句话，换个说法试试？我可以帮你做这些事：\n"
        f"{SMALLTALK_CAPABILITIES}\n\n"
        f"比如这样问：\n{samples}"
    )


def _context_for_llm(state: PivotState) -> str:
    intent = state.get("intent") or {}
    slots = state.get("slots") or {}
    metrics = state.get("metrics") or {}
    findings = state.get("findings") or []
    citations = state.get("citations") or []
    notes = state.get("notes") or []

    lines = [
        f"用户问题：{state.get('question', '')}",
        f"识别意图：{intent.get('domain')}/{intent.get('primary_name')}（置信度 {intent.get('confidence')}）",
        "抽取槽位：" + json.dumps(
            {k: v for k, v in slots.items() if v not in (None, "", [], {})}, ensure_ascii=False
        ),
        "",
        "子 Agent 结论：",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. {f}")
    if metrics:
        lines.append("")
        lines.append("关键指标：" + json.dumps(metrics, ensure_ascii=False, default=str))
    if citations:
        lines.append("")
        lines.append("知识来源：")
        for c in citations[:3]:
            lines.append(f"- {c.get('title')}（{c.get('source')}，相关度 {c.get('score')}）")
    if notes:
        lines.append("")
        lines.append("执行备注：" + "；".join(notes[:5]))
    table = state.get("table")
    if table:
        lines.append("")
        lines.extend(_render_table_for_llm(table))
    return "\n".join(lines)


# 喂给模型的最大明细行数：表格最多 50 行，全量铺进上下文会挤占输入预算，
# 而回答又限 400 字本来也列不完。30 行足够覆盖「逐行完整列举」的常见场景。
MAX_CONTEXT_ROWS = 30


def _render_table_for_llm(table: Dict[str, Any]) -> List[str]:
    """把表格逐行铺进 LLM 上下文。

    ``findings`` 是**结论摘要**：只覆盖头部几行、且不一定带全每个指标 —— 例如退货率
    排行仅报了商品名与比率，没报订单量。此前只喂 findings，模型被要求「逐行完整
    列举」时只能答「订单量未显示」（实测缺陷）。这里把 ``table.rows`` 原样喂进去，
    结论与表格才能对得上账。
    """
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    total = int(table.get("row_count") or len(rows))
    if not columns or not rows:
        return [f"可展示表格：共 {total} 行（无可展示明细）。"]
    out = [f"可展示表格（共 {total} 行，逐行明细如下，回答须逐行覆盖）："]
    shown = rows[:MAX_CONTEXT_ROWS]
    for i, row in enumerate(shown, 1):
        cells = "，".join(
            f"{c.get('label') or c.get('key')}={_cell_text(row.get(c.get('key')))}"
            for c in columns
        )
        out.append(f"{i}. {cells}")
    if len(shown) < total:
        out.append(f"（此处仅列出前 {len(shown)} 行，其余 {total - len(shown)} 行见前端表格。）")
    return out


def _cell_text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _template_answer(state: PivotState) -> str:
    """LLM 不可用时的确定性回答：全部内容来自真实查询结果。"""
    intent = state.get("intent") or {}
    slots = state.get("slots") or {}
    findings = state.get("findings") or []
    degraded = state.get("degraded")
    reasons = state.get("degrade_reasons") or []
    notes = state.get("notes") or []

    head = f"【{intent.get('domain', '')}·{intent.get('primary_name', '')}】"
    if not findings:
        body = "没有查询到符合条件的数据。请确认平台、时间范围或筛选条件是否需要调整。"
    else:
        body = "\n".join(findings)

    tail: List[str] = []
    defaults = state.get("defaults_applied") or []
    label = slots.get("period_label")
    if defaults:
        tail.append("数据说明：" + "；".join(defaults) + "。")
    elif label:
        tail.append(f"统计口径：{label}，平台={slots.get('platform') or '全平台'}。")
    if degraded and reasons:
        tail.append("数据说明：部分环节已降级 —— " + "；".join(dict.fromkeys(reasons))[:200] + "。")
    if notes:
        tail.append("执行备注：" + "；".join(notes[:3]))
    return f"{head}\n{body}" + ("\n\n" + "\n".join(tail) if tail else "")


async def _smalltalk_node(state: PivotState, started: float) -> Dict[str, Any]:
    """闲聊直答：不规划、不调 Agent、不检索知识库。

    仍然保留 LLM 可选：配了真实 API_KEY 就用专门的提示词把话说自然；
    不可用时回落到确定性模板（只讲平台**真实具备**的能力，不涉及业务数字，
    因此不存在编数据的风险）。
    """
    question = state.get("question", "")
    greeting = is_smalltalk_text(question)
    kind = "greeting" if greeting else "unclear"

    answer = ""
    # ``answer_source`` 表达的是**走了哪条链路**（chitchat 短路，未做任何检索），
    # 而「这段文字是大模型写的还是模板拼的」放在 ``answer_meta["generator"]``。
    # 两者分开的原因：闲聊是否检索知识库是链路问题（自检要断言），
    # 而 LLM 可用与否是运行时环境问题，不该互相遮住。
    source = "chitchat"
    meta: Dict[str, Any] = {
        "smalltalk": kind,
        "skip_retrieval": True,
        "generator": "template",
    }

    from commercepivot.models.llm_client import get_llm

    llm = get_llm()
    if llm.available:
        try:
            resp = await llm.chat(
                [
                    {"role": "system", "content": SMALLTALK_SYSTEM_PROMPT},
                    {"role": "user", "content": question},
                ],
                max_tokens=300,
            )
            if resp.text.strip():
                answer = resp.text.strip()
                meta["generator"] = "llm"
                meta.update(
                    {"model": resp.model, "usage": resp.usage, "llm_elapsed_ms": resp.elapsed_ms}
                )
        except LLMError as exc:
            log.warning("闲聊回答 LLM 不可用，使用模板", error=exc.message)
            meta["llm_error"] = exc.message
            meta["llm_degraded"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("闲聊回答异常，使用模板", error=str(exc))
            meta["llm_error"] = f"{type(exc).__name__}: {exc}"
    else:
        meta["llm_degraded"] = True
        meta["llm_error"] = "未配置 API_KEY（.env 保持占位符时的正常降级）"

    if not answer:
        answer = _smalltalk_answer(greeting)
        source = "chitchat"

    trace_add("answer", source=source, smalltalk=kind)
    return {
        "answer": answer,
        "answer_source": source,
        "answer_meta": meta,
        # 显式置空，让 API / CLI / 前端拿到一致的空结构而不是缺失键
        "plan": [],
        "plan_reason": (
            "闲聊寒暄 → 直接回应（不调用 Agent、不检索知识库）"
            if greeting
            else "未识别出明确意图 → 引导提问（不调用 Agent、不检索知识库）"
        ),
        "agent_tasks": [],
        "findings": [],
        "citations": [],
        "table": None,
        "trace": get_trace(),
        "node_timings": _tick("answer_node", state, started),
    }


async def answer_node(state: PivotState) -> Dict[str, Any]:
    started = time.perf_counter()

    # 追问分支：不调 LLM，直接返回模板化追问，避免多花一次 token
    if state.get("clarification") and state.get("missing_slots"):
        return {
            "answer": state["clarification"],
            "answer_source": "clarification",
            "answer_meta": {
                "missing_slots": state.get("missing_slots"),
                "generator": "template",
            },
            "node_timings": _tick("answer_node", state, started),
        }

    # 闲聊分支：不规划、不调 Agent、不检索知识库；也不走「数据说明」那套口径。
    if _is_smalltalk(state):
        return await _smalltalk_node(state, started)

    context = _context_for_llm(state)
    answer = ""
    source = "template"
    meta: Dict[str, Any] = {}

    from commercepivot.models.llm_client import get_llm

    llm = get_llm()
    if llm.available:
        try:
            resp = await llm.chat(
                [
                    {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": context},
                ],
                max_tokens=800,
            )
            answer = resp.text.strip()
            source = "llm"
            meta = {"model": resp.model, "usage": resp.usage, "llm_elapsed_ms": resp.elapsed_ms}
        except LLMError as exc:
            log.warning("LLM 不可用，使用模板回答", error=exc.message)
            meta["llm_error"] = exc.message
            meta["llm_degraded"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM 调用异常，使用模板回答", error=str(exc))
            meta["llm_error"] = f"{type(exc).__name__}: {exc}"
    else:
        meta["llm_degraded"] = True
        meta["llm_error"] = "未配置 API_KEY（.env 保持占位符时的正常降级）"

    if not answer:
        answer = _template_answer(state)
        source = "template"

    # ``generator`` 与 ``answer_source`` 是两个正交维度：前者回答「这段字是谁
    # 写的」，后者回答「走了哪条链路」。业务链路上两者恰好一致，闲聊链路上
    # ``answer_source`` 恒为 chitchat，只有 ``generator`` 会在 llm/template 间切换。
    meta["generator"] = "llm" if source == "llm" else "template"

    timings = _tick("answer_node", state, started)
    return {
        "answer": answer,
        "answer_source": source,
        "answer_meta": meta,
        "trace": get_trace(),
        "node_timings": timings,
    }


__all__ = [
    "AGENT_PRIORITY",
    "ANSWER_SYSTEM_PROMPT",
    "SMALLTALK_SYSTEM_PROMPT",
    "a2a_route_node",
    "aggregate_node",
    "answer_node",
    "build_llm_context",
    "build_template_answer",
    "intent_node",
    "planning_node",
    "route_after_slot",
    "slot_node",
]

# 对外公开别名：流式编排（orchestrator.graph.astream）需要复用同一套
# 提示词上下文与模板回答，避免在两处维护两份口径。
build_llm_context = _context_for_llm
build_template_answer = _template_answer
