// Pure helpers for the wizard (no Vue, no fetch): navigation, stage descriptions, what goes to Gemini.

export const STAGES = ['context', 'dimensions', 'profiling', 'sql_rules', 'tests', 'review', 'results']

export const STAGE_INFO = {
  config: {
    title: 'Configuration', kind: 'you',
    explanation: 'Tell dqvouch where your database is and which Gemini model to use. dqvouch only reads: a read-only user is enough.',
  },
  context: {
    title: 'Context', kind: 'code',
    explanation: "dqvouch reads every table's columns, types and comments, matches each table to the SQL file that builds it, and extracts facts from that SQL. Nothing is sent to Gemini yet. Untick the tables you do not want to analyse.",
  },
  dimensions: {
    title: 'Dimensions', kind: 'LLM, 1 call',
    explanation: 'Gemini reads the context of all tables and proposes which quality dimensions to check on which columns. The validator drops anything that names a table or column that does not exist.',
  },
  profiling: {
    title: 'Profiling', kind: 'code',
    explanation: 'dqvouch measures the data for the accepted dimensions (null counts, duplicates, frequent values...) with read-only queries. Only measurements, no thresholds.',
  },
  sql_rules: {
    title: 'SQL guarantees', kind: 'LLM, 2 calls per transformation',
    explanation: 'For each transformation, Gemini lists what its SQL guarantees about the output and requires of its inputs, each tied to the SQL excerpt that enforces it. The validator keeps only what it can check.',
  },
  tests: {
    title: 'Proposed tests', kind: 'LLM + code',
    explanation: 'Gemini turns the dimensions, the measurements and the SQL guarantees into concrete tests from a closed catalog of five kinds. The validator checks each one: tests the data contradicts are flagged, rejected ones can be sent back once to be repaired.',
  },
  review: {
    title: 'Review', kind: 'you',
    explanation: 'Confirm, edit or exclude each test. Only confirmed tests run, and your edits are validated again.',
  },
  results: {
    title: 'Results', kind: 'code',
    explanation: 'Each confirmed test becomes SQL and runs read-only. A test without a threshold reports what it observed.',
  },
}

export function parseHash(hash) {
  const parts = (hash || '').replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent)
  if (parts[0] === 'new' && parts.length === 1) return { view: 'new' }
  if (parts[0] === 'run' && parts[1]) return { view: 'run', runId: parts[1], stage: parts[2] || null }
  return { view: 'runs' }
}

export function toHash(route) {
  if (route.view === 'new') return '#/new'
  if (route.view === 'run') return `#/run/${encodeURIComponent(route.runId)}` + (route.stage ? `/${route.stage}` : '')
  return '#/'
}

export function nextStage(completed) {
  return STAGES.find((s) => !completed.includes(s)) ?? null
}

export function llmPrompts(stage, data) {
  if (!data) return []
  if (stage === 'dimensions' && data.prompt) return [{ scope: 'all tables', prompt: data.prompt }]
  if ((stage === 'sql_rules' || stage === 'tests') && Array.isArray(data.calls)) {
    return data.calls.map((c) => ({ scope: c.scope, prompt: c.prompt }))
  }
  return []
}

// Evidence paths may carry the table in front ("analytic.x.columns.c.description"), so any segment can name the origin.
const ORIGINS = [
  ['sql_invariants', 'SQL guarantee'], ['transformation', 'transformation SQL'], ['dq_signals', 'signal from the SQL'],
  ['lineage', 'lineage'], ['downstream_usage', 'downstream usage'],
]

export function evidenceOrigin(path) {
  const p = String(path || '')
  if (p.startsWith('profile:')) return 'profiling'
  const parts = p.split('.')
  const hit = ORIGINS.find(([key]) => parts.includes(key))
  if (hit) return hit[1]
  return parts.includes('description') ? 'description' : 'schema'
}

export function testGroups(data) {
  const specs = data?.test_specifications || []
  const flaggedSpec = (s) => Array.isArray(s.review_warnings) && s.review_warnings.length > 0
  return { accepted: specs.filter((s) => !flaggedSpec(s)), flagged: specs.filter(flaggedSpec), rejected: data?.rejected || [] }
}

