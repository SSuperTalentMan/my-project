"""MCP stdio Server（架构文档 §3.4「支持 stdio 与 SSE」）。

把 ``registry`` 里已注册的同一批工具以**标准 MCP 协议**经 stdin/stdout
暴露，供 Claude Desktop / Cursor / 自研宿主以 stdio 方式接入。
工具定义与协议构造在 :mod:`commercepivot.mcp_server.protocol_server`，
本模块只负责「stdio 这个传输方式」以及它的启动细节。

启动方式::

    python -m commercepivot.mcp_server.stdio_server

宿主配置（示例）::

    {"mcpServers": {"commercepivot": {
        "command": "D:\\\\CommercePivot\\\\.venv\\\\Scripts\\\\python.exe",
        "args": ["-m", "commercepivot.mcp_server.stdio_server"]}}}

**关键约束**：stdio 传输下 ``stdout`` 是 JSON-RPC 专用通道，
所以本进程的日志必须全部改道到 ``stderr``（见 :func:`main` 与
:func:`commercepivot.core.logging._resolve_stream` 的说明）。
"""

from __future__ import annotations

from commercepivot.mcp_server.protocol_server import build_server

__all__ = ["build_server", "main"]


def main() -> None:  # pragma: no cover - 由宿主进程拉起
    import sys

    from commercepivot.core.logging import setup_logging

    # 必须切到 stderr，否则宿主读到非 JSON 内容会直接握手失败（实测启动即
    # 输出 1032 字节脏数据）。时机上有个坑：``python -m`` 会先导入父包
    # ``commercepivot.mcp_server``，其 __init__ 又导入 registry，后者在模块级
    # 就调了 get_logger() —— 也就是说日志在进到这里之前已被锁到 stdout。
    # 因此切流靠的是 setup_logging 支持「重新配置」，而不是「只认第一次」。
    setup_logging(stream=sys.stderr)
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
