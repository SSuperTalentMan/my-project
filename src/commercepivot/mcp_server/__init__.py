"""MCP 工具层：注册表、审计、HTTP/stdio 双协议入口、进程内客户端。

架构文档 §3.4 要求 MCP Server 用 FastAPI 实现并支持 stdio 与 SSE，
本包对应关系：
- ``registry.py``        工具注册表（参数 schema / 超时 / 只读标记 / 校验 / 重试）
- ``audit.py``           审计与脱敏（JSONL + MySQL）
- ``protocol_server.py`` 标准 MCP 协议构造（FastMCP），stdio 与 Streamable HTTP 共用
- ``server.py``          FastAPI 路由：标准 ``POST /mcp``、REST tools/list & tools/call、SSE
- ``stdio_server.py``    stdio 传输入口（供 Claude Desktop 等宿主接入）
- ``client.py``          Agent 侧调用入口（进程内直调或 HTTP）
"""

from commercepivot.mcp_server.registry import REGISTRY, ToolRegistry, ToolSpec, get_registry
from commercepivot.mcp_server.tools import load_all

__all__ = [
    "REGISTRY",
    "ToolRegistry",
    "ToolSpec",
    "get_registry",
    "load_all",
]