// decisions: {id: 'confirmed' | 'excluded'}; anything not confirmed is sent as excluded, so nothing runs unseen.
// edits: {id: {threshold: {operator, value}, values: [...], restoreSuspended: true}}.
export function reviewPayload(specs, thresholds, decisions, edits) {
  const decide = (item) => {
    const out = JSON.parse(JSON.stringify(item)) // also copies Vue reactive proxies, which structuredClone refuses
    const edit = edits[item.id] || {}
    if (edit.restoreSuspended && out.suspended_threshold) {
      out.threshold = out.suspended_threshold
      delete out.suspended_threshold
    }
    if (edit.threshold) {
      if (out.threshold) Object.assign(out.threshold, edit.threshold)
      else if ('operator' in out) Object.assign(out, edit.threshold) // a metric threshold: operator/value at the top
    }
    const condition = out.assertion?.condition
    if (edit.values && condition && ['in', 'not_in'].includes(condition.op)) condition.values = [...edit.values]
    out.status = decisions[item.id] === 'confirmed' ? 'confirmed' : 'excluded'
    return out
  }
  return { test_specifications: specs.map(decide), metric_thresholds: thresholds.map(decide),
    undecided: [...specs, ...thresholds].filter((i) => !decisions[i.id]).map((i) => i.id) }
}

export function filterRows(rows, filters) {
  return rows.filter((r) => ['asset', 'dimension', 'verdict'].every((k) => !filters[k] || r[k] === filters[k]))
}

const CSV_COLUMNS = ['check_id', 'asset', 'dimension', 'title', 'verdict', 'observed', 'expected', 'threshold', 'error', 'sql']

