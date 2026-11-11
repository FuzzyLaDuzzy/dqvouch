<script setup>
import { computed, inject } from 'vue'
import StageFrame from '../StageFrame.vue'

defineProps({ stage: { type: String, required: true } })
const run = inject('run')
const data = computed(() => run.state.data.sql_rules || {})
const cols = (list) => (list || []).map((c) => (Array.isArray(c) ? c.join(' = ') : c)).join(', ')
const text = (g) => g.description || g.statement || g.rule || g.condition || ''
</script>

<template>
  <StageFrame :stage="stage">
    <p v-if="data.skipped" class="notice">Skipped: {{ data.skipped }}.</p>
    <template v-else>
      <h2 class="group-title">Accepted ({{ (data.accepted || []).length }})</h2>
      <p v-if="!(data.accepted || []).length" class="muted">No guarantee passed the validator.</p>
      <div v-for="g in data.accepted || []" :key="g.id" class="item">
        <h3>{{ g.category }} <span class="muted">· {{ cols(g.tables) }}</span></h3>
        <p v-if="text(g)">{{ text(g) }}</p>
        <p v-if="(g.columns || []).length || (g.column_pairs || []).length" class="muted">
          Columns: {{ cols(g.columns) }}{{ (g.column_pairs || []).length ? ' · pairs: ' + cols(g.column_pairs) : '' }}</p>
        <pre v-if="g.sql_quote">{{ g.sql_quote }}</pre>
        <p class="muted">Cited as <code>sql_invariants.{{ g.id }}</code></p>
      </div>
      <template v-if="(data.rejected || []).length">
        <h2 class="group-title">Rejected ({{ data.rejected.length }})</h2>
        <div v-for="(r, i) in data.rejected" :key="i" class="item">
          <strong>{{ r.scope }}</strong>: {{ r.reason }}
          <details v-if="r.detail"><summary>What Gemini proposed</summary><pre>{{ JSON.stringify(r.detail, null, 2) }}</pre></details>
        </div>
      </template>
    </template>
  </StageFrame>
</template>
