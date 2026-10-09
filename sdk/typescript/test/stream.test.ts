import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  type RunEvent,
  Tret,
  TretBudgetRefused,
  TretNotFound,
  TretStreamError,
  formatReceipt,
} from '../src/index.js'
import { frame, json, mockFetch, sse } from './helpers.js'

const BASE = 'https://tret.test'
const EVENTS = 'GET /api/runs/r1/events'
const RUN = 'GET /api/runs/r1'

const routing = {
  router_model: 'router/m',
  routing_prompt_version: 'v1',
  candidates: ['openrouter/a', 'openrouter/b'],
  chosen_model: 'openrouter/a',
  reasoning: 'cheapest capable',
  objective: 'balanced',
  fallback_used: false,
  spend: { cost_usd: 0.0026 },
}
const usage = {
  iteration: 1,
  input_tokens: 1200,
  output_tokens: 300,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  cost_usd: 0.0006,
  energy_wh: 0.04,
}
const wire = [
  frame('routing', { ...routing, ts: 1 }),
  frame('text_delta', { text: 'Hel', ts: 2 }),
  frame('text_delta', { text: 'lo', ts: 3 }),
  frame('usage', { ...usage, ts: 4 }),
  frame('finding_recorded', { finding_id: 'f1', ts: 5 }),
  frame('done', { status: 'completed', cost_usd: 0.0006, energy_wh: 0.04, co2e_g: 0.02, iterations: 1, ts: 6 }),
]

function runRecord(over: Record<string, unknown> = {}) {
  return {
    id: 'r1',
    status: 'completed',
    model_used: 'openrouter/a',
    routing,
    input_tokens: 1200,
    output_tokens: 300,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    cost_usd: 0.0006,
    reported_cost_usd: 0.00059,
    energy_wh: 0.04,
    co2e_g: 0.02,
    co2e_g_low: 0.01,
    co2e_g_high: 0.08,
    avoided_usd: 0.01,
    avoided_usd_pct: 94.2,
    avoided_co2e_g: 0.5,
    water_ml: null,
    error: null,
    iterations: 1,
    energy: { baseline: { model: 'anthropic/frontier', avoided_pct: 96.1 } },
    ...over,
  }
}

async function collect(iterable: AsyncIterable<RunEvent>): Promise<RunEvent[]> {
  const out: RunEvent[] = []
  for await (const event of iterable) out.push(event)
  return out
}

const types = (events: RunEvent[]) => events.map((e) => e.type)

