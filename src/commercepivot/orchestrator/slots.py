"""排槽位抽取与缺失追问（架构文档 §3.2 slot_node）。

抽取策略：**规则优先、LLM 可选增强**。
- 平台 / 时间 / 指标 / 排序 / 条数 / 状态 / 商品 / 单号 / 导出格式，全部用
  正则 + 词表 + ``utils.timeparse`` 抽取 —— 确定性、零成本、毫秒级；
- 不额外调 LLM 做槽位抽取，是因为这些字段的值域是**封闭的**（平台就那几个、
  指标就那十来个），规则比模型更准且可回归测试。

缺失追问的判定原则：**只有「缺了会导致答案错误」的槽位才追问**，
「缺了只是不够精确」的一律套默认值并在答案里注明（例如未给时间就默认近 30 天）。
这样既满足文档的「缺失追问」，又不会把用户烦死。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from commercepivot.core.logging import get_logger
from commercepivot.models.intent_bert import IntentResult
from commercepivot.utils.timeparse import parse_period

log = get_logger("commercepivot.orchestrator.slots")

# ------------------------------------------------------------------ 词表
PLATFORM_ALIASES: Dict[str, str] = {
    "抖店": "抖店", "抖音小店": "抖店", "抖音": "抖店", "抖in": "抖店",
    "京东": "京东", "京东自营": "京东", "jd": "京东",
    "淘宝": "淘宝", "淘系": "淘宝", "淘宝店": "淘宝",
    "天猫": "天猫", "tmall": "天猫", "天猫店": "天猫",
    "拼多多": "拼多多", "拼夕夕": "拼多多", "pdd": "拼多多",
    "快手": "快手", "快手小店": "快手",
    "小红书": "小红书", "视频号": "视频号", "唯品会": "唯品会",
}
ALL_PLATFORM_HINTS = ("全平台", "所有平台", "各个平台", "各平台", "全部平台", "不分平台")

# 广告投放渠道词表。注意「京东京准」含「京东」、「巨量引擎」口语常简称「巨量」，
# 若不先于店铺平台判定，会被误判成 platform=京东/抖店，从而把广告数据错误过滤。
AD_PLATFORM_ALIASES: Dict[str, str] = {
    "巨量引擎": "巨量引擎", "巨量": "巨量引擎", "千川": "巨量引擎", "抖音广告": "巨量引擎",
    "京东京准": "京东京准", "京准": "京东京准", "京东广告": "京东京准",
    "多多推广": "多多推广", "拼多多推广": "多多推广", "多多广告": "多多推广",
    "阿里万相台": "阿里万相台", "万相台": "阿里万相台", "直通车": "阿里万相台", "超级推荐": "阿里万相台",
    "小红书聚光": "小红书聚光", "聚光": "小红书聚光",
    "腾讯广告": "腾讯广告", "广点通": "腾讯广告", "视频号广告": "腾讯广告",
}

METRIC_ALIASES: Dict[str, str] = {
    "退货率": "退货率", "退款率": "退款率", "售后率": "售后率", "退货比例": "退货率",
    "销售额": "销售额", "gmv": "销售额", "成交额": "销售额", "营业额": "销售额", "营收": "销售额",
    "订单量": "订单量", "单量": "订单量", "订单数": "订单量", "成交笔数": "订单量",
    "销量": "销量", "出货量": "销量",
    "客单价": "客单价", "平均客单价": "客单价",
    "库存": "库存", "库存量": "库存", "周转": "库存周转",
    "roi": "ROI", "投产比": "ROI", "roas": "ROI",
    "转化率": "转化率", "曝光": "曝光量", "点击": "点击量", "花费": "广告花费",
    "退款金额": "退款金额", "退款额": "退款金额",
}

STATUS_VALUES = ("已付款", "已发货", "已完成", "已退款", "已取消", "待发货", "未发货")
AFTER_SALE_STATUS = ("待审核", "处理中", "已完成", "已拒绝")
AFTER_SALE_TYPES = ("退货", "换货", "仅退款")

DESC_WORDS = ("最高", "最多", "最大", "最好", "最差", "top", "排名", "排行", "降序", "由高到低")
ASC_WORDS = ("最低", "最少", "最小", "升序", "由低到高")

TOP_N_RE = re.compile(r"(?:top|前|排名前|头)\s*(\d{1,2})", re.IGNORECASE)
CN_NUM_RE = re.compile(r"前([一二三四五六七八九十]|\d{1,2})")
ORDER_NO_RE = re.compile(r"\b([A-Z]{2}\d{10,20})\b")
SKU_RE = re.compile(r"\b([A-Z]{2,6}-[A-Z0-9]{2,12})\b")
USER_RE = re.compile(r"(?:用户|买家|客户)\s*(?:id)?\s*[:：]?\s*([A-Za-z0-9_-]{3,32})")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

CATEGORY_HINTS = (
    "家居", "服饰", "数码", "美妆", "食品", "母婴", "运动", "户外", "家电",
    "日用", "个护", "鞋靴", "箱包", "宠物", "办公", "图书",
)

# 各主意图的「必需槽位」——缺了会导致答案错误
# 注：sales 不把 metric 列为必需 —— 「上个月销售趋势怎么样」缺指标时
# 套默认值（销售额）远好过弹一句反问；真正必须知道指标的「排行类」问题
# 由 _evaluate_missing 里的排行词另行触发追问。
REQUIRED_SLOTS: Dict[str, tuple[str, ...]] = {
    "sales": (),
    "order": (),
    "after_sales": (),
    "product": (),
    "inventory": (),
    "ad": (),
    "knowledge_qa": ("knowledge_query",),
    "report": ("report_type",),
    "ticket": ("ticket_summary",),
    "chitchat": (),
}


@dataclass(slots=True)
class SlotResult:
    slots: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    clarification: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    defaults_applied: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "slots": self.slots,
            "missing": self.missing,
            "clarification": self.clarification,
            "notes": self.notes,
            "defaults_applied": self.defaults_applied,
        }


def _pick_platform(text: str) -> tuple[Optional[str], bool]:
    if any(h in text for h in ALL_PLATFORM_HINTS):
        return None, True
    low = text.lower()
    for alias, canonical in PLATFORM_ALIASES.items():
        if alias in low:
            return canonical, True
    return None, False


def _pick_ad_platform(text: str) -> Optional[str]:
    low = text.lower()
    # 长词优先，避免「京东」先于「京东京准」命中
    for alias in sorted(AD_PLATFORM_ALIASES, key=len, reverse=True):
        if alias in low or alias in text:
            return AD_PLATFORM_ALIASES[alias]
    return None


def _pick_metric(text: str) -> Optional[str]:
    low = text.lower()
    exact = {"gmv": "销售额", "roi": "ROI", "roas": "ROI"}
    for token, canonical in exact.items():
        if token in low:
            return canonical
    for alias, canonical in METRIC_ALIASES.items():
        if alias in text or alias in low:
            return canonical
    return None


def _pick_top_n(text: str) -> Optional[int]:
    m = TOP_N_RE.search(text)
    if m:
        return max(1, min(int(m.group(1)), 50))
    m = CN_NUM_RE.search(text)
    if m:
        token = m.group(1)
        n = int(token) if token.isdigit() else CN_NUM.get(token, 0)
        if n:
            return max(1, min(n, 50))
    if any(k in text for k in ("最高", "最低", "最差", "最好")):
        return 5  # 排行类默认给 Top5，比只给一条更有用
    return None


def _pick_order(text: str) -> Optional[str]:
    low = text.lower()
    if any(w in low for w in ASC_WORDS):
        return "asc"
    if any(w in low for w in DESC_WORDS):
        return "desc"
    return None


def _pick_category(text: str) -> Optional[str]:
    for c in CATEGORY_HINTS:
        if c in text:
            return c
    return None


def _pick_status(text: str, intent: IntentResult) -> Optional[str]:
    pool = AFTER_SALE_STATUS if intent.primary == "after_sales" else STATUS_VALUES
    for v in pool:
        if v in text:
            return v
    return None


def _pick_after_sale_type(text: str) -> Optional[str]:
    for v in AFTER_SALE_TYPES:
        if v in text:
            return v
    return None


def _pick_format(text: str) -> Optional[str]:
    low = text.lower()
    if "csv" in low:
        return "csv"
    if "excel" in low or "xlsx" in low:
        return "csv"  # 统一走 CSV，Excel 可后续用 pandas 转
    if "markdown" in low or "md" == low.strip():
        return "markdown"
    return None


def _pick_report_type(text: str) -> Optional[str]:
    from commercepivot.agents.report_agent import KEYWORD_TO_REPORT

    low = text.lower()
    for report_type, keywords in KEYWORD_TO_REPORT:
        if any(k in low for k in keywords):
            return report_type
    return None


def _pick_region(text: str) -> Optional[str]:
    m = re.search(r"([\u4e00-\u9fff]{2,8}(?:省|市|区|县))", text)
    if m:
        return m.group(1)
    for r in ("华东", "华南", "华北", "华中", "西南", "西北", "东北", "江浙沪"):
        if r in text:
            return r
    return None


def _pick_warehouse(text: str) -> Optional[str]:
    m = re.search(r"([\u4e00-\u9fff]{2,6}仓)", text)
    return m.group(1) if m else None


def _pick_keyword(text: str) -> Optional[str]:
    m = re.search(r"「([^」]{2,20})」|“([^”]{2,20})”|\"([^\"]{2,20})\"|'([^']{2,20})'", text)
    if m:
        return next(g for g in m.groups() if g)
    return None


def _clean_knowledge_query(text: str) -> str:
    q = re.sub(r"^(请问|麻烦问下|想问一下|帮我查一下|帮我|请问一下)[，,：:]?", "", text.strip())
    return q.strip() or text.strip()


# ------------------------------------------------------------------ 主入口
def extract_slots(question: str, intent: IntentResult) -> SlotResult:
    text = (question or "").strip()
    result = SlotResult()

    # ---- 闲聊寒暄：没有任何业务槽位可抽
    # 「你好」若照常填充，槽位里会出现「未指定平台，按全平台统计 / 默认统计近 30 天」
    # 这类统计口径 —— 对一句问候毫无意义，还会让答案看起来像在答错题。
    if intent.primary == "chitchat":
        return result

    # ---- 知识问答：查的是「规则条文」而不是「经营数据」
    # 意图识别已保证进到这里的是纯政策/规则询问（带取数诉求的问句会被判为
    # 业务意图），因此业务槽位（平台 / 时间 / 指标 / 售后类型）全部无意义。
    # 若照常填充，答案里会挂上「统计近 30 天 / 全平台」这类误导性的统计口径。
    if intent.primary == "knowledge_qa":
        result.slots["knowledge_query"] = _clean_knowledge_query(text)
        _evaluate_missing(text, intent, result)
        return result

    # ---- 平台（先判广告渠道，再判店铺平台，二者互斥）
    ad_platform = _pick_ad_platform(text)
    if ad_platform:
        result.slots["ad_platform"] = ad_platform
        result.slots["platform"] = None
    else:
        platform, mentioned = _pick_platform(text)
        if platform:
            result.slots["platform"] = platform
        elif not mentioned:
            result.slots["platform"] = None
            result.defaults_applied.append("未指定平台，按全平台统计")

    # ---- 时间
    period = parse_period(text)
    if period:
        result.slots["start_date"] = period.start.isoformat()
        result.slots["end_date"] = period.end.isoformat()
        result.slots["period_label"] = period.label
    else:
        from commercepivot.utils.timeparse import Period
        import datetime as dt

        today = dt.date.today()
        start = today - dt.timedelta(days=29)
        fallback = Period(start, today, "近30天")
        result.slots["start_date"] = fallback.start.isoformat()
        result.slots["end_date"] = fallback.end.isoformat()
        result.slots["period_label"] = fallback.label
        result.defaults_applied.append("未指定时间范围，默认统计近 30 天")

    # ---- 指标 / 排序 / 条数
    metric = _pick_metric(text)
    if metric:
        result.slots["metric"] = metric
    top_n = _pick_top_n(text)
    if top_n:
        result.slots["top_k"] = top_n
    order = _pick_order(text)
    if order:
        result.slots["order"] = order
    want_trend = any(k in text for k in ("趋势", "走势", "环比", "同比", "对比", "变化", "每天", "每日", "逐日", "每月", "逐月"))
    if want_trend:
        result.slots["want_trend"] = True
    group_by = None
    if any(k in text for k in ("每天", "每日", "逐日", "按天")):
        group_by = "day"
    elif any(k in text for k in ("每周", "按周", "周度")):
        group_by = "week"
    elif any(k in text for k in ("每月", "按月", "月度")):
        group_by = "month"
    elif any(k in text for k in ("按类目", "按品类", "类目维度")):
        group_by = "category"
    elif any(k in text for k in ("按地区", "按区域", "地区分布")):
        group_by = "region"
    if group_by:
        result.slots["group_by"] = group_by

    # ---- 聚合维度（breakdowns）
    # 「各平台的销售额」「按类目看销量」这类问题问的是**按维度聚合后的事实**，
    # 而不是订单明细。这里把维度词映射成 breakdowns，交给 MCP 工具做 GROUP BY；
    # 若识别不出维度则保持 None，表格回落到明细行。
    breakdowns: List[str] = []
    if any(k in text for k in ("各平台", "分平台", "按平台", "平台分布", "平台对比", "哪个平台", "每个平台", "各渠道", "渠道分布")):
        breakdowns.append("platform")
    if any(k in text for k in ("按类目", "各类目", "分品类", "品类分布", "按品类", "类目分布", "各个类目")) or group_by == "category":
        breakdowns.append("category")
    if any(k in text for k in ("各商品", "按商品", "商品排行", "商品维度", "分商品", "各sku", "各 SKU")) or group_by == "product":
        breakdowns.append("product")
    if any(k in text for k in ("按地区", "各地区", "地区分布", "按区域", "各区域", "区域分布")) or group_by == "region":
        breakdowns.append("region")
    if any(k in text for k in ("按状态", "各状态", "状态分布")) or group_by == "status":
        breakdowns.append("status")
    if any(k in text for k in ("按天", "每天", "每日", "逐日", "按日期", "日期分布")) or group_by == "day":
        breakdowns.append("date")
    if breakdowns:
        result.slots["breakdowns"] = list(dict.fromkeys(breakdowns))

    # ---- 实体字段
    status = _pick_status(text, intent)
    if status:
        result.slots["status"] = status
    else:
        after_sale_type = _pick_after_sale_type(text)
        if after_sale_type:
            result.slots["after_sale_type"] = after_sale_type
    category = _pick_category(text)
    if category:
        result.slots["category"] = category
    region = _pick_region(text)
    if region:
        result.slots["region"] = region
    warehouse = _pick_warehouse(text)
    if warehouse:
        result.slots["warehouse"] = warehouse
    keyword = _pick_keyword(text)
    if keyword:
        result.slots["keyword"] = keyword
    m = ORDER_NO_RE.search(text)
    if m:
        result.slots["order_no"] = m.group(1)
    m = SKU_RE.search(text)
    if m:
        result.slots["sku"] = m.group(1)
    fmt = _pick_format(text)
    if fmt:
        result.slots["fmt"] = fmt

    # ---- 意图相关专属槽位
    if intent.primary == "knowledge_qa":
        result.slots["knowledge_query"] = _clean_knowledge_query(text)
    if intent.primary == "report" or "report" in intent.matched:
        report_type = _pick_report_type(text)
        if report_type:
            result.slots["report_type"] = report_type
    if intent.primary == "ticket" or "ticket" in intent.matched:
        result.slots["ticket_summary"] = text[:200]
        result.slots["want_ticket"] = True
        if any(k in text for k in ("紧急", "马上", "立刻", "严重")):
            result.slots["priority"] = "P1"
    if intent.primary == "inventory" and any(k in text for k in ("缺货", "断货", "预警", "低于安全库存", "不足")):
        result.slots["low_stock_only"] = True

    # ---- 派生槽位
    if metric == "退货率" and intent.primary == "after_sales":
        result.slots.setdefault("order", "desc")
    # 销售类泛问题（趋势/概览）没点明指标时，默认按销售额统计并在口径里注明
    if intent.primary == "sales" and not result.slots.get("metric"):
        result.slots["metric"] = "销售额"
        result.defaults_applied.append("未指定指标，默认按「销售额」统计")

    # ---- 缺失判定与追问
    _evaluate_missing(text, intent, result)
    return result


def _evaluate_missing(text: str, intent: IntentResult, result: SlotResult) -> None:
    slots = result.slots
    required = list(REQUIRED_SLOTS.get(intent.primary, ()))

    # 「排行/对比」类问题的隐含必需项：指标
    if any(k in text for k in ("最高", "最低", "最多", "最少", "最好", "最差", "最贵", "最便宜", "排行", "排名", "top", "前几")):
        if "metric" not in required:
            required.append("metric")

    for slot in required:
        if slots.get(slot) in (None, "", []):
            result.missing.append(slot)

    if not result.missing:
        return

    if "metric" in result.missing:
        result.clarification = (
            "想帮您排个榜，但没看出要按哪个指标排序。您更关注"
            "「退货率」「销售额」「订单量」还是「销量」？"
        )
    elif "knowledge_query" in result.missing:
        result.clarification = "请补充一下您具体想咨询的问题，例如「七天无理由退货的条件」。"
    elif "report_type" in result.missing:
        result.clarification = (
            "请问您要哪一类报表？可选：订单明细、商品、售后、库存、广告 ROI、"
            "退货率排行、销售趋势、畅销商品排行。"
        )
    elif "ticket_summary" in result.missing:
        result.clarification = "请简单描述一下需要人工处理的问题，我帮您建单。"
    else:
        result.clarification = f"为了准确回答，请补充以下信息：{'、'.join(result.missing)}。"


def slot_defaults_note(result: SlotResult) -> str:
    return "；".join(result.defaults_applied)


__all__ = [
    "PLATFORM_ALIASES",
    "REQUIRED_SLOTS",
    "SlotResult",
    "extract_slots",
    "slot_defaults_note",
]
