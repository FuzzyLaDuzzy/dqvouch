<script setup>
import { computed, inject, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api.js'
import { everyLabel, filterRows, historyRows, passRate, splitEvery, toCSV, trend } from '../logic.js'
import Icon from '../Icon.vue'
import Sparkline from '../Sparkline.vue'
import StageFrame from '../StageFrame.vue'

// A number that counts up to its value once (straight to it with reduced motion).
function countUp(source) {
  const shown = ref(0)
  watch(source, (target) => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return (shown.value = target)
    const t0 = performance.now()
    const tick = (t) => {
      const k = Math.min(1, (t - t0) / 800)
      shown.value = Math.round(target * (1 - (1 - k) ** 3))
      if (k < 1) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  }, { immediate: true })
  return shown
}

defineProps({ stage: { type: String, required: true } })
const run = inject('run')
onMounted(() => run.load('review'))

const rows = computed(() => run.state.data.results?.rows || [])
const filters = reactive({ asset: '', dimension: '', verdict: '' })
const options = (key) => [...new Set(rows.value.map((r) => r[key]).filter(Boolean))].sort()
const shown = computed(() => filterRows(rows.value, filters))
const counts = computed(() => rows.value.reduce((c, r) => ({ ...c, [r.verdict]: (c[r.verdict] || 0) + 1 }), {}))
const rate = computed(() => passRate(rows.value))
const passed = countUp(() => rate.value.pass)
const failed = countUp(() => rate.value.fail)
const other = countUp(() => rate.value.other)
// Edits the validator refused after the review (the person's own exclusions are not news to them).
const refused = computed(() => (run.state.data.review?.rejected || []).filter((r) => !/excluded during specialist review/.test(r.reason)))
const label = (v) => String(v).replace(/_/g, ' ')
const show = (v) => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : v)
const limitOf = (r) => (typeof r.threshold?.value === 'number' ? r.threshold.value : null)

// --- the schedule: run the confirmed tests again every n minutes, and every execution's verdicts
const schedule = ref(null)
const history = ref([])
const form = reactive({ n: 1, unit: 1440 })
const UNITS = [[1, 'minutes'], [60, 'hours'], [1440, 'days']]
const historyShown = computed(() => historyRows(history.value).slice(0, 20))
const at = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
async function loadSchedule() {
  try {
    const s = await api.schedule(run.state.id)
    schedule.value = s.schedule
    history.value = s.history
    if (s.schedule) Object.assign(form, splitEvery(s.schedule.every_minutes))
  } catch { /* a server without schedules: the panel stays empty */ }
}
// Every execution (a scheduled one included) reloads the results; the history follows.
watch(() => run.state.data.results, (r) => { if (r) loadSchedule() }, { immediate: true })
async function saveSchedule(every) {
  try {
    schedule.value = (await api.setSchedule(run.state.id, every)).schedule
    run.toast(every ? `The tests will run ${everyLabel(every)}` : 'Schedule stopped')
  } catch (e) {
    run.toast(e.message, 'error')
  }
}
const everyMinutes = computed(() => Math.round(form.n) * form.unit)
const TRIGGERS = { schedule: 'scheduled', command: 'command line', manual: 'by hand' }

// The same schedule without dqvouch open: cron and Task Scheduler lines that call `dqvouch rerun`.
const commands = ref(null)
const commandsOpen = ref(false)
async function loadCommands() {
  if (!commandsOpen.value || !(form.n >= 1)) return
  try {
    commands.value = await api.scheduleCommands(run.state.id, everyMinutes.value)
  } catch (e) {
    run.toast(e.message, 'error')
  }
}
watch(everyMinutes, loadCommands)
// Alerts: one message when a test starts failing or an execution cannot run (for every run of this dqvouch).
const ALERT_FIELDS = [
  ['DQVOUCH_SMTP_HOST', 'SMTP server', 'smtp.example.com'], ['DQVOUCH_SMTP_PORT', 'Port', '587'],
  ['DQVOUCH_SMTP_USER', 'User', ''], ['DQVOUCH_SMTP_FROM', 'From', 'the user, if empty'],
  ['DQVOUCH_SMTP_TO', 'To', 'a@example.com, b@example.com'],
]
const alertValues = reactive({})
const alertSecrets = ref({})
const alertTyped = reactive({ DQVOUCH_ALERT_WEBHOOK_URL: '', DQVOUCH_SMTP_PASSWORD: '' })
const alertNote = ref('')
async function loadAlerts() {
  try {
    const a = await api.alerts()
    Object.assign(alertValues, a.values)
    alertSecrets.value = a.secrets
  } catch { /* a server without alerts: the section stays empty */ }
}
const savedSecret = (name) => (alertSecrets.value[name]?.set ? 'saved (type to replace)' : '')
async function saveAlerts() {
  alertNote.value = ''
  try {
    const typed = Object.fromEntries(Object.entries(alertTyped).filter(([, v]) => v))
    const a = await api.setAlerts({ ...alertValues, ...typed })
    Object.assign(alertValues, a.values)
    alertSecrets.value = a.secrets
    Object.assign(alertTyped, { DQVOUCH_ALERT_WEBHOOK_URL: '', DQVOUCH_SMTP_PASSWORD: '' })
    alertNote.value = 'Saved.'
  } catch (e) {
    alertNote.value = e.message
  }
}
async function forgetSecret(name) {
  const a = await api.setAlerts({ [name]: '' })
  alertSecrets.value = a.secrets
}
async function testAlert() {
  alertNote.value = 'Sending…'
  try {
    const r = await api.testAlert()
    alertNote.value = r.ok ? `Sent by ${r.sent.join(' and ')}.` : `Not sent: ${r.error}`
  } catch (e) {
    alertNote.value = e.message
  }
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    run.toast('Copied')
  } catch {
    run.toast('Select the line and copy it by hand', 'error')
  }
}

