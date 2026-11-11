<script setup>
// Screen 2: what dqvouch read about each table. Unticking tables reruns this stage on the rest before going on.
import { computed, inject, ref, watch } from 'vue'
import StageFrame from '../StageFrame.vue'

defineProps({ stage: { type: String, required: true } })
const run = inject('run')
const data = computed(() => run.state.data.context)
const contexts = computed(() => data.value?.asset_contexts || {})
const available = computed(() => data.value?.tables_available || Object.keys(contexts.value))
const selected = ref([])
const open = ref(null)
watch(contexts, (c) => { selected.value = Object.keys(c) }, { immediate: true })

const facts = (ctx) => [
  ...Object.entries(ctx.dq_signals || {}).map(([k, s]) => ({ key: k, fact: s.fact, sent: true })),
  ...Object.entries(ctx._sql_facts || {}).map(([k, s]) => ({ key: k, fact: s.fact, sent: false })),
]
const list = (value) => (Array.isArray(value) ? (value.length ? value.join(', ') : 'none') : value || 'unknown')
const known = (value) => (value && value !== 'unknown' ? value : null)

async function proceed() {
  const chosen = available.value.filter((t) => selected.value.includes(t))
  if (!chosen.length) return run.toast('Choose at least one table.', 'error')
  const same = chosen.length === Object.keys(contexts.value).length && chosen.every((t) => t in contexts.value)
  if (!same) {
    const later = run.state.completed.some((s) => s !== 'context')
    if (later && !(await run.confirm('Changing the tables reruns every later stage.', 'Rerun'))) return
    const job = await run.start('context', { tables: chosen })
    if (!job || job.status !== 'done') return
  }
  if (run.state.completed.includes('dimensions')) return run.go('dimensions')
  run.start('dimensions')
}
</script>

<template>
  <StageFrame :stage="stage" :on-continue="proceed" :redo-body="{ tables: Object.keys(contexts) }">
    <p>{{ Object.keys(contexts).length }} of {{ available.length }} table(s) selected.</p>
    <div v-for="t in available" :key="t" class="table-card">
      <div class="table-head">
        <label><input v-model="selected" type="checkbox" :value="t"> <strong>{{ t }}</strong></label>
        <span v-if="contexts[t]" class="muted">
          {{ contexts[t].asset?.engine }} · {{ contexts[t].columns?.length }} columns
          · SQL: {{ known(contexts[t].transformation?.source_file) || 'none' }}
        </span>
        <button v-if="contexts[t]" class="link" @click="open = open === t ? null : t">{{ open === t ? 'Hide' : 'Details' }}</button>
      </div>
      <div v-if="contexts[t] && open === t" class="table-body">
        <p v-if="contexts[t].asset?.description && contexts[t].asset.description !== 'unknown'">{{ contexts[t].asset.description }}</p>
        <div class="table-scroll"><table class="grid">
          <thead><tr><th>Column</th><th>Type</th><th>Description</th><th>Semantic roles</th></tr></thead>
          <tbody>
            <tr v-for="c in contexts[t].columns" :key="c.name">
              <td class="nowrap">{{ c.name }}</td><td><code>{{ c.type }}</code></td>
              <td>{{ c.description === 'unknown' ? '' : c.description }}</td><td>{{ (c.semantic_roles || []).join(', ') }}</td>
            </tr>
          </tbody>
        </table></div>
        <p><strong>Built from:</strong> {{ list(contexts[t].lineage?.upstream_assets) }} ·
          <strong>Feeds:</strong> {{ list(contexts[t].lineage?.downstream_assets) }}</p>
        <div v-if="facts(contexts[t]).length">
          <strong>Facts from the SQL</strong>
          <ul>
            <li v-for="f in facts(contexts[t])" :key="f.key">{{ f.fact }}
              <span class="muted">({{ f.sent ? 'sent to Gemini' : 'used by the validator only' }})</span></li>
          </ul>
        </div>
        <details v-if="contexts[t].transformation?.raw_sql && contexts[t].transformation.raw_sql !== 'unknown'">
          <summary>SQL ({{ contexts[t].transformation.source_file }})</summary>
          <pre>{{ contexts[t].transformation.raw_sql }}</pre>
        </details>
      </div>
    </div>
  </StageFrame>
</template>
