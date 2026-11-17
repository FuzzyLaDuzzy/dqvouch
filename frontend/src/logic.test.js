import { describe, expect, test } from 'vitest'
import { STAGES, STAGE_INFO, changeLabel, changeNotice, llmPrompts, nextStage, parseHash, toHash, everyLabel, historyRows, jobProgress, previousDecisions, splitEvery, startingDecisions, trend } from './logic.js'

describe('navigation', () => {
  test('hash round trip', () => {
    for (const route of [{ view: 'runs' }, { view: 'new' }, { view: 'run', runId: '20261110-101010-2', stage: 'tests' }]) {
      expect(parseHash(toHash(route))).toEqual(route)
    }
  })

  test('empty or unknown hash is the run list', () => {
    expect(parseHash('')).toEqual({ view: 'runs' })
    expect(parseHash('#/whatever')).toEqual({ view: 'runs' })
  })

  test('a run without a stage', () => {
    expect(parseHash('#/run/abc')).toEqual({ view: 'run', runId: 'abc', stage: null })
  })

  test('next stage to run', () => {
    expect(nextStage([])).toBe('context')
    expect(nextStage(['context', 'dimensions'])).toBe('profiling')
    expect(nextStage(STAGES)).toBe(null)
  })

  test('every stage has a title, a kind and an explanation', () => {
    for (const stage of ['config', ...STAGES]) {
      expect(STAGE_INFO[stage].title && STAGE_INFO[stage].kind && STAGE_INFO[stage].explanation).toBeTruthy()
    }
  })
})

describe('what is sent to Gemini', () => {
  test('dimensions has one prompt for all tables', () => {
    expect(llmPrompts('dimensions', { prompt: 'P' })).toEqual([{ scope: 'all tables', prompt: 'P' }])
  })

  test('tests and SQL guarantees list one prompt per call', () => {
    const data = { calls: [{ scope: 'db.t', prompt: 'A', raw_response: 'x' }, { scope: 'db.u', prompt: 'B' }] }
    expect(llmPrompts('tests', data)).toEqual([{ scope: 'db.t', prompt: 'A' }, { scope: 'db.u', prompt: 'B' }])
    expect(llmPrompts('sql_rules', data)).toHaveLength(2)
  })

  test('a skipped or code-only stage sends nothing', () => {
    expect(llmPrompts('profiling', { sql: 'x' })).toEqual([])
    expect(llmPrompts('sql_rules', { skipped: 'no SQL' })).toEqual([])
    expect(llmPrompts('dimensions', null)).toEqual([])
  })
})

import { evidenceOrigin, filterRows, reviewPayload, testGroups, toCSV } from './logic.js'

describe('where a test comes from', () => {
  test('evidence paths name their origin', () => {
    expect(evidenceOrigin('sql_invariants.analytic_fact_orders_1')).toBe('SQL guarantee')
    expect(evidenceOrigin('profile:uniqueness:staging.orders_trans:order_id')).toBe('profiling')
    expect(evidenceOrigin('transformation.raw_sql')).toBe('transformation SQL')
    expect(evidenceOrigin('dq_signals.fallback_value_status')).toBe('signal from the SQL')
    expect(evidenceOrigin('lineage.upstream_assets')).toBe('lineage')
    expect(evidenceOrigin('downstream_usage.analytic_daily_sales')).toBe('downstream usage')
    expect(evidenceOrigin('columns.status.description')).toBe('description')
    expect(evidenceOrigin('analytic.fact_orders.columns.status.description')).toBe('description')
    expect(evidenceOrigin('columns.status.type')).toBe('schema')
  })

  test('tests are grouped into accepted, flagged and rejected', () => {
    const data = {
      test_specifications: [{ id: 'a' }, { id: 'b', review_warnings: ['contradicted'] }, { id: 'c', review_warnings: [] }],
      rejected: [{ scope: 'd', reason: 'no evidence' }],
    }
    const g = testGroups(data)
    expect(g.accepted.map((s) => s.id)).toEqual(['a', 'c'])
    expect(g.flagged.map((s) => s.id)).toEqual(['b'])
    expect(g.rejected).toEqual([{ scope: 'd', reason: 'no evidence' }])
  })
})

