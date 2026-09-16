<script setup>
/**
 * 单轮回答卡片：正文 + 元信息 + 表格 + 结论 + 引用。
 * 元信息刻意全部暴露出来 —— 这正是要"验证"的东西：
 * answer_source（走了哪条链路）、generator（谁写的字）、model、耗时、降级原因。
 */
import { computed, ref } from 'vue'

import DataTable from './DataTable.vue'

const props = defineProps({
  turn: { type: Object, required: true },
})

const copied = ref(false)

const SOURCE = {
  llm: { text: 'LLM 生成', cls: 'badge--ok' },
  template: { text: '模板降级', cls: 'badge--warn' },
  clarification: { text: '追问澄清', cls: 'badge--info' },
  chitchat: { text: '闲聊直答', cls: 'badge--accent' },
}

const sourceBadge = computed(() => SOURCE[props.turn.source] || { text: props.turn.source || '—', cls: 'badge--muted' })
const meta = computed(() => props.turn.meta || {})
const generator = computed(() => meta.value.generator || '')

const elapsed = computed(() => {
  const ms = props.turn.elapsedMs
  if (!ms) return ''
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`
})

async function copyAnswer() {
  try {
    await navigator.clipboard.writeText(props.turn.answer || '')
    copied.value = true
    setTimeout(() => (copied.value = false), 1400)
  } catch {
    /* 剪贴板不可用（非 https / 无权限），忽略 */
  }
}
</script>

<template>
  <div class="card">
    <div class="card__head">
      <span class="badge" :class="sourceBadge.cls">
        <i class="dot" />{{ sourceBadge.text }}
      </span>
      <span v-if="generator" class="badge badge--muted">
        生成者 {{ generator === 'llm' ? '大模型' : '模板' }}
      </span>
      <span v-if="meta.model" class="badge badge--muted badge--mono">{{ meta.model }}</span>
      <span v-if="elapsed" class="badge badge--muted badge--mono">{{ elapsed }}</span>
      <span v-if="turn.cached" class="badge badge--info">缓存命中</span>
      <span v-if="turn.degraded" class="badge badge--warn">降级</span>
      <span v-if="turn.streaming" class="badge badge--info"><i class="dot dot--pulse" />流式接收中</span>
      <div class="card__spacer" />
      <button class="btn btn--sm btn--ghost" :disabled="!turn.answer" @click="copyAnswer">
        {{ copied ? '已复制' : '复制' }}
      </button>
    </div>

    <div class="card__body">
      <!-- 错误分支：后端结构化错误（401/403/429/422…） -->
      <div v-if="turn.error" class="alert alert--err">
        <div class="alert__body">
          <div class="alert__title">{{ turn.error.code }} · {{ turn.error.message }}</div>
          <div v-if="turn.error.requestId" class="alert__detail">request_id: {{ turn.error.requestId }}</div>
          <div v-if="turn.error.status" class="alert__detail">HTTP {{ turn.error.status }}</div>
        </div>
      </div>

      <template v-else>
        <div v-if="turn.answer" class="answer__text">{{ turn.answer }}<span v-if="turn.streaming" class="answer__cursor" /></div>
        <div v-else-if="turn.streaming" class="empty"><span class="spin">◌</span>正在处理…</div>
        <div v-else class="empty">（无回答内容）</div>
      </template>

      <!-- 数据表格 -->
      <template v-if="turn.table">
        <div class="divider" />
        <div class="section-label">
          {{ turn.table.title || '数据明细' }}
          <span class="faint">
            · {{ turn.table.row_count }} 行<span v-if="turn.table.truncated">（已截断展示前 {{ turn.table.rows.length }} 行）</span>
          </span>
        </div>
        <DataTable :table="turn.table" />
      </template>

      <!-- 关键结论 -->
      <template v-if="turn.findings && turn.findings.length">
        <div class="divider" />
        <div class="section-label">关键结论（Agent 产出）</div>
        <ul class="findings">
          <li v-for="(f, i) in turn.findings" :key="i">{{ f }}</li>
        </ul>
      </template>

      <!-- 引用 -->
      <template v-if="turn.citations && turn.citations.length">
        <div class="divider" />
        <div class="section-label">知识库引用（{{ turn.citations.length }}）</div>
        <div class="citations">
          <div v-for="(c, i) in turn.citations" :key="i" class="citation">
            <div class="citation__top">
              <span class="badge badge--info badge--mono">{{ c.doc_id || c.source || `#${i + 1}` }}</span>
              <span class="citation__title">{{ c.title || c.question || '知识条目' }}</span>
              <span v-if="c.score !== undefined && c.score !== null" class="badge badge--muted badge--mono">
                score {{ Number(c.score).toFixed(3) }}
              </span>
            </div>
            <div v-if="c.content || c.answer" class="citation__body">{{ c.content || c.answer }}</div>
          </div>
        </div>
      </template>

      <!-- 降级原因 -->
      <template v-if="turn.degradeReasons && turn.degradeReasons.length">
        <div class="divider" />
        <div class="alert alert--warn">
          <div class="alert__body">
            <div class="alert__title">降级说明（结果仍可用，但请知悉）</div>
            <div v-for="(r, i) in turn.degradeReasons" :key="i" class="alert__detail">{{ r }}</div>
          </div>
        </div>
      </template>

      <!-- 追溯信息 -->
      <template v-if="turn.requestId || turn.sessionId">
        <div class="divider" />
        <div class="chips">
          <span v-if="turn.requestId" class="chip chip--plain mono">req {{ turn.requestId }}</span>
          <span v-if="turn.sessionId" class="chip chip--plain mono">session {{ turn.sessionId }}</span>
        </div>
      </template>
    </div>
  </div>
</template>
