"""命令行入口：建库、灌数、建索引、起服务、跑演示。

用法::

    python -m commercepivot migrate        # 建库建表
    python -m commercepivot seed --reset   # 生成并导入示例数据
    python -m commercepivot index --rebuild# 知识语料入 Milvus + MySQL 镜像
    python -m commercepivot dev            # 一键拉起全栈（API :8000 + MCP :8001）
    python -m commercepivot serve          # 只启动主服务 :8000
    python -m commercepivot mcp            # 只启动独立 MCP Server :8001
    python -m commercepivot stdio          # 启动 MCP stdio server（供宿主接入）
    python -m commercepivot mcp-check      # MCP 宿主联调自检
    python -m commercepivot status         # 打印各组件状态与降级情况
    python -m commercepivot smoke          # 端到端自检
    python -m commercepivot demo           # 跑架构文档 §7 的示例问句
    python -m commercepivot ask "上个月抖店退货率最高的商品是什么"
    python -m commercepivot token --role admin
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from commercepivot.core.config import BASE_DIR, get_settings
from commercepivot.core.logging import get_logger, setup_logging

log = get_logger("commercepivot.cli")

DEMO_QUESTIONS = [
    "上个月抖店退货率最高的商品是什么？",
    "上个月各平台的销售额分别是多少？",
    "哪些 SKU 快断货了？",
    "上个月巨量引擎的投产比是多少？",
    "七天无理由退货的条件是什么？",
    "帮我导出上个月的退货率排行报表",
]


# =============================================================== migrate
def _split_sql(text: str) -> List[str]:
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(raw)
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def cmd_migrate(args: argparse.Namespace) -> int:
    import pymysql

    settings = get_settings()
    sql_path = Path(args.sql) if args.sql else BASE_DIR / "data" / "init_mysql.sql"
    if not sql_path.exists():
        print(f"[x] 找不到 SQL 文件：{sql_path}")
        return 1
    statements = _split_sql(sql_path.read_text(encoding="utf-8"))
    print(f"[*] 目标 MySQL：{settings.mysql_user}@{settings.mysql_host}:{settings.mysql_port}")
    print(f"[*] 待执行语句：{len(statements)} 条（来自 {sql_path.name}）")

    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        charset=settings.mysql_charset,
        autocommit=True,
    )
    created, failed = 0, 0
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                try:
                    cur.execute(stmt)
                    created += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    head = " ".join(stmt.split())[:80]
                    print(f"  [!] 跳过语句（{type(exc).__name__}: {exc}）：{head}…")
            # 可选：中文全文索引，用于 §11 的 BM25 降级检索
            try:
                cur.execute(
                    "ALTER TABLE commercepivot.knowledge_docs "
                    "ADD FULLTEXT INDEX ft_knowledge (title, content) WITH PARSER ngram"
                )
                print("  [+] 已创建 ngram 全文索引 ft_knowledge（FULLTEXT 降级检索可用）")
            except Exception as exc:  # noqa: BLE001
                print(f"  [!] 未创建 ngram 全文索引（不影响 LIKE 降级）：{type(exc).__name__}: {exc}")
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES FROM commercepivot")
            tables = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    print(f"[✓] 建库完成：成功 {created} 条、跳过 {failed} 条")
    print(f"[✓] commercepivot 现有表（{len(tables)}）：{', '.join(tables)}")
    return 0


# =============================================================== seed
def cmd_seed(args: argparse.Namespace) -> int:
    from commercepivot.db import repository as repo
    from commercepivot.db.seed import generate, load_into_mysql, reset_tables, write_csv

    started = time.perf_counter()
    data = generate(
        days=args.days,
        base_orders_per_day=args.orders_per_day,
        seed=args.seed,
    )
    counts = {k: len(v) for k, v in data.items()}
    print(f"[*] 已生成示例数据：{counts}")

    if args.csv:
        written = write_csv(data)
        print(f"[*] CSV 已写出 {len(written)} 个文件到 {BASE_DIR / 'data'}")

    if args.reset:
        reset_tables()
    imported = load_into_mysql(data)
    print(f"[✓] 已导入 MySQL：{imported}")

    # 知识语料同时镜像进 MySQL，保证 Milvus 不可用时 §11 的降级检索可用
    try:
        from commercepivot.retrieval.ingest import load_corpus

        docs = load_corpus()
        if docs:
            n = repo.upsert_knowledge_docs(docs)
            print(f"[✓] 知识语料镜像入 MySQL：{len(docs)} 条（affected={n}）")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 知识语料镜像失败：{type(exc).__name__}: {exc}")

    print(f"[✓] 耗时 {time.perf_counter() - started:.1f}s")
    return 0


# =============================================================== index
def cmd_index(args: argparse.Namespace) -> int:
    from commercepivot.retrieval.ingest import build_index

    stats = build_index(rebuild=args.rebuild, only_mysql=args.only_mysql)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("milvus", {}).get("available"):
        print("[✓] Milvus 向量索引已就绪")
    else:
        print(f"[!] Milvus 未就绪，检索将走 MySQL 降级：{stats.get('milvus', {}).get('reason')}")
    return 0


# =============================================================== 问答
async def _ask_once(question: str, role: str = "admin", top_k: int | None = None,
                    verbose: bool = True) -> Dict[str, Any]:
    from commercepivot.core.context import request_context
    from commercepivot.orchestrator.graph import get_orchestrator

    with request_context() as rid:
        state = await get_orchestrator().arun(
            question=question, session_id="cli-session", user_id="cli", role=role,
            top_k=top_k, request_id=rid,
        )
    if verbose:
        _print_state(state)
    return dict(state)


def _print_state(state: Dict[str, Any]) -> None:
    intent = state.get("intent") or {}
    slots = state.get("slots") or {}
    print("\n" + "=" * 78)
    print(f"问题：{state.get('question')}")
    print("-" * 78)
    print(f"意图：{intent.get('domain')}/{intent.get('primary_name')} "
          f"（置信度 {intent.get('confidence')}，来源 {intent.get('source')}）")
    print(f"槽位：{json.dumps({k: v for k, v in slots.items() if v not in (None, '', [], {})}, ensure_ascii=False)}")
    if state.get("defaults_applied"):
        print(f"默认值：{'；'.join(state['defaults_applied'])}")
    plan = state.get("plan") or []
    if plan:
        desc = "；".join(
            f"{p.get('agent')}({p.get('focus')}/{p.get('role')})" for p in plan
        )
        print(f"规划：{desc}")
        print(f"      依据：{state.get('plan_reason', '')}")
    tasks = state.get("agent_tasks") or []
    if tasks:
        print("A2A 任务：")
        for t in tasks:
            mark = "✓" if t.get("status") == "completed" else "✗"
            print(f"  {mark} {t.get('agent_name'):26s} {t.get('status'):10s} {t.get('elapsed_ms')}ms "
                  f"{t.get('error') or ''}")
    findings = state.get("findings") or []
    if findings:
        print("关键结论：")
        for f in findings:
            print(f"  · {f}")
    table = state.get("table")
    if table:
        print(f"表格（{table.get('row_count')} 行）：")
        labels = [c["label"] for c in table["columns"]]
        print("  " + " | ".join(labels))
        for row in table["rows"][:8]:
            print("  " + " | ".join(str(row.get(c["key"], "")) for c in table["columns"]))
    if state.get("citations"):
        print("知识来源：")
        for c in state["citations"][:3]:
            print(f"  · {c.get('title')}（{c.get('source')}，相关度 {c.get('score')}）")
    print("-" * 78)
    print("回答（来源：" + str(state.get("answer_source")) + "）：")
    print(state.get("answer", ""))
    if state.get("degraded"):
        print("\n[降级说明] " + "；".join(state.get("degrade_reasons") or []))
    timings = state.get("node_timings") or {}
    print(f"\n节点耗时：{timings}  总耗时 {state.get('elapsed_ms')}ms  编排模式 {state.get('orchestrator_mode')}")
    print("=" * 78)


def cmd_ask(args: argparse.Namespace) -> int:
    import asyncio

    asyncio.run(_ask_once(args.question, role=args.role, top_k=args.top_k))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    import asyncio

    questions = args.questions or DEMO_QUESTIONS
    print(f"[*] 将依次提问 {len(questions)} 个问题（编排模式见每次输出末尾）")
    for q in questions:
        asyncio.run(_ask_once(q, role="admin", verbose=True))
    return 0


# =============================================================== 服务
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.api_port
    print(f"[*] 启动 {settings.app_name} → http://{host}:{port}  (docs: /docs)")
    uvicorn.run(
        "commercepivot.main:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level=("info" if settings.debug else "warning"),
    )
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    import uvicorn

    from commercepivot.mcp_server.server import create_mcp_app

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.mcp_port
    print(f"[*] 启动独立 MCP Server → http://{host}:{port}/mcp/tools/list")
    uvicorn.run(create_mcp_app(), host=host, port=port, log_level="info")
    return 0


def cmd_stdio(args: argparse.Namespace) -> int:
    from commercepivot.mcp_server.stdio_server import main as stdio_main

    stdio_main()
    return 0


def cmd_mcp_check(args: argparse.Namespace) -> int:
    from commercepivot.mcp_check import main as mcp_check_main

    argv: List[str] = []
    if getattr(args, "url", None):
        argv += ["--url", args.url]
    if getattr(args, "stdio_only", False):
        argv += ["--stdio-only"]
    if getattr(args, "json", False):
        argv += ["--json"]
    return mcp_check_main(argv)


# =============================================================== 一键启动
_ALL_SERVICES = ("api", "mcp")


def _parse_services(raw: str | None) -> List[str]:
    if not raw or not raw.strip():
        return ["api"]
    if raw.strip().lower() in ("all", "*"):
        return list(_ALL_SERVICES)
    names = [x.strip().lower() for x in raw.split(",") if x.strip()]
    unknown = [x for x in names if x not in _ALL_SERVICES]
    if unknown:
        raise SystemExit(
            f"[!] 未知服务 {unknown}；可选 {', '.join(_ALL_SERVICES)} 或 all"
        )
    return names or ["api"]


def _probe_port(host: str, port: int, timeout_s: float) -> bool:
    """轮询端口，直到可连接或超时。用于「启动后立刻知道自己是否成功」。"""
    import socket

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.4)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.3)
    return False


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def cmd_dev(args: argparse.Namespace) -> int:
    """一键拉起全栈（API + MCP Server），Ctrl+C / 停止按钮一起退出。

    专为 IDE 的一键启动设计：在 PyCharm 里把本命令存成一个 Run
    Configuration，点一下就能得到「完整可验证」的运行态。

    用**子进程**而不是线程来承载各服务，理由是让每个服务的运行方式与单独启动
    完全一致（各自独立的日志初始化、全局单例、信号处理），不会互相污染；
    代价只是多几个进程，对开发机可以忽略。
    """
    import subprocess

    # 子服务日志是直通本进程 stdout 的；一旦输出被重定向（IDE 的 Run 窗口、
    # 管道、日志文件），Python 默认切成块缓冲，会让「已就绪」这类关键提示
    # 滞后几十秒才出现。这里强制行缓冲，保证「点了就能看到反馈」。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    settings = get_settings()
    wanted = _parse_services(getattr(args, "services", None))
    host = args.host or settings.host
    api_port = args.port or settings.api_port
    wait_s = max(0.0, float(getattr(args, "wait", 30.0) or 0.0))

    specs: List[Dict[str, Any]] = []
    if "api" in wanted:
        cmd = [
            sys.executable, "-m", "commercepivot.cli", "serve",
            "--host", host, "--port", str(api_port),
        ]
        if args.reload:
            cmd.append("--reload")
        specs.append({
            "name": "api",
            "cmd": cmd,
            "port": api_port,
            "url": f"http://127.0.0.1:{api_port}",
            "docs": f"http://127.0.0.1:{api_port}/docs",
        })
    if "mcp" in wanted:
        specs.append({
            "name": "mcp",
            "cmd": [
                sys.executable, "-m", "commercepivot.cli", "mcp",
                "--host", host, "--port", str(settings.mcp_port),
            ],
            "port": settings.mcp_port,
            "url": f"http://127.0.0.1:{settings.mcp_port}/mcp",
            "docs": f"http://127.0.0.1:{settings.mcp_port}/docs",
        })

    # 端口预检：最烦人的是「点了启动却什么也没发生」。提前查一次，把
    # 「上一次没退干净」这种情况明确指出来，而不是丢一句 bind 失败就结束。
    occupied = [s["port"] for s in specs if _port_in_use(s["port"])]
    if occupied:
        print("[!] 以下端口已被占用，启动会失败：")
        for port in occupied:
            print(f"      :{port}")
        print("    常见原因是上一次运行没有正常退出（残留进程）。Windows 下清理：")
        print(f"      netstat -ano | findstr :{occupied[0]}")
        print("      taskkill /F /PID <上面的PID>")
        return 2

    line = "=" * 72
    print(line)
    print(f"  {settings.app_name} v{settings.app_version} · 一键启动")
    print(line)
    print(f"  工作目录 : {Path.cwd()}")
    print(f"  服务列表 : {', '.join(s['name'] for s in specs)}")
    llm_state = settings.llm_model if settings.llm_enabled else (
        f"{settings.llm_model}  ⚠ 未配置真实 API_KEY → 大模型将降级为模板回答"
    )
    print(f"  大模型   : {llm_state}")
    print(line)

    if len(specs) > 1:
        print("  ⚠ 多服务同启的已知限制（实测确认，不是 bug）：")
        print("    Milvus Lite 是**单进程独占**（自带文件锁），后启动的进程会拿到")
        print("    DataDirLockedError，其向量检索自动降级为 MySQL FULLTEXT/LIKE ——")
        print("    结果仍然正确，只是相关性会下降，/health 里能看到 degrade 原因。")
        print("    日常开发建议只起 api（它已内嵌 MCP 工具层，能力完整）；")
        print("    需要单独验证 :8001 时用 --services mcp 单独启动。")
        print(line)

    procs: List[tuple[str, subprocess.Popen]] = []
    for spec in specs:
        log.info("拉起子服务", service=spec["name"], cmd=" ".join(spec["cmd"]))
        procs.append((spec["name"], subprocess.Popen(spec["cmd"])))

    # 启动后主动探测端口，把「起没起来」当场告诉使用者，而不是等请求超时才发觉
    if wait_s > 0:
        print("")
        for spec in specs:
            ready = _probe_port("127.0.0.1", spec["port"], wait_s)
            if ready:
                print(f"  [✓] {spec['name']:<4} 已就绪  {spec['url']}")
            else:
                print(f"  [✗] {spec['name']:<4} {wait_s:.0f}s 内未监听 :{spec['port']}"
                      f"（终端窗口里有该服务的启动日志）")

    print("")
    print("  验证入口：")
    for spec in specs:
        print(f"    · {spec['name']:<4} 接口文档  {spec['docs']}")
    print(f"    · 端到端自检  python -m commercepivot smoke")
    print(f"    · MCP 联调    python -m commercepivot mcp-check")
    print("")
    print("  按 Ctrl+C（或 IDE 的停止按钮）可同时停止全部服务。")
    print(line)

    reason = ""
    try:
        while not reason:
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    reason = f"子服务 {name} 已退出（exit={code}）"
                    break
            if not reason:
                time.sleep(0.4)
    except KeyboardInterrupt:
        reason = "收到中断信号"

    for _name, proc in procs:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.time() + 8
    for _name, proc in procs:
        try:
            proc.wait(timeout=max(0.5, deadline - time.time()))
        except Exception:  # noqa: BLE001 - 兜底强杀，避免留下孤儿进程
            proc.kill()
    print(f"\n[!] {reason}，全部服务已停止。")
    return 0


# =============================================================== 运维工具
def cmd_token(args: argparse.Namespace) -> int:
    from commercepivot.core.security import create_access_token

    token = create_access_token(args.subject, args.role, expires_minutes=args.expires)
    print(token)
    print("\n请求示例：")
    print(f'  curl -H "Authorization: Bearer {token}" '
          f'-H "Content-Type: application/json" '
          f'-d \'{{"question":"上个月抖店退货率最高的商品是什么？"}}\' '
          f'http://localhost:{get_settings().api_port}/api/v1/chat/ask')
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    out: Dict[str, Any] = {"config": settings.public_dict()}

    from commercepivot.db.mysql import get_mysql
    from commercepivot.db.redis import get_redis

    try:
        out["mysql"] = get_mysql().health()
    except Exception as exc:  # noqa: BLE001
        out["mysql"] = {"available": False, "error": str(exc)}
    try:
        out["redis"] = get_redis().health()
    except Exception as exc:  # noqa: BLE001
        out["redis"] = {"available": False, "error": str(exc)}

    from commercepivot.retrieval.service import retrieval_status

    out["retrieval"] = retrieval_status(load_models=args.load_models)

    from commercepivot.models.intent_bert import intent_status
    from commercepivot.models.llm_client import get_llm

    out["llm"] = get_llm().status()
    out["intent"] = intent_status()

    from commercepivot.agents.registry import get_agent_registry
    from commercepivot.mcp_server.registry import get_registry
    from commercepivot.mcp_server.tools import load_all

    load_all()
    out["mcp"] = {"tools": get_registry().names()}
    out["a2a"] = {"agents": get_agent_registry().names(), "skills": get_agent_registry().all_skills()}

    from commercepivot.orchestrator.graph import get_orchestrator

    out["orchestrator"] = {"mode": get_orchestrator().mode}

    if args.tables:
        from commercepivot.db import repository as repo

        try:
            out["tables"] = repo.table_stats()
        except Exception as exc:  # noqa: BLE001
            out["tables"] = {"available": False, "error": str(exc)}

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """端到端自检：不依赖外部服务是否齐全，逐项给出 PASS/FAIL/DEGRADED。"""
    import asyncio

    from commercepivot.core.context import request_context

    results: List[tuple[str, str, str]] = []

    def record(name: str, ok: bool, detail: str = "", degraded: bool = False) -> None:
        status = "PASS" if ok else ("DEGRADED" if degraded else "FAIL")
        results.append((name, status, detail))

    # 1) 组件
    from commercepivot.db.mysql import get_mysql
    from commercepivot.db.redis import get_redis

    try:
        h = get_mysql().health()
        record("MySQL 连接", bool(h["available"]), h.get("error") or h.get("target", ""))
    except Exception as exc:  # noqa: BLE001
        record("MySQL 连接", False, str(exc))
    try:
        h = get_redis().health()
        record("Redis 连接", bool(h["available"]), h.get("error") or h.get("target", ""), degraded=True)
    except Exception as exc:  # noqa: BLE001
        record("Redis 连接", False, str(exc), degraded=True)

    from commercepivot.retrieval.service import retrieval_status

    ret = retrieval_status(load_models=False)
    record("意图分类器", True, ret and f"embedder={ret['embedder']['active']}, reranker={ret['reranker']['active']}")

    # 2) 工具层
    from commercepivot.mcp_server.tools import load_all

    names = load_all()
    record("MCP 工具注册", len(names) == 8, f"{len(names)} 个：{names}")

    # 3) Agent 层
    from commercepivot.agents.registry import get_agent_registry

    agents = get_agent_registry().names()
    record("A2A Agent 注册", len(agents) == 5, f"{len(agents)} 个：{agents}")

    # 4) 编排
    from commercepivot.orchestrator.graph import get_orchestrator

    record("LangGraph 编排", True, f"mode={get_orchestrator().mode}")

    # 5) 意图 + 槽位
    from commercepivot.models.intent_bert import classify_intent
    from commercepivot.orchestrator.slots import extract_slots

    q = "上个月抖店退货率最高的商品是什么？"
    intent = classify_intent(q)
    slots = extract_slots(q, intent)
    ok_intent = intent.primary == "after_sales"
    record("意图识别（示例问句）", ok_intent, f"{intent.domain}/{intent.primary_name} conf={intent.confidence}")
    ok_slots = slots.slots.get("platform") == "抖店" and slots.slots.get("metric") == "退货率"
    record("槽位抽取（平台+指标+时间）", ok_slots,
           f"platform={slots.slots.get('platform')} metric={slots.slots.get('metric')} "
           f"period={slots.slots.get('period_label')}")

    # 6) 工具真实调用
    async def _tool() -> None:
        from commercepivot.mcp_server.registry import get_registry

        res = await get_registry().call(
            "query_orders",
            {"start_date": "上个月", "platform": "抖店", "breakdowns": ["product"], "limit": 3},
            role="admin",
        )
        ok = bool(res.get("ok"))
        rows = res.get("row_count") or 0
        summary = res.get("summary") or {}
        record(
            "MCP 工具真实调用（query_orders）",
            ok,
            f"ok={ok} 明细 {rows} 行，订单量={summary.get('order_count')} GMV={summary.get('gmv')}",
            degraded=bool(res.get("degraded")),
        )

    with request_context():
        asyncio.run(_tool())

    # 6.5) 向量检索：**必须真跑一次检索**，不能只查配置状态。
    # 向量层的失败全是静默降级（集合没 load / sparse 字段不匹配 / 版本不配对
    # 都会让 search 抛错后被吞掉改走 MySQL），只看 health 会得出「一切正常」，
    # 而实际检索链路早已不是向量库。
    async def _vector() -> None:
        from commercepivot.mcp_server.registry import get_registry
        from commercepivot.retrieval.milvus_client import get_milvus

        store = get_milvus()
        if not store.available:
            record("Milvus（向量检索）", False, f"不可用：{store.reason}", degraded=True)
            return
        res = await get_registry().call(
            "search_knowledge", {"query": "七天无理由退货的条件", "top_k": 3}, role="admin"
        )
        rows = res.get("rows") or []
        modes = sorted({str(r.get("retrieval_mode") or "") for r in rows if r.get("retrieval_mode")})
        used_vector = any(m.startswith("milvus") for m in modes)
        counts = store.status().get("collections") or {}
        record(
            "Milvus（向量检索）",
            used_vector,
            f"mode={store.mode} sparse={store.supports_sparse} "
            f"集合={counts} 命中 {len(rows)} 条 链路={modes or '无'}",
            degraded=not used_vector,
        )

    with request_context():
        asyncio.run(_vector())

    # 7) 端到端
    async def _e2e():
        return await _ask_once(q, role="admin", verbose=False)

    e2e_state: Dict[str, Any] = {}
    try:
        e2e_state = asyncio.run(_e2e())
        state = e2e_state
        ok = bool(state.get("answer"))
        record("端到端问答链路", ok,
               f"来源={state.get('answer_source')} 耗时={state.get('elapsed_ms')}ms "
               f"任务={len(state.get('agent_tasks') or [])} 降级={state.get('degraded')}")
    except Exception as exc:  # noqa: BLE001
        record("端到端问答链路", False, f"{type(exc).__name__}: {exc}")

    # 7.5) 三处「真跑出来」的缺陷的回归断言 —— 证据全部取自上面这次真实 e2e 的 state，
    #      不额外起链路。任何一个再被改坏都会 FAIL：
    #      a) 规划说明的意图名必须取自计划项自身的 intent。历史上用 agent 反查意图表，
    #         而 sales/order/after_sales/ad 共用 order_analysis_agent，反查恒命中字典序
    #         最前的 sales，把「售后分析（退货率）」写成「销售分析」。
    #      b) trace 必须覆盖全链路。LangGraph 每节点在独立子上下文执行、工具调用还过
    #         asyncio.to_thread，若 trace 用「不可变元组 + set()」，最终只剩最后 2 条。
    #      c) 喂给 LLM 的上下文必须含表格逐行明细，否则模型被要求「逐行列举」时只能
    #         答「订单量未显示」（findings 只给头部行且不一定带全指标）。
    _plan_reason = e2e_state.get("plan_reason") or ""
    ok_plan_name = ("售后分析" in _plan_reason) and ("销售分析" not in _plan_reason)
    record(
        "规划说明·意图名口径",
        ok_plan_name,
        (_plan_reason[:56] + "…") if len(_plan_reason) > 56 else (_plan_reason or "（空）"),
    )

    _trace = e2e_state.get("trace") or []
    _stages = {t.get("stage") for t in _trace if t.get("stage")}
    # a2a-dispatch 在「派发」时写入，与工具是否成功无关，因此不依赖 MySQL/Milvus 可用性。
    ok_trace = len(_trace) > 2 and {"node", "a2a-dispatch"} <= _stages
    record(
        "链路 trace 完整性",
        ok_trace,
        f"{len(_trace)} 条 / 阶段={sorted(_stages)}",
    )

    from commercepivot.orchestrator.nodes import build_llm_context

    _ctx = build_llm_context(e2e_state)
    _table_rows = (e2e_state.get("table") or {}).get("rows") or []
    _first_name = (_table_rows[0].get("product_name") if _table_rows else None)
    ok_ctx_rows = bool(_table_rows) and ("逐行明细" in _ctx) and bool(_first_name) and (_first_name in _ctx)
    record(
        "LLM 上下文含表格明细",
        ok_ctx_rows,
        f"表格 {len(_table_rows)} 行，上下文 {len(_ctx)} 字，首行已在上下文={bool(_first_name) and _first_name in _ctx}",
    )

    # 8) 闲聊直答：问候**不该**触发知识库检索。
    # 闲聊在意图体系里没有对应 Agent，一旦 planning_node 的兜底规则把它当成
    # 「未匹配到专属 Agent」捞给 knowledge_rag_agent，就会给「你好」返回几条
    # 退货政策（实测耗时 ~2.6s）。这里同时断言计划为空 / 无 A2A 任务 / 无知识引用，
    # 任何一条被破坏都会 FAIL —— 否则这个缺陷很容易在后续改动里重新溜回来。
    async def _chitchat():
        return await _ask_once("你好", role="admin", verbose=False)

    try:
        state = asyncio.run(_chitchat())
        tasks = state.get("agent_tasks") or []
        citations = state.get("citations") or []
        # 只断言「链路」：闲聊必须短路（来源=chitchat、无计划/任务/引用）。
        # 不强断言 source==llm/template —— 那取决于当前 API_KEY 是否可用，
        # 是环境问题不是回归问题；生成者另看 answer_meta.generator。
        ok = (
            state.get("answer_source") == "chitchat"
            and not tasks
            and not citations
            and not (state.get("plan") or [])
        )
        generator = (state.get("answer_meta") or {}).get("generator", "-")
        record(
            "闲聊直答（不检索知识库）",
            ok,
            f"来源={state.get('answer_source')} 生成={generator} "
            f"计划={len(state.get('plan') or [])} "
            f"任务={len(tasks)} 引用={len(citations)} 耗时={state.get('elapsed_ms')}ms",
        )
    except Exception as exc:  # noqa: BLE001
        record("闲聊直答（不检索知识库）", False, f"{type(exc).__name__}: {exc}")

    # 13) MCP 宿主联调：用**官方 SDK 客户端**真开一个子进程做 stdio 握手。
    # 这一项和上面「MCP 工具注册/真实调用」互补 —— 那两项是进程内直调注册表，
    # 只证明工具本身好用；这里证明「外部宿主按 MCP 协议连得进来」，
    # 覆盖 stdout 通道污染、initialize/tools/list/tools/call 全链路。
    # 完整版（含 Streamable HTTP）见 `python -m commercepivot.mcp_check`。
    async def _stdio_handshake():
        from commercepivot.mcp_check import check_stdio, _verdict

        return await check_stdio(), _verdict

    try:
        r, verdict = asyncio.run(_stdio_handshake())
        ok, detail = verdict(r)
        record("MCP 宿主联调（stdio 握手）", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("MCP 宿主联调（stdio 握手）", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print("商枢 CommercePivot · 自检报告")
    print("=" * 78)
    for name, status, detail in results:
        mark = {"PASS": "✓", "DEGRADED": "~", "FAIL": "✗"}[status]
        print(f" [{mark}] {status:8s} {name:34s} {detail}")
    failed = [r for r in results if r[1] == "FAIL"]
    degraded = [r for r in results if r[1] == "DEGRADED"]
    print("-" * 78)
    print(f" 合计 {len(results)} 项：PASS {len(results) - len(failed) - len(degraded)}、"
          f"DEGRADED {len(degraded)}、FAIL {len(failed)}")
    print("=" * 78)
    return 1 if failed else 0


# =============================================================== 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commercepivot",
        description="「商枢」CommercePivot 电商智能经营分析与客服助手平台",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("migrate", help="建库建表（执行 data/init_mysql.sql）")
    p.add_argument("--sql", help="自定义 SQL 文件路径")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("seed", help="生成并导入示例数据")
    p.add_argument("--reset", action="store_true", help="导入前清空业务表")
    p.add_argument("--days", type=int, default=166, help="生成多少天的数据")
    p.add_argument("--orders-per-day", type=int, default=40, help="平均日订单量")
    p.add_argument("--seed", type=int, default=20260912, help="随机种子（保证可复现）")
    p.add_argument("--csv", action="store_true", default=True, help="同时写出 CSV（默认开）")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("index", help="知识语料入库（Milvus + MySQL 镜像）")
    p.add_argument("--rebuild", action="store_true", help="先删除集合再重建")
    p.add_argument("--only-mysql", action="store_true", help="只写 MySQL 镜像，不碰 Milvus")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("serve", help="启动主服务")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp", help="启动独立 MCP Server")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("stdio", help="启动 MCP stdio server")
    p.set_defaults(func=cmd_stdio)

    p = sub.add_parser("mcp-check", help="MCP 宿主联调自检（官方客户端握 stdio / HTTP）")
    p.add_argument("--url", help="已运行的 Streamable HTTP 端点（默认在本进程内临时起服务）")
    p.add_argument("--stdio-only", action="store_true", help="只测 stdio 传输")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_mcp_check)

    p = sub.add_parser("dev", help="一键拉起服务（默认 API :8000，含内嵌 MCP），适合 IDE 一键启动")
    p.add_argument(
        "--services", default=None,
        help="要启动的服务：api / mcp / all（默认 api，能力最完整）",
    )
    p.add_argument("--host", help="监听地址（默认 HOST，即 0.0.0.0）")
    p.add_argument("--port", type=int, help="主服务端口（默认 API_PORT）")
    p.add_argument("--reload", action="store_true", help="主服务开启热重载（改代码自动重启）")
    p.add_argument("--wait", type=float, default=30.0, help="端口就绪探测秒数，0 表示不探测")
    p.set_defaults(func=cmd_dev)

    p = sub.add_parser("ask", help="提一个问题")
    p.add_argument("question")
    p.add_argument("--role", default="admin", choices=["admin", "operator", "customer"])
    p.add_argument("--top-k", type=int, default=None)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("demo", help="跑示例问句")
    p.add_argument("questions", nargs="*", help="自定义问题（留空则用内置示例）")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("token", help="签发 JWT")
    p.add_argument("--subject", default="admin")
    p.add_argument("--role", default="admin", choices=["admin", "operator", "customer"])
    p.add_argument("--expires", type=int, default=None)
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("status", help="打印组件状态")
    p.add_argument("--tables", action="store_true", help="附带各表行数")
    p.add_argument("--load-models", action="store_true", help="真正加载模型（慢）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("smoke", help="端到端自检")
    p.set_defaults(func=cmd_smoke)

    return parser


def main(argv: List[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n[!] 已中断")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