function download(kind) {
  const body = kind === 'csv' ? toCSV(shown.value) : JSON.stringify(shown.value, null, 2)
  const url = URL.createObjectURL(new Blob([body], { type: kind === 'csv' ? 'text/csv' : 'application/json' }))
  const a = Object.assign(document.createElement('a'), { href: url, download: `dqvouch-${run.state.id}-results.${kind}` })
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <StageFrame :stage="stage">
    <div v-if="refused.length" class="notice">
      The validator refused {{ refused.length }} of your edits, so those tests did not run:
      <ul><li v-for="(r, i) in refused" :key="i"><strong>{{ String(r.scope).split('__').at(-1) }}</strong>: {{ r.reason }}</li></ul>
    </div>
    <div class="kpis">
      <div class="kpi ring-tile">
        <div class="ring" :title="`${rate.pass} of ${rate.pass + rate.fail} tests with a verdict passed`">
          <svg viewBox="0 0 50 50" aria-hidden="true">
            <circle class="track" :class="{ failing: rate.fail > 0 }" cx="25" cy="25" r="21" />
            <circle v-if="rate.pct > 0" class="arc" cx="25" cy="25" r="21" pathLength="100" :style="{ strokeDashoffset: 100 - rate.pct }" />
          </svg>
          <b>{{ rate.pct === null ? '–' : `${rate.pct}%` }}</b>
        </div>
        <span>of the tests with a verdict passed</span>
      </div>
      <div class="kpi ok"><b>{{ passed }}</b><span>passed</span></div>
      <div class="kpi bad"><b>{{ failed }}</b><span>failed</span></div>
      <div class="kpi"><b>{{ other }}</b><span>no verdict ({{ Object.entries(counts).filter(([v]) => v !== 'pass' && v !== 'fail').map(([v, n]) => `${n} ${label(v)}`).join(', ') || 'none' }})</span></div>
    </div>
    <div class="panel schedule">
      <div class="schedule-head">
        <Icon name="clock" :size="18" />
        <div>
          <h3>Run these tests again on a schedule</h3>
          <p class="muted">The tests you confirmed run again on the same database, read-only, while <code>dqvouch ui</code>
            is running on this computer; each execution is kept below.</p>
        </div>
      </div>
      <div class="schedule-form">
        <label>Every <input v-model.number="form.n" type="number" min="1" step="1" aria-label="how many" /></label>
        <select v-model.number="form.unit" aria-label="unit"><option v-for="[u, name] in UNITS" :key="u" :value="u">{{ name }}</option></select>
        <button class="primary" :disabled="!(form.n >= 1)" @click="saveSchedule(everyMinutes)">{{ schedule ? 'Update' : 'Start schedule' }}</button>
        <button v-if="schedule" @click="saveSchedule(null)">Stop</button>
        <span v-if="schedule" class="muted">Runs {{ everyLabel(schedule.every_minutes) }} · next {{ at(schedule.next_at) }}</span>
      </div>
      <details class="os-schedule alerts" @toggle="$event.target.open && loadAlerts()">
        <summary>Alerts when a test starts failing</summary>
        <p class="muted">One message when a test starts failing or erroring, or when an execution cannot run; not again
          while it stays that way. These settings apply to every run of this dqvouch and are saved in its
          <code>.env</code> file.</p>
        <div class="alert-form">
          <label class="wide">Webhook URL
            <span class="secret-row">
              <input v-model="alertTyped.DQVOUCH_ALERT_WEBHOOK_URL" type="password" autocomplete="off"
                     :placeholder="savedSecret('DQVOUCH_ALERT_WEBHOOK_URL')">
              <button v-if="alertSecrets.DQVOUCH_ALERT_WEBHOOK_URL?.set" type="button" class="link"
                      @click="forgetSecret('DQVOUCH_ALERT_WEBHOOK_URL')">Remove</button>
            </span>
            <small>Slack, Mattermost, Google Chat, Rocket.Chat or a Teams workflow; for Discord, add /slack to its
              webhook URL.</small>
          </label>
          <label v-for="[name, title, hint] in ALERT_FIELDS" :key="name">{{ title }}
            <input v-model="alertValues[name]" :placeholder="hint">
          </label>
          <label>Password
            <input v-model="alertTyped.DQVOUCH_SMTP_PASSWORD" type="password" autocomplete="off"
                   :placeholder="savedSecret('DQVOUCH_SMTP_PASSWORD')">
          </label>
        </div>
        <div class="schedule-form">
          <button class="primary" @click="saveAlerts">Save alerts</button>
          <button @click="testAlert">Send a test alert</button>
          <span v-if="alertNote" class="muted">{{ alertNote }}</span>
        </div>
      </details>
      <details class="os-schedule" @toggle="commandsOpen = $event.target.open; loadCommands()">
        <summary>Run it with dqvouch closed</summary>
        <p class="muted">Your operating system can run these tests {{ form.n >= 1 ? everyLabel(everyMinutes) : '' }} with
          <code>dqvouch rerun</code>, which needs no window open. It exits with 1 when a test fails, so CI and
          orchestrators (Airflow, GitHub Actions) can use it too. Both schedules may be on: two executions of this run
          never overlap.</p>
        <template v-if="commands">
          <div class="command">
            <span class="muted">Linux or macOS: add to <code>crontab -e</code></span>
            <pre>{{ commands.cron }}</pre>
            <button @click="copy(commands.cron)">Copy</button>
          </div>
          <div class="command">
            <span class="muted">Windows: run once in a Command Prompt</span>
            <pre>{{ commands.schtasks }}</pre>
            <button @click="copy(commands.schtasks)">Copy</button>
          </div>
        </template>
      </details>
      <div v-if="historyShown.length" class="table-scroll">
        <table class="grid history">
          <thead><tr><th>When</th><th>How</th><th>Passed</th><th>Failed</th><th>No verdict</th><th>Started failing</th></tr></thead>
          <tbody>
            <tr v-for="h in historyShown" :key="h.at">
              <td class="nowrap">{{ at(h.at) }}</td><td>{{ TRIGGERS[h.trigger] || h.trigger }}</td>
              <template v-if="h.error"><td colspan="4" class="bad-text">{{ h.error }}
                <span v-if="h.alerted" class="chip">alert sent ({{ h.alerted.join(', ') }})</span>
                <small v-if="h.alert_error">alert not sent: {{ h.alert_error }}</small></td></template>
              <template v-else>
                <td class="ok-text">{{ h.counts.pass || 0 }}</td><td :class="{ 'bad-text': h.counts.fail }">{{ h.counts.fail || 0 }}</td>
                <td>{{ Object.entries(h.counts).filter(([v]) => v !== 'pass' && v !== 'fail').reduce((n, [, c]) => n + c, 0) }}</td>
                <td><span v-for="id in h.newFailures" :key="id" class="chip bad-chip">{{ String(id).split('__').at(-1) }}</span>
                  <span v-if="h.alerted" class="chip">alert sent ({{ h.alerted.join(', ') }})</span>
                  <small v-if="h.alert_error" class="bad-text">alert not sent: {{ h.alert_error }}</small></td>
              </template>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div class="filters">
      <select v-model="filters.asset" aria-label="table"><option value="">All tables</option><option v-for="o in options('asset')" :key="o">{{ o }}</option></select>
      <select v-model="filters.dimension" aria-label="dimension"><option value="">All dimensions</option><option v-for="o in options('dimension')" :key="o">{{ o }}</option></select>
      <select v-model="filters.verdict" aria-label="verdict"><option value="">All verdicts</option><option v-for="o in options('verdict')" :key="o" :value="o">{{ label(o) }}</option></select>
      <span class="spacer" style="flex: 1" />
      <button @click="download('csv')">Export CSV</button>
      <button @click="download('json')">Export JSON</button>
      <a class="btn" :href="`/api/runs/${encodeURIComponent(run.state.id)}/export.sql`" download
         title="The confirmed tests as SQL, to run them in your own tools">Export SQL</a>
    </div>
    <div v-for="r in shown" :key="r.check_id" class="item">
      <h3><span class="verdict" :class="r.verdict">{{ label(r.verdict) }}</span> {{ r.title || String(r.check_id).split('__').at(-1) }}</h3>
      <p class="muted">{{ r.asset }} · {{ r.dimension }}</p>
      <p>{{ r.description }}</p>
      <p><strong>Observed:</strong> {{ show(r.observed) }} · <strong>Expected:</strong> {{ r.expected }}</p>
      <p v-if="trend(history, r.check_id).length >= 2" class="trend">
        <Sparkline :values="trend(history, r.check_id)" :threshold="limitOf(r)" />
        <span class="muted">last {{ trend(history, r.check_id).length }} executions</span>
      </p>
      <p v-if="r.error" class="error">{{ r.error }}</p>
      <details v-if="r.sql"><summary>SQL that ran</summary><pre>{{ r.sql }}</pre></details>
    </div>
    <p v-if="!shown.length" class="muted">No result matches these filters.</p>
  </StageFrame>
</template>
