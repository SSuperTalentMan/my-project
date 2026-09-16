<script setup>
/**
 * 数据表格：直接消费后端 ``table`` 字段
 * ``{title, kind, columns:[{key,label}], rows:[], row_count, truncated}``
 *
 * 数字列右对齐并用等宽字体（表格数字纵向可比对），金额加 ¥ 与千分位。
 * 列类型是**推断**的：后端只给 key/label，不给类型。
 */
import { computed } from 'vue'

const props = defineProps({
  table: { type: Object, required: true },
})

const MONEY_KEYS = new Set(['gmv', 'amount', 'refund_amount', 'revenue', 'cost', 'price'])

const columns = computed(() => props.table.columns || [])
const rows = computed(() => props.table.rows || [])

function numericKeys() {
  const out = new Set()
  for (const col of columns.value) {
    const vals = rows.value
      .slice(0, 30)
      .map((r) => r[col.key])
      .filter((v) => v !== null && v !== undefined && v !== '')
    if (vals.length && vals.every((v) => typeof v === 'number' || /^-?\d+(\.\d+)?$/.test(String(v)))) {
      out.add(col.key)
    }
  }
  return out
}

const numKeys = computed(numericKeys)

function isPercent(col) {
  return /率|%|比/.test(col.label || '') || ['return_rate', 'ctr'].includes(col.key)
}

function cell(col, value) {
  if (value === null || value === undefined || value === '') return '—'
  if (!numKeys.value.has(col.key)) return String(value)

  const n = Number(value)
  if (!Number.isFinite(n)) return String(value)

  if (MONEY_KEYS.has(col.key)) {
    return `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }
  if (isPercent(col)) return n.toFixed(2)
  if (col.key === 'roi') return n.toFixed(2)
  if (Number.isInteger(n)) return n.toLocaleString('zh-CN')
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
</script>

<template>
  <div class="dtable-wrap">
    <table class="dtable">
      <thead>
        <tr>
          <th v-for="col in columns" :key="col.key">{{ col.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, i) in rows" :key="i">
          <td
            v-for="col in columns"
            :key="col.key"
            :class="numKeys.has(col.key) ? 'num' : 'str'"
          >
            {{ cell(col, row[col.key]) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