export function toCSV(rows) {
  const cell = (v) => {
    const s = v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  return [CSV_COLUMNS.join(','), ...rows.map((r) => CSV_COLUMNS.map((c) => cell(r[c])).join(','))].join('\n')
}

// Which cached stage results to forget: a stage that just finished (a redo replaces the old result the screen may
// have cached while it ran) and anything the server no longer has (a redo deletes every later stage).
export function staleStages(cached, job, completed) {
  return cached.filter((s) => (job?.status === 'done' && s === job.stage) || !completed.includes(s))
}

// Back on a saved review: the confirm/exclude decisions and the edits that produced `submitted`, so a person sees
// exactly what will run again (reviewPayload's inverse).
export function restoreReview(specs, thresholds, submitted) {
  const decisions = {}
  const edits = {}
  const sent = new Map([...(submitted?.test_specifications || []), ...(submitted?.metric_thresholds || [])]
    .filter((i) => i && i.id).map((i) => [i.id, i]))
  for (const item of [...specs, ...thresholds]) {
    const then = sent.get(item.id)
    if (!then) continue
    decisions[item.id] = then.status === 'confirmed' ? 'confirmed' : 'excluded'
    const edit = {}
    if (item.assertion && item.suspended_threshold && !item.threshold && then.threshold) edit.restoreSuspended = true
    const was = item.assertion ? (edit.restoreSuspended ? item.suspended_threshold : item.threshold) : item
    const is = item.assertion ? then.threshold : then
    if (was && is && (was.operator !== is.operator || was.value !== is.value)) edit.threshold = { operator: is.operator, value: is.value }
    const before = item.assertion?.condition?.values
    const after = then.assertion?.condition?.values
    if (Array.isArray(before) && Array.isArray(after) && JSON.stringify(before) !== JSON.stringify(after)) edit.values = [...after]
    if (Object.keys(edit).length) edits[item.id] = edit
  }
  return { decisions, edits }
}

// The results ring: the share of passes among the tests that got a verdict (no threshold or an error has none).
export function passRate(rows) {
  const pass = rows.filter((r) => r.verdict === 'pass').length
  const fail = rows.filter((r) => r.verdict === 'fail').length
  return { pass, fail, other: rows.length - pass - fail, pct: pass + fail ? Math.round((100 * pass) / (pass + fail)) : null }
}

// Default and demo ports of each engine (ClickHouse: its HTTP interface). Switching the engine on the Configuration
// screen moves one of these to its counterpart; a port the person typed stays.
const PORT_PAIRS = [[5432, 8123], [15432, 18123]]

export function portForEngine(engine, port) {
  const p = Number(port)
  const pair = PORT_PAIRS.find((ports) => ports.includes(p))
  if (!pair) return p
  return engine === 'clickhouse' ? pair[1] : pair[0]
}

// A step's state in the sidebar: a stage running now (or a redo of a finished one) or one that failed shows that;
// otherwise finished, the next one that can run, or not reachable yet. The configuration is always set.
export function stepState(stage, completed, job) {
  if (stage === 'config') return 'done'
  if (job && job.stage === stage && job.status === 'running') return 'running'
  if (job && job.stage === stage && job.status === 'error') return 'error'
  if (completed.includes(stage)) return 'done'
  return stage === nextStage(completed) ? 'ready' : 'locked'
}

// A running stage's progress: the share done when the message counts ("call 10 of 16": call 10 is on its way, so
// 9 are done; a single call has no share) and how long it has run (`started` is the server's epoch in seconds).
export function jobProgress(job, now = Date.now()) {
  const m = /(\d+) of (\d+)/.exec(job?.message || '')
  const total = m ? Number(m[2]) : 0
  const pct = total > 1 ? Math.round((100 * (Number(m[1]) - 1)) / total) : null
  if (!job?.started) return { pct, elapsed: null }
  const s = Math.max(0, Math.floor(now / 1000 - job.started))
  return { pct, elapsed: s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s` }
}

// A schedule's interval (minutes) in words, and split into the form's number and unit (1, 60 or 1440 minutes).
export function everyLabel(minutes) {
  const { n, unit } = splitEvery(minutes)
  const word = { 1: 'min', 60: n === 1 ? 'hour' : 'h', 1440: n === 1 ? 'day' : 'days' }[unit]
  return n === 1 && unit > 1 ? `every ${word}` : `every ${n} ${word}`
}

export function splitEvery(minutes) {
  const unit = [1440, 60].find((u) => minutes % u === 0) ?? 1
  return { n: minutes / unit, unit }
}

// The history of executions (newest first), each with the tests that fail now but did not fail in the execution
// before it (an execution that could not run has no verdicts and is skipped in the comparison).
export function historyRows(history) {
  return history.map((entry, i) => {
    const before = history.slice(i + 1).find((h) => h.verdicts)
    const newFailures = entry.verdicts && before
      ? Object.keys(entry.verdicts).filter((id) => entry.verdicts[id] === 'fail' && before.verdicts[id] !== 'fail')
      : []
    return { ...entry, newFailures }
  })
}

// One test's observed values over the last `limit` executions that kept them (history is newest first), oldest
// first, numbers only: what its line chart draws.
export function trend(history, checkId, limit = 20) {
  return history.filter((h) => h.observed).slice(0, limit).reverse()
    .map((h) => h.observed[checkId]).filter((v) => typeof v === 'number' && Number.isFinite(v))
}

// A new run of the same database: the decisions of its last review (the server puts them in `_previous`) as the
// starting decisions, with the threshold the person confirmed when it differs from the proposed one.
export function previousDecisions(specs) {
  const decisions = {}
  const edits = {}
  for (const s of specs) {
    const p = s._previous
    if (!p) continue
    decisions[s.id] = p.status === 'confirmed' ? 'confirmed' : 'excluded'
    const was = p.threshold
    const now = s.threshold
    if (p.status === 'confirmed' && was && now && (was.operator !== now.operator || was.value !== now.value)) {
      edits[s.id] = { threshold: { operator: was.operator, value: was.value } }
    }
  }
  return { decisions, edits }
}

// The Review screen's starting decisions: a saved review's when there is one, otherwise the last review of this
// database and, for a run made by a structural change, the tests and metric thresholds it kept (both carry
// `_previous`), so a kept item never starts undecided and is never dropped unseen.
export function startingDecisions(specs, thresholds, review) {
  return review ? restoreReview(specs, thresholds, review.submitted) : previousDecisions([...specs, ...thresholds])
}

// Where a structural change left a run, for the run list: null when it left none.
export function changeLabel(run) {
  if (run.replaced_by) return `replaced by ${run.replaced_by}`
  if (run.changes?.superseded_by) return `superseded by ${run.changes.superseded_by}`
  if (run.origin?.kind === 'change' && !run.completed.includes('review')) return 'waiting for review: the structure changed'
  return null
}

// What a structural change found by a scheduled execution left on this run, for its Results screen; null when
// nothing: replaced by a reviewed run, new tests waiting for review, proposing failed, or proposing is off.
export function changeNotice(changes) {
  if (changes.superseded_by) return { kind: 'superseded', run: changes.superseded_by }
  if (changes.pending_run) return { kind: 'pending', run: changes.pending_run, proposed: changes.proposed ?? 0 }
  if (changes.failed) return { kind: 'failed', error: changes.failed.error, lines: changes.failed.lines || [] }
  if (changes.detected) return { kind: 'off', lines: changes.detected.lines || [] }
  return null
}
