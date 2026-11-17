<script setup>
// Screen 1. Without a stage prop it starts a new run; with stage="config" it shows an existing run's settings.
import { computed, inject, onMounted, reactive, ref } from 'vue'
import { api } from '../api.js'
import { portForEngine, toHash } from '../logic.js'
import StageFrame from '../StageFrame.vue'

const props = defineProps({ stage: { type: String, default: null } })
const run = inject('run')
const readonly = computed(() => props.stage === 'config')

const form = reactive({ engine: 'postgres', host: 'localhost', port: 5432, user: '', database: '', schemas: '',
                        query_timeout_seconds: 600, sql_dir: '', model: 'gemini-3.5-flash-lite',
                        provider: 'gemini', base_url: 'http://localhost:11434/v1', propose_on_change: true })
const OLLAMA = 'http://localhost:11434/v1'
const keyName = computed(() => (form.provider === 'openai' ? 'OPENAI_API_KEY' : 'GEMINI_API_KEY'))
function chooseProvider() {
  models.value = []
  freeModel.value = form.provider === 'openai'
  notes.gemini = ''
  if (form.provider === 'gemini' && !form.model.startsWith('gemini')) form.model = 'gemini-3.5-flash-lite'
  if (form.provider === 'openai' && form.model.startsWith('gemini')) form.model = ''
}
const secrets = ref({})
const envFile = ref('')
const password = ref('')
const apiKey = ref('')
const models = ref([])
const freeModel = ref(false)
const notes = reactive({ connection: '', writeWarning: '', gemini: '', start: '' })
const busy = ref(false)

const SOURCES = { environment: 'from your environment', env_file: 'in .env', config_file: 'from the config file' }

onMounted(async () => {
  try {
    const s = await api.settings()
    // An existing run shows its own saved settings; the secrets are this computer's, for every run.
    const settings = readonly.value ? run.state.info.settings : s.settings
    const d = settings.database
    Object.assign(form, {
      engine: d.engine, host: d.host, port: d.port, user: d.user, database: d.database || '',
      schemas: (d.schemas || []).join(', '), query_timeout_seconds: d.query_timeout_seconds ?? 600,
      sql_dir: settings.sql_dir || '', model: settings.model || form.model,
      provider: settings.provider || 'gemini', base_url: settings.base_url || OLLAMA,
      propose_on_change: settings.propose_on_change ?? true, // from the config file; a new run keeps it
    })
    secrets.value = s.secrets
    envFile.value = s.env_file
  } catch (e) {
    notes.start = e.message
  }
})

function placeholder(name) {
  const s = secrets.value[name]
  return s && s.set ? `saved (${SOURCES[s.source] || s.source})` : ''
}

function database() {
  return {
    engine: form.engine, host: form.host, port: Number(form.port), user: form.user,
    database: form.engine === 'postgres' ? form.database : '',
    schemas: form.schemas.split(',').map((s) => s.trim()).filter(Boolean),
    query_timeout_seconds: Number(form.query_timeout_seconds),
  }
}

async function saveTyped() {
  if (password.value) {
    secrets.value = await api.saveSecret('DQVOUCH_DB_PASSWORD', password.value)
    password.value = ''
  }
  if (apiKey.value) {
    secrets.value = await api.saveSecret(keyName.value, apiKey.value)
    apiKey.value = ''
  }
}

async function guarded(field, action) {
  busy.value = true
  notes[field] = ''
  try {
    await saveTyped()
    await action()
  } catch (e) {
    notes[field] = e.message
  } finally {
    busy.value = false
  }
}

const testConnection = () => guarded('connection', async () => {
  notes.writeWarning = ''
  const r = await api.testConnection(database())
  notes.connection = r.ok ? `Connected: ${r.tables} table(s) in these schemas.` : `Could not connect: ${r.error}`
  notes.writeWarning = (r.ok && r.warning) || ''
})

