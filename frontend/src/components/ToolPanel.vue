<script setup>
/**
 * MCP 工具面板：直接把工具层的契约暴露出来，可现场调用。
 *
 * 默认参数取工具自带的 ``examples[0]``（registry 里声明过），
 * 所以「选一个工具 → 直接点调用」就能跑通，不必手写参数。
 * 写类工具（create_ticket 等）会被后端门禁拦下 —— 这本身就是要验证的点。
 */
import { computed, onMounted, ref, watch } from 'vue'

import { ApiError, callTool, listTools } from '../api.js'

const props = defineProps({
  token: { type: String, default: '' },
})

const tools = ref([])
const loading = ref(false)
const loadError = ref(null)
const selected = ref(null)
const argsText = ref('')
const running = ref(false)
const result = ref(null)
const callError = ref(null)
const callMs = ref(0)

async function load() {
  loading.value = true
  loadError.value = null
  try {
    const data = await listTools(props.token)
    tools.value = data.tools || []
    if (!selected.value && tools.value.length) pick(tools.value[0])
  } catch (e) {
    loadError.value = e instanceof ApiError ? e : { code: 'NETWORK', message: String(e.message || e) }
    tools.value = []
  } finally {
    loading.value = false
  }
}

function pick(tool) {
  selected.value = tool
  const example = (tool.examples && tool.examples[0]) || {}
  argsText.value = JSON.stringify(example, null, 2)
  result.value = null
  callError.value = null
}

async function run() {
  if (!selected.value || running.value) return
  let args
  try {
    args = argsText.value.trim() ? JSON.parse(argsText.value) : {}
  } catch (e) {
    callError.value = { code: 'INVALID_JSON', message: `参数不是合法 JSON：${e.message}` }
    result.value = null
    return
  }
  running.value = true
  callError.value = null
  result.value = null
  const t0 = performance.now()
  try {
    result.value = await callTool(selected.value.name, args, props.token)
  } catch (e) {
    callError.value = e instanceof ApiError ? e : { code: 'NETWORK', message: String(e.message || e) }
  } finally {
    callMs.value = Math.round(performance.now() - t0)
    running.value = false
  }
}

const prettyResult = computed(() => {
  if (result.value === null || result.value === undefined) return ''
  return JSON.stringify(result.value, null, 2)
})

onMounted(load)
watch(() => props.token, load)
</script>

<template>
  <div>
    <div v-if="loading" class="empty"><span class="spin">◌</span>加载工具清单…</div>

    <div v-else-if="loadError" class="alert alert--err">
      <div class="alert__body">
        <div class="alert__title">{{ loadError.code }} · {{ loadError.message }}</div>
        <div v-if="loadError.requestId" class="alert__detail">request_id: {{ loadError.requestId }}</div>
        <div class="alert__actions">
          <button class="btn btn--sm" @click="load">重试</button>
        </div>
      </div>
    </div>

    <template v-else>
      <div class="section-label">工具清单（{{ tools.length }}）</div>
      <div class="tool-list">
        <div
          v-for="t in tools"
          :key="t.name"
          class="tool"
          :class="{ 'tool--active': selected && selected.name === t.name }"
          @click="pick(t)"
        >
          <div class="tool__top">
            <span class="tool__name">{{ t.name }}</span>
            <span v-if="t.readonly" class="badge badge--ok">只读</span>
            <span v-else class="badge badge--warn">写入 · {{ t.write_kind }}</span>
            <span v-if="t.timeout_s" class="badge badge--muted badge--mono">{{ t.timeout_s }}s</span>
          </div>
          <div class="tool__desc">{{ t.description }}</div>
        </div>
      </div>

      <template v-if="selected">
        <div class="divider" />
        <div class="section-label">调用 {{ selected.name }}</div>
        <textarea v-model="argsText" class="textarea" rows="5" spellcheck="false" />

        <div class="composer__row">
          <button class="btn btn--primary btn--sm" :disabled="running" @click="run">
            <span v-if="running" class="spin">◌</span>{{ running ? '调用中…' : '调用' }}
          </button>
          <span v-if="callMs" class="composer__hint">{{ callMs }} ms</span>
          <span v-if="selected.owner_agent" class="badge badge--muted">归属 {{ selected.owner_agent }}</span>
        </div>

        <div v-if="callError" style="margin-top: 10px" class="alert alert--err">
          <div class="alert__body">
            <div class="alert__title">{{ callError.code }} · {{ callError.message }}</div>
            <div v-if="callError.requestId" class="alert__detail">request_id: {{ callError.requestId }}</div>
          </div>
        </div>

        <template v-if="prettyResult">
          <div class="section-label" style="margin-top: 12px">返回结果</div>
          <div class="pane">{{ prettyResult }}</div>
        </template>
      </template>
    </template>
  </div>
</template>
