"""站点图标与根路径落地页（主服务 :8000 与独立 MCP Server :8001 共用）。

为什么单独开一个模块：浏览器打开任意页面后都会**自动**再请求一次
``/favicon.ico``。服务若不提供，控制台每次刷新都会多一条
``GET /favicon.ico 404 Not Found`` —— 无害，但会淹掉真正的错误日志，
而且标签页没有图标。这两个端点纯粹是"运维观感"层的东西，和业务无关，
所以独立成模块、两个 app 复用，而不是往 ``main.py`` 里塞。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

# 内联 SVG，不引入任何静态文件依赖（也就没有打包/部署路径问题）。
# 图形语义：深蓝圆角底 + 白色上扬折线 = "商"业增长的"枢"纽。
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
    '<rect width="64" height="64" rx="14" fill="#1f4e79"/>'
    '<path d="M13 45 L26 30 L36 38 L51 19" fill="none" stroke="#ffffff" '
    'stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="51" cy="19" r="5.5" fill="#ff9f2e"/>'
    "</svg>"
)

# 图标不变，允许浏览器长期缓存，避免每次刷新都产生一次请求
_ICON_HEADERS = {"Cache-Control": "public, max-age=86400"}


def _icon_response() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml", headers=_ICON_HEADERS)


def register_favicon(app: FastAPI) -> None:
    """注册 ``/favicon.ico`` 与 ``/favicon.svg``，消除浏览器 404 噪音。

    ``/favicon.ico`` 也返回 SVG：现代浏览器按 ``Content-Type`` 解析，
    能正常显示；这样就不必额外维护一个二进制 ICO 文件。
    """
    app.add_api_route("/favicon.ico", lambda: _icon_response(), include_in_schema=False, methods=["GET"])
    app.add_api_route("/favicon.svg", lambda: _icon_response(), include_in_schema=False, methods=["GET"])


def wants_html(request: Request) -> bool:
    """判断调用方是不是浏览器。

    浏览器发 ``Accept: text/html,application/xhtml+xml,...``；
    而 ``curl`` / ``httpx`` / ``requests`` 默认发 ``Accept: */*``。
    因此只需判断是否显式声明了 ``text/html``，程序的 JSON 契约天然不受影响。
    """
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


# 仅用于落地页展示的中文说明。**不改动 JSON 契约**（``GET /`` 的
# ``endpoints`` key 保持英文，程序侧不受影响）。
_LABELS = {
    "ask": "单轮问答（同步返回完整链路）",
    "ask_stream": "单轮问答（SSE 流式）",
    "token": "签发 JWT（调试用）",
    "mcp_tools_list": "MCP 工具清单",
    "mcp_tools_call": "MCP 工具调用",
    "mcp_sse": "MCP 工具调用（SSE）",
    "agent_card": "A2A Agent Card",
    "agents": "A2A Agent 列表",
    "a2a_rpc": "A2A 调用",
    "health": "健康检查（含降级状态）",
    "metrics": "Prometheus 指标",
}


def render_landing(
    app_name: str,
    app_version: str,
    endpoints: Dict[str, str],
    extra: Iterable[Tuple[str, str]] = (),
) -> str:
    """渲染根路径落地页：把散落的入口变成可点的链接。

    ``extra`` 用于补充"非接口"入口（如 ``/docs``），它们在 OpenAPI 里被
    ``include_in_schema=False`` 隐藏或不属于业务接口。
    带参数的路径（含 ``{`` / ``...`` / ``?``）不做成链接 —— 点进去只会拿到
    422 或 404，看着像故障，不如标成"需参数"引导去 Swagger。
    """
    rows = []
    for label, spec in list(endpoints.items()) + list(extra):
        method, _, target = spec.partition(" ")
        if not target:  # 形如 "/docs"，只有路径没写方法
            method, target = "GET", spec
        inner = (
            f'<span class="m">{method}</span>'
            f'<span class="p">{target}</span>'
            f'<span class="d">{_LABELS.get(label, label)}</span>'
        )
        if any(ch in target for ch in "{?"):
            rows.append(f'<li class="na" title="需要参数，请在 Swagger 中调试">{inner}</li>')
        else:
            rows.append(f'<li><a href="{target}">{inner}</a></li>')
    items = "\n      ".join(rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{app_name} v{app_version}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 48px 20px; min-height: 100vh;
    font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: #1c2430;
    background: linear-gradient(180deg, #f5f8fc 0%, #eef3f9 100%);
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  .card {{ background: #fff; border: 1px solid #e3e9f0; border-radius: 14px; padding: 28px 30px; box-shadow: 0 6px 24px rgba(31,78,121,.06); }}
  header {{ display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }}
  header img {{ width: 44px; height: 44px; border-radius: 10px; }}
  h1 {{ font-size: 20px; margin: 0; font-weight: 650; letter-spacing: .2px; }}
  .ver {{ color: #7b8794; font-size: 13px; font-weight: 400; margin-left: 6px; }}
  .ok {{ display: inline-block; margin: 10px 0 22px; padding: 3px 10px; border-radius: 999px; background: #e8f6ee; color: #1a7f4b; font-size: 12.5px; }}
  h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .8px; color: #8a95a1; margin: 22px 0 8px; font-weight: 600; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li + li {{ border-top: 1px solid #f0f3f7; }}
  a, li.na {{ display: flex; align-items: baseline; gap: 10px; padding: 9px 10px; border-radius: 8px; text-decoration: none; color: inherit; }}
  li.na {{ cursor: default; }}
  li.na .m {{ color: #8a95a1; background: #f2f4f7; }}
  li.na .p {{ color: #93a0ad; }}
  li.na .d {{ color: #bcc4cd; }}
  a:hover {{ background: #f4f8fc; }}
  .m {{ font: 600 11px/1 ui-monospace, Consolas, monospace; color: #1f4e79; background: #eaf1f8; padding: 4px 6px; border-radius: 5px; min-width: 42px; text-align: center; }}
  .p {{ font: 13px/1 ui-monospace, Consolas, monospace; color: #2c3e50; }}
  .d {{ margin-left: auto; color: #8a95a1; font-size: 12.5px; }}
  footer {{ margin-top: 18px; color: #9aa5b1; font-size: 12.5px; text-align: center; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <header>
        <img src="/favicon.svg" alt="">
        <h1>{app_name}<span class="ver">v{app_version}</span></h1>
      </header>
      <div class="ok">服务运行中</div>
      <h2>接口入口</h2>
      <ul>
      {items}
      </ul>
    </div>
    <footer>该页面仅在浏览器访问（<code>Accept: text/html</code>）时返回；程序调用 <code>GET /</code> 仍是 JSON。</footer>
  </div>
</body>
</html>"""


def register_landing(app: FastAPI, app_name: str, app_version: str, payload: Dict[str, Any]) -> None:
    """把 ``GET /`` 注册成"浏览器看页面、程序看 JSON"的双形态端点。"""

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Any:
        if wants_html(request):
            return HTMLResponse(
                render_landing(
                    app_name,
                    app_version,
                    payload.get("endpoints") or {},
                    extra=[("接口文档 Swagger UI", "/docs"), ("OpenAPI Schema", "/openapi.json")],
                )
            )
        return payload
