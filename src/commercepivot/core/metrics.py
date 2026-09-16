"""轻量 Prometheus 指标（架构文档 §12）。

为什么不直接用 prometheus_client？本项目要求「零新增依赖也能跑」，
因此这里用标准库实现了 Counter / Gauge / Histogram 三个原语，
并暴露 Prometheus 标准文本格式（text/plain; version=0.0.4），
可直接被 Prometheus / VictoriaMetrics 抓取。
"""

from __future__ import annotations

import math
import threading
from typing import Dict, Iterable, List, Sequence, Tuple

DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

_LOCK = threading.Lock()
_COUNTERS: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
_GAUGES: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
_HISTOGRAMS: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], List[float]] = {}
_META: Dict[str, Tuple[str, str]] = {}  # name -> (type, help)


def _label_key(labels: Dict[str, str] | None) -> Tuple[Tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _fmt_labels(key: Tuple[Tuple[str, str], ...]) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _declare(name: str, type_: str, help_: str) -> None:
    _META.setdefault(name, (type_, help_))


# --------------------------------------------------------------- 原语
class Counter:
    def __init__(self, name: str, help_: str) -> None:
        self.name = name
        _declare(name, "counter", help_)

    def inc(self, labels: Dict[str, str] | None = None, value: float = 1.0) -> None:
        key = (self.name, _label_key(labels))
        with _LOCK:
            _COUNTERS[key] = _COUNTERS.get(key, 0.0) + value

    def get(self, labels: Dict[str, str] | None = None) -> float:
        with _LOCK:
            return _COUNTERS.get((self.name, _label_key(labels)), 0.0)


class Gauge:
    def __init__(self, name: str, help_: str) -> None:
        self.name = name
        _declare(name, "gauge", help_)

    def set(self, value: float, labels: Dict[str, str] | None = None) -> None:
        with _LOCK:
            _GAUGES[(self.name, _label_key(labels))] = float(value)

    def inc(self, value: float = 1.0, labels: Dict[str, str] | None = None) -> None:
        with _LOCK:
            key = (self.name, _label_key(labels))
            _GAUGES[key] = _GAUGES.get(key, 0.0) + float(value)

    def dec(self, value: float = 1.0, labels: Dict[str, str] | None = None) -> None:
        self.inc(-value, labels)


class Histogram:
    def __init__(
        self, name: str, help_: str, buckets: Sequence[float] = DEFAULT_BUCKETS
    ) -> None:
        self.name = name
        self.buckets = tuple(sorted(buckets))
        _declare(name, "histogram", help_)

    def observe(self, value: float, labels: Dict[str, str] | None = None) -> None:
        key = (self.name, _label_key(labels))
        with _LOCK:
            slot = _HISTOGRAMS.get(key)
            if slot is None:
                slot = [0.0] * (len(self.buckets) + 1)  # 各桶 + (sum, count)
                slot[-1] = 0.0
                _HISTOGRAMS[key] = slot
            for i, upper in enumerate(self.buckets):
                if value <= upper:
                    slot[i] += 1
            slot[len(self.buckets)] += 1  # +Inf 桶计数
            slot.append(value)  # 累计 sum

    def stats(self, labels: Dict[str, str] | None = None) -> Tuple[List[int], float, int]:
        key = (self.name, _label_key(labels))
        with _LOCK:
            slot = _HISTOGRAMS.get(key)
            if slot is None:
                return ([0] * len(self.buckets), 0.0, 0)
            counts = [int(x) for x in slot[: len(self.buckets) + 1]]
            total = float(slot[-1]) if len(slot) > len(self.buckets) + 1 else 0.0
            return (counts, total, counts[-1])


# --------------------------------------------------------------- 业务指标
HTTP_REQUESTS = Counter(
    "commercepivot_http_requests_total", "HTTP 请求数（按方法/路径/状态码）"
)
HTTP_LATENCY = Histogram(
    "commercepivot_http_request_duration_seconds", "HTTP 请求耗时（秒）"
)
MCP_CALLS = Counter(
    "commercepivot_mcp_tool_calls_total", "MCP 工具调用次数（按工具/状态）"
)
MCP_LATENCY = Histogram(
    "commercepivot_mcp_tool_duration_seconds", "MCP 工具调用耗时（秒）"
)
A2A_TASKS = Counter(
    "commercepivot_a2a_tasks_total", "A2A 任务数（按 Agent/状态）"
)
A2A_LATENCY = Histogram(
    "commercepivot_a2a_task_duration_seconds", "A2A 任务耗时（秒）"
)
LLM_CALLS = Counter(
    "commercepivot_llm_requests_total", "LLM 调用次数（按状态）"
)
LLM_LATENCY = Histogram(
    "commercepivot_llm_duration_seconds", "LLM 调用耗时（秒）"
)
RETRIEVAL_CALLS = Counter(
    "commercepivot_retrieval_total", "检索调用次数（按模式：hybrid/dense/like）"
)
GRAPH_NODES = Counter(
    "commercepivot_orchestrator_node_total", "编排节点执行次数（按节点/状态）"
)
CACHE_CALLS = Counter(
    "commercepivot_cache_total", "问答缓存命中情况（hit/miss/bypass）"
)
DEGRADED = Counter(
    "commercepivot_degraded_total", "降级事件次数（按组件）"
)


def render_prometheus() -> str:
    """渲染为 Prometheus 文本格式。"""
    lines: List[str] = []
    with _LOCK:
        counters = dict(_COUNTERS)
        gauges = dict(_GAUGES)
        histograms = dict(_HISTOGRAMS)
        meta = dict(_META)

    emitted: set[str] = set()
    for (name, lkey), value in sorted(counters.items()):
        if name not in emitted:
            type_, help_ = meta.get(name, ("counter", ""))
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {type_}")
            emitted.add(name)
        lines.append(f"{name}{_fmt_labels(lkey)} {_num(value)}")

    for (name, lkey), value in sorted(gauges.items()):
        if name not in emitted:
            type_, help_ = meta.get(name, ("gauge", ""))
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {type_}")
            emitted.add(name)
        lines.append(f"{name}{_fmt_labels(lkey)} {_num(value)}")

    for (name, lkey), slot in sorted(histograms.items()):
        if name not in _META or name not in emitted:
            type_, help_ = meta.get(name, ("histogram", ""))
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {type_}")
            emitted.add(name)
        buckets = _hist_buckets(name)
        counts = [int(x) for x in slot[: len(buckets) + 1]]
        total = float(slot[-1]) if len(slot) > len(buckets) + 1 else 0.0
        for upper, count in zip(buckets, counts):
            lines.append(
                f"{name}_bucket{_fmt_labels(lkey + (('le', _num(upper)),))} {count}"
            )
        lines.append(f"{name}_bucket{_fmt_labels(lkey + (('le', '+Inf'),))} {counts[-1]}")
        lines.append(f"{name}_sum{_fmt_labels(lkey)} {_num(total)}")
        lines.append(f"{name}_count{_fmt_labels(lkey)} {counts[-1]}")
    return "\n".join(lines) + "\n"


_HIST_BUCKETS: Dict[str, Tuple[float, ...]] = {
    HTTP_LATENCY.name: HTTP_LATENCY.buckets,
    MCP_LATENCY.name: MCP_LATENCY.buckets,
    A2A_LATENCY.name: A2A_LATENCY.buckets,
    LLM_LATENCY.name: LLM_LATENCY.buckets,
}


def _hist_buckets(name: str) -> Tuple[float, ...]:
    return _HIST_BUCKETS.get(name, DEFAULT_BUCKETS)


def _num(value: float) -> str:
    if isinstance(value, float):
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        if value.is_integer():
            return str(int(value))
        return repr(round(value, 6))
    return str(value)


def snapshot() -> Dict[str, float]:
    """给 /health 用的简要快照。"""
    with _LOCK:
        return {
            "http_requests": sum(_COUNTERS.get(k, 0.0) for k in _COUNTERS if k[0] == HTTP_REQUESTS.name),
            "mcp_calls": sum(_COUNTERS.get(k, 0.0) for k in _COUNTERS if k[0] == MCP_CALLS.name),
            "a2a_tasks": sum(_COUNTERS.get(k, 0.0) for k in _COUNTERS if k[0] == A2A_TASKS.name),
            "llm_calls": sum(_COUNTERS.get(k, 0.0) for k in _COUNTERS if k[0] == LLM_CALLS.name),
            "degraded": sum(_COUNTERS.get(k, 0.0) for k in _COUNTERS if k[0] == DEGRADED.name),
        }


def reset() -> None:
    """单测用。"""
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
        _HISTOGRAMS.clear()


__all__ = [
    "A2A_LATENCY",
    "A2A_TASKS",
    "CACHE_CALLS",
    "Counter",
    "DEGRADED",
    "Gauge",
    "GRAPH_NODES",
    "HTTP_LATENCY",
    "HTTP_REQUESTS",
    "Histogram",
    "LLM_CALLS",
    "LLM_LATENCY",
    "MCP_CALLS",
    "MCP_LATENCY",
    "RETRIEVAL_CALLS",
    "render_prometheus",
    "reset",
    "snapshot",
]
