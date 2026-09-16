<script setup>
/**
 * 右侧链路面板：概览 / 事件流水 / MCP 工具。
 * 把编排层每一步的中间产物都摊开 —— 这是"真实链路"最直接的证据。
 */
import { computed, ref } from 'vue'

import ToolPanel from './ToolPanel.vue'

const props = defineProps({
  turn: { type: Object, default: null },
  token: { type: String, default: '' },
})

const tab = ref('overview')

const EVENT_LABEL = {
  start: '受理请求',
  intent: '意图识别',
  slots: '槽位抽取',
  plan: '任务规划',
  agents: 'A2A 子 Agent 执行',
  findings: '结果聚合',
  table: '表格生成',
  answer_delta: '回答生成',
  done: '链路完成',
  error: '异常',
}

const intent = computed(() => props.turn?.intent || null)
const slots = computed(() => props.turn?.slots || null)
const plan = computed(() => props.turn?.plan || [])
const tasks = computed(() => props.turn?.tasks || [])
const timings = computed(() => props.turn?.nodeTimings || {})
const metrics = computed(() => props.turn?.metrics || {})
const events = computed(() => props.turn?.events || [])

const confidencePct = computed(() => {
  const c = intent.value?.confidence
  return c === undefined || c === null ? null : Math.round(Number(c) * 100)
})

/** 节点耗时条形图：以最大耗时归一化 */
const timingRows = computed(() => {
  const entries = Object.entries(timings.value).filter(([, v]) => typeof v === 'number')
  if (!entries.length) return []
  const max = Math.max(...entries.map(([, v]) => v)) || 1
  return entries
    .sort((a, b) => b[1] - a[1])
    .map(([name, ms]) => ({ name, ms, pct: Math.max(3, Math.round((ms / max) * 100)) }))
})

const metricRows = computed(() =>
  Object.entries(metrics.value).filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object'),
)

function pretty(value) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function slotLabel(k) {
  const MAP = {
    platform: '平台', metric: '指标', period: '时间范围', category: '类目',
    product: '商品', region: '地区', ad_channel: '广告渠道', top_k: '条数',
    order_by: '排序', dims: '维度', breakdowns: '拆分维度',
  }
  return MAP[k] || k
}
</script>