describe('review decisions', () => {
  const specs = [
    { id: 'u', threshold: { metric: 'failed_rows', operator: 'equals', value: 0 }, assertion: { type: 'unique' } },
    { id: 'v', threshold: { metric: 'failed_rows', operator: 'equals', value: 0 },
      assertion: { type: 'row_predicate', condition: { op: 'in', left: { column: 's' }, values: ['a', 'b'] } } },
    { id: 'f', suspended_threshold: { metric: 'failed_rows', operator: 'equals', value: 0 }, review_warnings: ['x'],
      assertion: { type: 'unique' } },
  ]
  const thresholds = [{ id: 'm', check_id: 'c', metric: 'null_rate_pct', operator: 'less_than_or_equal', value: 5 }]

  test('only confirmed items keep their status; the rest are excluded', () => {
    const p = reviewPayload(specs, thresholds, { u: 'confirmed', v: 'excluded', m: 'confirmed' }, {})
    expect(p.test_specifications.map((s) => s.status)).toEqual(['confirmed', 'excluded', 'excluded'])
    expect(p.metric_thresholds[0].status).toBe('confirmed')
    expect(p.undecided).toEqual(['f']) // excluded so that it does not run, but nobody decided it
  })

  test('edits change the threshold, the list of values and restore a suspended threshold', () => {
    const p = reviewPayload(specs, thresholds, { u: 'confirmed', v: 'confirmed', f: 'confirmed', m: 'confirmed' }, {
      u: { threshold: { operator: 'less_than_or_equal', value: 3 } },
      v: { values: ['a', 'b', 'c'] },
      f: { restoreSuspended: true },
      m: { threshold: { operator: 'less_than', value: 1 } },
    })
    const [u, v, f] = p.test_specifications
    expect(u.threshold).toEqual({ metric: 'failed_rows', operator: 'less_than_or_equal', value: 3 })
    expect(v.assertion.condition.values).toEqual(['a', 'b', 'c'])
    expect(f.threshold).toEqual({ metric: 'failed_rows', operator: 'equals', value: 0 })
    expect(f.suspended_threshold).toBeUndefined()
    expect(f.review_warnings).toEqual(['x'])
    expect(p.metric_thresholds[0]).toMatchObject({ operator: 'less_than', value: 1 })
  })

  test('the original objects are not changed', () => {
    const before = structuredClone({ specs, thresholds })
    reviewPayload(specs, thresholds, { u: 'confirmed', f: 'confirmed' }, { u: { threshold: { operator: 'x', value: 9 } }, f: { restoreSuspended: true } })
    expect({ specs, thresholds }).toEqual(before)
  })
})

describe('results', () => {
  const rows = [
    { check_id: 'a', asset: 'db.t', dimension: 'uniqueness', verdict: 'fail', observed: 3 },
    { check_id: 'b', asset: 'db.u', dimension: 'validity', verdict: 'pass', observed: 0 },
  ]

  test('filters by table, dimension and verdict; empty filters keep everything', () => {
    expect(filterRows(rows, {})).toHaveLength(2)
    expect(filterRows(rows, { asset: 'db.t' }).map((r) => r.check_id)).toEqual(['a'])
    expect(filterRows(rows, { verdict: 'pass', dimension: 'validity' }).map((r) => r.check_id)).toEqual(['b'])
    expect(filterRows(rows, { verdict: 'pass', asset: 'db.t' })).toEqual([])
  })

  test('CSV quotes commas, quotes and line breaks', () => {
    const csv = toCSV([{ check_id: 'a', asset: 'db.t', title: 'a,"b"\nc', verdict: 'fail', threshold: { value: 0 }, sql: 'SELECT 1' }])
    const [header, line] = csv.split('\n', 1).concat(csv.slice(csv.indexOf('\n') + 1))
    expect(header).toBe('check_id,asset,dimension,title,verdict,observed,expected,threshold,error,sql')
    expect(line).toBe('a,db.t,,"a,""b""\nc",fail,,,"{""value"":0}",,SELECT 1')
  })
})

import { restoreReview, staleStages } from './logic.js'

describe('cached stage results', () => {
  test('a finished redo drops its own old result and every later one', () => {
    const cached = ['context', 'dimensions', 'profiling']
    expect(staleStages(cached, { stage: 'dimensions', status: 'done' }, ['context', 'dimensions'])).toEqual(['dimensions', 'profiling'])
  })

  test('while a stage runs or after it failed, only results the server no longer has are dropped', () => {
    expect(staleStages(['context', 'dimensions'], { stage: 'dimensions', status: 'running' }, ['context', 'dimensions'])).toEqual([])
    expect(staleStages(['context', 'dimensions'], { stage: 'dimensions', status: 'error' }, ['context'])).toEqual(['dimensions'])
    expect(staleStages(['context'], null, ['context'])).toEqual([])
  })
})

