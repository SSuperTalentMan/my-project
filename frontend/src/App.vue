<script setup>
/**
 * 商枢 CommercePivot · 经营分析控制台
 *
 * 这个页面的定位是**验证台**，不是演示页：后端每一步的中间产物
 * （意图 / 槽位 / 规划 / A2A 任务 / 节点耗时 / 引用 / 降级原因）都如实摊开，
 * 回答本身反而只是其中一块。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import { ApiError, askOnce, askStream, fetchHealth, fetchVersion, issueToken } from './api.js'
import AnswerCard from './components/AnswerCard.vue'
import TracePanel from './components/TracePanel.vue'

const SAMPLES = [
  '上个月各平台的销售额分别是多少？',
  '上个月抖店退货率最高的商品是什么？',
  '哪些 SKU 快断货了？',
  '上个月巨量引擎的投产比是多少？',
  '七天无理由退货的条件是什么？',
  '你好呀，你是谁？',
]

// ------------------------------------------------------------ 状态
const token = ref('')
const tokenInfo = ref(null)
const tokenError = ref(null)
const role = ref('admin')
const subject = ref('admin')

const question = ref('')
const streamMode = ref(true)
const includeTrace = ref(false)

// 是否在缺令牌时自动补签。页面首次加载会补签以省去手点；
// 但用户主动「清除令牌」后必须关掉 —— 否则他要验证的 401 分支永远走不到。
const autoSign = ref(true)

const turns = ref([])
const activeIdx = ref(-1)
const busy = ref(false)
const sessionId = ref('')

const health = ref(null)
const healthError = ref(null)
const version = ref(null)
let controller = null
let pollTimer = null

const activeTurn = computed(() => turns.value[activeIdx.value] || null)
// 注意：**不**把 token 作为可发送的前提 —— 无令牌时要让请求真实打到后端，
// 由后端返回 401 并展示出来，这才是对鉴权链路的验证。
const canSend = computed(() => question.value.trim().length > 0 && !busy.value)

const connBadge = computed(() => {
  if (healthError.value) return { cls: 'badge--err', text: '后端未连接' }
  if (!health.value) return { cls: 'badge--muted', text: '连接中…' }
  const deg = health.value.degraded || []
  if (deg.length) return { cls: 'badge--warn', text: `服务正常 · ${deg.length} 项降级` }
  return { cls: 'badge--ok', text: '服务正常' }
})

// ------------------------------------------------------------ 鉴权
async function signToken(silent = false) {
  tokenError.value = null
  autoSign.value = true
  try {
    const data = await issueToken({ subject: subject.value.trim() || role.value, role: role.value })
    token.value = data.access_token
    tokenInfo.value = data
  } catch (e) {
    const err = e instanceof ApiError ? e : { code: 'NETWORK', message: String(e.message || e) }
    token.value = ''
    tokenInfo.value = null
    if (!silent) tokenError.value = err
  }
}

function clearToken() {
  token.value = ''
  tokenInfo.value = null
  sessionId.value = ''
  // 关掉自动补签 —— 用户清令牌就是为了验证 401 拦截，不能替他悄悄签回来
  autoSign.value = false
}

// ------------------------------------------------------------ 探活
async function probeHealth() {
  try {
    health.value = await fetchHealth()
    healthError.value = null
  } catch (e) {
    health.value = null
    healthError.value = e instanceof ApiError ? e : { code: 'NETWORK', message: String(e.message || e) }
  }
}

// ------------------------------------------------------------ 发送
function newTurn(q) {
  return reactive({
    q,
    answer: '',
    source: '',
    meta: {},
    intent: null,
    slots: null,
    missingSlots: [],
    defaultsApplied: [],
    plan: [],
    planReason: '',
    tasks: [],
    findings: [],
    citations: [],
    table: null,
    metrics: {},
    nodeTimings: {},
    degraded: false,
    degradeReasons: [],
    cached: false,
    elapsedMs: 0,
    requestId: '',
    sessionId: '',
    events: [],
    streaming: false,
    error: null,
    t0: performance.now(),
    deltaSeen: false,
  })
}

function pushEvent(turn, event, detail = '') {
  turn.events.push({ event, t: Math.round(performance.now() - turn.t0), detail })
}

function slotsSummary(slots) {
  const parts = Object.entries(slots || {})
    .filter(([, v]) => v !== null && v !== undefined && v !== '' && typeof v !== 'object')
    .map(([k, v]) => `${k}=${v}`)
  const objKeys = Object.keys(slots || {}).filter((k) => typeof slots[k] === 'object' && slots[k] !== null)
  return [...parts, ...objKeys.map((k) => `${k}=(…)`)].join(' ') || '无'
}

/** 处理一个 SSE 事件（流式模式） */
function handleEvent(turn, event, data) {
  switch (event) {
    case 'start':
      turn.sessionId = data?.session_id || ''
      pushEvent(turn, 'start', data?.session_id ? `session ${data.session_id}` : '')
      break

    case 'intent':
      turn.intent = data
      pushEvent(
        turn, 'intent',
        `${data?.primary_name || data?.primary || '—'}${data?.confidence !== undefined ? ` · ${Math.round(Number(data.confidence) * 100)}%` : ''}`,
      )
      break

    case 'slots':
      turn.slots = data?.slots || {}
      turn.missingSlots = data?.missing || []
      turn.defaultsApplied = data?.defaults_applied || []
      pushEvent(turn, 'slots', slotsSummary(turn.slots))
      break

    case 'plan':
      turn.plan = data?.plan || []
      turn.planReason = data?.reason || ''
      pushEvent(turn, 'plan', `${turn.plan.length} 个 Agent${turn.planReason ? ` · ${turn.planReason}` : ''}`)
      break

    case 'agents':
      turn.tasks = data?.tasks || []
      pushEvent(
        turn, 'agents',
        turn.tasks.map((t) => `${t.agent}(${t.status}${t.elapsed_ms ? ` ${t.elapsed_ms}ms` : ''})`).join('、') || '无',
      )
      break

    case 'findings':
      turn.findings = data?.findings || []
      turn.metrics = data?.metrics || {}
      turn.citations = data?.citations || []
      turn.degraded = !!data?.degraded
      turn.degradeReasons = data?.degrade_reasons || []
      pushEvent(
        turn, 'findings',
        `${turn.findings.length} 条结论 · ${turn.citations.length} 条引用${turn.degraded ? ' · 有降级' : ''}`,
      )
      break

    case 'table':
      turn.table = data
      pushEvent(turn, 'table', `${data?.row_count ?? 0} 行 / ${data?.columns?.length ?? 0} 列`)
      break

    case 'answer_delta':
      if (typeof data?.text === 'string') turn.answer += data.text
      if (!turn.deltaSeen) {
        turn.deltaSeen = true
        pushEvent(turn, 'answer_delta', '开始产出回答')
      }
      break

    case 'done': {
      turn.source = data?.answer_source || turn.source
      turn.meta = data?.answer_meta || turn.meta
      turn.elapsedMs = data?.elapsed_ms ?? turn.elapsedMs
      turn.nodeTimings = data?.node_timings || {}
      turn.requestId = data?.request_id || ''
      if (!turn.sessionId) turn.sessionId = data?.session_id || ''
      const st = data?.orchestrator_state || {}
      // done 事件补齐：澄清/闲聊分支不会发 plan，agents 之类的事件
      turn.intent = st.intent || turn.intent
      turn.slots = st.slots || turn.slots
      if (!turn.plan.length && st.plan) turn.plan = st.plan
      turn.planReason = st.plan_reason || turn.planReason
      if (!turn.findings.length && st.findings) turn.findings = st.findings
      if (!turn.citations.length && st.citations) turn.citations = st.citations
      if (!turn.table && st.table) turn.table = st.table
      turn.metrics = st.metrics || turn.metrics
      turn.degraded = st.degraded ?? turn.degraded
      turn.degradeReasons = st.degrade_reasons?.length ? st.degrade_reasons : turn.degradeReasons
      pushEvent(turn, 'done', `总耗时 ${turn.elapsedMs} ms · 来源 ${turn.source}`)
      break
    }

    case 'error':
      turn.error = { code: data?.code || 'STREAM_ERROR', message: data?.message || '流式异常', requestId: data?.request_id }
      pushEvent(turn, 'error', turn.error.message)
      break

    default:
      pushEvent(turn, event, '')
  }
}

