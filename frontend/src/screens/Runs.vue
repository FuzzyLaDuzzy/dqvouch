<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import Icon from '../Icon.vue'
import { STAGES, STAGE_INFO, changeLabel, everyLabel, toHash } from '../logic.js'
import Logo from '../Logo.vue'

const runs = ref(null)
const error = ref('')
onMounted(async () => {
  try {
    runs.value = await api.runs()
  } catch (e) {
    error.value = e.message
  }
})
// "20261110-193334" -> "2026-11-10 19:33 UTC"
const when = (id) => id.replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}.*$/, '$1-$2-$3 $4:$5 UTC')
const last = (r) => (r.completed.length ? STAGE_INFO[r.completed.at(-1)].title : 'Not started')
const HOW = ['context', 'dimensions', 'profiling', 'sql_rules', 'tests', 'review', 'results']
</script>

<template>
  <section>
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="runs && !runs.length" class="hero">
      <span class="mark"><Logo animated /></span>
      <h1>Vouch for your data</h1>
      <p>dqvouch reads your tables and the SQL that builds them, lets Gemini propose data quality tests, checks every
        proposal in code, and runs only the tests you confirm. Read-only, on your own database.</p>
      <a class="btn primary" href="#/new"><Icon name="plus" :size="16" />Start your first run</a>
      <div class="how stagger">
        <span v-for="s in HOW" :key="s" class="chip">{{ STAGE_INFO[s].title }}</span>
      </div>
    </div>

    <template v-else-if="runs">
      <header class="page-head">
        <div class="kind-badge you"><Icon name="runs" :size="22" /></div>
        <div>
          <p class="eyebrow">Your runs</p>
          <h1>Runs</h1>
          <p class="lede">Each run is one pass over a database, saved in its own folder with everything it sent and
            received. Opening a run continues where it stopped.</p>
        </div>
        <a class="btn primary" href="#/new"><Icon name="plus" :size="16" />New run</a>
      </header>
      <div class="run-grid stagger">
        <a v-for="r in runs" :key="r.id" class="run-tile" :href="toHash({ view: 'run', runId: r.id, stage: null })">
          <div class="top">
            <span class="engine-pill"><Icon name="database" :size="13" />{{ r.engine === 'postgres' ? 'PostgreSQL' : 'ClickHouse' }}</span>
            <span class="muted small">{{ when(r.id) }}</span>
          </div>
          <h3>{{ r.schemas.join(', ') }}</h3>
          <span v-if="r.every_minutes" class="chip schedule-chip"><Icon name="clock" :size="13" />Runs {{ everyLabel(r.every_minutes) }}</span>
          <span v-if="changeLabel(r)" class="chip change-chip">{{ changeLabel(r) }}</span>
          <div class="segments" :title="`${r.completed.length} of ${STAGES.length} stages`">
            <i v-for="s in STAGES" :key="s" :class="{ on: r.completed.includes(s) }" />
          </div>
          <div class="foot"><span>{{ last(r) }}</span><span class="go">Open<Icon name="arrow-right" :size="15" /></span></div>
        </a>
      </div>
    </template>
  </section>
</template>
