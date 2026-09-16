"""MCP 工具集合（架构文档 §5 工具清单）。

导入本包即完成全部工具注册；``load_all()`` 幂等，可安全重复调用
（例如 CLI 子命令、stdio server 启动时各调一次）。
"""

from __future__ import annotations

from typing import List

from commercepivot.mcp_server.registry import get_registry

# 注意导入顺序无关紧要，注册动作发生在模块导入时
from commercepivot.mcp_server.tools import (  # noqa: E402
    ad_tools,
    knowledge_tools,
    order_tools,
    product_tools,
    report_tools,
)

TOOL_MODULES = (
    ad_tools,
    knowledge_tools,
    order_tools,
    product_tools,
    report_tools,
)


def load_all() -> List[str]:
    """确保全部工具已注册，返回工具名列表。"""
    return get_registry().names()


__all__ = ["TOOL_MODULES", "load_all"]
