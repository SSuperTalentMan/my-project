# 取证归档说明

本目录保存「真跑」原始响应（非构造样例），用于支撑 `../端到端问答全链路示例.md` 里的观测结论。
所有响应均由 `qwen3.7-flash` + 真实 MySQL/Redis 产生，未使用 mock。

## 目录结构

```
docs/evidence/
├── ask_raw.json        # 「上个月抖店退货率最高的商品是什么」—— 修复前基线
├── ask_breakdown.json  # 「上个月各平台的销售额分别是多少」  —— 修复前基线
├── ask_chitchat.json   # 「你好」                          —— 修复前基线
├── ask_rag.json        # 「七天无理由退货的条件是什么」      —— 修复前基线（RAG 分支）
└── fixed/              # 同问句、同数据的「修复后」响应
    ├── ask_raw.json
    ├── ask_breakdown.json
    └── ask_chitchat.json
```

## 两批文件的关系

上面 4 个 `ask_*.json` 是**修复前**的原始响应，它们**正是那三处瑕疵的复现样本**：

| 文件 | 可复现的瑕疵 | 表现 |
|------|--------------|------|
| 4 个文件全部 | 瑕疵 3 · trace 不全 | `trace` 只有 `stage=llm` 与 `stage=node(answer_node)` 两条，而 `node_timings` 有 6 个节点 |
| `ask_raw.json` | 瑕疵 1 · 意图名错 | `plan_reason` 写成「销售分析 → order_analysis_agent」（实为售后分析） |
| `ask_raw.json` | 瑕疵 2 · 回答不完整 | 回答对第 2~5 名写「订单量未显示」 |

`fixed/` 下的同问句响应是**修复后**采集的，三处均已消失（`trace` 15/13/5 条、`plan_reason` 为「售后分析」、回答逐行带「订单量」）。

两批文件**采用完全相同的序列化形态**（即 `POST /api/v1/chat/ask` 的返回体，`_state_to_payload`），
因此可以直接逐字段对照。实测对照结果 —— 除 `trace` / `plan_reason` / `answer`（以及每次运行都会变的
`elapsed_ms` / `node_timings` / `request_id` / `agent_tasks.id`）之外，**其余字段逐字节相同**：

| 问句 | `trace` 条数 | `plan_reason` | 回答含「未显示」 |
|------|--------------|---------------|------------------|
| 上个月抖店退货率最高的商品是什么 | 2 → **15** | 销售分析 → **售后分析** | 是 → **否** |
| 上个月各平台的销售额分别是多少 | 2 → **13** | 销售分析（正确，未变） | 否 → 否 |
| 你好 | 2 → **5** | 闲聊寒暄（未变） | 否 → 否 |

> 这张表本身就是「修复是外科手术式、没有副作用」的证据：业务数据（`table` / `metrics` / `findings` / `intent` / `slots` / `plan`）全部一字未动。


## 为什么 RAG 场景没有「修复后」副本

Milvus Lite 是**单进程独占**（文件锁 `DataDirLockedError`）。采集 `fixed/` 时本机 `:8000` 主服务正在运行并持有 `data/milvus/commercepivot.db`，独立进程重跑 RAG 会静默降级为 MySQL FULLTEXT/LIKE，
那样存下来的就不再是「真实混合检索」的证据了。RAG 链路本身**不受这三处修复影响**——
唯一变化是它的 `trace` 同样会变完整，`ask_rag.json` 里的 citations 与 `retrieval_mode: milvus-hybrid` 依然有效。

## 复现方式

```bash
# 修复前基线所在的服务版本已不可回退；如需重新采集当前版本：
python -m commercepivot ask "上个月抖店退货率最高的商品是什么"
# 含 trace 的 HTTP 版本见 ../端到端问答全链路示例.md §5
```
