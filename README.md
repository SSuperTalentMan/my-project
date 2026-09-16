# 商枢 CommercePivot

> 电商智能经营分析与客服助手平台 —— 基于 **LangGraph 主控 Agent + A2A 协议 + MCP 工具层** 的分层智能体系统。

用一句大白话讲：**运营同学用中文问一句「上个月抖店退货率最高的商品是什么？」，系统自己判断意图、补齐条件、派活给专职 Agent、Agent 通过 MCP 工具去查真实数据库、再把结果算成人话回答出来。**

本仓库是《架构文档》V1.2 的完整落地实现。设计上有一条铁律：**任何外部依赖缺失都要优雅降级，绝不让整个链路挂掉，更不允许编造数据**（见 [降级矩阵](#降级矩阵)）。

---

## 目录

- [架构总览](#架构总览)
- [快速开始](#快速开始)
- [在 PyCharm 中一键启动](#在-pycharm-中一键启动)
- [前端控制台（Vue 3）](#前端控制台vue-3)
- [目录结构](#目录结构)
- [接口用法](#接口用法)
  - [HTTP API](#http-api)
  - [CLI](#cli)
  - [MCP 工具层](#mcp-工具层)
- [A2A Agent 层](#a2a-agent-层)
- [降级矩阵](#降级矩阵)
- [向量库形态与 Windows 说明](#向量库形态与-windows-说明)
- [配置项](#配置项)
- [自检与验证](#自检与验证)
- [安全约定](#安全约定)

---

## 架构总览

六层结构，自上而下依赖，同层之间只通过协议交互：

```
┌──────────────────────────────────────────────────────────────────────┐
│ ① 接入层    FastAPI · JWT(admin/operator/customer) · Redis 限流      │
│             SSE 流式 · 会话落库 · X-Request-ID 贯穿 · Prometheus     │
├──────────────────────────────────────────────────────────────────────┤
│ ② 编排层    LangGraph 主控 Agent（6 节点）                            │
│    START → intent_node → slot_node ─┬─(缺必需槽位)→ answer_node ─┐   │
│                                     ├─(闲聊寒暄)  → answer_node ─┤   │
│                                     └─(齐)→ planning_node →      │   │
│                                            a2a_route_node →      │   │
│                                            aggregate_node → answer│   │
│                                     ←────────────────────────────┘   │
├──────────────────────────────────────────────────────────────────────┤
│ ③ A2A Agent 层  5 个专职 Agent（JSON-RPC · Agent Card · 熔断）        │
│    订单分析 · 商品分析 · 售后分析 · 知识问答(RAG) · 报表生成           │
├──────────────────────────────────────────────────────────────────────┤
│ ④ MCP 工具层    8 个工具（校验/超时/重试/审计/写门禁）                 │
├──────────────────────────────────────────────────────────────────────┤
│ ⑤ 数据层        MySQL(业务+审计) · Redis(会话/缓存/AgentCard)         │
│                 Milvus(向量) ‖ MySQL FULLTEXT(降级检索)               │
├──────────────────────────────────────────────────────────────────────┤
│ ⑥ 模型层        百炼 qwen(生成) · BERT(意图) · BGE-M3(向量) · Reranker│
└──────────────────────────────────────────────────────────────────────┘
```

**关键设计决策（评审与排障时最常被追问的几条）：**

| 决策 | 原因 |
|---|---|
| Agent **不直连数据库**，一律经 MCP 工具层取数 | 参数校验、权限门禁、审计、限流集中在一层，Agent 无法绕过 |
| 退货率由**订单 Agent 自行 join 两个工具结果**计算 | 对应架构文档 §7 第 6 步：工具层只给原子数据，业务口径留在 Agent |
| 编排层探测到 LangGraph 不可用时**降级为顺序执行** | 保证在最小依赖环境下仍能跑通全链路 |
| 闲聊在 `route_after_slot` 就**短路**到回答节点 | 闲聊没有对应 Agent，若进入规划会被兜底规则"未匹配 → 退回知识库问答"捞走：一句「你好」也要跑一次向量检索（实测 ~2.6s），返回的还是三条退货政策。现在 2-40ms 直答，且管线里不产生任何工具/Agent 调用 |
| 审计**双写** JSONL + MySQL | 文件便于本地排查，入库便于查询与统计 |
| 写操作默认**全局关闭**（`ENABLE_WRITE_OPS=false`） | 只读优先，符合 §11 的安全基线 |

---

## 快速开始

环境要求：**Python 3.12 + uv**，MySQL 8.0+ 与 Redis 为可选在线依赖（本地开发用任意口令即可，配置见 `.env.example`；对外环境请替换默认值）。

```bash
# 1. 装依赖（清华源已配在 pyproject.toml 里）
uv sync
#    torch 建议单独装 CPU 版：
#    uv pip install torch==2.4.0 --index-url https://download.pytorch.org/whl/cpu

# 2. 准备环境变量（.env 已被 gitignore，仅本地生效）
cp .env.example .env
#    按需把 API_KEY 换成真实百炼密钥；真实密钥建议只放控制台环境变量

# 3. 起 Redis（可选，没有会自动降级为进程内 KV）
docker compose up -d

# 4. 建库建表（12 张表 + ngram 全文索引）
uv run python -m commercepivot migrate

# 5. 灌示例数据（约 7400 订单 / 2000 买家 / 650 售后 / 40 篇知识语料）
uv run python -m commercepivot seed --reset

# 6. 知识语料入 Milvus + MySQL 镜像（无 Milvus 时自动只写 MySQL）
uv run python -m commercepivot index --rebuild

# 7. 自检（最有用的一条命令，逐项给 PASS/DEGRADED/FAIL）
uv run python -m commercepivot smoke

# 8. 起服务
uv run python -m commercepivot dev            # 一键拉起（API :8000，含内嵌 MCP 工具层）
# 或分开起：
uv run python -m commercepivot serve          # 主服务 :8000  → http://localhost:8000/docs
uv run python -m commercepivot mcp            # 独立 MCP Server :8001
```

**跑通第一个问题：**

```bash
uv run python -m commercepivot ask "上个月抖店退货率最高的商品是什么？"
```

输出会逐步打印：意图 → 槽位 → 规划 → A2A 任务耗时 → 关键结论 → 表格 → 最终回答 → 节点耗时。

---

## 在 PyCharm 中一键启动

`.idea/runConfigurations/` 里已经放好 10 个运行配置，**用 PyCharm 打开项目后，右上角运行下拉框里就能直接选**，点绿三角即可：

| 配置 | 对应命令 | 用途 |
|---|---|---|
| ① 一键启动 dev（推荐） | `dev --wait 30` | 日常开发：起主服务 :8000 + 探测端口就绪 + 打印验证入口 |
| ② API 主服务 :8000 | `serve` | 只调 API 时 |
| ③ MCP Server :8001 | `mcp` | 只验证独立 MCP 入口时 |
| ④ 端到端自检 smoke | `smoke` | 16 项 PASS/FAIL/DEGRADED，提交前回归 |
| ⑤ MCP 宿主联调 mcp-check | `mcp-check` | 官方 MCP 客户端真握 stdio / HTTP |
| ⑥ 单问验证 ask | `ask "…"` | 打印意图→槽位→A2A→MCP→聚合→LLM 全链路 |
| ⑦ 组件状态 status | `status --tables` | 看各组件降级原因 |
| ⑧ 全栈同启 | Compound ②+③ | 少见，见下方第 2 条提醒 |
| ⑨ 前端控制台（Vue） | `npm run dev`（`frontend/`） | 起 Vue 控制台 :5173，配合 ① 使用 |
| ⑩ 全端同启 | Compound ①+⑨ | 后端 + 前端一起起，日常就用这个 |

> `⑨` / `⑩` 需要一个带 JS 支持的 PyCharm（Professional）。社区版没有 npm 运行配置类型，直接在终端里 `cd frontend && npm run dev` 即可，效果一样。

启动成功后控制台会直接给出可点的验证入口（`/docs`、自检命令），不用再去翻文档：

```
  [✓] api  已就绪  http://127.0.0.1:8000

  验证入口：
    · api  接口文档  http://127.0.0.1:8000/docs
    · 端到端自检  python -m commercepivot smoke
    · MCP 联调    python -m commercepivot mcp-check
```

### 三个必须先知道的点

**1. API_KEY 不用手填，也不会写进任何受版本控制的文件。**
环境变量是「进程启动那一刻的快照」—— 如果密钥是通过控制台 / `setx` 设置的，那么**在此之前启动的 PyCharm 读不到它**，服务会静默降级成模板回答（`answer_source=template`）。
为此 `core/config.py` 加了注册表兜底：环境变量 → `.env` → **Windows 注册表**（用户级优先，其次系统级）。所以只要密钥存在于系统环境里，PyCharm 里一键启动就能直接走真实大模型，无需在 Run Configuration 里填 `Environment variables`，也就不会把密钥写进 `.idea/`。启动 banner 会打印实际状态：

```
  大模型   : qwen3.7-flash          # 密钥可用
  大模型   : qwen3.7-flash  ⚠ 未配置真实 API_KEY → 大模型将降级为模板回答
```

**2. 一个进程只能独占一个 Milvus Lite（重要）。**
Milvus Lite 是嵌入式本地库，**自带文件锁、不支持多进程同时打开**。实测第二个进程会拿到：

```
milvus_lite.exceptions.DataDirLockedError: another process holds the lock on
'D:\CommercePivot\data\milvus\commercepivot.db'
```

它的向量检索会自动降级为 MySQL FULLTEXT/LIKE（结果仍正确，只是相关性下降，`/health` 里能看到 `degrade_reason`）。因此：

- **日常开发用 ①（只起 api）** —— 主服务已内嵌完整 MCP 工具层（`/api/v1/mcp/*`），能力不缺；
- 需要单独验证 :8001 时用 ③，此时别同时开 ①；
- ⑧ 这个 Compound 配置仅用于「两个入口都能起来」的验证，同时跑时两者只有一个能拿到向量库 —— `dev --services all` 会主动把这条限制打在 banner 上。

**3. 运行配置不会进版本库。**
根 `.gitignore` 第 33 行忽略了 `.idea/`，所以这些配置只在你本机生效（对个人开发正好）。若要给团队共享，需在 `.gitignore` 放开 `.idea/runConfigurations/`。

### 如果要手工建（或想在别的 IDE / 命令行用）

命令本身不依赖 PyCharm，等价写法：

```bash
python -m commercepivot dev --wait 30        # 等价于 ①
python -m commercepivot dev --services all   # 等价于 ⑧
python -m commercepivot dev --services mcp   # 等价于 ③
```

在 PyCharm 里手工新增一个 Python 运行配置时，填这几项即可（其余留空）：

| 字段 | 值 |
|---|---|
| 解释器 / SDK | 项目内置的 `.venv`（`D:\CommercePivot\.venv\Scripts\python.exe`） |
| 运行目标 | **模块**（不是脚本）→ `commercepivot.cli` |
| 形参（Parameters） | `dev` |
| 工作目录（Working directory） | 项目根 `D:\CommercePivot` |

> `src` 目录已在 `commercepivot.iml` 里标记为 Sources Root，且 `.venv` 中是 editable 安装（`commercepivot.pth`），所以模块方式运行不受工作目录影响。

---

## 前端控制台（Vue 3）

浏览器打开后端根路径（`GET /`）只会看到一个导航落地页；真正可交互的控制台在 `frontend/`，用 Vue 3 + Vite 写成。
它定位为**验证台**：把编排层每一步的中间产物都摊开，回答本身反而只是其中一块。

```bash
cd frontend
npm install        # 首次
npm run dev        # → http://127.0.0.1:5173
```

> 需先起后端（`python -m commercepivot.cli dev`）。前端经 dev server 代理访问后端，**开发期不涉及 CORS**。

### 页面上能看到什么

| 区块 | 内容 |
|---|---|
| 顶栏 | 后端连接状态（含降级项数）、MCP 工具数 / A2A Agent 数、角色切换、JWT 签发与清除 |
| 回答卡片 | 正文 + `answer_source` / `generator` / 模型名 / 耗时 / 缓存命中 / 降级徽章；数据表格；Agent 结论；知识库引用（含 score）；`request_id` 与 `session_id` |
| 链路概览（右） | 意图（置信度 + 命中的规则词）、槽位、缺失槽位与套用的默认值、任务规划、A2A 子 Agent 逐个状态与耗时、编排节点耗时条形图、业务指标 |
| 事件流水（右） | SSE 事件按到达顺序实时追加：`start → intent → slots → plan → agents → findings → table → answer_delta → done` |
| MCP 工具（右） | 8 个工具的完整清单可现场调用，默认参数取 registry 里的 `examples[0]`；**写类工具会被门禁拒绝**，这本身就是要验证的点 |

**几个可复现的验证动作**

- 点右上「清除令牌」→ 再提问 → 回答区出现 `UNAUTHORIZED · 缺少 Bearer Token`（既不是 500，也不是白屏）
- 问「你好呀，你是谁？」→ `answer_source=chitchat`、计划与任务恒为空、不产生 `agents` / `table` 事件
- 问「上个月各平台的销售额分别是多少？」→ 表格 5 行，正文逐行列举，与「全平台合计」对得上账
- 切到「MCP 工具」→ 选 `create_ticket` → 调用 → 被写门禁拒绝（`ENABLE_WRITE_OPS=false`）

### 两个实现要点

1. **SSE 用 fetch 手写解析，不能用 `EventSource`** —— 流式端点是 `POST /api/v1/chat/ask_stream`，而浏览器原生 `EventSource` 只支持 GET。解析逻辑抽成了 `consumeSse()`（只依赖标准 `Response`），因此能在 Node 里对真实后端直接跑。
2. **dev server 代理而非跨域直连** —— `vite.config.js` 把 `/api`、`/health`、`/version`、`/openapi.json` 等前缀代理到 `127.0.0.1:8000`；换后端地址设 `VITE_API_TARGET`。

### 前端侧回归验证

```bash
npm run verify        # 等价 node verify-sse.mjs，需后端在运行
```

复用 `src/api.js` 的实现（同一份代码），共 18 项断言，覆盖两类：

- **解析边界**：`\r\n\r\n` 跨 chunk 被切断、`data` 跨 chunk 被切断、15s 心跳注释 `: ping` 必须被忽略、多行 `data` 拼接、非 JSON 载荷降级、空块返回 `null`
- **对真实后端的往返**：事件序列完整且顺序正确、表格可解析、多个 `answer_delta` 拼出完整回答、`answer_source=llm`、闲聊短路不产生 `agents`/`table`、无令牌 401 错误体可解析

组件本身能否渲染、点击有没有反应，由另外两个零依赖检查覆盖：

```bash
npm run verify:render     # 用 Vue SSR 把 App.vue 真实渲染成 HTML（不需后端）

# UI 端到端：需要本机有 Chrome，且 dev server 在运行
chrome --headless --virtual-time-budget=60000 --dump-dom \
  http://127.0.0.1:5173/verify-ui.html | grep -o "<title>[^<]*</title>"
```

实测输出：

```
verify:render  → 渲染 3137 字节；顶栏 / 连接状态 / 示例问题 / 输入区 / 链路面板 / 内联图标全部命中，无 Vue 报错
verify-ui.html → RESULT signed=true answerLen=306 tableRows=5 tableCols=4
                        findings=3 badges=LLM 生成/生成者 大模型/qwen3.7-flash/3.28 s
                        eventSteps=9 tools=8
```

`verify-ui.html` 走的是**真实点击**：等 Vue 挂载 → 等自动签发 JWT → 取消勾选流式 → 点击示例问题 → 等回答产出，把结果写进 `document.title` 再 dump 出来。它补的正是 SSR 覆盖不到的那一环：**事件处理器接线** —— 渲染正常不等于点击有反应。

> 它刻意先切成非流式再发送：SSE 是长连接，在 `--virtual-time-budget` 下永不结束，无头取证跑不完；SSE 那条路径由 `verify-sse.mjs` 覆盖。

---

## 目录结构

```
CommercePivot/
├── 架构文档.txt                     # 设计蓝本
├── pyproject.toml / uv.lock         # 依赖与锁定
├── docker-compose.yml               # Redis（默认）+ Milvus Standalone（profile）
├── .env.example                     # 环境变量模板（真实密钥不入库）
├── data/
│   ├── init_mysql.sql               # 12 张表 DDL
│   ├── knowledge/                   # product_kb.json / faq_kb.json 语料
│   └── *.csv                        # seed 产出的数据快照
├── logs/mcp_audit.jsonl             # MCP 调用审计（JSONL 侧）
├── frontend/                        # Vue 3 控制台（Vite，dev :5173）
│   ├── src/App.vue                  # 主页面（鉴权 / 对话 / 输入区）
│   ├── src/api.js                   # 接口封装 + SSE 解析（consumeSse）
│   ├── src/styles.css               # 设计令牌与组件类
│   ├── src/components/              # AnswerCard / TracePanel / ToolPanel / DataTable
│   └── verify-sse.mjs               # 解析层回归（npm run verify）
└── src/commercepivot/
    ├── core/          config / logging / security / metrics / errors / context
    ├── db/            mysql(连接池) / redis(降级门面) / repository(SQL 集中地) / seed / models
    ├── retrieval/     embedder / reranker / milvus_client / service / ingest
    ├── models/        llm_client(百炼) / intent_bert(意图)
    ├── agents/        base(JSON-RPC+熔断) / 5 个专职 Agent / registry
    ├── orchestrator/  state / slots / nodes(6 节点) / graph
    ├── mcp_server/    registry / protocol_server(标准 MCP 协议) / server(HTTP)
    │                  stdio_server / client / audit / tools(8 个)
    ├── api/           chat / a2a / mcp / schemas / errors(共用异常映射)
    ├── mcp_check.py   MCP 宿主联调自检（官方客户端握 stdio / HTTP）
    ├── main.py        FastAPI 应用装配
    └── cli.py         命令入口
```

---

## 接口用法

### HTTP API

先签发一个 JWT（默认 admin）：

```bash
TOKEN=$(uv run python -m commercepivot token --role admin | head -1)
```

**① 问答（一次性）**

```bash
curl -s -X POST http://localhost:8000/api/v1/chat/ask \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"上个月各平台的销售额分别是多少？"}' | jq
```

响应关键字段：`answer`、`table`（列定义 + 行）、`citations`、`findings`、`agent_tasks`、`node_timings`、`degraded`/`degrade_reasons`、`answer_source`（`llm` / `template` / `clarification` / `chitchat`）。

**② 问答（SSE 流式）**

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/ask_stream \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"哪些 SKU 快断货了？"}'
```

事件顺序（实测）：`start` → `intent` → `slots` → `plan` → `agents` → `findings` → `table` → `answer_delta` → `done`。工具/Agent 失败时仍会走到 `done` 并在 `degrade_reasons` 里说明。

**③ MCP 工具层**

```bash
# 列工具（需鉴权；无 token 返回 401）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/mcp/tools/list?detail=false" | jq -r '.tools[]'

# 查某个工具的参数 Schema
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/mcp/tools/query_inventory \
  | jq '.tool.inputSchema.properties | keys'

# 调用工具 —— 原生风格：字段是 tool + params
curl -s -X POST http://localhost:8000/api/v1/mcp/tools/call \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"tool":"query_inventory","params":{"low_stock_only":true}}' | jq '{ok,row_count,summary}'

# 同一路由也接受 MCP JSON-RPC 风格
curl -s -X POST http://localhost:8000/api/v1/mcp/tools/call \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"query_inventory","arguments":{"low_stock_only":true}}}' | jq '.result.ok'

# SSE 形式（GET，参数走 query）
curl -N "http://localhost:8000/api/v1/mcp/sse?tool=query_orders&params=%7B%22start_date%22%3A%22%E4%B8%8A%E4%B8%AA%E6%9C%88%22%7D"
```

> 约定：**工具执行失败不抛 HTTP 错误**，而是返回 `200` + 结构化结果 `{"ok":false,"code":"...","message":"...","details":{...}}`。这样调用方（Agent）能拿到可读原因继续降级处理，而不是被异常打断。写工具被门禁拦下时也是这种形态：
> `{"ok":false,"code":"FORBIDDEN","message":"写操作 create_ticket 未开启（ENABLE_WRITE_OPS=false）","details":{"hint":"在 .env 中设置 ENABLE_WRITE_OPS=true 后重启服务"}}`

**④ A2A 发现与调用**

```bash
curl -s http://localhost:8000/.well-known/agent-card.json | jq      # 主控 Agent Card（含 skills / endpoint / sub_agents）
curl -s http://localhost:8000/.well-known/agents | jq '.agents[].name'  # 5 个专职 Agent 及其 skills
curl -s -X POST http://localhost:8000/api/v1/a2a/order_analysis_agent \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"t1","method":"tasks/send",
       "params":{"message":{"role":"user","parts":[{"text":"上个月退货率排行"}]},
                 "metadata":{"focus":"return_rate"}}}' | jq '.result.task | {id,status,agent_name}'
```

返回结构为 `{"jsonrpc":"2.0","id":..,"result":{"ok":true,"task":{...}}}`，`task.status` ∈ `completed/failed/...`，`task.output.findings` 即该 Agent 的结论列表。另有 `tasks/get`（按 id 查询）与 `tasks/cancel`。

**⑤ 运维观测**

```bash
curl -s "http://localhost:8000/health?deep=true" | jq   # 各组件 + 降级状态 + 表行数
curl -s  http://localhost:8000/metrics                   # Prometheus 文本格式
curl -s  http://localhost:8000/version  | jq '.orchestrator_graph'   # 含 Mermaid 图
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/config | jq   # 仅 admin，密钥脱敏
```

> **`GET /` 是双形态端点**：浏览器访问（`Accept: text/html`）返回**导航落地页**，把所有入口列成可点链接；程序调用（`curl` / `httpx` / `requests` 默认 `Accept: */*`）仍返回原来的 JSON，**契约未变**。另外提供了 `/favicon.ico` 与 `/favicon.svg`（内联 SVG，无静态文件依赖）—— 浏览器每次打开页面都会自动请求图标，服务不提供就会在控制台反复刷 `GET /favicon.ico 404 Not Found`，这条噪音极易被误判成"服务启动报错"。

### CLI

| 命令 | 作用 |
|---|---|
| `migrate` | 建库建表 + ngram 全文索引 |
| `seed [--reset] [--days N] [--orders-per-day N]` | 生成并导入示例数据（可复现，默认种子 20260912） |
| `index [--rebuild] [--only-mysql]` | 知识语料入 Milvus + MySQL 镜像 |
| `dev [--services api\|mcp\|all] [--wait N] [--reload]` | **一键启动**：拉起服务 + 探测端口就绪 + 打印验证入口；默认只起 api |
| `serve [--port N] [--reload]` | 主服务 |
| `mcp` / `stdio` | 独立 MCP HTTP Server / MCP stdio Server |
| `mcp-check [--url U] [--stdio-only] [--json]` | MCP 宿主联调（官方客户端真握手 stdio 与 Streamable HTTP） |
| `ask "问题" [--role r] [--top-k N]` | 单问，打印完整链路 |
| `demo` | 依次跑架构文档 §7 的 6 个示例问句 |
| `token [--role r] [--expires N]` | 签发 JWT 并给出 curl 示例 |
| `status [--tables] [--load-models]` | 打印各组件状态与降级情况 |
| `smoke` | 端到端自检（16 项） |

### MCP 工具层

8 个工具，全部有 Pydantic 参数校验、超时、重试 1 次、审计落库：

| 工具 | 类型 | 说明 |
|---|---|---|
| `query_orders` | 读 | 订单聚合；`breakdowns` 支持 `product/platform/status/date/region/category` 多维切片 |
| `query_products` | 读 | 商品与 SKU 明细 |
| `query_after_sales` | 读 | 售后/退货记录聚合 |
| `query_inventory` | 读 | 库存与低库存预警 |
| `query_ad_reports` | 读 | 广告投放（消耗/成交/ROI） |
| `search_knowledge` | 读 | 知识检索：BGE-M3 → Milvus 混合 → Reranker，逐级降级 |
| `generate_report` | 写(文件) | 9 种报表导出 CSV/JSON |
| `create_ticket` | 写 | 建工单，受 `ENABLE_WRITE_OPS` 门禁 |

> `breakdowns` 用**列表参数**而不是一堆布尔开关，好处是切片维度可组合、参数面收敛、Schema 自解释。

#### 三种接入方式（都已在 README 之外实测过）

| 传输 | 地址 / 启动 | 协议 | 鉴权 | 适用 |
|---|---|---|---|---|
| **stdio** | `python -m commercepivot.mcp_server.stdio_server` | 标准 MCP（2025-06-18） | 无（宿主进程内） | Claude Desktop / Cursor 等桌面宿主 |
| **Streamable HTTP** | `POST http://127.0.0.1:8001/mcp` | 标准 MCP | 无（仅本机） | 进程外 / 远程 MCP 客户端、MCP Inspector |
| **REST** | `POST /api/v1/mcp/tools/call`、`POST /mcp/tools/call` | 自定义（原生 + JSON-RPC 双风格） | JWT + 限流 | 自研前端、curl、第 3 方系统集成 |

`GET /mcp/transports` 会返回当前可用的接入方式，便于运行时确认。

**宿主配置示例**（Claude Desktop 的 `claude_desktop_config.json`）：

```json
{"mcpServers": {"commercepivot": {
  "command": "D:\\CommercePivot\\.venv\\Scripts\\python.exe",
  "args": ["-m", "commercepivot.mcp_server.stdio_server"],
  "cwd": "D:\\CommercePivot",
  "env": {"API_KEY": "<你的百炼密钥>"}
}}}
```

> ⚠️ **`env` 不能省**。MCP 官方 stdio 客户端在未显式传 `env` 时会走
> `get_default_environment()`，**只保留 `PATH`/`HOME` 等「安全子集」**，
> 于是子进程拿不到 `API_KEY`（大模型静默降级成模板回答）也拿不到 `MYSQL_PASSWORD`
> 等凭据。Windows 的系统级环境变量同样会被这一层过滤掉，别指望自动继承。

**联调自检**（用官方 `mcp` SDK 客户端真开子进程 / 真发 HTTP，跑完
`initialize → tools/list → tools/call` 全链路）：

```bash
python -m commercepivot.mcp_check                 # stdio + Streamable HTTP 都测
python -m commercepivot.mcp_check --stdio-only    # 只测 stdio
python -m commercepivot.mcp_check --url http://127.0.0.1:8001/mcp   # 测已在跑的服务
# 等价 CLI：python -m commercepivot.cli mcp-check
```

> `smoke` 里只放了一条 stdio 握手（保持自检轻量），完整两种传输用 `mcp-check`。

#### 接宿主踩过的两个坑

**1. stdio 下 stdout 被日志污染。** stdout 是 JSON-RPC 专用通道，而项目日志原本统一写
`sys.stdout` —— 实测服务一启动就往 stdout 吐 **1032 字节**日志，宿主直接解析失败。
修法是让日志支持改道 stderr（`core/logging.py`）。**注意时机**：`python -m
commercepivot.mcp_server.stdio_server` 会先导入父包 `commercepivot.mcp_server`，
它的 `__init__` 又导入 `registry`，而 `registry` 在**模块级**就调了 `get_logger()`
—— 日志在 `stdio_server` 自己执行之前就被锁到 stdout 了。所以 `setup_logging`
必须是「可切换」而不是「只认第一次」，且 structlog 的 `cache_logger_on_first_use`
必须关掉，否则已创建的 logger 对象仍会继续写 stdout。

**2. HTTP 端点 500 `Task group is not initialized`。** FastMCP 的
`StreamableHTTPSessionManager` 要靠 lifespan 里的 `run()` 启动，而 **FastAPI 的默认
lifespan 只调 Starlette 的 `startup()`，不会进入被 mount 子应用的 lifespan** ——
mount 上去看着正常，一请求就 500。必须在父应用的 lifespan 里显式
`async with protocol.session_manager.run()`。另外标准端点挂到 `/`（而非 `/mcp`），
否则 `POST /mcp` 会先吃一个 307 跳 `/mcp/`。

顺带修掉一个同源问题：这套「异常 → JSON」映射原本只注册在 `main.create_app()` 里，
独立 MCP Server 没有，于是 `:8001` 上**鉴权失败返回 500 而不是 401**，排查方向被带偏。
现已抽成 `api/errors.py::register_exception_handlers()`，两个 app 共用。

---

## A2A Agent 层

| Agent | 端口 | 职责 | 主要技能 |
|---|---|---|---|
| `order_analysis_agent` | 8002 | 订单与退货分析 | 销售统计、退货率排行、趋势 |
| `product_analysis_agent` | 8003 | 商品与库存 | 商品表现、库存预警 |
| `after_sales_agent` | 8004 | 售后客服 | 售后原因分布、处理时效 |
| `knowledge_rag_agent` | 8005 | 知识问答 | 政策/FAQ 检索与回答 |
| `report_agent` | 8006 | 报表导出 | 按关键词选报表类型并生成文件 |

协议：JSON-RPC 方法 `tasks/send` / `tasks/get` / `tasks/cancel`，任务状态 `submitted→working→completed/failed/canceled`。每个 Agent 带**熔断器**（连续失败 3 次打开，30s 后半开），编排层并行派发时**部分失败不影响整体**。

---

## 降级矩阵

这是本项目最核心的工程价值。**没有密钥、没有模型、没有中间件，系统照样能给出基于真实数据的答案。**

| 能力 | 首选 | 缺失时降级 | 行为 |
|---|---|---|---|
| 意图识别 | BERT (`INTENT_BERT_PATH`) | 规则引擎（关键词 + 歧义消解） | 返回 `source=rule`，多意图仍可识别 |
| 向量化 | BGE-M3 稠密+稀疏 | 哈希向量器（blake2b 词袋，dim=1024） | 检索质量下降但可用 |
| 重排 | BGE-Reranker cross-encoder | 词法重排（bigram 覆盖 + 词交集） | 排序仍有区分度 |
| 向量库 | Milvus (lite/remote) | MySQL FULLTEXT(ngram) → LIKE | `stages` 字段标注实际链路；**Lite 单进程独占**，第二个进程会走此降级 |
| 数据查询 | MySQL | **不降级** | MySQL 不可用直接报错，绝不用假数据 |
| 生成模型 | 百炼 qwen | 模板回答（真实数据拼装） | `answer_source=template` |
| 缓存/会话 | Redis | 进程内 KV | 单实例可用，多实例失效 |
| 编排 | LangGraph | 顺序执行节点 | `orchestrator_mode=sequential` |
| 写操作 | 门禁开启 | 默认关闭 | 返回结构化拒绝，不报异常 |

> 统一原则：**降级必须显式可见** —— 每次响应都带 `degraded` / `degrade_reasons`，`/health` 里能逐组件看到原因，方便排查而不是"悄悄变成错答案"。

---

## 向量库形态与 Windows 说明

架构文档默认用 **Milvus Lite**（本地内嵌），本项目在 Windows 上同样用它，**不需要 Docker**。

关键点：**milvus-lite 3.x 是纯 Python 重写版**，提供 `py3-none-any` 通用 wheel（依赖 `faiss-cpu`/`pyarrow`/`grpcio`，三平台都有轮子），因此 Windows 可用。老版本 2.x 只有 manylinux/macOS wheel，那才是"Windows 用不了 Lite"的由来。

```bash
# 默认配置即 Lite，无需改任何东西
MILVUS_URI=./data/milvus/commercepivot.db
uv run python -m commercepivot index --rebuild
```

**版本必须成对**，否则会在 import 阶段报 `Protocol message ShowCollectionsResponse has no "shards_num" field`：

| 组件 | 版本 | 说明 |
|---|---|---|
| `pymilvus` | `2.6.17` | **≥2.6.4**：proto 才有 `ShowCollectionsResponse.shards_num` |
| `milvus-lite` | `3.2.1` | 3.x 才提供跨平台 wheel |

另两个已内置处理的坑（改动见 `retrieval/milvus_client.py`）：

- **`MILVUS_URI` 不能用相对路径喂给 pymilvus**。`pymilvus/settings.py` 模块顶层有 `load_dotenv()`，会把项目 `.env` 里的 `MILVUS_URI` 读进环境变量并当连接串解析，本地 `.db` 路径过不了它的 `urlparse` 校验 → `import pymilvus` 直接抛 `ConnectionConfigException`。项目里统一传 `milvus_abs_uri`（绝对路径），并在导入 pymilvus 期间临时置空该环境变量。
- **集合必须先 load**。新进程打开已存在的集合时状态是 `released`，直接检索会报 `call load() before search`。不处理的话表现是"向量库可用但检索一直静默走 MySQL 降级"。

**备选方案：Docker 版 Standalone**（多进程共享或大数据量时）

```bash
docker compose --profile milvus up -d     # 起 etcd + minio + milvus
# .env 中改为 MILVUS_URI=http://localhost:19530
```

**零依赖方案**：`uv run python -m commercepivot index --only-mysql`，`search_knowledge` 会用 MySQL FULLTEXT(ngram) / LIKE 检索，`stages` 字段标明实际链路。

---

## 配置项

全部配置集中在 `src/commercepivot/core/config.py`（单一 `Settings` 实例），可用环境变量覆盖，完整清单见 `.env.example`。分组速查：

- **模型**：`API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`（默认 `qwen3.7-flash`，与架构文档 §3.6 一致）/ `LLM_ENABLE_THINKING`（默认 `false`，混合推理模型关掉思维链）/ `LLM_TEMPERATURE` / `LLM_TIMEOUT_S`
- **MySQL**：`MYSQL_HOST/PORT/USER/PASSWORD/DB`、`MYSQL_POOL_SIZE`（轻量 LifoQueue 连接池 + ping 保活）
- **Redis**：`REDIS_HOST/PORT/PASSWORD/DB`
- **检索**：`MILVUS_URI`、`MILVUS_COLLECTION_*`、`MILVUS_DENSE_DIM`、`RETRIEVAL_TOP_K`、`RERANK_TOP_K`
- **本地模型**：`INTENT_BERT_PATH` / `BGE_M3_PATH` / `RERANKER_PATH`（留空即降级）、`MODEL_AUTODOWNLOAD`、`HF_ENDPOINT`
- **链路**：`MCP_TRANSPORT`（`local` 进程内直调 / `http` 走 :8001）、`MCP_TOOL_TIMEOUT_S`、`MCP_TOOL_RETRIES`、`A2A_TIMEOUT_S`、`A2A_CIRCUIT_*`、`CACHE_TTL_S`
- **安全**：`JWT_SECRET`、`JWT_EXPIRE_MINUTES`、`RATE_LIMIT_*`、`ENABLE_WRITE_OPS`
- **可观测**：`LOG_LEVEL`、`LOG_JSON`、`AUDIT_LOG_PATH`

`/config` 接口返回的 `public_dict()` 会**自动脱敏** API Key、数据库密码、JWT 密钥。

---

## 自检与验证

```bash
uv run python -m commercepivot smoke
```

逐项检查：MySQL/Redis 连接 → 意图分类器 → MCP 工具注册数(=8) → A2A Agent 注册数(=5) → 编排模式 → 示例问句的意图与槽位 → **MCP 工具真实调用**（校验真取了数、非 mock）→ **Milvus 真实检索一次**（校验返回链路确实是 `milvus-*` 而不是静默降级）→ 端到端问答链路。三项结论：`PASS` / `DEGRADED`（依赖缺失但已降级）/ `FAIL`。

> 自检里这两项刻意不查配置状态而**真跑一次调用**：工具层与向量层的失败都是"静默降级"，只读 health 会得到"一切正常"，而实际链路早已换人。这一点是踩过坑才补上的（见下方面向 Windows 的说明）。

本机实测（Windows 11，MySQL + Redis 在线，**Milvus Lite 向量库在线**，BGE/BERT 模型走降级）：

```bash
$ uv run python -m commercepivot migrate            # 12 表 + ngram 全文索引
$ uv run python -m commercepivot seed --reset       # 装载演示数据集（约 7.5k 单，说明见下）
$ uv run python -m commercepivot smoke              # 16 项：PASS 16 / DEGRADED 0 / FAIL 0
$ uv run python -m commercepivot mcp-check          # MCP 宿主联调 2 项：PASS 2 / FAIL 0
$ uv run python -m commercepivot serve              # :8000
```

> **关于演示数据集**：`seed` 生成的是**便于本地快速启动的缩减版数据**
> （7,489 订单 / 649 售后单 / 16 条商品知识 + 24 条 FAQ 语料），
> 只用于跑通链路与回归自检，与任何真实客户数据无关。它按固定买家池与长尾分布生成，
> 规模刻意压小以便秒级灌库；**实际交付时的数据量由客户生产库决定，无需改动任何代码。**

**HTTP 接口层实测（9 项全通过）**

| 验证项 | 结果 |
|---|---|
| 无 token 访问 `/chat/ask` | `401`（鉴权生效） |
| admin 提问 `/chat/ask` | `200`，~244ms，`agent_tasks` 含耗时 |
| SSE `/chat/ask_stream` | 事件链 `start→intent→slots→plan→agents→findings→table→answer_delta→done` |
| `GET /mcp/tools/list` 无 token / 有 token | `401` / `200`，`count=8` |
| `POST /mcp/tools/call` 原生风格 | `ok=true`，1.400 单 / GMV ¥437,698.77 |
| `POST /mcp/tools/call` JSON-RPC 风格 | `result.ok=true` |
| 传未声明参数（`threshold`） | `422 VALIDATION_FAILED`（`Extra inputs are not permitted`） |
| customer 调 `create_ticket` | `ok=false, code=FORBIDDEN`（写开关默认关闭） |
| `/.well-known/agent-card.json` 与 `/agents` | 主控 Card + 5 个专职 Agent |

**MCP 宿主联调实测（`mcp-check`，2026-09-14）**

| 传输 | 结果 |
|---|---|
| `stdio`（spawn 子进程） | 协议 `2025-06-18`、服务 `commercepivot`、8 个工具；真实调用 `query_orders` 返回 订单量 568 / GMV ¥152,849.50 |
| `Streamable HTTP`（`POST /mcp`） | 同上，且无 307 跳转（`POST /mcp` → `202 Accepted`） |
| REST 端点回归（9 项） | `/mcp/health`、`/mcp/transports`、双前缀 `tools/list` & `tools/call`（原生 + JSON-RPC）、无 token 返回 **401**、`/docs` —— 全部通过 |

**前端 SSE 解析回归实测（`npm run verify`，2026-09-14）**

| 类别 | 结果 |
|---|---|
| 解析边界（8 项） | `\r\n\r\n` 分隔符跨 chunk 切断、`data` 跨 chunk 切断、`: ping` 心跳忽略、多行 `data` 拼接、缺省事件名、非 JSON 载荷降级为 `raw`、空块返回 `null` —— 全部通过 |
| 真实后端往返（10 项） | 事件序列 `start→intent→slots→plan→agents→findings→table→answer_delta×78→done`；表格 5 行 4 列；意图 `sales` conf 0.69；`answer_source=llm` `model=qwen3.7-flash` 3681 ms；闲聊不产生 `agents`/`table`；无令牌 401 错误体可解析 —— **PASS 18 / FAIL 0** |
| 生产构建 | `npm run build` 16 modules / 568 ms / JS 99.6 kB（gzip 37.7 kB）/ **零警告** |

> 标准端点挂到 `/` 之后，原有 `POST /mcp/tools/call` 等显式路由仍优先命中，未被 catch-all 抢走。

**四个代表性问句的端到端结果**

- `上个月抖店退货率最高的商品是什么？` → 意图「售后分析 0.8333 / rule」，槽位 `platform=抖店 / metric=退货率`，规划收敛为 `order_analysis_agent(return_rate)`，A2A 耗时 ~92ms；结论「抖店整体退货率 **3.66%**，最高为折叠晾衣架 33.33%（2 退 / 6 单）」，并注明「已过滤成交少于 5 单的商品」。
- `上个月各平台的销售额分别是多少？` → 槽位自动带上 `breakdowns=["platform"]`，表格为 **5 行平台聚合**（平台 | 订单量 | GMV | 买家数）：抖店 ¥107,642 / 淘宝 ¥97,942 / 京东 ¥106,787 / 拼多多 ¥44,449 / 天猫 ¥80,878，合计 ¥437,698.77。
- `生鲜类商品支持七天无理由退货吗？` → 意图「知识问答 0.78」，规划只调度 `knowledge_rag_agent`，检索模式 **`milvus-hybrid`**（Milvus Lite 稠密 + 稀疏 RRF 融合 → Reranker），命中「鲜活易腐（如大闸蟹等生鲜）不支持无理由退货」并给出 2 小时验货索赔口径。
- `你好` / `你能做什么` → 意图「闲聊寒暄 0.95」，**不规划、不调 Agent、不检索**：`plan=[]`、`agent_tasks=[]`、`citations=0`、`table=null`、`slots={}`，`answer_source=chitchat`，**未配 API_KEY 时**由模板直答（2~40ms，改前为 2597ms 且返回三条退货政策）。流式事件链相应精简为 `start → intent → slots → plan(空) → answer_delta → done`。注意 `你好，帮我看下上个月销售额` 这类**带业务诉求的问候不会被短路**（意图判为 `sales`，正常调度）。

**Milvus Lite 实测**（`index --rebuild`）：两个集合 `product_kb`(16) / `faq_kb`(24) 均建成 `dense+sparse` 双路 schema（`supports_sparse=true`），写入 40 条；检索 `stages.vector_available=true`、`degraded=false`。

**大模型链路状态（2026-09-14 实测已跑通）**

模型 `qwen3.7-flash`（与架构文档 §3.6 一致），`API_KEY` 走控制台环境变量读取，鉴权与生成链路正常：闲聊问句与业务问句均返回 **HTTP 200**、`answer_source=llm`，回答内容与 MCP 查询结果一致。自检 **16/16 PASS**。

踩过的坑：`qwen-flash` / `qwen-plus` / `qwen3-max` / `qwen-max` / `qwen-turbo` 在本账号下均返回 **HTTP 403 `AllocationQuota.FreeTierOnly`** —— 百炼免费额度是**按模型独立计算**的，这五个恰好都耗尽了，而 `qwen3.7-flash` 仍有额度。所以「403 额度耗尽」不等于账号没额度，先确认模型名，别急着充值。错误文案会带上游 `error.code` 与处置提示，且 401/403/404/422 这类重试无意义的状态码不再重试。

`answer_source` 与 `answer_meta.generator` 是两个维度，不要混：`answer_source` 表示**走了哪条链路**（`llm` / `template` / `clarification` / `chitchat`），`generator` 表示**这段文字由谁生成**（`llm` / `template`）。闲聊就是这样：链路恒为 `chitchat`（自检据此断言「绝不检索知识库」），而生成者随 `API_KEY` 可用与否在 `llm` / `template` 之间切换。

**推理模型的两个坑（都是实测踩出来的）**

`qwen3.7-flash` 是**混合推理模型**，默认会先输出一大段思维链。对本项目这种「把 MCP 查询结果改写成通顺话术」的任务，思维链纯属浪费：

| | 响应耗时 | reasoning 字数 | completion tokens |
|---|---|---|---|
| 默认（开思考） | 12.7s | 2447 | 839 |
| `enable_thinking=false` | **5.3s** | 0 | **300** |

可见不只是慢 —— 思维链**照常计入计费 token**，对额度紧张的账号是 2.8 倍浪费。因此默认 `LLM_ENABLE_THINKING=false`（`llm_client._payload` 里只在「要关」时才下发该字段，换到不支持它的非 Qwen3 模型不会 400）。开启后闲聊 13.7s→1.7s、业务问句 31.0s→2.1s。

另一个坑是**结论截断与全量合计打架**：`order_agent._order_findings` 原本写死 `by_platform[:4]`，问「各平台销售额」时天猫被截掉，下游大模型还忠实地照抄了这个残缺结论 —— 于是正文列 4 个平台、却同时报「全平台合计」，一加就对不上账。现在改为：**问题本身就是该维度拆分时（`slots.breakdowns` 命中）逐行给全**，只有概览场景才取前几名并标注「共 N 个平台」。

排这个问题时还挖出一个更隐蔽的：`summary["by_platform"]` 存在**同一个 key 两种形状** —— 「未请求该维度」时由固定查询产出（列名 `cnt`），「请求了该维度」时被 `extra_breakdowns` 覆盖（列名 `order_count`）。下游按 `cnt` 取值就静默拿到 `None`，结论里渲染出「抖店 None 单」。已在 repository 侧补齐列名别名，并让 agent 侧用 `_row_count()` 兼容读取。

---

## 安全约定

- **真实 API Key 只写在控制台环境变量里**，`.env` 内一律保持占位符 `sk-your-dashscope-api-key`，禁止提交；`.env` 已被 `.gitignore` 忽略。
- 三角色权限矩阵：`admin`（全部 + 配置/写）、`operator`（读 + 有限写）、`customer`（仅读且限自有会话）；**写操作默认全局关闭**。
- 所有 MCP 调用落审计（JSONL + MySQL `mcp_audit` 表），含 `request_id`、工具名、参数摘要、耗时、成败。
- 无密钥时**不编造数据**：生成走模板拼真实查询结果，查不到就明说查不到。
