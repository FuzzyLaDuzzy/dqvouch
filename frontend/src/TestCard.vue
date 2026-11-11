<script setup>
// One proposed test as a person reads it: what it checks, where it comes from, the evidence and what it expects.
// The review screen puts its controls in the default slot.
import { computed } from 'vue'
import { evidenceOrigin } from './logic.js'

const props = defineProps({ spec: { type: Object, required: true } })
const display = computed(() => props.spec._display || {})
const evidence = computed(() => display.value.evidence || (props.spec.evidence || []).map((e) => ({ path: e.path, value: null })))
const origins = computed(() => [...new Set(evidence.value.map((e) => evidenceOrigin(e.path)))])
const flagged = computed(() => (props.spec.review_warnings || []).length > 0)
const shortId = computed(() => String(props.spec.id || '').split('__').at(-1))

function show(value) {
  if (value === null || value === undefined) return ''
  const s = typeof value === 'string' ? value : JSON.stringify(value)
  return s.length > 240 ? `${s.slice(0, 240)}…` : s
}
const limit = (t) => (t ? `${t.metric} ${t.operator.replace(/_/g, ' ')} ${t.value}` : '')
</script>

<template>
  <div class="item" :class="{ flagged }">
    <h3>{{ spec.title || shortId }}</h3>
    <p class="muted">{{ spec.asset }} · {{ spec.dimension || '' }} · {{ spec.assertion?.type }} · from {{ origins.join(', ') || 'no evidence' }}</p>
    <p>{{ display.description }}</p>
    <p v-if="spec.threshold"><strong>Expects:</strong> {{ display.expected || limit(spec.threshold) }}</p>
    <p v-else-if="spec.suspended_threshold"><strong>Threshold suspended:</strong> {{ limit(spec.suspended_threshold) }}
      <span class="muted">(runs without a verdict until you restore it)</span></p>
    <p v-else class="muted">No threshold: it will report what it observes.</p>
    <div v-if="flagged" class="notice">
      <strong>The data contradicts this rule.</strong>
      <ul><li v-for="(w, i) in spec.review_warnings" :key="i">{{ typeof w === 'string' ? w : JSON.stringify(w) }}</li></ul>
      Either the rule is wrong for this data (exclude it) or the data has a real problem (restore the threshold).
    </div>
    <details v-if="evidence.length || spec.rationale">
      <summary>Why: evidence and rationale</summary>
      <p v-if="spec.rationale"><strong>Rationale (the model):</strong> {{ spec.rationale }}</p>
      <ul>
        <li v-for="(e, i) in evidence" :key="i"><code>{{ e.path }}</code> <span class="muted">({{ evidenceOrigin(e.path) }})</span>
          <div v-if="e.value !== null && e.value !== undefined"><code>{{ show(e.value) }}</code></div></li>
      </ul>
    </details>
    <slot />
  </div>
</template>
