/**
 * 后端 API 封装。
 *
 * 全部请求走相对路径 —— 由 Vite dev server 代理到后端（见 vite.config.js），
 * 因此开发期完全不涉及 CORS。
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/** 后端统一错误体：{code, message, request_id, details} */
export class ApiError extends Error {
  constructor(status, body) {
    const d = body && typeof body === 'object' ? body : {}
    super(d.message || `请求失败（HTTP ${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = d.code || `HTTP_${status}`
    this.requestId = d.request_id || ''
    this.details = d.details || {}
  }
}

async function toApiError(res) {
  let body = null
  const text = await res.text().catch(() => '')
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = { message: text.slice(0, 300) }
    }
  }
  return new ApiError(res.status, body)
}

function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function request(path, { method = 'GET', body, token, signal } = {}) {
  const res = await fetch(path, {
    method,
    headers: { ...(body ? JSON_HEADERS : {}), ...authHeaders(token) },
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  if (!res.ok) throw await toApiError(res)
  return res.json()
}

// ----------------------------------------------------------------- 接口
export function issueToken({ subject, role, expiresMinutes }) {
  const body = { subject, role }
  if (expiresMinutes) body.expires_minutes = expiresMinutes
  return request('/api/v1/auth/token', { method: 'POST', body })
}

export function askOnce(payload, token, signal) {
  return request('/api/v1/chat/ask', { method: 'POST', body: payload, token, signal })
}

export function fetchHealth() {
  return request('/health')
}

export function fetchVersion() {
  return request('/version')
}

export function listTools(token) {
  return request('/api/v1/mcp/tools/list', { token })
}

export function callTool(name, args, token, signal) {
  return request('/api/v1/mcp/tools/call', {
    method: 'POST',
    body: { tool: name, params: args },
    token,
    signal,
  })
}

export function fetchSession(sessionId, token) {
  return request(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`, { token })
}

// ----------------------------------------------------------------- SSE
/**
 * 解析一个 SSE 事件块。
 * 返回 null 表示该块没有 data（例如 sse_starlette 每 15s 发的 `: ping` 心跳）。
 */
export function parseBlock(raw) {
  let event = 'message'
  const dataLines = []
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue // 注释 / 心跳
    const i = line.indexOf(':')
    const field = i === -1 ? line : line.slice(0, i)
    let value = i === -1 ? '' : line.slice(i + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
  }
  if (!dataLines.length) return null
  const data = dataLines.join('\n')
  let parsed = null
  try {
    parsed = JSON.parse(data)
  } catch {
    parsed = { raw: data }
  }
  return { event, data: parsed }
}

/**
 * 消费一个 SSE 响应流，把每个事件回调出去。
 *
 * 抽成独立函数（而不是内联在 askStream 里）有实际好处：
 * 它只依赖标准 `Response`，因此可以在 Node 里拿真实后端响应直接跑，
 * 无需浏览器 —— SSE 解析是最容易写错、也最难在浏览器里调试的一段。
 *
 * @param response 已 fetch 到、且 status 为 ok 的 Response
 * @param onEvent (eventName, payload) => void
 */
export async function consumeSse(response, onEvent) {
  if (!response.body) throw new Error('当前环境不支持流式响应')

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  const SEP = /\r?\n\r?\n/ // 兼容 \n\n 与 \r\n\r\n
  let buffer = ''

  const drain = () => {
    let m
    while ((m = SEP.exec(buffer))) {
      const raw = buffer.slice(0, m.index)
      buffer = buffer.slice(m.index + m[0].length)
      const evt = parseBlock(raw)
      if (evt) onEvent(evt.event, evt.data)
    }
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      drain()
    }
    buffer += decoder.decode() // flush 残留字节
    drain()
    const tail = parseBlock(buffer)
    if (tail) onEvent(tail.event, tail.data)
  } finally {
    try {
      reader.releaseLock()
    } catch {
      /* 流已关闭，忽略 */
    }
  }
}

/**
 * 流式问答。
 *
 * 注意：后端是 **POST + SSE**，浏览器原生 `EventSource` 只支持 GET，
 * 所以这里用 fetch + ReadableStream 手动解析 `event:` / `data:` 帧。
 *
 * @param onEvent (eventName, payload) => void
 */
export async function askStream(payload, token, onEvent, signal) {
  const res = await fetch('/api/v1/chat/ask_stream', {
    method: 'POST',
    headers: { ...JSON_HEADERS, ...authHeaders(token) },
    body: JSON.stringify(payload),
    signal,
  })
  if (!res.ok) throw await toApiError(res)
  await consumeSse(res, onEvent)
}
