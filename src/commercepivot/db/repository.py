"""MySQL 仓储层：业务 SQL 全部集中在这里（架构文档 §6 数据模型）。

约定
----
- **只读为主**：绝大多数函数是 SELECT；写操作（工单、会话、审计、任务）
  集中在文件末尾并显式标注，是否放行由 ``core.security.ensure_write_allowed`` 控制。
- **不吞异常**：连接类错误统一抛 ``MySQLUnavailable``，由 MCP 注册表 / API 层
  按 §11 转成结构化降级响应，避免上层看到裸 500。
- **返回 dict**：与 ``db/models.py`` 的模型一一对应，便于跨层直接透传。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from commercepivot.core.logging import get_logger
from commercepivot.db.mysql import MySQLUnavailable, get_mysql
from commercepivot.utils.timeparse import Period, period_from_dates, to_datetime_range

log = get_logger("commercepivot.db.repository")

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
ORDER_STATUSES = ("已付款", "已发货", "已完成", "已退款", "已取消")
AFTER_SALE_TYPES = ("退货", "换货", "仅退款")


def clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT) -> int:
    try:
        value = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_LIMIT))


def resolve_period(start_date: Any = None, end_date: Any = None) -> Optional[Period]:
    return period_from_dates(start_date, end_date)


def _dtr(period: Period | None) -> Tuple[Optional[str], Optional[str]]:
    return to_datetime_range(period)


def _notice(period: Period | None) -> str:
    return period.label if period and period.label else "全部时间"


# --------------------------------------------------------------- 聚合切片
# 订单 / 售后的可选聚合维度。declare 成表驱动，新增维度只需加一行。
_ORDER_BREAKDOWNS: Dict[str, Dict[str, str]] = {
    "product": {
        "select": "o.product_id, p.name AS product_name, p.category, o.platform AS platform",
        "group": "o.product_id, p.name, p.category, o.platform",
        "name": "product",
    },
    "platform": {"select": "o.platform AS platform", "group": "o.platform", "name": "platform"},
    "status": {"select": "o.status AS status", "group": "o.status", "name": "status"},
    "date": {"select": "DATE(o.order_date) AS date", "group": "DATE(o.order_date)", "name": "date"},
    "region": {"select": "o.region AS region", "group": "o.region", "name": "region"},
    "category": {"select": "p.category AS category", "group": "p.category", "name": "category"},
}

_AFTER_SALE_BREAKDOWNS: Dict[str, Dict[str, str]] = {
    "product": {
        "select": "o.product_id, p.name AS product_name, p.category, o.platform AS platform",
        "group": "o.product_id, p.name, p.category, o.platform",
        "name": "product",
    },
    "platform": {"select": "o.platform AS platform", "group": "o.platform", "name": "platform"},
    "status": {"select": "a.status AS status", "group": "a.status", "name": "status"},
    "type": {"select": "a.type AS type", "group": "a.type", "name": "type"},
    "date": {"select": "DATE(a.created_at) AS date", "group": "DATE(a.created_at)", "name": "date"},
}

ORDER_BREAKDOWN_KEYS = tuple(_ORDER_BREAKDOWNS)
AFTER_SALE_BREAKDOWN_KEYS = tuple(_AFTER_SALE_BREAKDOWNS)


def _normalize_dims(dims: Any, allowed: Sequence[str]) -> List[str]:
    if not dims:
        return []
    if isinstance(dims, str):
        dims = re.split(r"[,，\s]+", dims)
    out: List[str] = []
    for d in dims:
        key = str(d).strip().lower()
        if key in allowed and key not in out:
            out.append(key)
    return out


# =============================================================== 订单
_ORDER_SELECT = """
SELECT o.id, o.order_no, o.user_id, o.product_id,
       p.name AS product_name, p.category,
       o.amount, o.status, o.platform, o.order_date, o.region
