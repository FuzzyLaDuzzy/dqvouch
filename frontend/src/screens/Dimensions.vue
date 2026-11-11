<script setup>
import { computed, inject } from 'vue'
import StageFrame from '../StageFrame.vue'

defineProps({ stage: { type: String, required: true } })
const run = inject('run')
const data = computed(() => run.state.data.dimensions || {})
const tables = computed(() => data.value.dimensions?.tables || [])
const cross = computed(() => data.value.dimensions?.cross_table_checks || [])
const rejected = computed(() => data.value.rejected || [])
const cols = (list) => (list || []).join(', ')
</script>

<template>
  <StageFrame :stage="stage">
    <p v-if="!tables.length && !cross.length">Gemini proposed nothing the validator could accept.</p>
    <div v-for="t in tables" :key="t.table" class="item">
      <h3>{{ t.table }}</h3>
      <div class="table-scroll"><table class="grid">
        <thead><tr><th>Dimension</th><th>Columns</th><th>Why (Gemini)</th></tr></thead>
        <tbody><tr v-for="(d, i) in t.dimensions" :key="i"><td class="nowrap">{{ d.dimension }}</td><td>{{ cols(d.columns) }}</td><td>{{ d.rationale }}</td></tr></tbody>
      </table></div>
    </div>
    <template v-if="cross.length">
      <h2 class="group-title">Across tables</h2>
      <div class="table-scroll"><table class="grid">
        <thead><tr><th>Dimension</th><th>Tables</th><th>Columns</th><th>Join key</th><th>Why (Gemini)</th></tr></thead>
        <tbody>
          <tr v-for="(c, i) in cross" :key="i">
            <td class="nowrap">{{ c.dimension }}</td><td>{{ cols(c.tables_involved) }}</td><td>{{ cols(c.columns_involved) }}</td>
            <td>{{ c.join_key || '' }}</td><td>{{ c.rationale }}</td>
          </tr>
        </tbody>
      </table></div>
    </template>
    <template v-if="rejected.length">
      <h2 class="group-title">Rejected by the validator</h2>
      <ul><li v-for="(r, i) in rejected" :key="i"><strong>{{ r.scope }}</strong>: {{ r.reason }}</li></ul>
    </template>
  </StageFrame>
</template>
