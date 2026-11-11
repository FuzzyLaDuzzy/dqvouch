<script setup>
// dqvouch's mark: rows of a table, and a check over them -- data someone has vouched for -- on a violet gradient.
import { useId } from 'vue'

defineProps({ animated: { type: Boolean, default: false } })
const id = useId()
</script>

<template>
  <svg class="logo-mark" :class="{ animated }" viewBox="0 0 32 32" aria-hidden="true">
    <defs>
      <linearGradient :id="`g${id}`" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="var(--brand-1)" />
        <stop offset="1" stop-color="var(--brand-2)" />
      </linearGradient>
    </defs>
    <rect width="32" height="32" rx="9" :fill="`url(#g${id})`" />
    <rect class="row r1" x="6.5" y="8" width="14" height="3" rx="1.5" />
    <rect class="row r2" x="6.5" y="14" width="10.5" height="3" rx="1.5" />
    <rect class="row r3" x="6.5" y="20" width="7" height="3" rx="1.5" />
    <path class="check" d="M16.5 20.5 20 24l6-8.5" pathLength="1" />
  </svg>
</template>

<style scoped>
.row { fill: #fff; }
.r1 { opacity: .42; } .r2 { opacity: .62; } .r3 { opacity: .85; }
.check { fill: none; stroke: #fff; stroke-width: 2.6; stroke-linecap: round; stroke-linejoin: round; }
.animated .row { animation: row-in .55s cubic-bezier(.2, .7, .2, 1) both; transform-box: fill-box; transform-origin: left; }
.animated .r2 { animation-delay: .08s; }
.animated .r3 { animation-delay: .16s; }
.animated .check { stroke-dasharray: 1; animation: draw .6s .32s cubic-bezier(.65, 0, .35, 1) both; }
@keyframes row-in { from { transform: scaleX(0); } }
@keyframes draw { from { stroke-dashoffset: 1; } to { stroke-dashoffset: 0; } }
</style>
