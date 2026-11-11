<script setup>
import { computed, inject } from 'vue'
import StageFrame from '../StageFrame.vue'

defineProps({ stage: { type: String, required: true } })
const run = inject('run')
const data = computed(() => run.state.data.profiling || {})
// {table: {check_id: [metrics, ...]}}
const byTable = computed(() => {
  const out = {}
  for (const r of data.value.profiling_results || []) {
    ((out[r.table_name] ??= {})[r.check_id] ??= []).push(r.metrics)
  }
  return out
})
const columnsOf = (rows) => Object.keys(rows[0] || {})
const cell = (v) => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : v)
</script>

<template>
  <StageFrame :stage="stage">
    <p class="notice">These metrics (counts and up to 8 frequent values per check) are sent to Gemini in the Proposed tests stage.</p>
    <template v-if="(data.errors || []).length">
      <h2 class="group-title">Queries that failed</h2>
      <ul><li v-for="(e, i) in data.errors" :key="i"><code>{{ e.check_id }}</code>: {{ e.error }}</li></ul>
    </template>
    <details v-if="data.sql"><summary>SQL that ran (read-only)</summary><pre>{{ data.sql }}</pre></details>
    <div v-for="(checks, table) in byTable" :key="table" class="item">
      <h3>{{ table }}</h3>
      <div v-for="(rows, check) in checks" :key="check">
        <p class="muted check-id"><code>{{ check }}</code></p>
        <dl v-if="rows.length === 1" class="metrics">
          <div v-for="c in columnsOf(rows)" :key="c"><dt>{{ c.toLowerCase() }}</dt><dd>{{ cell(rows[0][c]) }}</dd></div>
        </dl>
        <div v-else class="table-scroll"><table class="grid">
          <thead><tr><th v-for="c in columnsOf(rows)" :key="c">{{ c }}</th></tr></thead>
          <tbody><tr v-for="(r, i) in rows" :key="i"><td v-for="c in columnsOf(rows)" :key="c">{{ cell(r[c]) }}</td></tr></tbody>
        </table></div>
      </div>
    </div>
  </StageFrame>
</template>