describe('going back to a saved review', () => {
  const specs = [
    { id: 'u', threshold: { metric: 'failed_rows', operator: 'equals', value: 0 }, assertion: { type: 'unique' } },
    { id: 'v', threshold: { metric: 'failed_rows', operator: 'equals', value: 0 },
      assertion: { type: 'row_predicate', condition: { op: 'in', left: { column: 's' }, values: ['a', 'b'] } } },
    { id: 'f', suspended_threshold: { metric: 'failed_rows', operator: 'equals', value: 0 }, review_warnings: ['x'],
      assertion: { type: 'unique' } },
    { id: 'n', threshold: { metric: 'failed_rows', operator: 'equals', value: 0 }, assertion: { type: 'unique' } },
  ]
  const thresholds = [{ id: 'm', check_id: 'c', metric: 'null_rate_pct', operator: 'less_than_or_equal', value: 5 }]

  test('decisions and edits come back as they were sent', () => {
    const decisions = { u: 'confirmed', v: 'confirmed', f: 'confirmed', n: 'excluded', m: 'confirmed' }
    const edits = {
      u: { threshold: { operator: 'less_than_or_equal', value: 3 } },
      v: { values: ['a', 'b', 'c'] },
      f: { restoreSuspended: true },
      m: { threshold: { operator: 'less_than', value: 1 } },
    }
    const submitted = reviewPayload(specs, thresholds, decisions, edits)
    expect(restoreReview(specs, thresholds, submitted)).toEqual({ decisions, edits })
  })

  test('without a saved review nothing is decided', () => {
    expect(restoreReview(specs, thresholds, undefined)).toEqual({ decisions: {}, edits: {} })
  })
})

import { passRate } from './logic.js'

describe('presentation', () => {
  test('the results ring counts passes among the tests that have a verdict', () => {
    const rows = ['pass', 'pass', 'pass', 'fail', 'no_threshold', 'error'].map((verdict) => ({ verdict }))
    expect(passRate(rows)).toEqual({ pass: 3, fail: 1, other: 2, pct: 75 })
    expect(passRate([{ verdict: 'no_threshold' }])).toEqual({ pass: 0, fail: 0, other: 1, pct: null })
  })
})

import { portForEngine } from './logic.js'

test('switching the engine moves a default or demo port to the other engine\'s', () => {
  expect(portForEngine('clickhouse', 15432)).toBe(18123)
  expect(portForEngine('postgres', 18123)).toBe(15432)
  expect(portForEngine('clickhouse', 5432)).toBe(8123)
  expect(portForEngine('postgres', '8123')).toBe(5432)
  expect(portForEngine('clickhouse', 8125)).toBe(8125) // a port the person chose stays
})

import { stepState } from './logic.js'

test('each step in the sidebar shows its state', () => {
  const completed = ['context', 'dimensions']
  expect(stepState('config', completed, null)).toBe('done')
  expect(stepState('context', completed, null)).toBe('done')
  expect(stepState('profiling', completed, null)).toBe('ready')
  expect(stepState('tests', completed, null)).toBe('locked')
  expect(stepState('profiling', completed, { stage: 'profiling', status: 'running' })).toBe('running')
  expect(stepState('profiling', completed, { stage: 'profiling', status: 'error' })).toBe('error')
  expect(stepState('dimensions', completed, { stage: 'dimensions', status: 'running' })).toBe('running') // a redo
})

describe('jobProgress', () => {
  test('turns "i of n" into the share already done (call i is on its way)', () => {
    expect(jobProgress({ message: 'SQL Rule Extractor: x (input), call 10 of 16' }).pct).toBe(56)
    expect(jobProgress({ message: 'Running test 1 of 120' }).pct).toBe(0)
  })
  test('has no share for one call or a message without a count', () => {
    expect(jobProgress({ message: 'Dimension Selector: Gemini is reading 10 tables' }).pct).toBe(null)
    expect(jobProgress({ message: 'Test Specifier: a (1 of 1)' }).pct).toBe(null)
  })
  test('says how long the stage has been running', () => {
    expect(jobProgress({ message: '', started: 1000 }, 1000_000 + 42_400).elapsed).toBe('42 s')
    expect(jobProgress({ message: '', started: 1000 }, 1000_000 + 185_000).elapsed).toBe('3 min 05 s')
    expect(jobProgress({ message: '' }).elapsed).toBe(null)
  })
})

describe('schedules', () => {
  test('the interval in words', () => {
    expect(everyLabel(15)).toBe('every 15 min')
    expect(everyLabel(60)).toBe('every hour')
    expect(everyLabel(180)).toBe('every 3 h')
    expect(everyLabel(90)).toBe('every 90 min')
    expect(everyLabel(1440)).toBe('every day')
    expect(everyLabel(4320)).toBe('every 3 days')
  })
  test('the form takes the largest whole unit', () => {
    expect(splitEvery(4320)).toEqual({ n: 3, unit: 1440 })
    expect(splitEvery(120)).toEqual({ n: 2, unit: 60 })
    expect(splitEvery(90)).toEqual({ n: 90, unit: 1 })
  })
  test('each execution names the tests that started failing since the one before', () => {
    const rows = historyRows([
      { at: '3', counts: { pass: 1, fail: 2 }, verdicts: { a: 'fail', b: 'fail', c: 'pass' } },
      { at: '2', error: 'cannot connect' },
      { at: '1', counts: { pass: 2, fail: 1 }, verdicts: { a: 'fail', b: 'pass', c: 'pass' } },
    ])
    expect(rows.map((r) => r.newFailures)).toEqual([['b'], [], []])
  })
})

