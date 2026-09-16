"""MCP 宿主联调自检 —— 用**官方 mcp SDK 客户端**真实握手两种传输。

与 ``cli.py smoke`` 里那些「进程内直调注册表」的检查不同，本模块是真正
把 MCP Server 当成外部服务来连：

- ``stdio``：spawn 一个子进程（``python -m commercepivot.mcp_server.stdio_server``），
  走 stdin/stdout 的 JSON-RPC —— 覆盖 Claude Desktop / Cursor 的接入方式；
- ``streamable-http``：对 ``http://127.0.0.1:<port>/mcp`` 发标准 MCP 请求 ——
  覆盖远程/进程外宿主。若目标地址没在跑，会在本进程内临时拉起一个。

每次都跑完整链路：``initialize`` → ``tools/list`` → ``tools/call``（真实工具 + 真实库），
因为只验前两步的话，「工具能列出来但调不动」这类问题会被漏掉。

用法::

    python -m commercepivot.mcp_check              # 两种传输都测
    python -m commercepivot.mcp_check --stdio-only
    python -m commercepivot.mcp_check --url http://127.0.0.1:8001/mcp   # 测已在跑的服务
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from typing import Any, Dict, List, Optional, Tuple

# 联调用一个只读、真实可打的工具，避免用假数据掩盖问题
TOOL_NAME = "query_orders"
TOOL_ARGS: Dict[str, Any] = {"start_date": "上个月", "platform": "抖店", "limit": 3}


# ------------------------------------------------------------------ 公共探测
async def _probe(session: Any, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """在一条已建立的 MCP 会话上跑完 initialize / tools/list / tools/call。"""
    init = await session.initialize()
    listed = await session.list_tools()
    called = await session.call_tool(tool, args)

    payload: Any = None
    for block in called.content or []:
        if getattr(block, "type", "") == "text":
            try:
                payload = json.loads(block.text)
            except json.JSONDecodeError:
                payload = {"_unparsed": str(block.text)[:200]}
            break

    summary = (payload or {}).get("summary") or {}
    tool_names = [t.name for t in (listed.tools or [])]
    return {
        "protocol_version": getattr(init, "protocolVersion", None),
        "server_name": getattr(getattr(init, "serverInfo", None), "name", None),
        "server_version": getattr(getattr(init, "serverInfo", None), "version", None),
        "tool_count": len(tool_names),
        "has_probe_tool": tool in tool_names,
        "call_is_error": bool(called.isError),
        "call_ok": bool((payload or {}).get("ok")),
        "order_count": summary.get("order_count"),
        "gmv": summary.get("gmv"),
        "degraded": (payload or {}).get("degraded"),
        "error": (payload or {}).get("message") if not (payload or {}).get("ok") else None,
    }


def _verdict(r: Dict[str, Any]) -> Tuple[bool, str]:
    ok = (
        r.get("server_name") is not None
        and r.get("has_probe_tool")
        and not r.get("call_is_error")
        and r.get("call_ok")
    )
    detail = (
        f"协议={r.get('protocol_version')} 服务={r.get('server_name')} "
        f"工具={r.get('tool_count')} 调用 {TOOL_NAME}="
        f"{'OK' if r.get('call_ok') else 'FAIL'}"
    )
    if r.get("order_count") is not None:
        detail += f" 订单量={r.get('order_count')} GMV={r.get('gmv')}"
    if r.get("error"):
        detail += f" 错误={r.get('error')}"
    return ok, detail


# ------------------------------------------------------------------ stdio
async def check_stdio() -> Dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from commercepivot.core.config import BASE_DIR

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "commercepivot.mcp_server.stdio_server"],
        # 必须显式透传 os.environ：StdioServerParameters.env 默认走
        # get_default_environment()，只保留 PATH/HOME 等「安全子集」，
        # API_KEY、MYSQL_* 会被丢掉 —— 子进程会静默降级成模板回答/连不上库。
        # 这也是宿主配置里需要写 env 的原因（见 README「接宿主」小节）。
        env={**os.environ},
        cwd=str(BASE_DIR),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            return await _probe(session, TOOL_NAME, TOOL_ARGS)


# ------------------------------------------------------------------ HTTP
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _start_ephemeral(app: Any, port: int) -> Tuple[Any, asyncio.Task]:
    """在本事件循环里拉起 uvicorn（避免联调依赖「先手动起服务」）。"""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):  # 最多等 10s
        if server.started:
            break
        if task.done():
            task.result()  # 抛出启动异常
        await asyncio.sleep(0.05)
    if not server.started:
        raise RuntimeError("内嵌 uvicorn 启动超时")
    return server, task


async def _stop(server: Any, task: asyncio.Task) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
        task.cancel()


async def check_streamable_http(url: Optional[str] = None) -> Dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    server = task = None
    if url is None:
        from commercepivot.mcp_server.server import create_mcp_app

        port = _free_port()
        url = f"http://127.0.0.1:{port}/mcp"
        server, task = await _start_ephemeral(create_mcp_app(), port)
    try:
        async with streamablehttp_client(url) as (read, write, _get_sid):
            async with ClientSession(read, write) as session:
                result = await _probe(session, TOOL_NAME, TOOL_ARGS)
        result["url"] = url
        return result
    finally:
        if server is not None and task is not None:
            await _stop(server, task)


# ------------------------------------------------------------------ 入口
async def run_all(http_url: Optional[str] = None, stdio_only: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    try:
        r = await check_stdio()
        r["transport"] = "stdio"
    except Exception as exc:  # noqa: BLE001
        r = {"transport": "stdio", "fatal": f"{type(exc).__name__}: {exc}"}
    out.append(r)

    if not stdio_only:
        try:
            r = await check_streamable_http(http_url)
            r["transport"] = "streamable-http"
        except Exception as exc:  # noqa: BLE001
            r = {"transport": "streamable-http", "fatal": f"{type(exc).__name__}: {exc}"}
        out.append(r)
    return out


def _print_report(results: List[Dict[str, Any]]) -> int:
    failed = 0
    print("\n" + "=" * 78)
    print("MCP 宿主联调报告（官方 mcp SDK 客户端真实握手）")
    print("=" * 78)
    for r in results:
        name = f"{r.get('transport')}"
        if r.get("fatal"):
            failed += 1
            print(f" [✗] FAIL  {name:18s} {r['fatal']}")
            continue
        ok, detail = _verdict(r)
        failed += 0 if ok else 1
        target = r.get("url") or "python -m commercepivot.mcp_server.stdio_server"
        print(f" [{'✓' if ok else '✗'}] {'PASS' if ok else 'FAIL'}  {name:18s} {detail}")
        print(f"           目标：{target}")
    print("-" * 78)
    print(f" 合计 {len(results)} 项：PASS {len(results) - failed}、FAIL {failed}")
    print("=" * 78)
    return 0 if failed == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MCP 宿主联调自检")
    ap.add_argument("--url", default=None, help="已运行的 Streamable HTTP 端点（默认自动临时起服务）")
    ap.add_argument("--stdio-only", action="store_true", help="只测 stdio 传输")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args(argv)

    results = asyncio.run(run_all(http_url=args.url, stdio_only=args.stdio_only))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 0 if all(not r.get("fatal") and _verdict(r)[0] for r in results) else 1
    return _print_report(results)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
