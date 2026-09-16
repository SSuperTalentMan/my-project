"""请求上下文：X-Request-ID 与链路 trace（架构文档 §12）。

用 contextvars 实现，天然适配 asyncio：同一个请求的协程之间自动继承，
跨 await 不会串号。FastAPI 中间件在入口 set 一次，之后 MCP / A2A / LLM
各层只读即可，日志与审计里统一带上 request_id。

**trace 为什么用「可变列表 + 原地 append」，而不是「不可变元组 + set()」**：
编排层跑在 LangGraph 上，每个节点都在**独立的子上下文**里执行（子 Agent 调工具
还过一层 ``asyncio.to_thread``）。``ContextVar.set()`` 只改当前上下文里的绑定，
子上下文里换出去的新对象不会回传给父上下文 —— 实测用元组实现时，``/ask`` 的
``include_trace`` 最终只剩 ``answer_node`` 自己写的那两行，前面 5 个节点的
route/a2a/mcp 记录全丢。改成「入口创建一个列表、就地 append」后，父子上下文
持有的是**同一个列表引用**，任何深度的子上下文追加都能被最外层读到。
（``node_timings`` 之所以一直是完整的，正是因为它不走 contextvar、而是显式随
LangGraph state 传递；trace 现在用同样的"共享可见"语义，但无需改动每个节点函数。）
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_user: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar("user", default={})
# 默认 None 而非空列表：模块级可变默认值会被所有未显式开启 trace 的上下文共享，
# 造成跨请求串号。默认 None + 入口 ``begin_trace()`` 显式开新列表，才能保证隔离。
_trace: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "trace", default=None
)


def new_request_id() -> str:
    """生成 32 位十六进制请求 ID。"""
    return uuid.uuid4().hex


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(request_id: str) -> contextvars.Token:
    return _request_id.set(request_id)


def current_user() -> Dict[str, Any]:
    return _user.get()


def set_current_user(user: Dict[str, Any]) -> contextvars.Token:
    return _user.set(user)


def begin_trace() -> List[Dict[str, Any]]:
    """在当前上下文开启一份**新的** trace 列表，并返回它的引用。

    必须在「一次请求 / 一次调用」的入口调用（HTTP 中间件、``request_context``、
    编排器入口）。返回的是共享引用：之后所有子上下文（LangGraph 节点、
    ``asyncio.to_thread`` 里的工具调用）都会继承同一个列表。
    """
    items: List[Dict[str, Any]] = []
    _trace.set(items)
    return items


def ensure_trace() -> List[Dict[str, Any]]:
    """确保当前上下文已有一份 trace，返回它；没有则就地开一份。

    用于「入口可能已开、也可能没开」的兜底场景（例如中间件未覆盖、
    直接单测节点），保证 ``trace_add`` 永远有地方落，而不是抛异常。
    """
    items = _trace.get()
    if items is None:
        items = begin_trace()
    return items


def trace_add(stage: str, **detail: Any) -> None:
    """追加一段链路记录：route -> a2a -> mcp -> llm。

    注意：``stage`` 是第一个位置参数名，调用方**不要再传** ``stage=`` 关键字，
    否则会与位置参数冲突（Python 会直接抛 TypeError）。要追加自定义节点名时
    传 ``node=`` 之类的自有键即可。

    这里刻意用「取引用 → 原地 append」而非 ``set(get() + (item,))``：原地改的是
    共享列表对象本身，子上下文（LangGraph 节点、线程池）里的追加对最外层可见。
    """
    item: Dict[str, Any] = {"stage": stage}
    item.update({k: v for k, v in detail.items() if v is not None})
    ensure_trace().append(item)


def get_trace() -> List[Dict[str, Any]]:
    """返回当前上下文累计的链路记录（浅拷贝，避免调用方误改）。"""
    return [dict(x) for x in ensure_trace()]


@contextmanager
def request_context(
    request_id: str | None = None,
    user: Dict[str, Any] | None = None,
) -> Iterator[str]:
    """在非 HTTP 场景（脚本、MCP stdio、A2A 内部调用）手工开一个上下文。"""
    rid = request_id or new_request_id()
    trace_token = _trace.set([])
    id_token = _request_id.set(rid)
    user_token = _user.set(user) if user is not None else None
    try:
        yield rid
    finally:
        # LIFO 重置：后 set 的先 reset。
        for tok in (user_token, id_token, trace_token):
            if tok is None:
                continue
            try:
                tok.var.reset(tok)
            except ValueError:  # pragma: no cover - 跨上下文 reset
                pass