<template>
  <div class="card">
    <div class="card__head" style="padding: 10px 12px">
      <div class="tabs" style="flex: 1">
        <button class="tab" :class="{ 'tab--active': tab === 'overview' }" @click="tab = 'overview'">链路概览</button>
        <button class="tab" :class="{ 'tab--active': tab === 'events' }" @click="tab = 'events'">
          事件流水<span v-if="events.length" class="faint"> {{ events.length }}</span>
        </button>
        <button class="tab" :class="{ 'tab--active': tab === 'tools' }" @click="tab = 'tools'">MCP 工具</button>
      </div>
    </div>

    <!-- ==================== 概览 ==================== -->
    <div v-if="tab === 'overview'" class="card__body card__body--tight">
      <div v-if="!turn" class="empty">
        <span class="empty__icon">◇</span>
        提问后此处显示编排链路的中间产物
      </div>

      <template v-else>
        <!-- 意图 -->
        <div class="section-label">意图识别</div>
        <div v-if="intent" class="card__body" style="padding: 0">
          <div class="trace__head">
            <span class="badge badge--info">{{ intent.primary_name || intent.primary }}</span>
            <span class="badge badge--muted badge--mono">{{ intent.primary }}</span>
            <span v-if="intent.source" class="badge badge--muted">{{ intent.source === 'rule' ? '规则引擎' : intent.source }}</span>
            <span v-if="confidencePct !== null" class="badge badge--muted badge--mono">{{ confidencePct }}%</span>
          </div>
          <div v-if="intent.domain" class="trace__detail">领域：{{ intent.domain }}</div>
          <div v-if="intent.agents && intent.agents.length" class="chips" style="margin-top: 7px">
            <span v-for="a in intent.agents" :key="a" class="chip">{{ a }}</span>
          </div>
          <div v-if="intent.matched && intent.matched.length" class="chips" style="margin-top: 6px">
            <span v-for="m in intent.matched" :key="m" class="chip chip--plain">命中 {{ m }}</span>
          </div>
        </div>
        <div v-else class="muted" style="font-size: 12.5px">—</div>

        <!-- 槽位 -->
        <div class="divider" />
        <div class="section-label">槽位抽取</div>
        <div v-if="slots && Object.keys(slots).length" class="kv">
          <template v-for="(v, k) in slots" :key="k">
            <div class="kv__k">{{ slotLabel(k) }}</div>
            <div class="kv__v mono">{{ pretty(v) }}</div>
          </template>
        </div>
        <div v-else class="muted" style="font-size: 12.5px">无槽位（闲聊 / 澄清短路）</div>
        <div v-if="turn.missingSlots && turn.missingSlots.length" class="chips" style="margin-top: 8px">
          <span v-for="m in turn.missingSlots" :key="m" class="chip chip--warn">缺 {{ slotLabel(m) }}</span>
        </div>
        <div v-if="turn.defaultsApplied && turn.defaultsApplied.length" class="chips" style="margin-top: 6px">
          <span v-for="d in turn.defaultsApplied" :key="d" class="chip chip--plain">默认 {{ d }}</span>
        </div>

        <!-- 计划 -->
        <div class="divider" />
        <div class="section-label">任务规划（{{ plan.length }} 个 Agent）</div>
        <div v-if="plan.length" class="tool-list">
          <div v-for="(p, i) in plan" :key="i" class="tool" style="cursor: default">
            <div class="tool__top">
              <span class="tool__name">{{ p.agent }}</span>
              <span v-if="p.intent" class="badge badge--muted badge--mono">{{ p.intent }}</span>
            </div>
            <div v-if="p.focus" class="tool__desc">focus: {{ p.focus }}</div>
          </div>
        </div>
        <div v-else class="muted" style="font-size: 12.5px">未规划（闲聊直答 / 信息不足追问）</div>
        <div v-if="turn.planReason" class="trace__detail" style="margin-top: 7px">{{ turn.planReason }}</div>

        <!-- 子 Agent 执行 -->
        <template v-if="tasks.length">
          <div class="divider" />
          <div class="section-label">A2A 子 Agent 执行</div>
          <div class="trace">
            <div v-for="(t, i) in tasks" :key="i" class="trace__step">
              <div class="trace__rail">
                <span class="trace__dot" :class="t.status === 'completed' ? 'trace__dot--done' : t.status === 'failed' ? 'trace__dot--err' : ''" />
                <span v-if="i < tasks.length - 1" class="trace__line" />
              </div>
              <div class="trace__body">
                <div class="trace__head">
                  <span class="trace__name">{{ t.agent }}</span>
                  <span class="badge" :class="t.status === 'completed' ? 'badge--ok' : 'badge--err'">{{ t.status }}</span>
                  <span v-if="t.elapsed_ms" class="trace__time">{{ t.elapsed_ms }} ms</span>
                </div>
                <div v-if="t.error" class="trace__detail" style="color: var(--err)">{{ t.error }}</div>
              </div>
            </div>
          </div>
        </template>

        <!-- 节点耗时 -->
        <template v-if="timingRows.length">
          <div class="divider" />
          <div class="section-label">编排节点耗时</div>
          <div class="bars">
            <div v-for="row in timingRows" :key="row.name">
              <div class="bar__top">
                <span class="bar__name">{{ row.name }}</span>
                <span class="bar__val">{{ row.ms }} ms</span>
              </div>
              <div class="bar__track"><div class="bar__fill" :style="{ width: row.pct + '%' }" /></div>
            </div>
          </div>
        </template>

        <!-- 指标 -->
        <template v-if="metricRows.length">
          <div class="divider" />
          <div class="section-label">业务指标</div>
          <div class="kv">
            <template v-for="([k, v]) in metricRows" :key="k">
              <div class="kv__k">{{ k }}</div>
              <div class="kv__v mono">{{ pretty(v) }}</div>
            </template>
          </div>
        </template>
      </template>
    </div>

    <!-- ==================== 事件流水 ==================== -->
    <div v-else-if="tab === 'events'" class="card__body card__body--tight">
      <div v-if="!events.length" class="empty">
        <span class="empty__icon">≡</span>
        SSE 事件会按到达顺序实时出现在这里
      </div>
      <div v-else class="trace">
        <div v-for="(e, i) in events" :key="i" class="trace__step">
          <div class="trace__rail">
            <span
              class="trace__dot"
              :class="e.event === 'done' ? 'trace__dot--done' : e.event === 'error' ? 'trace__dot--err' : ''"
            />
            <span v-if="i < events.length - 1" class="trace__line" />
          </div>
          <div class="trace__body">
            <div class="trace__head">
              <span class="trace__name">{{ EVENT_LABEL[e.event] || e.event }}</span>
              <span class="badge badge--muted badge--mono">{{ e.event }}</span>
              <span class="trace__time">+{{ e.t }} ms</span>
            </div>
            <div v-if="e.detail" class="trace__detail">{{ e.detail }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== 工具面板 ==================== -->
    <div v-else class="card__body card__body--tight">
      <ToolPanel :token="token" />
    </div>
  </div>
</template>