/** 一次性返回的完整 payload → turn（字段与流式对齐） */
function applyPayload(turn, d) {
  turn.answer = d.answer || ''
  turn.source = d.answer_source || ''
  turn.meta = d.answer_meta || {}
  turn.intent = d.intent || null
  turn.slots = d.slots || null
  turn.missingSlots = d.missing_slots || []
  turn.defaultsApplied = d.defaults_applied || []
  turn.plan = d.plan || []
  turn.planReason = d.plan_reason || ''
  turn.tasks = d.agent_tasks || []
  turn.findings = d.findings || []
  turn.citations = d.citations || []
  turn.table = d.table || null
  turn.metrics = d.metrics || {}
  turn.nodeTimings = d.node_timings || {}
  turn.degraded = !!d.degraded
  turn.degradeReasons = d.degrade_reasons || []
  turn.cached = !!d.cached
  turn.elapsedMs = d.elapsed_ms || 0
  turn.requestId = d.request_id || ''
  turn.sessionId = d.session_id || ''

  // 一次性模式没有 SSE，用节点耗时还原一条等价的事件流水
  pushEvent(turn, 'start', turn.sessionId ? `session ${turn.sessionId}` : '')
  if (turn.intent) pushEvent(turn, 'intent', turn.intent.primary_name || turn.intent.primary || '')
  pushEvent(turn, 'slots', slotsSummary(turn.slots))
  pushEvent(turn, 'plan', `${turn.plan.length} 个 Agent`)
  if (turn.tasks.length) pushEvent(turn, 'agents', turn.tasks.map((t) => `${t.agent}(${t.status})`).join('、'))
  if (turn.findings.length) pushEvent(turn, 'findings', `${turn.findings.length} 条结论`)
  if (turn.table) pushEvent(turn, 'table', `${turn.table.row_count} 行`)
  pushEvent(turn, 'answer_delta', '一次性返回（非流式）')
  pushEvent(turn, 'done', `总耗时 ${turn.elapsedMs} ms · 来源 ${turn.source}`)
}

