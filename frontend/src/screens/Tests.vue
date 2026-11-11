<script setup>
import { computed, inject } from 'vue'
import { testGroups } from '../logic.js'
import StageFrame from '../StageFrame.vue'
import TestCard from '../TestCard.vue'

defineProps({ stage: { type: String, required: true } })
const run = inject('run')
const data = computed(() => run.state.data.tests || {})
const groups = computed(() => testGroups(data.value))
const thresholds = computed(() => data.value.metric_thresholds || [])
const rejectedThresholds = computed(() => data.value.metric_thresholds_rejected || [])
</script>

<template>
  <StageFrame :stage="stage">
    <h2 class="group-title">Accepted ({{ groups.accepted.length }})</h2>
    <TestCard v-for="s in groups.accepted" :key="s.id" :spec="s" />
    <template v-if="groups.flagged.length">
      <h2 class="group-title">Flagged ({{ groups.flagged.length }}): the data contradicts them, threshold suspended</h2>
      <TestCard v-for="s in groups.flagged" :key="s.id" :spec="s" />
    </template>
    <template v-if="thresholds.length">
      <h2 class="group-title">Limits on measured metrics ({{ thresholds.length }})</h2>
      <div v-for="m in thresholds" :key="m.id" class="item">
        <h3>{{ m.title || m.id }}</h3>
        <p class="muted">{{ m.asset }} · <code>{{ m.check_id }}</code></p>
        <p><strong>Expects:</strong> {{ m.metric }} {{ String(m.operator).replace(/_/g, ' ') }} {{ m.value }}</p>
        <p v-if="m.rationale" class="muted">{{ m.rationale }}</p>
      </div>
    </template>
    <template v-if="groups.rejected.length || rejectedThresholds.length">
      <h2 class="group-title">Rejected by the validator ({{ groups.rejected.length + rejectedThresholds.length }})</h2>
      <ul>
        <li v-for="(r, i) in [...groups.rejected, ...rejectedThresholds]" :key="i">
          <strong>{{ String(r.scope).split('__').at(-1) }}</strong>: {{ r.reason }}</li>
      </ul>
    </template>
  </StageFrame>
</template>
