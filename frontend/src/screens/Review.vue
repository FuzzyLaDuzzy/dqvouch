<script setup>
// Screen 7: the person decides. Only confirmed items run; "Run confirmed tests" is the one action that runs tests.
import { computed, inject, onMounted, reactive, ref } from 'vue'
import { reviewPayload, startingDecisions, testGroups } from '../logic.js'
import StageFrame from '../StageFrame.vue'
import TestCard from '../TestCard.vue'

defineProps({ stage: { type: String, required: true } })
const run = inject('run')
const OPERATORS = ['equals', 'not_equals', 'less_than_or_equal', 'greater_than_or_equal', 'less_than', 'greater_than']
const decisions = reactive({})
const edits = reactive({})
const busy = ref(false)
const error = ref('')

const tests = computed(() => run.state.data.tests || {})
const groups = computed(() => testGroups(tests.value))
const specs = computed(() => [...groups.value.flagged, ...groups.value.accepted])
const thresholds = computed(() => tests.value.metric_thresholds || [])
const items = computed(() => [...specs.value, ...thresholds.value])
const confirmed = computed(() => items.value.filter((i) => decisions[i.id] === 'confirmed').length)
// Tests decided in the last review of this database (or kept by a structural change) start with that decision;
// the person can show only the new ones.
const onlyNew = ref(false)
const fromEarlier = computed(() => items.value.filter((s) => s._previous))
const earlierRun = computed(() => fromEarlier.value[0]?._previous.run)
const visible = computed(() => (onlyNew.value ? items.value.filter((i) => !i._previous) : items.value))
// A run made by a structural change found after a scheduled execution: what changed, what was kept and set aside.
const origin = computed(() => (run.state.info?.origin?.kind === 'change' ? run.state.info.origin : null))
const excluded = computed(() => items.value.filter((i) => decisions[i.id] === 'excluded').length)

onMounted(async () => {
  try {
    await run.load('tests')
    await run.load('review') // a review saved earlier comes back with its decisions and its edits
    const saved = startingDecisions(specs.value, thresholds.value, run.state.data.review)
    Object.assign(decisions, saved.decisions)
    Object.assign(edits, saved.edits)
  } catch (e) {
    error.value = e.message
  }
})

const isMetric = (item) => !item.assertion
function limit(item) {
  const base = isMetric(item) ? { operator: item.operator, value: item.value }
    : edits[item.id]?.restoreSuspended ? item.suspended_threshold : item.threshold
  return base ? { ...base, ...(edits[item.id]?.threshold || {}) } : null
}
function setLimit(item, field, raw) {
  const value = field === 'value' && raw.trim() !== '' && !Number.isNaN(Number(raw)) ? Number(raw) : raw
  edits[item.id] = { ...edits[item.id], threshold: { ...(edits[item.id]?.threshold || {}), [field]: value } }
}
function toggleRestore(item) {
  edits[item.id] = { ...edits[item.id], restoreSuspended: !edits[item.id]?.restoreSuspended }
}
const listOf = (item) => (['in', 'not_in'].includes(item.assertion?.condition?.op) ? item.assertion.condition.values : null)
function setValues(item, raw) {
  const original = listOf(item)
  const numeric = original.length && original.every((v) => typeof v === 'number')
  const values = raw.split(',').map((v) => v.trim()).filter(Boolean).map((v) => (numeric ? Number(v) : v))
  edits[item.id] = { ...edits[item.id], values }
}
function confirmUndecided() {
  for (const i of items.value) if (!decisions[i.id]) decisions[i.id] = 'confirmed'
}