async function send() {
  const q = question.value.trim()
  if (!q || busy.value) return
  // 首次使用自动补签；用户主动清过令牌则不再补签，直接让后端拒绝
  if (!token.value && autoSign.value) await signToken(true)

  question.value = ''
  const turn = newTurn(q)
  turns.value.push(turn)
  activeIdx.value = turns.value.length - 1
  busy.value = true
  turn.streaming = true
  controller = new AbortController()

  const payload = { question: q, use_cache: true }
  if (sessionId.value) payload.session_id = sessionId.value
  if (!streamMode.value) payload.include_trace = includeTrace.value

  try {
    if (streamMode.value) {
      await askStream(payload, token.value, (event, data) => handleEvent(turn, event, data), controller.signal)
    } else {
      applyPayload(turn, await askOnce(payload, token.value, controller.signal))
    }
    if (turn.sessionId) sessionId.value = turn.sessionId
  } catch (e) {
    if (e?.name === 'AbortError') {
      pushEvent(turn, 'error', '客户端已中止')
    } else {
      turn.error = e instanceof ApiError ? e : { code: 'NETWORK', message: String(e.message || e) }
    }
  } finally {
    turn.streaming = false
    busy.value = false
    controller = null
  }
}

function stop() {
  controller?.abort()
}

function askSample(q) {
  question.value = q
  send()
}

function clearFeed() {
  turns.value = []
  activeIdx.value = -1
  sessionId.value = ''
}

