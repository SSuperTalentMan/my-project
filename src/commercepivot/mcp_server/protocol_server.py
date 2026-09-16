"""标准 MCP 协议 Server 的构造 —— stdio 与 Streamable HTTP 两种传输共用。

官方 ``mcp`` SDK 的 FastMCP 把「工具定义」与「传输方式」解耦：同一个 server
实例既能 ``run(transport="stdio")`` 供 Claude Desktop / Cursor 等桌面宿主接入，
也能 ``streamable_http_app()`` 挂到 FastAPI 上供进程外 / 远程调用。本模块只负责
**把 registry 里已注册的同一批工具**翻译成 FastMCP 工具，传输方式交给调用方。

为什么用「动态生成函数源码」而不是 ``**kwargs`` 包装：
FastMCP 依据函数签名 + 类型注解生成 ``inputSchema``，只有显式参数
才能让宿主看到完整的参数名、类型与描述。因此这里按 Pydantic 模型的
字段定义拼出一段函数源码再 ``exec``，等价于手写每个工具的转发函数，
但不会随工具增删而失同步。

两种传输对应的接入方式::

    stdio（桌面宿主）:  {"command": "...python.exe",
                          "args": ["-m", "commercepivot.mcp_server.stdio_server"]}

    Streamable HTTP:   http://127.0.0.1:8001/mcp
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any, Dict, List, Optional, get_args, get_origin

from commercepivot.core.config import get_settings
from commercepivot.core.logging import get_logger
from commercepivot.mcp_server.registry import get_registry
from commercepivot.mcp_server.tools import load_all

log = get_logger("commercepivot.mcp.protocol")

_SIMPLE = {
    str: "str", int: "int", float: "float", bool: "bool",
    dict: "dict", list: "list", type(None): "None",
}


def _type_src(ann: Any) -> str:
    if ann in _SIMPLE:
        return _SIMPLE[ann]
    origin = get_origin(ann)
    args = get_args(ann)
    if origin in (typing.Union, types.UnionType):
        parts = [a for a in args if a is not type(None)]
        inner = " | ".join(_type_src(a) for a in parts) or "None"
        return f"{inner} | None" if len(parts) != len(args) else inner
    if origin in (dict, typing.Dict):
        return "dict"
    if origin in (list, typing.List, tuple, set):
        return "list"
    return "typing.Any"


def _default_src(field: Any) -> Optional[str]:
    from pydantic_core import PydanticUndefined

    if field.default_factory is not None:
        try:
            produced = field.default_factory()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(produced, (dict, list, tuple, set)) or produced is None:
            return repr(produced)
        return None
    if field.default is PydanticUndefined or field.is_required():
        return None
    if isinstance(field.default, (str, int, float, bool)) or field.default is None:
        return repr(field.default)
    return None


def _build_forwarder(spec: Any) -> Any:
    """为单个 ToolSpec 生成一个签名完整的 async 转发函数。"""
    fields = spec.input_model.model_fields
    ordered = [n for n, f in fields.items() if _default_src(f) is None]
    ordered += [n for n in fields if n not in ordered]

    params_src: List[str] = []
    payload_items: List[str] = []
    for name in ordered:
        field = fields[name]
        ann = _type_src(field.annotation)
        default = _default_src(field)
        params_src.append(f"{name}: {ann}" if default is None else f"{name}: {ann} = {default}")
        payload_items.append(f"{name!r}: {name}")

    src = "\n".join(
        [
            f"async def {spec.name}({', '.join(params_src)}) -> str:",
            f"    payload = {{{', '.join(payload_items)}}}",
            "    from commercepivot.mcp_server.registry import get_registry",
            f"    result = await get_registry().call({spec.name!r}, payload, role='admin')",
            "    return json.dumps(result, ensure_ascii=False, default=str)",
        ]
    )
    namespace: Dict[str, Any] = {"typing": typing, "json": json}
    exec(compile(src, f"<mcp-tool:{spec.name}>", "exec"), namespace)  # noqa: S102
    fn = namespace[spec.name]
    fn.__doc__ = spec.description
    return fn


def build_server(
    *,
    name: str = "commercepivot",
    streamable_http_path: str = "/mcp",
) -> Any:
    """构造 FastMCP server 并注册全部工具（两种传输共用同一个实例形态）。

    ``streamable_http_path`` 只在 HTTP 传输下有意义，默认 ``/mcp``，即标准宿主
    探测的路径。挂到 FastAPI 时用 ``app.mount("/", protocol.streamable_http_app())``
    且**必须**在父应用的 lifespan 里显式 ``async with protocol.session_manager.run()``
    —— FastAPI 默认 lifespan 不会进入被 mount 子应用的 lifespan，漏了就会
    500 ``Task group is not initialized``（详见 server.create_mcp_app）。
    """
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    load_all()
    mcp = FastMCP(
        name,
        instructions=(
            "「商枢」CommercePivot 电商经营分析 MCP 工具集："
            "订单、商品、售后、库存、广告、知识库检索与报表生成。"
            "所有查询类工具均为只读；写操作默认被服务端拒绝。"
        ),
        streamable_http_path=streamable_http_path,
        # 保持 SDK 默认（未传即关闭 DNS rebinding 校验）：本服务默认绑定
        # 0.0.0.0 供本机多进程调试，若开启校验则非 localhost 的 Host 会被拒。
        # 该端点**不带 JWT**（标准宿主难以携带自定义头），仅适合本机使用。
    )
    # FastMCP 构造 MCPServer 时没有暴露 version 参数，不设的话 initialize 返回的
    # serverInfo.version 会是 **mcp SDK 的版本**（如 1.13.1）—— 宿主界面上显示这个
    # 版本号对运维是完全误导的，直接改成项目版本。
    protocol_version = get_settings().app_version
    try:
        mcp._mcp_server.version = protocol_version  # noqa: SLF001
    except AttributeError:  # pragma: no cover - SDK 内部结构变化时静默降级
        log.warning("无法覆盖 MCP serverInfo.version，将显示 SDK 版本")
    for spec in get_registry().list_specs():
        fn = _build_forwarder(spec)
        mcp.add_tool(
            fn,
            name=spec.name,
            title=spec.name,
            description=spec.description,
            annotations=ToolAnnotations(
                title=spec.name,
                readOnlyHint=spec.readonly,
                destructiveHint=not spec.readonly,
                idempotentHint=spec.readonly,
                openWorldHint=False,
            ),
        )
        log.info("MCP 工具已注册", tool=spec.name, readonly=spec.readonly)
    return mcp


def describe_transports() -> Dict[str, Any]:
    """给自检/健康检查用：说明当前可用的接入方式。"""
    settings = get_settings()
    return {
        "stdio": {
            "command": "python -m commercepivot.mcp_server.stdio_server",
            "transport": "stdio",
        },
        "streamable_http": {
            "url": f"http://127.0.0.1:{settings.mcp_port}/mcp",
            "transport": "streamable-http",
            "auth": "none (仅本机使用)",
        },
        "rest": {
            "url": f"http://127.0.0.1:{settings.mcp_port}/api/v1/mcp/tools/call",
            "transport": "http-json 双风格（原生 / JSON-RPC）",
            "auth": "JWT",
        },
    }


__all__ = ["build_server", "describe_transports"]