FROM orders o
LEFT JOIN products p ON p.id = o.product_id
WHERE 1=1
"""


def query_orders(
    start_date: Any = None,
    end_date: Any = None,
    platform: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    product_id: int | None = None,
    region: str | None = None,
    limit: int = DEFAULT_LIMIT,
    breakdowns: Any = None,
    breakdown_limit: int = 200,
) -> Dict[str, Any]:
    """按时间、平台、状态查询订单，附带汇总。

    ``breakdowns`` 指定额外聚合切片（product / platform / status / date / region / category），
    切片结果放在 ``summary['by_<dim>']``。这是「取数据」与「算指标」的分工点：
    MCP 工具只负责按维度聚合出**事实**，派生指标（退货率、环比、TopN）由
    A2A 子 Agent 自行计算 —— 对应架构文档 §7 第 6 步。
    """
    period = resolve_period(start_date, end_date)
    sql = _ORDER_SELECT
    params: List[Any] = []
    sql += " AND o.order_date >= %s AND o.order_date < %s" if period else ""
    if period:
        params += list(_dtr(period))
    if platform:
        sql += " AND o.platform = %s"
        params.append(platform)
    if status:
        sql += " AND o.status = %s"
        params.append(status)
    if product_id:
        sql += " AND o.product_id = %s"
        params.append(product_id)
    if region:
        sql += " AND o.region LIKE %s"
        params.append(f"%{region}%")
    if keyword:
        sql += " AND (o.order_no LIKE %s OR p.name LIKE %s OR o.region LIKE %s)"
        params += [f"%{keyword}%"] * 3

    limit = clamp_limit(limit)
    rows = get_mysql().query(sql + " ORDER BY o.order_date DESC, o.id DESC LIMIT %s", params + [limit])

    # ---- 汇总：条数 / GMV / 客单价 / 去重买家
    agg_sql = """
        SELECT COUNT(*) AS order_count,
               COALESCE(SUM(o.amount), 0) AS gmv,
               COALESCE(AVG(o.amount), 0) AS avg_amount,
               COUNT(DISTINCT o.user_id) AS buyer_count
        FROM orders o LEFT JOIN products p ON p.id = o.product_id
        WHERE 1=1
    """
    agg_params: List[Any] = []
    if period:
        agg_sql += " AND o.order_date >= %s AND o.order_date < %s"
        agg_params += list(_dtr(period))
    if platform:
        agg_sql += " AND o.platform = %s"
        agg_params.append(platform)
    if status:
        agg_sql += " AND o.status = %s"
        agg_params.append(status)
    agg = get_mysql().query_one(agg_sql, agg_params) or {}
    by_status = get_mysql().query(
        """
        SELECT o.status, COUNT(*) AS cnt, COALESCE(SUM(o.amount), 0) AS gmv
        FROM orders o LEFT JOIN products p ON p.id = o.product_id WHERE 1=1
        """
        + (" AND o.order_date >= %s AND o.order_date < %s" if period else "")
        + (" AND o.platform = %s" if platform else "")
        + " GROUP BY o.status ORDER BY cnt DESC",
        (list(_dtr(period)) if period else []) + ([platform] if platform else []),
    )
    by_platform = get_mysql().query(
        """
        SELECT o.platform, COUNT(*) AS cnt, COALESCE(SUM(o.amount), 0) AS gmv
        FROM orders o LEFT JOIN products p ON p.id = o.product_id
        WHERE 1=1
        """
        + (" AND o.order_date >= %s AND o.order_date < %s" if period else "")
        + " GROUP BY o.platform ORDER BY gmv DESC",
        list(_dtr(period)) if period else [],
    )

    by_product: List[Dict[str, Any]] = []
    extra_breakdowns: Dict[str, List[Dict[str, Any]]] = {}
    wanted = _normalize_dims(breakdowns, ORDER_BREAKDOWN_KEYS)
    for dim in wanted:
        spec = _ORDER_BREAKDOWNS[dim]
        sql = f"""
            SELECT {spec['select']},
                   COUNT(*) AS order_count,
                   COALESCE(SUM(o.amount), 0) AS gmv,
                   COUNT(DISTINCT o.user_id) AS buyer_count
            FROM orders o LEFT JOIN products p ON p.id = o.product_id
            WHERE 1=1
        """
        params: List[Any] = []
        if period:
            sql += " AND o.order_date >= %s AND o.order_date < %s"
            params += list(_dtr(period))
        if platform:
            sql += " AND o.platform = %s"
            params.append(platform)
        if status:
            sql += " AND o.status = %s"
            params.append(status)
        sql += f" GROUP BY {spec['group']} ORDER BY order_count DESC LIMIT %s"
        params.append(clamp_limit(breakdown_limit, 200))
        rows_dim = get_mysql().query(sql, params)
        for r in rows_dim:
            r["gmv"] = round(float(r.get("gmv") or 0), 2)
            # 统一切片列名：summary["by_platform"] / ["by_status"] 在「未请求该维度」
            # 时由上面的固定查询产出（列名 cnt），「请求了该维度」时由这里产出
            # （列名 order_count），且本循环会把前者覆盖掉 —— 同一个 key 两种形状，
            # 下游按 cnt 取值就会静默拿到 None（实测让结论渲染出「抖店 None 单」）。
            # 这里补上别名，保证无论命中哪条分支，切片形状一致。
            if r.get("cnt") is None:
                r["cnt"] = r.get("order_count")
        extra_breakdowns[f"by_{dim}"] = rows_dim
        if dim == "product":
            # 兼容直接读 summary['by_product'] 的调用方
            by_product = rows_dim

    summary: Dict[str, Any] = {
        "label": _notice(period),
        "order_count": int(agg.get("order_count") or 0),
        "gmv": round(float(agg.get("gmv") or 0), 2),
        "avg_order_amount": round(float(agg.get("avg_amount") or 0), 2),
        "buyer_count": int(agg.get("buyer_count") or 0),
        # 状态与平台分布是「几乎总要用」的切片，默认带上，避免多打一次工具调用
        "by_status": by_status,
        "by_platform": by_platform,
        "by_product": by_product,
    }
    for key, value in extra_breakdowns.items():
        summary[key] = value
    return {
        "rows": rows,
        "row_count": len(rows),
        "limit": limit,
        "summary": summary,
    }


def return_rate_by_product(
    start_date: Any = None,
    end_date: Any = None,
    platform: str | None = None,
    order: str = "desc",
    limit: int = 10,
    min_orders: int = 1,
    category: str | None = None,
) -> Dict[str, Any]:
    """退货率 TopN —— 支撑架构文档 §7 的核心示例问句。

    退货率 = 该商品的「退货类售后单数」/ 该商品的「订单数」。
    用 COUNT(DISTINCT a.id) 防止一对多 JOIN 放大订单数。
    """
    period = resolve_period(start_date, end_date)
    direction = "ASC" if str(order).lower() in ("asc", "最低", "升序") else "DESC"
    limit = clamp_limit(limit, 10)
    sql = """
        SELECT p.id AS product_id, p.name AS product_name, p.category,
               o.platform AS platform,
               COUNT(DISTINCT o.id) AS order_count,
               COUNT(DISTINCT a.id) AS return_count,
               COALESCE(SUM(a.refund_amount), 0) AS refund_amount,
               ROUND(COUNT(DISTINCT a.id) * 100.0 / NULLIF(COUNT(DISTINCT o.id), 0), 2) AS return_rate
        FROM orders o
        JOIN products p ON p.id = o.product_id
        LEFT JOIN after_sales a ON a.order_id = o.id AND a.type = %s
        WHERE 1=1
    """
    params: List[Any] = ["退货"]
    sql += " AND o.order_date >= %s AND o.order_date < %s" if period else ""
    if period:
        params += list(_dtr(period))
    if platform:
        sql += " AND o.platform = %s"
        params.append(platform)
    if category:
        sql += " AND p.category = %s"
        params.append(category)
    sql += " GROUP BY p.id, p.name, p.category, o.platform HAVING order_count >= %s"
    params.append(max(1, int(min_orders)))
    sql += f" ORDER BY return_rate {direction}, order_count DESC LIMIT %s"
    params.append(limit)
    rows = get_mysql().query(sql, params)
    total_orders = sum(int(r.get("order_count") or 0) for r in rows)
    total_returns = sum(int(r.get("return_count") or 0) for r in rows)
    return {
        "rows": rows,
        "row_count": len(rows),
        "summary": {
            "label": _notice(period),
            "platform": platform or "全平台",
            "metric": "退货率",
            "order": "降序" if direction == "DESC" else "升序",
            "product_count": len(rows),
            "order_count": total_orders,
            "return_count": total_returns,
            "overall_return_rate": round(total_returns * 100.0 / total_orders, 2) if total_orders else 0.0,
        },
    }


def sales_stats(
    start_date: Any = None,
    end_date: Any = None,
    platform: str | None = None,
    group_by: str = "day",
    limit: int = 200,
) -> Dict[str, Any]:
    """销量/销售额趋势统计。group_by ∈ {day, week, month, category, platform, region}。"""
    period = resolve_period(start_date, end_date)
    group_by = (group_by or "day").lower()
    if group_by in ("day", "日", "天", "每日"):
        expr, alias = "DATE(o.order_date)", "day"
    elif group_by in ("week", "周", "每周"):
        expr, alias = "DATE_FORMAT(o.order_date, '%%x-W%%v')", "week"
    elif group_by in ("month", "月", "每月"):
        expr, alias = "DATE_FORMAT(o.order_date, '%%Y-%%m')", "month"
    elif group_by in ("category", "类目", "品类"):
        expr, alias = "p.category", "category"
    elif group_by in ("platform", "平台"):
        expr, alias = "o.platform", "platform"
    elif group_by in ("region", "地区", "区域"):
        expr, alias = "o.region", "region"
    else:
        expr, alias = "DATE(o.order_date)", "day"

    sql = f"""
        SELECT {expr} AS {alias},
               COUNT(*) AS order_count,
               COALESCE(SUM(o.amount), 0) AS gmv,
               COALESCE(SUM(oi.quantity), 0) AS quantity,
               COUNT(DISTINCT o.user_id) AS buyer_count
        FROM orders o
        LEFT JOIN products p ON p.id = o.product_id
        LEFT JOIN order_items oi ON oi.order_id = o.id
        WHERE 1=1
    """
    params: List[Any] = []
    sql += " AND o.order_date >= %s AND o.order_date < %s" if period else ""
    if period:
        params += list(_dtr(period))
    if platform:
        sql += " AND o.platform = %s"
        params.append(platform)
    sql += f" GROUP BY {expr} ORDER BY {alias} ASC LIMIT %s"
    params.append(clamp_limit(limit))
    rows = get_mysql().query(sql, params)
    for r in rows:
        r["gmv"] = round(float(r.get("gmv") or 0), 2)
        r["avg_order_amount"] = round(float(r["gmv"]) / int(r["order_count"]), 2) if r.get("order_count") else 0.0
    return {
        "rows": rows,
        "row_count": len(rows),
        "summary": {
            "label": _notice(period),
            "group_by": alias,
            "platform": platform or "全平台",
            "total_gmv": round(sum(float(r.get("gmv") or 0) for r in rows), 2),
            "total_orders": sum(int(r.get("order_count") or 0) for r in rows),
        },
    }


def top_products(
    start_date: Any = None,
    end_date: Any = None,
    platform: str | None = None,
    metric: str = "gmv",
    limit: int = 10,
) -> Dict[str, Any]:
    """畅销榜：metric ∈ {gmv, quantity, order_count}。"""
    period = resolve_period(start_date, end_date)
    metric_map = {
        "gmv": ("COALESCE(SUM(o.amount), 0)", "gmv"),
        "amount": ("COALESCE(SUM(o.amount), 0)", "gmv"),
        "销售额": ("COALESCE(SUM(o.amount), 0)", "gmv"),
        "quantity": ("COALESCE(SUM(oi.quantity), 0)", "quantity"),
        "销量": ("COALESCE(SUM(oi.quantity), 0)", "quantity"),
        "order_count": ("COUNT(DISTINCT o.id)", "order_count"),
        "订单量": ("COUNT(DISTINCT o.id)", "order_count"),
    }
    expr, alias = metric_map.get(str(metric).lower(), metric_map["gmv"])
    sql = f"""
        SELECT p.id AS product_id, p.name AS product_name, p.category,
               {expr} AS {alias},
               COUNT(DISTINCT o.id) AS order_count,
               COALESCE(SUM(oi.quantity), 0) AS quantity
        FROM orders o
        JOIN products p ON p.id = o.product_id
        LEFT JOIN order_items oi ON oi.order_id = o.id
        WHERE 1=1
    """
    params: List[Any] = []
    sql += " AND o.order_date >= %s AND o.order_date < %s" if period else ""
    if period:
        params += list(_dtr(period))
    if platform:
        sql += " AND o.platform = %s"
        params.append(platform)
    sql += f" GROUP BY p.id, p.name, p.category ORDER BY {alias} DESC LIMIT %s"
    params.append(clamp_limit(limit, 10))
    rows = get_mysql().query(sql, params)
    return {
        "rows": rows,
        "row_count": len(rows),
        "summary": {"label": _notice(period), "metric": alias, "platform": platform or "全平台"},
    }


# =============================================================== 商品
def query_products(
    keyword: str | None = None,
    category: str | None = None,
    platform: str | None = None,
    product_id: int | None = None,
    limit: int = 50,
) -> Dict[str, Any]:
    sql = """
        SELECT p.id, p.name, p.category, p.price, p.platform, p.created_at,
               COALESCE(SUM(CASE WHEN o.status <> '已取消' THEN 1 ELSE 0 END), 0) AS order_count
        FROM products p
        LEFT JOIN orders o ON o.product_id = p.id
        WHERE 1=1
    """
    params: List[Any] = []
    if product_id:
        sql += " AND p.id = %s"
        params.append(product_id)
    if keyword:
        sql += " AND (p.name LIKE %s OR p.category LIKE %s)"
        params += [f"%{keyword}%"] * 2
    if category:
        sql += " AND p.category = %s"
        params.append(category)
    if platform:
        sql += " AND p.platform = %s"
        params.append(platform)
    sql += " GROUP BY p.id, p.name, p.category, p.price, p.platform, p.created_at ORDER BY order_count DESC LIMIT %s"
    params.append(clamp_limit(limit, 50))
    rows = get_mysql().query(sql, params)
    categories = get_mysql().query(
        "SELECT category, COUNT(*) AS cnt, COALESCE(AVG(price),0) AS avg_price "
        "FROM products GROUP BY category ORDER BY cnt DESC"
    )
    for r in categories:
        r["avg_price"] = round(float(r.get("avg_price") or 0), 2)
    return {
        "rows": rows,
        "row_count": len(rows),
        "summary": {
            "keyword": keyword or "",
            "category": category or "全部类目",
            "platform": platform or "全平台",
            "product_count": len(rows),
            "category_distribution": categories,
        },
    }


# =============================================================== 售后
def query_after_sales(
    order_id: int | None = None,
    order_no: str | None = None,
    status: str | None = None,
    type_: str | None = None,
    start_date: Any = None,
    end_date: Any = None,
    platform: str | None = None,
    limit: int = DEFAULT_LIMIT,
    breakdowns: Any = None,
    breakdown_limit: int = 200,
) -> Dict[str, Any]:
    period = resolve_period(start_date, end_date)
    sql = """
        SELECT a.id, a.order_id, o.order_no, o.platform, p.name AS product_name,
               a.type, a.status, a.refund_amount, a.created_at
        FROM after_sales a
        LEFT JOIN orders o ON o.id = a.order_id
        LEFT JOIN products p ON p.id = o.product_id
        WHERE 1=1
    """
    params: List[Any] = []
    if order_id:
        sql += " AND a.order_id = %s"
        params.append(order_id)
    if order_no:
        sql += " AND o.order_no = %s"
        params.append(order_no)
    if status:
        sql += " AND a.status = %s"
        params.append(status)
    if type_:
        sql += " AND a.type = %s"
        params.append(type_)
    if platform:
        sql += " AND o.platform = %s"
        params.append(platform)
    sql += " AND a.created_at >= %s AND a.created_at < %s" if period else ""
    if period:
        params += list(_dtr(period))
    limit = clamp_limit(limit)
    rows = get_mysql().query(sql + " ORDER BY a.created_at DESC, a.id DESC LIMIT %s", params + [limit])

    agg_sql = """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(a.refund_amount), 0) AS refund_total,
               COALESCE(SUM(CASE WHEN a.type = '退货' THEN 1 ELSE 0 END), 0) AS return_count,
               COALESCE(SUM(CASE WHEN a.type = '换货' THEN 1 ELSE 0 END), 0) AS exchange_count,
               COALESCE(SUM(CASE WHEN a.type = '仅退款' THEN 1 ELSE 0 END), 0) AS refund_only_count
        FROM after_sales a
        LEFT JOIN orders o ON o.id = a.order_id
        LEFT JOIN products p ON p.id = o.product_id
        WHERE 1=1
    """
    agg_params: List[Any] = []
    agg_sql += " AND a.created_at >= %s AND a.created_at < %s" if period else ""
    if period:
        agg_params += list(_dtr(period))
    if platform:
        agg_sql += " AND o.platform = %s"
        agg_params.append(platform)
    agg = get_mysql().query_one(agg_sql, agg_params) or {}
    by_status = get_mysql().query(
        """
        SELECT a.status, COUNT(*) AS cnt, COALESCE(SUM(a.refund_amount),0) AS refund_amount
        FROM after_sales a LEFT JOIN orders o ON o.id = a.order_id LEFT JOIN products p ON p.id = o.product_id
        WHERE 1=1
        """
        + (" AND a.created_at >= %s AND a.created_at < %s" if period else "")
        + (" AND o.platform = %s" if platform else "")
        + " GROUP BY a.status ORDER BY cnt DESC",
        (list(_dtr(period)) if period else []) + ([platform] if platform else []),
    )
    order_count = int(
        get_mysql().scalar(
            "SELECT COUNT(*) FROM orders o WHERE 1=1"
            + (" AND o.order_date >= %s AND o.order_date < %s" if period else "")
            + (" AND o.platform = %s" if platform else ""),
            (list(_dtr(period)) if period else []) + ([platform] if platform else []),
            default=0,
        )
        or 0
    )
    total = int(agg.get("total") or 0)
    by_product: List[Dict[str, Any]] = []
    extra_breakdowns: Dict[str, List[Dict[str, Any]]] = {}
    for dim in _normalize_dims(breakdowns, AFTER_SALE_BREAKDOWN_KEYS):
        spec = _AFTER_SALE_BREAKDOWNS[dim]
        # 约束在「退货」口径，这样与 query_orders 的 order_count 相除即为退货率
        sql = f"""
            SELECT {spec['select']},
                   COUNT(*) AS return_count,
                   COALESCE(SUM(a.refund_amount), 0) AS refund_amount
            FROM after_sales a
            JOIN orders o ON o.id = a.order_id
            LEFT JOIN products p ON p.id = o.product_id
            WHERE a.type = '退货'
        """
        params: List[Any] = []
        if period:
            sql += " AND a.created_at >= %s AND a.created_at < %s"
            params += list(_dtr(period))
        if platform:
            sql += " AND o.platform = %s"
            params.append(platform)
        sql += f" GROUP BY {spec['group']} ORDER BY return_count DESC LIMIT %s"
        params.append(clamp_limit(breakdown_limit, 200))
        rows_dim = get_mysql().query(sql, params)
        for r in rows_dim:
            r["refund_amount"] = round(float(r.get("refund_amount") or 0), 2)
        extra_breakdowns[f"by_{dim}"] = rows_dim
        if dim == "product":
            by_product = rows_dim

    summary: Dict[str, Any] = {
        "label": _notice(period),
        "platform": platform or "全平台",
        "after_sale_count": total,
        "order_count": order_count,
        "after_sale_rate": round(total * 100.0 / order_count, 2) if order_count else 0.0,
        "refund_total": round(float(agg.get("refund_total") or 0), 2),
        "return_count": int(agg.get("return_count") or 0),
        "exchange_count": int(agg.get("exchange_count") or 0),
        "refund_only_count": int(agg.get("refund_only_count") or 0),
        "by_status": by_status,
        "by_product": by_product,
    }
    for key, value in extra_breakdowns.items():
        summary[key] = value
    return {
        "rows": rows,
        "row_count": len(rows),
        "limit": limit,
        "summary": summary,
    }


# =============================================================== 库存
def query_inventory(
    sku: str | None = None,
    warehouse: str | None = None,
    product_id: int | None = None,
    low_stock_only: bool = False,
    limit: int = DEFAULT_LIMIT,
    turnover_days: int = 30,
) -> Dict[str, Any]:
    since = (dt.date.today() - dt.timedelta(days=turnover_days)).strftime("%Y-%m-%d 00:00:00")
    sql = """
        SELECT i.id, i.sku, i.product_id, p.name AS product_name, i.warehouse,
               i.quantity, i.safety_stock, i.updated_at,
               COALESCE(s.sold, 0) AS sold_recent
        FROM inventory i
        LEFT JOIN products p ON p.id = i.product_id
        LEFT JOIN (
            SELECT oi.product_id, SUM(oi.quantity) AS sold
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            WHERE o.order_date >= %s AND o.status <> '已退款'
            GROUP BY oi.product_id
        ) s ON s.product_id = i.product_id
        WHERE 1=1
    """
    params: List[Any] = [since]
    if sku:
        sql += " AND i.sku LIKE %s"
        params.append(f"%{sku}%")
    if warehouse:
        sql += " AND i.warehouse LIKE %s"
        params.append(f"%{warehouse}%")
    if product_id:
        sql += " AND i.product_id = %s"
        params.append(product_id)
    if low_stock_only:
        sql += " AND i.quantity <= i.safety_stock"
    sql += " ORDER BY (i.quantity - i.safety_stock) ASC LIMIT %s"
    params.append(clamp_limit(limit))
    rows = get_mysql().query(sql, params)
    for r in rows:
        r["low_stock"] = int(r.get("quantity") or 0) <= int(r.get("safety_stock") or 0)
        sold = float(r.pop("sold_recent", 0) or 0)
        daily = sold / turnover_days if turnover_days else 0.0
        r["turnover_days"] = round(float(r.get("quantity") or 0) / daily, 1) if daily > 0 else None
    total_qty = sum(int(r.get("quantity") or 0) for r in rows)
    low = [r for r in rows if r.get("low_stock")]
    return {
        "rows": rows,
        "row_count": len(rows),
        "summary": {
            "sku": sku or "",
            "warehouse": warehouse or "全部仓库",
            "sku_count": len(rows),
            "total_quantity": total_qty,
            "low_stock_count": len(low),
            "low_stock_skus": [r["sku"] for r in low[:20]],
        },
    }


# =============================================================== 广告
def query_ad_reports(
    platform: str | None = None,
    start_date: Any = None,
    end_date: Any = None,
    group_by: str = "platform",
) -> Dict[str, Any]:
    period = resolve_period(start_date, end_date)
    group_by = (group_by or "platform").lower()
    expr = "report_date" if group_by in ("date", "day", "日期") else "platform"
    sql = f"""
        SELECT {expr} AS dim,
               SUM(impressions) AS impressions,
               SUM(clicks) AS clicks,
               COALESCE(SUM(cost), 0) AS cost,
               COALESCE(SUM(revenue), 0) AS revenue
        FROM ad_reports WHERE 1=1
    """
    params: List[Any] = []
    if platform:
        sql += " AND platform = %s"
        params.append(platform)
    if period:
        sql += " AND report_date >= %s AND report_date < %s"
        params += list(_dtr(period))
    sql += f" GROUP BY {expr} ORDER BY {expr} ASC"
    rows = get_mysql().query(sql, params)
    for r in rows:
        impressions = int(r.get("impressions") or 0)
        clicks = int(r.get("clicks") or 0)
        cost = float(r.get("cost") or 0)
        revenue = float(r.get("revenue") or 0)
        r["impressions"] = impressions
        r["clicks"] = clicks
        r["cost"] = round(cost, 2)
        r["revenue"] = round(revenue, 2)
        r["ctr"] = round(clicks * 100.0 / impressions, 2) if impressions else 0.0
        r["cpc"] = round(cost / clicks, 2) if clicks else 0.0
        r["roi"] = round(revenue / cost, 2) if cost else 0.0
        r["roas"] = r["roi"]
    total_cost = sum(float(r.get("cost") or 0) for r in rows)
    total_rev = sum(float(r.get("revenue") or 0) for r in rows)
    return {
        "rows": rows,
        "row_count": len(rows),
        "summary": {
            "label": _notice(period),
            "platform": platform or "全平台",
            "group_by": expr,
            "impressions": sum(int(r.get("impressions") or 0) for r in rows),
            "clicks": sum(int(r.get("clicks") or 0) for r in rows),
            "cost": round(total_cost, 2),
            "revenue": round(total_rev, 2),
            "roi": round(total_rev / total_cost, 2) if total_cost else 0.0,
        },
    }


# =============================================================== 知识库（降级路径）
_FULLTEXT_OK: Optional[bool] = None


def _fulltext_available() -> bool:
    global _FULLTEXT_OK
    if _FULLTEXT_OK is None:
        try:
            get_mysql().query(
                "SELECT id FROM knowledge_docs "
                "WHERE MATCH(title, content) AGAINST (%s IN NATURAL LANGUAGE MODE) LIMIT 1",
                ("测试",),
            )
            _FULLTEXT_OK = True
        except Exception:
            _FULLTEXT_OK = False
    return bool(_FULLTEXT_OK)


def search_knowledge_like(
    query: str,
    top_k: int = 5,
    collection: str | None = None,
) -> Dict[str, Any]:
    """§11 降级路径：Milvus 不可用时用 MySQL 全文 / LIKE 检索。

    优先 FULLTEXT（MySQL ngram，近似 BM25 相关性排序），
    不可用则退化到多关键词 LIKE 评分。
    """
    top_k = clamp_limit(top_k, 5)
    mode = "fulltext"
    if _fulltext_available():
        sql = """
            SELECT id, collection, title, content, source,
                   MATCH(title, content) AGAINST (%s IN NATURAL LANGUAGE MODE) AS score
            FROM knowledge_docs WHERE MATCH(title, content) AGAINST (%s IN NATURAL LANGUAGE MODE)
        """
        params: List[Any] = [query, query]
        if collection:
            sql += " AND collection = %s"
            params.append(collection)
        sql += " ORDER BY score DESC LIMIT %s"
        params.append(top_k)
        rows = get_mysql().query(sql, params)
        if not rows:
            mode = "like"
    else:
        rows, mode = [], "like"

    if mode == "like":
        terms = [t for t in _split_terms(query) if t]
        if not terms:
            return {"rows": [], "row_count": 0, "mode": "like", "summary": {"query": query}}
        score_parts, where_parts = [], []
        params = []
        for term in terms[:8]:
            score_parts.append(
                "(CASE WHEN title LIKE %s THEN 3 ELSE 0 END + CASE WHEN content LIKE %s THEN 1 ELSE 0 END)"
            )
            params += [f"%{term}%", f"%{term}%"]
            where_parts.append("(title LIKE %s OR content LIKE %s)")
            params += [f"%{term}%", f"%{term}%"]
        sql = (
            f"SELECT id, collection, title, content, source, ({' + '.join(score_parts)}) AS score "
            f"FROM knowledge_docs WHERE ({' OR '.join(where_parts)})"
        )
        if collection:
            sql += " AND collection = %s"
            params.append(collection)
        sql += f" ORDER BY score DESC LIMIT %s"
        params.append(top_k)
        rows = get_mysql().query(sql, params)

    for r in rows:
        r["retrieval_mode"] = f"mysql-{mode}"
    return {
        "rows": rows,
        "row_count": len(rows),
        "mode": mode,
        "summary": {
            "query": query,
            "retrieval_mode": f"mysql-{mode}",
            "degraded": True,
            "note": "Milvus 不可用，已降级为 MySQL " + ("全文索引" if mode == "fulltext" else "LIKE"),
        },
    }


def _split_terms(text: str) -> List[str]:
    import re

    words = re.split(r"[\s,，。、;；:：/\\()（）\[\]【】?？!！\"'`]+", text or "")
    terms = [w for w in words if len(w) >= 2]
    if not terms and text:
        terms = [text[:12]]
    # 中文长词再按 2-gram 切一刀，提高 LIKE 召回
    extra: List[str] = []
    for w in terms:
        if len(w) > 4 and all("\u4e00" <= ch <= "\u9fff" for ch in w):
            extra += [w[i : i + 2] for i in range(0, len(w) - 1, 2)]
    return list(dict.fromkeys(terms + extra))


def list_knowledge_docs(collection: str | None = None, limit: int = 200) -> Dict[str, Any]:
    sql = "SELECT id, collection, title, content, source, keywords FROM knowledge_docs WHERE 1=1"
    params: List[Any] = []
    if collection:
        sql += " AND collection = %s"
        params.append(collection)
    sql += " ORDER BY collection, id LIMIT %s"
    params.append(clamp_limit(limit))
    rows = get_mysql().query(sql, params)
    return {"rows": rows, "row_count": len(rows)}


def upsert_knowledge_docs(docs: Sequence[Dict[str, Any]]) -> int:
    """把知识语料镜像进 MySQL，供 §11 的 LIKE / FULLTEXT 降级检索使用。

    返回值语义：**本次写入的文档条数**，而不是驱动返回的 affected rows。
    因为语句是 ``INSERT ... ON DUPLICATE KEY UPDATE``：当行已存在且字段值
    完全没变时 MySQL 会计 0，直接透出会让 CLI 显示 "写入 0 条"，看起来像失败。
    """
    if not docs:
        return 0
    payload = [
        (
            d["id"], d.get("collection", ""), d.get("title", ""), d.get("content", ""),
            d.get("source", ""), d.get("keywords", ""),
        )
        for d in docs
    ]
    get_mysql().execute_many(
        """
        INSERT INTO knowledge_docs (id, collection, title, content, source, keywords)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          collection = VALUES(collection), title = VALUES(title), content = VALUES(content),
          source = VALUES(source), keywords = VALUES(keywords)
        """,
        payload,
    )
    return len(payload)


# =============================================================== 会话（写）
def ensure_session(session_id: str, user_id: str = "anonymous", role: str = "customer") -> None:
    get_mysql().execute(
        "INSERT INTO chat_sessions (id, user_id, role) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE user_id = VALUES(user_id), role = VALUES(role)",
        (session_id, user_id, role),
    )


def append_message(session_id: str, role: str, content: str) -> int:
    return get_mysql().insert(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, %s, %s)",
        (session_id, role, content),
    )


def list_messages(session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    rows = get_mysql().query(
        "SELECT id, session_id, role, content, created_at FROM chat_messages "
        "WHERE session_id = %s ORDER BY id DESC LIMIT %s",
        (session_id, clamp_limit(limit, 20)),
    )
    return list(reversed(rows))


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    return get_mysql().query_one(
        "SELECT id, user_id, role, created_at FROM chat_sessions WHERE id = %s",
        (session_id,),
    )


# =============================================================== 审计（写）
def insert_audit(
    tool_name: str,
    params: Any,
    result: Any,
    elapsed_ms: int,
    status: str,
    request_id: str | None = None,
) -> int:
    def dump(v: Any) -> str:
        try:
            text = json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(v)
        return text[:60000]

    return get_mysql().insert(
        "INSERT INTO mcp_audit (tool_name, params, result, elapsed_ms, status, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (tool_name, dump(params), dump(result), int(elapsed_ms), status, request_id),
    )


def list_audit(limit: int = 50, tool_name: str | None = None) -> List[Dict[str, Any]]:
    sql = "SELECT id, tool_name, params, result, elapsed_ms, status, request_id, created_at FROM mcp_audit WHERE 1=1"
    params: List[Any] = []
    if tool_name:
        sql += " AND tool_name = %s"
        params.append(tool_name)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(clamp_limit(limit, 50))
    return get_mysql().query(sql, params)


# =============================================================== A2A 任务（写）
def save_agent_task(
    task_id: str,
    agent_name: str,
    status: str,
    payload_in: Any,
    payload_out: Any = None,
    elapsed_ms: int = 0,
    request_id: str | None = None,
) -> None:
    def dump(v: Any) -> str:
        try:
            return json.dumps(v, ensure_ascii=False, default=str)[:60000]
        except (TypeError, ValueError):
            return str(v)

    get_mysql().execute(
        """
        INSERT INTO agent_tasks (id, agent_name, status, input, output, elapsed_ms, request_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE status = VALUES(status), output = VALUES(output),
          elapsed_ms = VALUES(elapsed_ms)
        """,
        (task_id, agent_name, status, dump(payload_in), dump(payload_out), int(elapsed_ms), request_id),
    )


def get_agent_task(task_id: str) -> Optional[Dict[str, Any]]:
    return get_mysql().query_one(
        "SELECT id, agent_name, status, input, output, elapsed_ms, request_id, created_at "
        "FROM agent_tasks WHERE id = %s",
        (task_id,),
    )


def list_agent_tasks(limit: int = 50, agent_name: str | None = None) -> List[Dict[str, Any]]:
    sql = "SELECT id, agent_name, status, elapsed_ms, request_id, created_at FROM agent_tasks WHERE 1=1"
    params: List[Any] = []
    if agent_name:
        sql += " AND agent_name = %s"
        params.append(agent_name)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(clamp_limit(limit, 50))
    return get_mysql().query(sql, params)


# =============================================================== 工单（写）
def create_ticket(
    ticket_id: str,
    summary: str,
    priority: str = "P2",
    contact: str | None = None,
    session_id: str | None = None,
) -> Dict[str, Any]:
    get_mysql().execute(
        "INSERT INTO tickets (id, summary, priority, contact, session_id) VALUES (%s, %s, %s, %s, %s)",
        (ticket_id, summary[:255], priority, contact, session_id),
    )
    return {
        "id": ticket_id,
        "summary": summary,
        "priority": priority,
        "contact": contact,
        "status": "open",
        "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def list_tickets(limit: int = 20, status: str | None = None) -> List[Dict[str, Any]]:
    sql = "SELECT id, summary, priority, contact, status, created_at FROM tickets WHERE 1=1"
    params: List[Any] = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(clamp_limit(limit, 20))
    return get_mysql().query(sql, params)


# =============================================================== 元数据
def distinct_values(column: str, table: str = "orders", limit: int = 50) -> List[str]:
    allowed = {
        ("platform", "orders"), ("status", "orders"), ("region", "orders"),
        ("category", "products"), ("platform", "products"), ("name", "products"),
        ("warehouse", "inventory"), ("type", "after_sales"), ("status", "after_sales"),
        ("platform", "ad_reports"),
    }
    if (column, table) not in allowed:
        raise ValueError(f"不允许的列/表组合：{table}.{column}")
    rows = get_mysql().query(
        f"SELECT DISTINCT {column} AS v FROM {table} WHERE {column} IS NOT NULL "
        f"ORDER BY {column} LIMIT %s",
        (limit,),
    )
    return [str(r["v"]) for r in rows if r.get("v") not in (None, "")]


def table_stats() -> Dict[str, Any]:
    """给 /health 用的数据概览。"""
    tables = (
        "products", "orders", "order_items", "after_sales", "inventory",
        "ad_reports", "chat_sessions", "chat_messages", "mcp_audit",
        "agent_tasks", "knowledge_docs", "tickets",
    )
    out: Dict[str, Any] = {}
    for t in tables:
        try:
            out[t] = int(get_mysql().scalar(f"SELECT COUNT(*) FROM {t}", default=0) or 0)
        except MySQLUnavailable:
            return {"available": False, "error": "MySQL 不可用", "counts": out}
        except Exception:
            out[t] = None
    return {"available": True, "counts": out}


__all__ = [
    "AFTER_SALE_TYPES",
    "ORDER_STATUSES",
    "append_message",
    "clamp_limit",
    "create_ticket",
    "distinct_values",
    "ensure_session",
    "get_agent_task",
    "get_session",
    "insert_audit",
    "list_agent_tasks",
    "list_audit",
    "list_knowledge_docs",
    "list_messages",
    "list_tickets",
    "query_ad_reports",
    "query_after_sales",
    "query_inventory",
    "query_orders",
    "query_products",
    "resolve_period",
    "return_rate_by_product",
    "sales_stats",
    "save_agent_task",
    "search_knowledge_like",
    "table_stats",
    "top_products",
    "upsert_knowledge_docs",
]