const testKey = () => guarded('gemini', async () => {
  const local = form.provider === 'openai'
  const r = local ? await api.testOpenAI(form.base_url) : await api.testGemini()
  if (r.ok) {
    models.value = !form.model || r.models.includes(form.model) ? r.models : [form.model, ...r.models]
    if (!form.model && r.models.length) form.model = r.models[0]
    freeModel.value = false
    notes.gemini = local ? `The server answers: ${r.models.length} model(s) available.`
      : `The key works: ${r.models.length} model(s) available.`
  } else {
    freeModel.value = true
    notes.gemini = `Could not list the models: ${r.error}`
  }
})

function saveOnly() {
  const typed = Boolean(password.value || apiKey.value)
  return guarded('start', async () => { notes.start = typed ? 'Saved.' : 'Nothing to save: type a new value first.' })
}

const startRun = () => guarded('start', async () => {
  const local = form.provider === 'openai'
  const { id } = await api.createRun({ database: database(), sql_dir: form.sql_dir || null, model: form.model,
                                       provider: form.provider, base_url: local ? form.base_url : null,
                                       propose_on_change: form.propose_on_change })
  await api.start(id, 'context', {})
  location.hash = toHash({ view: 'run', runId: id, stage: 'context' })
})
</script>

<template>
  <StageFrame stage="config">
    <table v-if="readonly" class="grid settings">
      <tbody>
        <tr><th>Engine</th><td>{{ form.engine }}</td></tr>
        <tr><th>Server</th><td>{{ form.host }}:{{ form.port }}</td></tr>
        <tr><th>User</th><td>{{ form.user }}</td></tr>
        <tr v-if="form.engine === 'postgres'"><th>Database</th><td>{{ form.database }}</td></tr>
        <tr><th>Schemas</th><td>{{ form.schemas }}</td></tr>
        <tr><th>Query time limit</th><td>{{ Number(form.query_timeout_seconds) ? `${form.query_timeout_seconds} s` : 'none' }}</td></tr>
        <tr><th>SQL folder</th><td>{{ form.sql_dir || 'none (no lineage, no SQL guarantees)' }}</td></tr>
        <tr><th>Model</th><td>{{ form.model }}<span v-if="form.provider === 'openai'" class="muted"> at {{ form.base_url }}</span></td></tr>
      </tbody>
    </table>

    <form v-if="readonly" class="config" @submit.prevent>
      <fieldset>
        <legend>Password and API key</legend>
        <p class="note muted">These are saved in {{ envFile || '.env' }} on this computer and apply to every run, this one
          included. A run's other settings cannot change: start a new run for that.</p>
        <label>Database password
          <input v-model="password" type="password" autocomplete="current-password" :placeholder="placeholder('DQVOUCH_DB_PASSWORD')">
        </label>
        <label>{{ form.provider === 'openai' ? 'Model server key (optional)' : 'Gemini API key' }}
          <input v-model="apiKey" type="password" autocomplete="off" :placeholder="placeholder(keyName)">
        </label>
        <button type="button" :disabled="busy" @click="saveOnly">Save</button>
        <button type="button" :disabled="busy" @click="testConnection">Test connection</button>
        <button type="button" :disabled="busy" @click="testKey">{{ form.provider === 'openai' ? 'Test the model server' : 'Test key' }}</button>
        <p v-if="notes.connection" class="note">{{ notes.connection }}</p>
        <p v-if="notes.writeWarning" class="note warn">{{ notes.writeWarning }}</p>
        <p v-if="notes.gemini" class="note">{{ notes.gemini }}</p>
        <p v-if="notes.start" class="note">{{ notes.start }}</p>
      </fieldset>
    </form>

    <form v-else class="config" @submit.prevent>
      <fieldset>
        <legend>Database</legend>
        <label>Engine
          <select v-model="form.engine" @change="form.port = portForEngine(form.engine, form.port)">
            <option value="postgres">PostgreSQL</option><option value="clickhouse">ClickHouse</option>
          </select>
        </label>
        <label>Host <input v-model="form.host" required></label>
        <label>Port <input v-model="form.port" type="number" required>
          <small v-if="form.engine === 'clickhouse'">The HTTP port (usually 8123), not the native 9000.</small>
        </label>
        <label>User <input v-model="form.user" autocomplete="username"></label>
        <label>Password
          <input v-model="password" type="password" autocomplete="current-password" :placeholder="placeholder('DQVOUCH_DB_PASSWORD')">
        </label>
        <label v-if="form.engine === 'postgres'">Database <input v-model="form.database"></label>
        <label class="wide">Schemas to analyse <input v-model="form.schemas" placeholder="landing, staging, analytic">
          <small>{{ form.engine === 'postgres' ? 'PostgreSQL schemas' : 'ClickHouse databases' }}, separated by commas.</small>
        </label>
        <label>Query time limit (s) <input v-model="form.query_timeout_seconds" type="number" min="0" step="1">
          <small>The longest one query may run; 0 for no limit.</small>
        </label>
        <button type="button" :disabled="busy" @click="testConnection">Test connection</button>
        <p v-if="notes.connection" class="note">{{ notes.connection }}</p>
        <p v-if="notes.writeWarning" class="note warn">{{ notes.writeWarning }}</p>
      </fieldset>

      <fieldset>
        <legend>Transformation SQL (optional)</legend>
        <label class="wide">Folder with the SQL that builds your tables <input v-model="form.sql_dir" placeholder="C:\path\to\sql">
          <small>Each file is matched to the table it produces (INSERT INTO, CREATE TABLE AS, CREATE VIEW). Without it
            there is no lineage, no SQL in the context and no SQL guarantees stage.</small>
        </label>
      </fieldset>

      <fieldset>
        <legend>Model</legend>
        <label class="wide">Where the model runs
          <select v-model="form.provider" @change="chooseProvider">
            <option value="gemini">Gemini (Google, hosted)</option>
            <option value="openai">OpenAI-compatible server: Ollama, LM Studio, vLLM, OpenAI…</option>
          </select>
          <small v-if="form.provider === 'openai'">A model on your own machine keeps the metadata and the profiling
            numbers inside your network.</small>
        </label>
        <label v-if="form.provider === 'openai'" class="wide">Server address
          <input v-model="form.base_url" placeholder="http://localhost:11434/v1">
          <small>Ollama: http://localhost:11434/v1 · LM Studio: http://localhost:1234/v1 · OpenAI: https://api.openai.com/v1</small>
        </label>
        <label class="wide">{{ form.provider === 'openai' ? 'API key (optional)' : 'API key' }}
          <input v-model="apiKey" type="password" autocomplete="off" :placeholder="placeholder(keyName)">
          <small v-if="form.provider === 'gemini'">Create one in <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Google AI Studio</a>.
            Saved in {{ envFile || '.env' }} on this computer and sent only to Google.</small>
          <small v-else>Only if the server asks for one (local servers usually do not). Saved in
            {{ envFile || '.env' }} as OPENAI_API_KEY.</small>
          <small v-if="secrets[keyName]?.source === 'environment'" class="warn">
            {{ keyName }} is set in your environment, which takes priority.</small>
        </label>
        <label class="wide">Model
          <select v-if="models.length && !freeModel" v-model="form.model">
            <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
          </select>
          <input v-else v-model="form.model" :placeholder="form.provider === 'openai' ? 'e.g. qwen3:14b' : ''">
          <small v-if="form.provider === 'gemini'">gemini-3.5-flash-lite is the model the thesis evaluated.</small>
          <small v-else>The thesis measured dqvouch with Gemini; a local model's tests may differ.</small>
        </label>
        <button type="button" :disabled="busy" @click="testKey">{{ form.provider === 'openai' ? 'Test the server and list models' : 'Test key' }}</button>
        <p v-if="notes.gemini" class="note">{{ notes.gemini }}</p>
      </fieldset>
      <p v-if="notes.start" class="error">{{ notes.start }}</p>
    </form>

    <template v-if="!readonly" #actions>
      <a class="btn ghost" href="#/">Cancel</a>
      <button class="primary" :disabled="busy" @click="startRun">Start</button>
    </template>
  </StageFrame>
</template>
