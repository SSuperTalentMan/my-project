"""「商枢」CommercePivot —— 电商智能经营分析与客服助手平台。

分层架构（自下而上）：模型层 → 数据层 → MCP 工具层 → A2A Agent 层
→ 编排层（LangGraph 主控 Agent）→ 接入层（FastAPI）。

命令行入口见 :mod:`commercepivot.cli`。
"""

from __future__ import annotations

__version__ = "1.2.0"

__all__ = ["__version__", "main"]


def main() -> None:  # pragma: no cover - console_scripts 入口
    """``commercepivot`` 命令入口（等价于 ``python -m commercepivot``）。"""
    import sys

    from commercepivot.cli import main as _main

    sys.exit(_main())
