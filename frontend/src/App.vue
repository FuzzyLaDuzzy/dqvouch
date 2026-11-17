<script setup>
// The app shell: a sidebar (brand, navigation, the open run, its 8 steps, the theme) and the screen the address
// hash names (#/, #/new, #/run/<id>/<stage>). The open run's state, toasts and the confirm dialog are shared with
// every screen through provide('run').
import { computed, provide, reactive, ref, watch } from 'vue'
import { api, watchRun } from './api.js'
import { STAGES, STAGE_INFO, parseHash, staleStages, stepState, toHash } from './logic.js'
import Icon from './Icon.vue'
import Logo from './Logo.vue'
import Config from './screens/Config.vue'
import Context from './screens/Context.vue'
import Dimensions from './screens/Dimensions.vue'
import Generic from './screens/Generic.vue'
import Profiling from './screens/Profiling.vue'
import Results from './screens/Results.vue'
import Review from './screens/Review.vue'
import Runs from './screens/Runs.vue'
import SqlRules from './screens/SqlRules.vue'
import Tests from './screens/Tests.vue'

const screens = {
  config: Config, context: Context, dimensions: Dimensions, profiling: Profiling, sql_rules: SqlRules, tests: Tests,
  review: Review, results: Results,
}
const route = ref(parseHash(location.hash))
window.addEventListener('hashchange', () => { route.value = parseHash(location.hash) })

const state = reactive({ id: null, info: null, completed: [], job: null, changes: {}, data: {}, error: null })
let stopWatching = () => {}
let waiting = []

// --- theme: the system's, light or dark; remembered in this browser only
function readTheme() {
  try { return localStorage.getItem('dqvouch-theme') || 'auto' } catch { return 'auto' }
}
const theme = ref(readTheme())
watch(theme, (t) => {
  if (t === 'auto') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = t
  try { localStorage.setItem('dqvouch-theme', t) } catch { /* private mode: not remembered */ }
}, { immediate: true })
const THEMES = [['auto', 'System theme', 'auto'], ['light', 'Light theme', 'sun'], ['dark', 'Dark theme', 'moon']]

// --- toasts and the confirm dialog (instead of the browser's alert/confirm boxes)
const toasts = ref([])
let toastId = 0
function toast(message, kind = 'info', ms = 4000) {
  const t = { id: ++toastId, message, kind }
  toasts.value.push(t)
  setTimeout(() => { toasts.value = toasts.value.filter((x) => x.id !== t.id) }, ms)
}
const dialog = ref(null)
function confirmDialog(message, confirm = 'Continue') {
  return new Promise((resolve) => { dialog.value = { message, confirm, resolve } })
}
function answer(yes) {
  dialog.value?.resolve(yes)
  dialog.value = null
}

function go(stage) {
  location.hash = toHash({ view: 'run', runId: state.id, stage })
}

async function loadStage(stage) {
  if (STAGES.includes(stage) && state.completed.includes(stage) && !state.data[stage]) {
    state.data[stage] = await api.stage(state.id, stage)
  }
}

function follow() {
  stopWatching()
  stopWatching = watchRun(state.id, async (r) => {
    const before = state.job
    state.completed = r.completed
    state.job = r.job
    state.changes = r.changes || {}  // a structural change found by a schedule: the run it is waiting in
    // A job that has just finished: one we saw running, or one that ran entirely between two polls (a schedule).
    const finished = r.job && r.job.status !== 'running' && (before?.status === 'running' || before?.started !== r.job.started)
    for (const s of staleStages(Object.keys(state.data), finished ? r.job : null, r.completed)) delete state.data[s]
    if (finished) {
      await loadStage(r.job.stage)
      await loadStage(route.value.stage)
      if (r.job.status === 'done') toast(`${STAGE_INFO[r.job.stage].title} finished`)
      waiting.forEach((resolve) => resolve(r.job))
      waiting = []
    }
  })
}

// Starts a stage in the background and opens its screen. Resolves with the job once it has finished.
let starting = false
async function start(stage, body = {}) {
  if (starting) return null // a double click: the first request is already on its way
  starting = true
  let r
  try {
    r = await api.start(state.id, stage, body)
  } catch (e) {
    toast(e.message, 'error')
    return null
  } finally {
    starting = false
  }
  const from = STAGES.indexOf(stage)
  for (const s of STAGES.slice(from)) delete state.data[s]
  state.completed = state.completed.filter((s) => STAGES.indexOf(s) < from)
  state.job = r.job
  go(stage)
  const done = new Promise((resolve) => waiting.push(resolve))
  follow()
  return done
}

// The review is the person's own decision: it is saved at once, then the confirmed tests run.
async function submitReview(payload) {
  const data = await api.start(state.id, 'review', payload)
  state.data.review = data
  delete state.data.results
  state.completed = STAGES.slice(0, STAGES.indexOf('review') + 1)
  return data
}

async function openRun(id) {
  const r = await api.run(id)
  Object.assign(state, { id, info: r.info, completed: r.completed, job: r.job, changes: r.changes || {}, data: {}, error: null })
  follow()
}

watch(route, async (r) => {
  if (r.view !== 'run') {
    stopWatching()
    return
  }
  try {
    if (r.runId !== state.id) await openRun(r.runId)
    if (!r.stage || !STAGE_INFO[r.stage]) {
      // Opening a run continues where it is: the stage running now, or else the last one that finished.
      const running = state.job?.status === 'running' ? state.job.stage : null
      return location.replace(toHash({ view: 'run', runId: r.runId, stage: running ?? state.completed.at(-1) ?? 'context' }))
    }
    await loadStage(r.stage)
  } catch (e) {
    state.error = e.message
  }
}, { immediate: true })

