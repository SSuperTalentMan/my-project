/**
 * 前端 SSE 解析层回归验证。
 *
 * 直接复用 src/api.js 里的 consumeSse / parseBlock —— 同一份代码，
 * 既在浏览器里跑，也在这里对着真实后端跑。这样"浏览器里才暴露的解析 bug"
 * 在命令行就能先一步发现，不必等打开页面。
 *
 * 前提：后端已在运行（默认 http://127.0.0.1:8000）。
 * 用法：node verify-sse.mjs      （可用 API_BASE 覆盖后端地址）
 */
import { consumeSse, parseBlock } from './src/api.js'

const BASE = process.env.API_BASE || 'http://127.0.0.1:8000'
let pass = 0
let fail = 0

function ok(label, cond, detail = '') {
  if (cond) {
    pass += 1
    console.log(`  [✓] ${label}${detail ? '  ' + detail : ''}`)
  } else {
    fail += 1
    console.log(`  [✗] ${label}${detail ? '  ' + detail : ''}`)
  }
}

/** 把若干字符串分片包装成 Response，用于模拟任意 chunk 边界 */
function chunked(chunks) {
  const enc = new TextEncoder()
  return new Response(
    new ReadableStream({
      start(c) {
        for (const ch of chunks) c.enqueue(enc.encode(ch))
        c.close()
      },
    }),
  )
}

console.log('========== 1. 解析边界样本（chunk 任意切断） ==========')

// 把 \r\n\r\n 从中间劈开，并按字节切断 JSON
const frags = ['event: a\r\ndata: {"x":1}\r\n\r', '\nevent: b\r\ndata: {"y":', '2}\r\n\r\n']
const got = []
await consumeSse(chunked(frags), (e, d) => got.push([e, d]))
ok('\\r\\n 分隔符跨 chunk 仍能切分', got.length === 2, JSON.stringify(got))
ok('事件名正确', got[0]?.[0] === 'a' && got[1]?.[0] === 'b')
ok('JSON 跨 chunk 仍能拼回', got[0]?.[1]?.x === 1 && got[1]?.[1]?.y === 2)

// 心跳注释（sse_starlette 每 15s 发 `: ping`）必须被忽略、且不产生事件
const pinged = []
await consumeSse(chunked([': ping\n\n', 'event: c\ndata: {"ok":true}\n\n', ': ping\n\n']), (e, d) => pinged.push(e))
ok('注释心跳不产生事件', pinged.length === 1 && pinged[0] === 'c', JSON.stringify(pinged))

// 多行 data 应按规范用 \n 拼接
ok('多行 data 拼接', parseBlock('event: m\ndata: line1\ndata: line2')?.data?.raw === 'line1\nline2')

// 无 event 字段时默认 message
ok('缺省事件名为 message', parseBlock('data: {"k":1}')?.event === 'message')

// 非 JSON 载荷不抛异常
ok('非 JSON 载荷降级为 raw', parseBlock('data: hello')?.data?.raw === 'hello')

// 空块返回 null
ok('空块返回 null', parseBlock('') === null)

console.log('\n========== 2. 对真实后端跑同一条解析链路 ==========')

const tokenRes = await fetch(`${BASE}/api/v1/auth/token`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ subject: 'admin', role: 'admin' }),
})
const { access_token: token } = await tokenRes.json()
ok('签发 JWT', !!token, `len=${token?.length}`)

async function streamAsk(question) {
  const res = await fetch(`${BASE}/api/v1/chat/ask_stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ question }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const events = []
  await consumeSse(res, (event, data) => events.push({ event, data }))
  return events
}

const t0 = Date.now()
const events = await streamAsk('上个月各平台的销售额分别是多少？')
const order = events.map((e) => e.event)
console.log(`  事件序列: ${order.join(' → ')}`)

const REQUIRED = ['start', 'intent', 'slots', 'plan', 'agents', 'findings', 'table', 'answer_delta', 'done']
ok('关键事件齐全', REQUIRED.every((n) => order.includes(n)), `缺 ${REQUIRED.filter((n) => !order.includes(n))}`)
ok('事件顺序符合编排流程', order.indexOf('intent') < order.indexOf('slots') && order.indexOf('slots') < order.indexOf('done'))

const intent = events.find((e) => e.event === 'intent')?.data
ok('intent 可解析', !!intent?.primary, `primary=${intent?.primary} conf=${intent?.confidence}`)

const table = events.find((e) => e.event === 'table')?.data
ok('table 可解析', !!table?.columns?.length && !!table?.rows?.length, `${table?.row_count} 行 / ${table?.columns?.length} 列`)

const deltas = events.filter((e) => e.event === 'answer_delta').map((e) => e.data?.text || '')
const done = events.find((e) => e.event === 'done')?.data
ok('answer_delta 可拼接出回答', deltas.join('').length > 0, `${deltas.length} 个增量 / ${deltas.join('').length} 字`)
ok('done 带 answer_source', !!done?.answer_source, `source=${done?.answer_source} model=${done?.answer_meta?.model} ${done?.elapsed_ms}ms`)
ok('done 带节点耗时', Object.keys(done?.node_timings || {}).length > 0, Object.keys(done?.node_timings || {}).join(','))
console.log(`  真实流式往返耗时: ${Date.now() - t0} ms`)

// 闲聊：应短路、无 table / agents
const chat = (await streamAsk('你好呀，你是谁？')).map((e) => e.event)
ok('闲聊不产生 agents / table 事件', !chat.includes('agents') && !chat.includes('table'), chat.join(' → '))

// 错误分支：无令牌
const bad = await fetch(`${BASE}/api/v1/chat/ask`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ question: 'x' }),
})
const badBody = await bad.json()
ok('无令牌返回 401 且错误体可解析', bad.status === 401 && !!badBody.code, `HTTP ${bad.status} code=${badBody.code}`)

console.log(`\n合计：PASS ${pass}、FAIL ${fail}`)
process.exit(fail ? 1 : 0)
