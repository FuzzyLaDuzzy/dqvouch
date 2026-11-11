// One function per route of the local server (src/dqvouch/server.py). Errors carry the server's `detail`.

async function call(method, path, body) {
  const init = { method }
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  const res = await fetch(path, init)
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) {
    const detail = data && data.detail
    throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `${res.status} ${res.statusText}`)
  }
  return data
}

const run = (id) => `/api/runs/${encodeURIComponent(id)}`

export const api = {
  settings: () => call('GET', '/api/settings'),
  saveSecret: (name, value) => call('PUT', `/api/secrets/${name}`, { value }),
  testConnection: (database) => call('POST', '/api/connection/test', { database }),
  testGemini: () => call('POST', '/api/gemini/test', {}),
  testOpenAI: (baseUrl) => call('POST', '/api/openai/test', { base_url: baseUrl }),
  runs: () => call('GET', '/api/runs'),
  createRun: (settings) => call('POST', '/api/runs', settings),
  run: (id) => call('GET', run(id)),
  stage: (id, stage) => call('GET', `${run(id)}/stages/${stage}`),
  start: (id, stage, body = {}) => call('POST', `${run(id)}/stages/${stage}`, body),
  schedule: (id) => call('GET', `${run(id)}/schedule`),
  setSchedule: (id, everyMinutes) => call('PUT', `${run(id)}/schedule`, { every_minutes: everyMinutes }),
  alerts: () => call('GET', '/api/alerts'),
  setAlerts: (values) => call('PUT', '/api/alerts', values),
  testAlert: () => call('POST', '/api/alerts/test', {}),
  scheduleCommands: (id, everyMinutes) => call('GET', `${run(id)}/schedule/commands?every_minutes=${everyMinutes}`),
}

// Polls the open run: every `interval` ms while one of its stages runs, every `idle` ms otherwise (a scheduled
// execution can start at any time). onUpdate(run) gets every answer. Returns stop().
export function watchRun(id, onUpdate, interval = 1000, idle = 5000) {
  let stopped = false
  async function tick() {
    if (stopped) return
    let current = null
    try {
      current = await api.run(id)
    } catch {
      // the server restarted or is busy: try again on the next tick
    }
    if (stopped) return
    if (current) onUpdate(current)
    setTimeout(tick, !current || current.job?.status === 'running' ? interval : idle)
  }
  tick()
  return () => { stopped = true }
}