const inRun = computed(() => route.value.view === 'run' && state.info)
const db = computed(() => state.info?.settings.database)
const engineLabel = computed(() => (db.value?.engine === 'clickhouse' ? 'ClickHouse' : 'PostgreSQL'))
// The line behind the steps fills down to the last finished one (8 rows; the configuration is the first).
const fill = computed(() => `calc(${(100 * state.completed.length) / STAGES.length}% - ${(38 * state.completed.length) / STAGES.length}px)`)
const KIND_ICON = { llm: 'llm', code: 'code', you: 'you' }
const kindOf = (s) => (STAGE_INFO[s].kind.startsWith('LLM') ? 'llm' : STAGE_INFO[s].kind === 'you' ? 'you' : 'code')
const screenKey = computed(() => (route.value.view === 'run' ? `${state.id}/${route.value.stage}` : route.value.view))

provide('run', { state, start, go, submitReview, load: loadStage, toast, confirm: confirmDialog })
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <a href="#/" class="brand" aria-label="dqvouch, all runs"><Logo animated /><strong>dqvouch</strong></a>

      <nav class="nav" aria-label="Main">
        <a href="#/" :class="{ active: route.view === 'runs' }"><Icon name="runs" />Runs</a>
        <a href="#/new" :class="{ active: route.view === 'new' }"><Icon name="plus" />New run</a>
      </nav>

      <template v-if="inRun">
        <div class="run-card">
          <div class="engine"><Icon name="database" />{{ engineLabel }}</div>
          <div class="muted">{{ db.host }}:{{ db.port }}</div>
          <div class="muted">{{ db.schemas.join(', ') }}</div>
        </div>
        <div class="steps-wrap">
          <p class="side-label">Stages</p>
          <nav class="steps-list" aria-label="Stages">
            <span class="steps-line" /><span class="steps-fill" :style="{ height: fill }" />
            <a v-for="(s, i) in ['config', ...STAGES]" :key="s" class="step"
               :class="[stepState(s, state.completed, state.job), { current: s === route.stage }]"
               :aria-current="s === route.stage ? 'step' : undefined"
               :href="stepState(s, state.completed, state.job) !== 'locked' ? toHash({ view: 'run', runId: state.id, stage: s }) : undefined">
              <span class="bullet">
                <span v-if="stepState(s, state.completed, state.job) === 'running'" class="spinner" />
                <Icon v-else-if="stepState(s, state.completed, state.job) === 'error'" name="alert" :size="14" />
                <Icon v-else-if="stepState(s, state.completed, state.job) === 'done' && s !== route.stage" name="check" :size="14" />
                <template v-else>{{ i + 1 }}</template>
              </span>
              <span class="label">{{ STAGE_INFO[s].title }}</span>
              <Icon class="kind" :name="KIND_ICON[kindOf(s)]" :size="15" />
            </a>
          </nav>
        </div>
      </template>

      <div v-else class="how-side">
        <p class="side-label">How a run works</p>
        <div v-for="s in STAGES" :key="s" class="how-row">
          <Icon :name="KIND_ICON[kindOf(s)]" :size="15" :class="kindOf(s)" /><span>{{ STAGE_INFO[s].title }}</span>
        </div>
      </div>

      <div class="side-foot">
        <div class="theme-switch" role="radiogroup" aria-label="Theme">
          <button v-for="[t, label, icon] in THEMES" :key="t" role="radio" :aria-checked="theme === t" :title="label"
                  :aria-label="label" :class="{ on: theme === t }" @click="theme = t"><Icon :name="icon" :size="16" /></button>
        </div>
        <div class="version">dqvouch 0.1.0</div>
      </div>
    </aside>

    <main class="main">
      <div class="main-inner">
        <nav v-if="inRun" class="crumbs" aria-label="Breadcrumb">
          <a href="#/">Runs</a><span class="sep">/</span><span>{{ engineLabel }} · {{ db.schemas.join(', ') }}</span>
          <span class="sep">/</span><span>{{ STAGE_INFO[route.stage]?.title }}</span>
        </nav>
        <p v-if="state.error && route.view === 'run'" class="error">{{ state.error }} <a href="#/">Back to the runs</a></p>
        <Transition name="screen" mode="out-in">
          <Runs v-if="route.view === 'runs'" key="runs" />
          <Config v-else-if="route.view === 'new'" key="new" />
          <component :is="screens[route.stage] || Generic" v-else-if="state.info && STAGE_INFO[route.stage]" :key="screenKey"
                     :stage="route.stage" />
        </Transition>
      </div>
    </main>
  </div>

  <TransitionGroup tag="div" name="toast" class="toasts" aria-live="polite">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="t.kind">
      <Icon :name="t.kind === 'error' ? 'alert' : 'check'" :size="16" />{{ t.message }}
    </div>
  </TransitionGroup>

  <Transition name="fade">
    <div v-if="dialog" class="scrim" @click.self="answer(false)" @keydown.esc="answer(false)">
      <div class="dialog" role="dialog" aria-modal="true">
        <p>{{ dialog.message }}</p>
        <footer>
          <button @click="answer(false)">Cancel</button>
          <button class="primary" autofocus @click="answer(true)">{{ dialog.confirm }}</button>
        </footer>
      </div>
    </div>
  </Transition>
</template>
