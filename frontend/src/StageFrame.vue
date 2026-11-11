<script setup>
// What every screen has: which step it is and who does it, what happens here, calls and time, progress or error,
// what was sent to Gemini, the saved JSON, and Back / Redo / Continue. The screen's own content goes in the slot.
import { computed, inject, onMounted, onUnmounted, ref } from 'vue'
import Icon from './Icon.vue'
import { STAGES, STAGE_INFO, jobProgress, llmPrompts } from './logic.js'

const props = defineProps({
  stage: { type: String, required: true },
  onContinue: { type: Function, default: null },   // replaces the default Continue
  redoBody: { type: Object, default: () => ({}) },  // what Redo / Try again send with the stage
})
const run = inject('run')

const info = computed(() => STAGE_INFO[props.stage])
const index = computed(() => STAGES.indexOf(props.stage))
const step = computed(() => (props.stage === 'config' ? 1 : index.value + 2))
const data = computed(() => run.state.data[props.stage])
const job = computed(() => (run.state.job && run.state.job.stage === props.stage ? run.state.job : null))
const running = computed(() => job.value?.status === 'running')
const failed = computed(() => job.value?.status === 'error')
const done = computed(() => run.state.completed.includes(props.stage))
const interactive = computed(() => props.stage === 'config' || props.stage === 'review')
const prompts = computed(() => llmPrompts(props.stage, data.value))
const previous = computed(() => (props.stage === 'config' ? null : index.value === 0 ? 'config' : STAGES[index.value - 1]))
const next = computed(() => (props.stage === 'config' ? 'context' : STAGES[index.value + 1] ?? null))
const laterDone = computed(() => run.state.completed.some((s) => STAGES.indexOf(s) > index.value))
// A clock for the elapsed time while the stage runs.
const now = ref(Date.now())
let clock
onMounted(() => { clock = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => clearInterval(clock))
const progress = computed(() => jobProgress(job.value, now.value))
const kind = computed(() => (info.value.kind.startsWith('LLM') ? 'llm' : info.value.kind === 'you' ? 'you' : 'code'))

async function redo() {
  if (laterDone.value && !(await run.confirm(`Redoing ${info.value.title} deletes the results of every later stage.`, 'Redo'))) return
  run.start(props.stage, props.redoBody)
}

function proceed() {
  if (props.onContinue) return props.onContinue()
  // Going forward again after Back shows the saved result; only Redo runs a finished stage again.
  if (props.stage === 'config' || next.value === 'review' || run.state.completed.includes(next.value)) return run.go(next.value)
  run.start(next.value)
}
</script>

<template>
  <section>
    <header class="page-head">
      <div class="kind-badge" :class="kind"><Icon :name="kind" :size="22" /></div>
      <div>
        <p class="eyebrow">Step {{ step }} of 8 · {{ info.kind }}</p>
        <h1>{{ info.title }}</h1>
        <p class="lede"><strong>What happens here.</strong> {{ info.explanation }}</p>
      </div>
      <div v-if="data && (data.llm_calls !== undefined || data.seconds !== undefined)" class="chips">
        <span v-if="data.llm_calls !== undefined" class="chip"><Icon name="llm" :size="13" />{{ data.llm_calls }} call{{ data.llm_calls === 1 ? '' : 's' }}</span>
        <span v-if="data.seconds !== undefined" class="chip"><Icon name="clock" :size="13" />{{ data.seconds }} s</span>
      </div>
    </header>

    <Transition name="fade" mode="out-in">
      <div v-if="running" key="running" class="progress" role="status">
        <div class="progress-line">
          <span class="spinner" /><span class="progress-text">{{ job.message }}</span>
          <span v-if="progress.pct !== null" class="progress-num">{{ progress.pct }}%</span>
          <span v-if="progress.elapsed" class="progress-time"><Icon name="clock" :size="13" />{{ progress.elapsed }}</span>
        </div>
        <p v-if="job.note" class="progress-note"><Icon name="alert" :size="14" />{{ job.note }}</p>
        <div class="progress-bar" :class="{ determinate: progress.pct !== null }">
          <span v-if="progress.pct !== null" :style="{ width: `${progress.pct}%` }" />
        </div>
      </div>
      <div v-else-if="failed" key="failed" class="error" role="alert">
        <p>{{ job.error }}</p>
        <div class="filters">
          <button v-if="job.error_kind === 'key' || job.error_kind === 'connection'" @click="run.go('config')">Open Configuration</button>
          <button @click="run.start(stage, redoBody)">Try again</button>
        </div>
      </div>
      <div v-else-if="!done && !interactive" key="notrun" class="notrun">
        <p>This stage has not run yet.</p>
        <button class="primary" @click="run.start(stage, redoBody)"><Icon name="play" :size="14" />Run {{ info.title }}</button>
      </div>
    </Transition>

    <Transition name="fade">
      <div v-if="(done && data) || interactive" class="content stagger"><slot /></div>
    </Transition>

    <details v-if="prompts.length">
      <summary>Sent to Gemini ({{ prompts.length }} prompt{{ prompts.length === 1 ? '' : 's' }})</summary>
      <div v-for="(p, i) in prompts" :key="i"><h4>{{ p.scope }}</h4><pre>{{ p.prompt }}</pre></div>
    </details>
    <details v-if="data"><summary>View JSON</summary><pre>{{ JSON.stringify(data, null, 2) }}</pre></details>

    <footer class="actions">
      <button v-if="previous" class="btn ghost" @click="run.go(previous)"><Icon name="arrow-left" :size="16" />Back</button>
      <span class="spacer" />
      <button v-if="done && !interactive" :disabled="running" @click="redo">Redo</button>
      <slot name="actions">
        <button v-if="next" class="primary" :disabled="running || (!done && !interactive)" @click="proceed">
          Continue<Icon name="arrow-right" :size="16" />
        </button>
      </slot>
    </footer>
  </section>
</template>
