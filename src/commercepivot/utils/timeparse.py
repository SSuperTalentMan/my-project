"""中文时间表达解析（槽位抽取的核心组件之一）。

两个消费方：
1. ``orchestrator/slots.py`` —— 从用户问句里抽「时间」槽位；
2. ``mcp_server/tools/*``   —— MCP 工具同时接受 ISO 日期与自然语言周期，
   这样外部 Agent 直接调工具时也能写 ``"上个月"``。

统一输出 ``Period``（左闭右闭的自然日区间），再经 ``to_datetime_range``
转成 SQL 用的左闭右开区间，避免漏掉当天 23:59 的数据。
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass
from typing import Optional, Tuple

_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?"),
    re.compile(r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"),
)
_MONTH_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-/年](?P<m>\d{1,2})月?$"),
)
_RECENT_DAYS = re.compile(r"(?:最近|近|过去)\s*(?P<n>\d{1,3})\s*(?:天|日)")
_QUARTER = re.compile(r"第?([一二三四1-4])季度")


@dataclass(slots=True)
class Period:
    start: dt.date
    end: dt.date  # 含当天
    label: str = ""
    raw: str = ""

    def as_dict(self) -> dict:
        return {
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "label": self.label or f"{self.start.isoformat()}~{self.end.isoformat()}",
        }


def _today(today: dt.date | None = None) -> dt.date:
    return today or dt.date.today()


def _month_bounds(year: int, month: int) -> Tuple[dt.date, dt.date]:
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last)


def _add_months(d: dt.date, delta: int) -> dt.date:
    total = (d.year * 12 + d.month - 1) + delta
    year, month = divmod(total, 12)
    month += 1
    return dt.date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def parse_period(text: str | None, today: dt.date | None = None) -> Optional[Period]:
    """解析自然语言时间表达式；无法识别返回 None。"""
    if not text:
        return None
    raw = text.strip()
    s = raw.replace(" ", "")
    now = _today(today)

    # ---------- 显式日期区间： 2026-08-01到2026-08-31 / 2026年8月1日-8月31日
    m = re.search(
        r"(?P<a>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{8})"
        r"\s*(?:到|至|~|-|—|—)\s*"
        r"(?P<b>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{8})",
        s,
    )
    if m:
        a, b = _parse_single_date(m.group("a")), _parse_single_date(m.group("b"))
        if a and b:
            if a > b:
                a, b = b, a
            return Period(a, b, f"{a.isoformat()}~{b.isoformat()}", raw)

    # ---------- 单日
    single = _parse_single_date(s)
    if single:
        return Period(single, single, single.isoformat(), raw)

    # ---------- 显式年月（整月）
    m = re.search(r"(?P<y>\d{4})[-/年](?P<m>\d{1,2})月?(?![-/\d])", s)
    if m:
        y, mo = int(m.group("y")), int(m.group("m"))
        if 1 <= mo <= 12:
            a, b = _month_bounds(y, mo)
            return Period(a, b, f"{y}年{mo}月", raw)

    # ---------- 相对天数
    m = _RECENT_DAYS.search(s)
    if m:
        n = max(1, int(m.group("n")))
        return Period(now - dt.timedelta(days=n - 1), now, f"近{n}天", raw)

    # ---------- 季度
    m = _QUARTER.search(s)
    if m:
        q = "一二三四".index(m.group(1)) + 1 if m.group(1) in "一二三四" else int(m.group(1))
        y = _extract_year(s) or now.year
        if "上" in s:
            y, q = (y - 1, 4) if q == 1 else (y, q - 1)
        start = dt.date(y, 3 * (q - 1) + 1, 1)
        end = dt.date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
        return Period(start, end, f"{y}Q{q}", raw)

    # ---------- 关键词
    if re.search(r"今天|今日|当天", s):
        return Period(now, now, "今天", raw)
    if re.search(r"昨天|昨日", s):
        d = now - dt.timedelta(days=1)
        return Period(d, d, "昨天", raw)
    if re.search(r"前天", s):
        d = now - dt.timedelta(days=2)
        return Period(d, d, "前天", raw)
    if re.search(r"本周|这周|这个星期|这一周", s):
        start = now - dt.timedelta(days=now.weekday())
        return Period(start, now, "本周", raw)
    if re.search(r"上周|上星期|上一个星期", s):
        end = now - dt.timedelta(days=now.weekday() + 1)
        start = end - dt.timedelta(days=6)
        return Period(start, end, "上周", raw)
    if re.search(r"本季度|这季度|当季", s):
        q = (now.month - 1) // 3 + 1
        start = dt.date(now.year, 3 * (q - 1) + 1, 1)
        return Period(start, now, "本季度", raw)
    if re.search(r"上季度|上一季度|上个季度", s):
        q = (now.month - 1) // 3 + 1
        y, q = (now.year - 1, 4) if q == 1 else (now.year, q - 1)
        a = dt.date(y, 3 * (q - 1) + 1, 1)
        b = dt.date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
        return Period(a, b, f"{y}Q{q}", raw)
    if re.search(r"今年|本年|年初至今|本年至今|今年以来", s):
        return Period(dt.date(now.year, 1, 1), now, f"{now.year}年至今", raw)
    if re.search(r"去年|上年|上一年", s):
        y = now.year - 1
        return Period(dt.date(y, 1, 1), dt.date(y, 12, 31), f"{y}年", raw)
    if re.search(r"本月|这个月|当月|本月份", s):
        a, b = _month_bounds(now.year, now.month)
        return Period(a, min(b, now) if "至" in s or "至今" in s else b, f"{now.year}年{now.month}月", raw)
    if re.search(r"上个月|上月|上个?月份|前一?个月|上月度", s):
        first = dt.date(now.year, now.month, 1)
        prev_last = first - dt.timedelta(days=1)
        a, b = _month_bounds(prev_last.year, prev_last.month)
        return Period(a, b, f"{prev_last.year}年{prev_last.month}月", raw)

    # ---------- 仅年份
    y = _extract_year(s)
    if y and re.search(r"年", s):
        return Period(dt.date(y, 1, 1), dt.date(y, 12, 31), f"{y}年", raw)

    return None


def _extract_year(text: str) -> Optional[int]:
    m = re.search(r"(\d{4})", text)
    if m:
        y = int(m.group(1))
        if 1990 <= y <= 2100:
            return y
    return None


def _parse_single_date(text: str) -> Optional[dt.date]:
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return dt.date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
            except ValueError:
                return None
    return None


def period_from_dates(
    start_date: str | dt.date | None,
    end_date: str | dt.date | None,
    today: dt.date | None = None,
) -> Optional[Period]:
    """把 ``start_date`` / ``end_date`` 参数（ISO 或自然语言）统一成 Period。

    只给一端时另一端补默认：只给 start → end=今天；只给 end → start=end。
    """
    start = _coerce(start_date, today)
    end = _coerce(end_date, today)
    if start is None and end is None:
        return None
    now = _today(today)
    if start is None:
        return Period(end, end, end.isoformat(), "")
    if end is None:
        return Period(start, max(start, now), f"{start.isoformat()}~", "")
    if start > end:
        start, end = end, start
    return Period(start, end, f"{start.isoformat()}~{end.isoformat()}", "")


def _coerce(value: str | dt.date | None, today: dt.date | None) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        return value
    p = parse_period(str(value), today)
    if p is None:
        return None
    # 只给了月份/年份这类"区间"表述时，取整段区间
    return p.start if p.start == p.end else p.start


def to_datetime_range(period: Period | None) -> Tuple[str | None, str | None]:
    """转成 SQL 左闭右开区间 ``[start 00:00:00, end+1d 00:00:00)``。"""
    if period is None:
        return None, None
    start = dt.datetime.combine(period.start, dt.time.min)
    end_exclusive = dt.datetime.combine(period.end + dt.timedelta(days=1), dt.time.min)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end_exclusive.strftime("%Y-%m-%d %H:%M:%S")


def month_label(period: Period | None) -> str:
    if period is None:
        return "全部时间"
    if period.label:
        return period.label
    return f"{period.start.isoformat()} 至 {period.end.isoformat()}"


__all__ = [
    "Period",
    "month_label",
    "parse_period",
    "period_from_dates",
    "to_datetime_range",
]
