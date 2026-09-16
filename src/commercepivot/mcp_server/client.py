"""Agent 侧的 MCP 调用入口。

两种传输：
- ``MCP_TRANSPORT=local``（默认）：进程内直调注册表，零网络开销，
  审计、超时、重试、权限语义完全一致；
- ``MCP_TRANSPORT=http``：POST 到独立 MCP Server（:8001），
  用于验证「A2A Agent → MCP Server」真的走了一次 HTTP 协议链路。

无论哪种传输，返回体结构一致（``ToolResult``），Agent 无需关心差异。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import httpx

from commercepivot.core.config import get_settings
from commercepivot.core.errors import ToolExecutionError
from commercepivot.core.logging import get_logger
from commercepivot.mcp_server.registry import get_registry
from commercepivot.mcp_server.tools import load_all

log = get_logger("commercepivot.mcp.client")


class MCPClient:
    def __init__(self, transport: str | None = None, base_url: str | None = None) -> None:
        settings = get_settings()
        self.transport = (transport or settings.mcp_transport or "local").lower()
        self.base_url = (base_url or settings.mcp_call_url).rstrip("/")
        load_all()

    async def call(
        self,
        tool: str,
        params: Dict[str, Any] | None = None,
        *,
        role: str = "admin",
        session_id: str | None = None,
    ) -> Dict[str, Any]:
        if self.transport == "http":
            return await self._call_http(tool, params, role, session_id)
        return await get_registry().call(
            tool, params or {}, role=role, session_id=session_id
        )

    async def _call_http(
        self,
        tool: str,
        params: Optional[Dict[str, Any]],
        role: str,
        session_id: str | None,
    ) -> Dict[str, Any]:
        body = {"tool": tool, "params": params or {}, "session_id": session_id}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.base_url}/tools/call", json=body, headers={"X-Role": role}
                )
            if resp.status_code >= 400:
                raise ToolExecutionError(
                    f"MCP HTTP 调用失败 HTTP {resp.status_code}",
                    details={"body": resp.text[:300], "tool": tool},
                )
            return resp.json()
        except ToolExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP HTTP 传输失败，回退进程内调用", tool=tool, error=str(exc))
            return await get_registry().call(tool, params or {}, role=role, session_id=session_id)

    async def call_many(
        self,
        calls: Sequence[Dict[str, Any]],
        *,
        role: str = "admin",
        session_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """顺序调用多个工具（Agent 内做多步取数时用）。"""
        results: List[Dict[str, Any]] = []
        for item in calls:
            results.append(
                await self.call(
                    item.get("tool", ""), item.get("params") or {},
                    role=role, session_id=session_id,
                )
            )
        return results

    def list_tools(self) -> Dict[str, Any]:
        return get_registry().describe()

    def tool_specs(self) -> List[Dict[str, Any]]:
        return [spec.to_payload() for spec in get_registry().list_specs()]

    def describe_for_llm(self) -> str:
        """把工具清单压缩成给 LLM 看的紧凑文本（用于 Function Calling 提示）。"""
        lines = []
        for spec in get_registry().list_specs():
            props = spec.input_schema().get("properties", {})
            args = ", ".join(props.keys())
            lines.append(f"- {spec.name}({args}): {spec.description}")
        return "\n".join(lines)


_CLIENT: Optional[MCPClient] = None


def get_mcp_client() -> MCPClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = MCPClient()
    return _CLIENT


__all__ = ["MCPClient", "get_mcp_client"]
