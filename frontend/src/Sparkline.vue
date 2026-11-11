<script setup>
// A small line chart of one test's observed values (oldest to newest), with its threshold as a dashed line.
import { computed } from 'vue'

const props = defineProps({ values: { type: Array, required: true }, threshold: { type: Number, default: null } })
const W = 132
const H = 30
const P = 4
const scale = computed(() => {
  const all = props.threshold === null ? props.values : [...props.values, props.threshold]
  const lo = Math.min(...all)
  return { lo, span: Math.max(...all) - lo || 1 }
})
const x = (i) => P + (i * (W - 2 * P)) / Math.max(1, props.values.length - 1)
const y = (v) => H - P - ((v - scale.value.lo) * (H - 2 * P)) / scale.value.span
const points = computed(() => props.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' '))
</script>

<template>
  <svg v-if="values.length >= 2" class="sparkline" :viewBox="`0 0 ${W} ${H}`" :width="W" :height="H" role="img"
       :aria-label="`Observed in the last ${values.length} executions, oldest first: ${values.join(', ')}`">
    <title>Observed, oldest to newest: {{ values.join(' → ') }}{{ threshold === null ? '' : ` (limit ${threshold})` }}</title>
    <line v-if="threshold !== null" class="limit" :x1="P" :x2="W - P" :y1="y(threshold)" :y2="y(threshold)" />
    <polyline :points="points" />
    <circle :cx="x(values.length - 1)" :cy="y(values.at(-1))" r="2.5" />
  </svg>
</template>
