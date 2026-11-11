<script setup>
// A handful of line icons drawn for dqvouch (24x24, 2px strokes, round caps). Filled parts are marked fill: true.
import { computed } from 'vue'

const ICONS = {
  check: ['M5 12.5 10 17 19 7'],
  x: ['M6 6l12 12M18 6 6 18'],
  'arrow-left': ['M19 12H5M11 6l-6 6 6 6'],
  'arrow-right': ['M5 12h14M13 6l6 6-6 6'],
  plus: ['M12 5v14M5 12h14'],
  llm: ['M12 3c.9 4.4 3.6 7.1 8 8-4.4.9-7.1 3.6-8 8-.9-4.4-3.6-7.1-8-8 4.4-.9 7.1-3.6 8-8z', 'M19 3v3M17.5 4.5h3'],
  code: ['M8 7 3 12l5 5M16 7l5 5-5 5M13.5 5l-3 14'],
  you: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M4 21a8 8 0 0 1 16 0'],
  database: ['M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3z', 'M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6', 'M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6'],
  sun: ['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z', 'M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'],
  moon: ['M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z'],
  auto: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', { d: 'M12 3a9 9 0 0 1 0 18z', fill: true }],
  runs: ['M9 6h11M9 12h11M9 18h11', 'M4.5 6h.01M4.5 12h.01M4.5 18h.01'],
  alert: ['M12 9v4M12 17h.01', 'M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'],
  clock: ['M12 7v5l3 2', 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z'],
  lock: ['M7 11V8a5 5 0 0 1 10 0v3', 'M5 11h14v10H5z'],
  play: ['M7 5l12 7-12 7z'],
  sliders: ['M4 6h16M4 12h16M4 18h16', 'M15 4v4M8 10v4M17 16v4'],
}
const props = defineProps({ name: { type: String, required: true }, size: { type: [Number, String], default: 18 } })
const parts = computed(() => (ICONS[props.name] || []).map((p) => (typeof p === 'string' ? { d: p } : p)))
</script>

<template>
  <svg class="icon" :width="size" :height="size" viewBox="0 0 24 24" aria-hidden="true" fill="none"
       stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path v-for="(p, i) in parts" :key="i" :d="p.d" :fill="p.fill ? 'currentColor' : 'none'" :stroke="p.fill ? 'none' : undefined" />
  </svg>
</template>