function onKeydown(e) {
  // Enter 发送，Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}

onMounted(async () => {
  await probeHealth()
  fetchVersion().then((d) => (version.value = d)).catch(() => {})
  await signToken(true)
  pollTimer = setInterval(probeHealth, 20000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  controller?.abort()
})
</script>

<template>
  <div class="app">
    <!-- ================= 顶栏 ================= -->
    <header class="topbar">
      <div class="topbar__brand">
        <!-- 内联 SVG 而非 <img src="/favicon.svg">：SFC 模板里的图片 src 会被
             Vite 当作模块导入去解析，而该路径由 dev server 代理到后端，构建期找不到文件。
             图标本身只有几百字节，内联同时省掉一次请求。 -->
        <svg class="topbar__logo" viewBox="0 0 64 64" aria-hidden="true">
          <rect width="64" height="64" rx="14" fill="#1f4e79" />
          <path
            d="M13 45 L26 30 L36 38 L51 19"
            fill="none"
            stroke="#ffffff"
            stroke-width="5.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <circle cx="51" cy="19" r="5.5" fill="#ff9f2e" />
        </svg>
        <div>
          <div class="topbar__title">
            商枢 CommercePivot<span class="topbar__ver">控制台 v{{ version?.version || '1.2.0' }}</span>
          </div>
        </div>
      </div>

      <span class="badge" :class="connBadge.cls">
        <i class="dot" :class="{ 'dot--pulse': !health && !healthError }" />{{ connBadge.text }}
      </span>
      <span v-if="health" class="badge badge--muted badge--mono">
        工具 {{ health.mcp?.tool_count ?? '—' }} · Agent {{ health.a2a?.agent_count ?? '—' }}
      </span>

      <div class="topbar__spacer" />

      <div class="topbar__group">
        <span class="topbar__label">角色</span>
        <select v-model="role" class="select" style="width: 108px" @change="signToken(true)">
          <option value="admin">admin</option>
          <option value="operator">operator</option>
          <option value="customer">customer</option>
        </select>
        <input v-model="subject" class="input" style="width: 118px" placeholder="subject" @keydown.enter="signToken()" />
        <button v-if="!token" class="btn btn--primary btn--sm" @click="signToken()">签发 JWT</button>
        <template v-else>
          <span class="badge badge--ok"><i class="dot" />已签发 · {{ tokenInfo?.role }}</span>
          <button class="btn btn--sm" @click="clearToken" title="清空令牌后可验证 401 分支">清除令牌</button>
        </template>
      </div>
    </header>

    <div v-if="tokenError" style="padding: 12px 22px 0">
      <div class="alert alert--err">
        <div class="alert__body">
          <div class="alert__title">{{ tokenError.code }} · {{ tokenError.message }}</div>
          <div v-if="tokenError.requestId" class="alert__detail">request_id: {{ tokenError.requestId }}</div>
        </div>
      </div>
    </div>

    <!-- ================= 主体 ================= -->
    <div class="layout">
      <!-- 左：对话 -->
      <div class="col col--main">
        <div v-if="!turns.length" class="card">
          <div class="card__head">
            <span class="card__title">开始验证</span>
            <span class="card__sub">点下面的示例问题，或直接输入</span>
          </div>
          <div class="card__body">
            <div class="chips">
              <button v-for="s in SAMPLES" :key="s" class="btn btn--sm" @click="askSample(s)">{{ s }}</button>
            </div>
            <div class="divider" />
            <div class="kv">
              <div class="kv__k">流式</div>
              <div class="kv__v">SSE 逐事件返回（POST + fetch 流解析，后端非 GET，原生 EventSource 不适用）</div>
              <div class="kv__k">右侧</div>
              <div class="kv__v">意图 / 槽位 / 规划 / A2A 任务 / 节点耗时 —— 每一步都摊开</div>
              <div class="kv__k">工具</div>
              <div class="kv__v">「MCP 工具」页签可现场调用 8 个工具，含写类门禁的拒绝验证</div>
            </div>
          </div>
        </div>

        <div class="feed">
          <div v-for="(t, i) in turns" :key="i">
            <div class="turn--user">
              <div class="bubble">{{ t.q }}</div>
            </div>
            <div style="margin-top: 10px">
              <AnswerCard :turn="t" />
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="composer">
          <div class="composer__box">
            <textarea
              v-model="question"
              class="composer__input"
              rows="2"
              placeholder="问点什么，例如：上个月各平台的销售额分别是多少？"
              @keydown="onKeydown"
            />
            <div class="composer__row">
              <button class="btn btn--primary" :disabled="!canSend" @click="send">
                <span v-if="busy" class="spin">◌</span>{{ busy ? '处理中…' : '发送' }}
              </button>
              <button v-if="busy" class="btn btn--danger btn--sm" @click="stop">中止</button>
              <label class="switch">
                <input v-model="streamMode" type="checkbox" />流式 SSE
              </label>
              <label v-if="!streamMode" class="switch">
                <input v-model="includeTrace" type="checkbox" />回传 trace
              </label>
              <div class="card__spacer" />
              <button v-if="turns.length" class="btn btn--sm btn--ghost" @click="clearFeed">清空对话</button>
              <span class="composer__hint">Enter 发送 · Shift+Enter 换行</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 右：链路 -->
      <div class="col col--side">
        <TracePanel :turn="activeTurn" :token="token" />
      </div>
    </div>
  </div>
</template>