test('streams typed events in order and stops at done', async () => {
  const m = mockFetch({ [EVENTS]: () => sse([...wire, frame('text_delta', { text: 'after', ts: 7 })]) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const events = await collect(tret.runs.events('r1'))
  assert.deepEqual(types(events), ['routing', 'text_delta', 'text_delta', 'usage', 'finding_recorded', 'done'])
  assert.equal(m.calls[0]!.headers.get('accept'), 'text/event-stream')
  const done = events.at(-1)!
  assert.equal(done.ts, 6)
  assert.ok(done.type === 'done' && done.data.co2e_g === 0.02)
})

test('a dropped connection resumes without repeating or losing events (by position)', async () => {
  const m = mockFetch({
    [EVENTS]: [() => sse(wire.slice(0, 3), 'error'), () => sse(wire)],
    [RUN]: () => json(runRecord({ status: 'running' })),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const events = await collect(tret.runs.events('r1', { reconnectDelayMs: 1 }))
  assert.deepEqual(types(events), ['routing', 'text_delta', 'text_delta', 'usage', 'finding_recorded', 'done'])
  // Straight back to the stream: no run-record check while events keep coming.
  assert.deepEqual(m.calls.map((c) => c.path), ['/api/runs/r1/events', '/api/runs/r1/events'])
})

test('a trimmed backlog on reconnect falls back to skipping by timestamp', async () => {
  // Second connection's replay starts at ts 3: the server trimmed ts 1-2.
  const m = mockFetch({
    [EVENTS]: [() => sse(wire.slice(0, 4), 'close'), () => sse(wire.slice(2))],
    [RUN]: () => json(runRecord({ status: 'running' })),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const events = await collect(tret.runs.events('r1', { reconnectDelayMs: 1 }))
  assert.deepEqual(types(events), ['routing', 'text_delta', 'text_delta', 'usage', 'finding_recorded', 'done'])
})

test('a finished run whose stream is gone ends with a synthetic terminal event', async () => {
  // A forgotten backlog: the server only ever sends keepalives.
  const m = mockFetch({
    [EVENTS]: (call) => sse(['event: ping\ndata: {"ts": 9}\n\n'], 'hang', call.signal),
    [RUN]: () => json(runRecord({ status: 'failed', error: 'max_iterations (8) reached without completion' })),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const events = await collect(tret.runs.events('r1'))
  assert.equal(events.length, 1)
  const [only] = events
  assert.ok(only?.type === 'error' && only.synthetic === true)
  assert.equal(only.data.message, 'max_iterations (8) reached without completion')
  // The hanging stream was closed when the iterator finished.
  assert.equal(m.calls[0]!.signal?.aborted, true)
})

test('a run that finished while disconnected still delivers its trailing events', async () => {
  // Dropped after the first text_delta. By the time we reconnect the run has
  // finished (the record says so), but the server still holds the backlog and
  // replays all of it: the rest of the text, the finding, usage and the real
  // done must all arrive, not a synthetic done from the record.
  const m = mockFetch({
    'POST /api/runs': () => json({ run_id: 'r1' }),
    [EVENTS]: [() => sse(wire.slice(0, 2), 'error'), () => sse(wire)],
    [RUN]: () => json(runRecord()),
    'GET /api/findings': () => json([{ id: 'f1', run_id: 'r1', status: 'draft' }]),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const seen: RunEvent[] = []
  const result = await tret.runs.complete({ harnessId: 'h1' }, { onEvent: (e) => void seen.push(e) })
  assert.deepEqual(types(seen), ['routing', 'text_delta', 'text_delta', 'usage', 'finding_recorded', 'done'])
  assert.equal(seen.some((e) => e.synthetic), false)
  assert.equal(result.events.text, 'Hello')
  assert.deepEqual(result.events.findingIds, ['f1'])
  assert.equal(result.events.lastUsage?.output_tokens, 300)
  const eventCalls = m.calls.filter((c) => c.path === '/api/runs/r1/events')
  assert.equal(eventCalls.length, 2)
  // The record is read once, after the stream ended, not before reconnecting.
  const order = m.calls.map((c) => c.path)
  assert.ok(order.lastIndexOf('/api/runs/r1/events') < order.indexOf('/api/runs/r1'))
})

test('a reconnect that brings nothing new ends from the finished run record', async () => {
  // The server forgot the backlog between connections: the second stream
  // closes empty, so the record (failed) supplies the terminal event.
  const m = mockFetch({
    [EVENTS]: [() => sse(wire.slice(0, 2), 'error'), () => sse([])],
    [RUN]: () => json(runRecord({ status: 'cancelled', error: null })),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const events = await collect(tret.runs.events('r1', { reconnectDelayMs: 1 }))
  assert.deepEqual(types(events), ['routing', 'text_delta', 'error'])
  const last = events.at(-1)!
  assert.ok(last.type === 'error' && last.synthetic === true && last.data.status === 'cancelled')
})

test('a 4xx on connect is thrown, not retried', async () => {
  const m = mockFetch({ [EVENTS]: () => json({ detail: 'Run not found' }, 404) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await assert.rejects(collect(tret.runs.events('r1')), TretNotFound)
  assert.equal(m.calls.length, 1)
})

test('gives up after maxReconnects consecutive failures', async () => {
  const m = mockFetch({
    [EVENTS]: () => json({ detail: 'bad gateway' }, 502),
    [RUN]: () => json(runRecord({ status: 'running' })),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await assert.rejects(collect(tret.runs.events('r1', { maxReconnects: 2, reconnectDelayMs: 1 })), TretStreamError)
  assert.equal(m.calls.filter((c) => c.path.endsWith('/events')).length, 3)
})

test('aborting the signal ends the iteration with an AbortError', async () => {
  const controller = new AbortController()
  const m = mockFetch({ [EVENTS]: (call) => sse(wire.slice(0, 2), 'hang', call.signal) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const seen: string[] = []
  await assert.rejects(
    (async () => {
      for await (const event of tret.runs.events('r1', { signal: controller.signal })) {
        seen.push(event.type)
        if (seen.length === 2) controller.abort()
      }
    })(),
    (e: unknown) => e instanceof Error && e.name === 'AbortError',
  )
  assert.deepEqual(seen, ['routing', 'text_delta'])
})

test('breaking out of the loop closes the connection', async () => {
  const m = mockFetch({ [EVENTS]: (call) => sse(wire.slice(0, 2), 'hang', call.signal) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  for await (const event of tret.runs.events('r1')) {
    assert.equal(event.type, 'routing')
    break
  }
  assert.equal(m.calls[0]!.signal?.aborted, true)
})

test('complete() runs to the end and returns run, summary, findings and receipt', async () => {
  const m = mockFetch({
    'POST /api/runs': () => json({ run_id: 'r1' }),
    [EVENTS]: () => sse(wire),
    [RUN]: () => json(runRecord()),
    'GET /api/findings': () => json([{ id: 'f1', run_id: 'r1', status: 'draft' }]),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const seen: string[] = []
  const result = await tret.runs.complete(
    { harnessId: 'h1', taskType: 'qa_review', documentIds: ['d1'] },
    { onEvent: (e) => void seen.push(e.type) },
  )
  assert.equal(seen.length, 6)
  assert.equal(result.events.text, 'Hello')
  assert.deepEqual(result.events.findingIds, ['f1'])
  assert.equal(result.events.counts.text_delta, 2)
  assert.equal(result.events.terminal?.type, 'done')
  assert.equal(result.findings[0]?.id, 'f1')
  assert.equal(m.calls.find((c) => c.path === '/api/findings')!.query.get('run_id'), 'r1')
  // Past the server's default of 100: complete() asks for its cap.
  assert.equal(m.calls.find((c) => c.path === '/api/findings')!.query.get('limit'), '500')
  assert.deepEqual(result.receipt, {
    model: 'openrouter/a',
    usd: 0.0006,
    reportedUsd: 0.00059,
    co2eG: 0.02,
    co2eGLow: 0.01,
    co2eGHigh: 0.08,
    energyWh: 0.04,
    waterMl: null,
    baselineModel: 'anthropic/frontier',
    avoidedUsd: 0.01,
    avoidedUsdPct: 94.2,
    avoidedCo2eG: 0.5,
    avoidedCo2ePct: 96.1,
    usage: { inputTokens: 1200, outputTokens: 300, cacheReadTokens: 0, cacheWriteTokens: 0 },
    routing: {
      chosenModel: 'openrouter/a',
      reasoning: 'cheapest capable',
      candidates: ['openrouter/a', 'openrouter/b'],
      fallbackUsed: false,
      objective: 'balanced',
    },
    overhead: { cost_usd: 0.0026 },
  })
  assert.equal(formatReceipt(result.receipt), 'receipt · $0.0006 (+$0.0026 routing) · 0.02 gCO₂e · a')
})

test('a run with no usage reported gets usd null, never $0', async () => {
  const m = mockFetch({
    'POST /api/runs': () => json({ run_id: 'r1' }),
    [EVENTS]: () => sse([frame('routing', { ...routing, ts: 1 }), frame('error', { message: 'provider down', status: 'failed', ts: 2 })]),
    [RUN]: () =>
      json(runRecord({ status: 'failed', error: 'provider down', input_tokens: 0, output_tokens: 0, cost_usd: 0, co2e_g: null })),
    'GET /api/findings': () => json([]),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const { run, receipt } = await tret.runs.complete({ harnessId: 'h1' })
  assert.equal(run.status, 'failed')
  assert.equal(receipt.usd, null)
  assert.equal(receipt.co2eG, null)
  assert.equal(formatReceipt(receipt), 'receipt · estimate unavailable · a')
})

test('complete() throws TretBudgetRefused when the pre-run gate refused the run', async () => {
  const message = 'insufficient_credits: This workspace has $0.00 available. Add credits to continue.'
  const m = mockFetch({
    'POST /api/runs': () => json({ run_id: 'r1' }),
    [EVENTS]: () => sse([frame('error', { message, ts: 1 })]),
    [RUN]: () => json(runRecord({ status: 'failed', error: message, routing: null, model_used: null })),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await assert.rejects(tret.runs.complete({ harnessId: 'h1' }), (e: unknown) => {
    assert.ok(e instanceof TretBudgetRefused)
    assert.equal(e.status, 0)
    assert.equal(e.reason, 'insufficient_credits')
    assert.equal(e.message, 'This workspace has $0.00 available. Add credits to continue.')
    assert.equal(e.runId, 'r1')
    return true
  })
})

test('a failure before routing that is not a gate refusal resolves normally', async () => {
  const message = "unknown_task_type: 'qa_reveiw' is not declared by this run's pack (declared: ['qa_review'])"
  const m = mockFetch({
    'POST /api/runs': () => json({ run_id: 'r1' }),
    [EVENTS]: () => sse([frame('error', { message, ts: 1 })]),
    [RUN]: () => json(runRecord({ status: 'failed', error: message, routing: null, input_tokens: 0, output_tokens: 0 })),
    'GET /api/findings': () => json([]),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  const { run, receipt } = await tret.runs.complete({ harnessId: 'h1', taskType: 'qa_reveiw' })
  assert.equal(run.error, message)
  assert.equal(receipt.routing?.chosenModel, undefined)
})