describe('evolution of a test', () => {
  test('numeric observed values, oldest first, older history lines and non-numbers skipped', () => {
    const history = [
      { at: '4', observed: { a: 7, b: 'x' } },
      { at: '3', error: 'cannot connect' },
      { at: '2', observed: { a: 5 } },
      { at: '1', verdicts: { a: 'pass' } }, // written before observed values were kept
    ]
    expect(trend(history, 'a')).toEqual([5, 7])
    expect(trend(history, 'b')).toEqual([])
  })
  test('only the last 20 executions', () => {
    const history = Array.from({ length: 25 }, (_, i) => ({ at: String(25 - i), observed: { a: 25 - i } }))
    expect(trend(history, 'a')).toEqual(Array.from({ length: 20 }, (_, i) => i + 6))
  })
})

describe('reusing the last review', () => {
  test('a test decided in an earlier run starts with that decision and its threshold', () => {
    const limit = { metric: 'failed_rows', operator: 'equals', value: 0 }
    const specs = [
      { id: 'a', threshold: limit, _previous: { status: 'excluded', threshold: null, run: 'r1' } },
      { id: 'b', threshold: limit, _previous: { status: 'confirmed', threshold: { ...limit, operator: 'less_than_or_equal', value: 5 }, run: 'r1' } },
      { id: 'c', threshold: limit, _previous: { status: 'confirmed', threshold: limit, run: 'r1' } },
      { id: 'd', threshold: limit },
    ]
    expect(previousDecisions(specs)).toEqual({
      decisions: { a: 'excluded', b: 'confirmed', c: 'confirmed' },
      edits: { b: { threshold: { operator: 'less_than_or_equal', value: 5 } } },
    })
  })
})

describe('structural changes', () => {
  test('a change run starts with its kept tests and thresholds confirmed', () => {
    const kept = { status: 'confirmed', run: 'r1' }
    const specs = [
      { id: 's1', threshold: { operator: 'equals', value: 0 }, _previous: { ...kept, threshold: { operator: 'equals', value: 0 } } },
      { id: 's2' },
    ]
    const thresholds = [{ id: 'm1', metric: 'null_rate', operator: 'less_than_or_equal', value: 0.1, _previous: kept }]
    expect(startingDecisions(specs, thresholds, null).decisions).toEqual({ s1: 'confirmed', m1: 'confirmed' })
  })

  test('a saved review wins over the kept decisions', () => {
    const specs = [{ id: 's1', _previous: { status: 'confirmed', run: 'r1' } }]
    const review = { submitted: { test_specifications: [{ id: 's1', status: 'excluded' }], metric_thresholds: [] } }
    expect(startingDecisions(specs, [], review).decisions).toEqual({ s1: 'excluded' })
  })

  test('runs say where a change left them', () => {
    expect(changeLabel({ origin: { kind: 'change' }, completed: ['tests'], changes: {} }))
      .toBe('waiting for review: the structure changed')
    expect(changeLabel({ origin: { kind: 'change' }, completed: ['tests'], replaced_by: 'r9', changes: {} })).toBe('replaced by r9')
    expect(changeLabel({ origin: null, completed: ['review'], changes: { superseded_by: 'r2' } })).toBe('superseded by r2')
    expect(changeLabel({ origin: null, completed: ['review'], changes: {} })).toBe(null)
  })
})

describe('the change notice on a run', () => {
  test('what a structural change left on this run, most important first', () => {
    expect(changeNotice({})).toBe(null)
    expect(changeNotice({ superseded_by: 'r2', pending_run: 'r3' })).toEqual({ kind: 'superseded', run: 'r2' })
    expect(changeNotice({ pending_run: 'r3', proposed: 2 })).toEqual({ kind: 'pending', run: 'r3', proposed: 2 })
    expect(changeNotice({ failed: { error: 'model down', lines: ['t: SQL changed'] } }))
      .toEqual({ kind: 'failed', error: 'model down', lines: ['t: SQL changed'] })
    expect(changeNotice({ detected: { lines: ['t: + c (Int32)'] } })).toEqual({ kind: 'off', lines: ['t: + c (Int32)'] })
  })
})