async function runConfirmed() {
  busy.value = true
  error.value = ''
  try {
    await run.submitReview(reviewPayload(specs.value, thresholds.value, decisions, edits))
    run.start('results')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <StageFrame :stage="stage">
    <div v-if="origin" class="note change">
      <p v-if="run.state.info.replaced_by"><strong>This run was replaced by
        <code>{{ run.state.info.replaced_by }}</code></strong>: the structure changed again before it was reviewed.
        Review that one instead; this one will not take over the schedule.</p>
      <p>The structure changed since run <code>{{ origin.from_run }}</code>:</p>
      <ul><li v-for="line in origin.summary" :key="line">{{ line }}</li></ul>
      <p>{{ origin.kept }} test(s) kept from run <code>{{ origin.from_run }}</code> start confirmed.</p>
      <template v-if="origin.set_aside.length">
        <p>Set aside (they no longer fit the tables):</p>
        <ul><li v-for="a in origin.set_aside" :key="a.id"><code>{{ a.id }}</code>: {{ a.reason }}</li></ul>
      </template>
    </div>
    <p class="muted">{{ confirmed }} confirmed, {{ excluded }} excluded, {{ items.length - confirmed - excluded }} undecided.
      Only confirmed items run.
      <button class="link" @click="confirmUndecided">Confirm all undecided</button></p>
    <p v-if="fromEarlier.length" class="note reused">{{ fromEarlier.length }} of these tests were decided in run
      <code>{{ earlierRun }}</code> of this database and start with that decision<template
        v-if="items.length > fromEarlier.length">; check them, or
      <label class="inline"><input v-model="onlyNew" type="checkbox"> show only the {{ items.length - fromEarlier.length }} new
        {{ items.length - fromEarlier.length === 1 ? 'one' : 'ones' }}</label></template><template v-else>; none is
        new</template>.</p>

    <template v-for="item in visible" :key="item.id">
      <TestCard v-if="!isMetric(item)" :spec="item" :class="decisions[item.id]">
        <span v-if="item._previous" class="chip">decided in run {{ item._previous.run }}: {{ item._previous.status }}</span>
        <div class="filters">
          <button :class="{ on: decisions[item.id] === 'confirmed' }" @click="decisions[item.id] = 'confirmed'">Confirm</button>
          <button :class="{ on: decisions[item.id] === 'excluded' }" @click="decisions[item.id] = 'excluded'">Exclude</button>
          <button v-if="item.suspended_threshold" :class="{ on: edits[item.id]?.restoreSuspended }" @click="toggleRestore(item)">
            Restore threshold</button>
          <template v-if="limit(item)">
            <select :value="limit(item).operator" @change="setLimit(item, 'operator', $event.target.value)">
              <option v-for="o in OPERATORS" :key="o" :value="o">{{ o.replace(/_/g, ' ') }}</option>
            </select>
            <input :value="limit(item).value" size="8" aria-label="threshold value" @change="setLimit(item, 'value', $event.target.value)">
          </template>
          <label v-if="listOf(item)">Allowed values
            <input :value="(edits[item.id]?.values || listOf(item)).join(', ')" size="40" @change="setValues(item, $event.target.value)"></label>
        </div>
      </TestCard>
      <div v-else class="item" :class="decisions[item.id]">
        <h3>{{ item.title || item.id }}</h3>
        <p class="muted">Limit on a measured metric · {{ item.asset }} · <code>{{ item.check_id }}</code></p>
        <div class="filters">
          <button :class="{ on: decisions[item.id] === 'confirmed' }" @click="decisions[item.id] = 'confirmed'">Confirm</button>
          <button :class="{ on: decisions[item.id] === 'excluded' }" @click="decisions[item.id] = 'excluded'">Exclude</button>
          <span>{{ item.metric }}</span>
          <select :value="limit(item).operator" @change="setLimit(item, 'operator', $event.target.value)">
            <option v-for="o in OPERATORS" :key="o" :value="o">{{ o.replace(/_/g, ' ') }}</option>
          </select>
          <input :value="limit(item).value" size="8" aria-label="threshold value" @change="setLimit(item, 'value', $event.target.value)">
        </div>
      </div>
    </template>
    <p v-if="!items.length" class="muted">No test was proposed, so there is nothing to review.</p>
    <p v-if="error" class="error">{{ error }}</p>

    <template #actions>
      <a v-if="run.state.data.review" class="btn ghost" :href="`/api/runs/${encodeURIComponent(run.state.id)}/export.sql`"
         download title="The tests confirmed in the saved review, as SQL for your own tools">Export SQL</a>
      <button class="primary" :disabled="busy || confirmed === 0" @click="runConfirmed">Run confirmed tests ({{ confirmed }})</button>
    </template>
  </StageFrame>
</template>
