import assert from 'node:assert/strict'
import { test } from 'node:test'

import { NON_JSON, Tret, TretAuthError, TretError, TretNotFound } from '../src/index.js'
import { json, mockFetch } from './helpers.js'

const BASE = 'https://tret.test'
const WS = '11111111-1111-1111-1111-111111111111'

test('bearer auth: Authorization header, no cookies, workspace header', async () => {
  const m = mockFetch({ 'GET /api/harnesses': () => json([]) })
  const tret = new Tret({ baseUrl: `${BASE}/`, auth: { kind: 'bearer', token: 'tok' }, fetch: m.fetch, workspaceId: WS })
  await tret.harnesses.list()
  const call = m.calls[0]!
  assert.equal(call.url, `${BASE}/api/harnesses`)
  assert.equal(call.headers.get('authorization'), 'Bearer tok')
  assert.equal(call.headers.get('x-tret-workspace'), WS)
  assert.equal(call.credentials, 'omit')
})

test('session auth (the default) sends cookies and no Authorization', async () => {
  const m = mockFetch({ 'GET /api/auth/me': () => json({ id: 'u' }) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await tret.auth.me()
  assert.equal(m.calls[0]!.credentials, 'include')
  assert.equal(m.calls[0]!.headers.get('authorization'), null)
  assert.equal(m.calls[0]!.headers.get('x-tret-workspace'), null)
})

test('a token function is asked per request and once more after a 401', async () => {
  let n = 0
  const m = mockFetch({
    'GET /api/packs': [() => json({ detail: 'Could not verify the bearer token' }, 401), () => json([])],
  })
  const tret = new Tret({
    baseUrl: BASE,
    auth: { kind: 'bearer', token: async () => `t${++n}` },
    fetch: m.fetch,
  })
  assert.deepEqual(await tret.packs.list(), [])
  assert.deepEqual(
    m.calls.map((c) => c.headers.get('authorization')),
    ['Bearer t1', 'Bearer t2'],
  )
})

test('a second 401 is thrown as TretAuthError', async () => {
  const m = mockFetch({ 'GET /api/packs': () => json({ detail: 'Not authenticated' }, 401) })
  const tret = new Tret({ baseUrl: BASE, auth: { kind: 'bearer', token: async () => 't' }, fetch: m.fetch })
  await assert.rejects(tret.packs.list(), (e: unknown) => e instanceof TretAuthError && e.status === 401)
  assert.equal(m.calls.length, 2)
})

test('selectWorkspace switches the server session and the workspace header', async () => {
  const other = '22222222-2222-2222-2222-222222222222'
  const m = mockFetch({
    'POST /api/auth/workspace': () => json({ id: 'u', workspaces: [] }),
    'GET /api/runs': () => json({ items: [], next_cursor: null }),
  })
  const tret = new Tret({ baseUrl: BASE, auth: { kind: 'bearer', token: 't' }, fetch: m.fetch })
  await tret.auth.selectWorkspace(other)
  assert.deepEqual(m.calls[0]!.body, { workspace_id: other })
  await tret.runs.list()
  assert.equal(m.calls[1]!.headers.get('x-tret-workspace'), other)
  assert.equal(tret.workspaceId, other)
})

test('version() reads /api/version, and falls back to healthz on an older server', async () => {
  const fresh = mockFetch({ 'GET /api/version': () => json({ version: '0.1.0', git_sha: 'abc1234' }) })
  assert.deepEqual(await new Tret({ baseUrl: BASE, fetch: fresh.fetch }).version(), {
    version: '0.1.0',
    git_sha: 'abc1234',
  })

  const old = mockFetch({
    'GET /api/version': () => json({ detail: 'Not Found' }, 404),
    'GET /api/healthz': () => json({ ok: true }),
  })
  assert.deepEqual(await new Tret({ baseUrl: BASE, fetch: old.fetch }).version(), { version: null, git_sha: null })
  assert.deepEqual(old.calls.map((c) => c.path), ['/api/version', '/api/healthz'])
})

test('version() also falls back when the server answers with its SPA shell', async () => {
  // An older single-app deployment: no /api/version route, so its catch-all
  // serves index.html with a 200.
  const html = () => new Response('<!doctype html><title>tret</title>', { headers: { 'Content-Type': 'text/html' } })
  const m = mockFetch({ 'GET /api/version': html, 'GET /api/healthz': () => json({ ok: true }) })
  assert.deepEqual(await new Tret({ baseUrl: BASE, fetch: m.fetch }).version(), { version: null, git_sha: null })
})

test('a 2xx that is not JSON is a TretError, never a parse crash', async () => {
  const m = mockFetch({
    'GET /api/packs': () => new Response('<!doctype html>', { headers: { 'Content-Type': 'text/html; charset=utf-8' } }),
    'GET /api/harnesses': () => new Response('{not json', { headers: { 'Content-Type': 'application/json' } }),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  for (const call of [() => tret.packs.list(), () => tret.harnesses.list()]) {
    await assert.rejects(call(), (e: unknown) => {
      assert.ok(e instanceof TretError)
      assert.equal(e.status, 200)
      assert.equal(e.detail, NON_JSON)
      return true
    })
  }
})

test('runs.create maps camelCase options onto CreateRunBody', async () => {
  const m = mockFetch({ 'POST /api/runs': () => json({ run_id: 'r1' }) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  assert.deepEqual(
    await tret.runs.create({ harnessId: 'h', taskType: 'qa_review', taskInput: { q: 1 }, documentIds: ['d1'] }),
    { run_id: 'r1' },
  )
  assert.deepEqual(m.calls[0]!.body, {
    harness_id: 'h',
    project_id: null,
    task_type: 'qa_review',
    task_input: { q: 1 },
    document_ids: ['d1'],
    model_override: null,
  })
  assert.equal(m.calls[0]!.headers.get('content-type'), 'application/json')
})

test('runs.list always asks for the paged shape', async () => {
  const m = mockFetch({ 'GET /api/runs': () => json({ items: [], next_cursor: null }) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await tret.runs.list()
  await tret.runs.list({ limit: 10, cursor: 'c', topLevelOnly: true })
  assert.equal(m.calls[0]!.query.toString(), 'limit=50')
  assert.equal(m.calls[1]!.query.toString(), 'limit=10&cursor=c&top_level_only=true')
})

test('documents.upload sends one multipart `file` part from a Buffer', async () => {
  const m = mockFetch({ 'POST /api/documents': () => json({ id: 'd1', filename: 'a.txt' }) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await tret.documents.upload(Buffer.from('hello'), 'a.txt', 'text/plain')
  const form = m.calls[0]!.body as FormData
  assert.ok(form instanceof FormData)
  const file = form.get('file') as File
  assert.equal(file.name, 'a.txt')
  assert.equal(file.type, 'text/plain')
  assert.equal(await file.text(), 'hello')
  // fetch sets the multipart boundary itself; a hand-set Content-Type breaks it.
  assert.equal(m.calls[0]!.headers.get('content-type'), null)
})

test('documents.upload accepts a Blob and an ArrayBuffer', async () => {
  const m = mockFetch({ 'POST /api/documents': () => json({ id: 'd' }) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await tret.documents.upload(new Blob(['x'], { type: 'application/pdf' }), 'a.pdf')
  await tret.documents.upload(new TextEncoder().encode('y').buffer as ArrayBuffer, 'b.bin')
  assert.equal(((m.calls[0]!.body as FormData).get('file') as File).type, 'application/pdf')
  assert.equal(((m.calls[1]!.body as FormData).get('file') as File).type, 'application/octet-stream')
})

test('findings.decide sends {action, note}; list filters by run', async () => {
  const m = mockFetch({
    'POST /api/findings/f1/approval': () => json({ ok: true, status: 'approved', approver: 'A' }),
    'GET /api/findings': () => json([]),
  })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await tret.findings.decide('f1', { approved: true, comment: 'Checked against the source.' })
  assert.deepEqual(m.calls[0]!.body, { action: 'approve', note: 'Checked against the source.' })
  await tret.findings.decide('f1', { approved: false })
  assert.deepEqual(m.calls[1]!.body, { action: 'reject', note: null })
  await tret.findings.list({ runId: 'r1' })
  assert.equal(m.calls[2]!.query.get('run_id'), 'r1')
})

test('ids are path-encoded and a 404 is TretNotFound', async () => {
  const m = mockFetch({ 'GET /api/runs/a%2Fb': () => json({ detail: 'Run not found' }, 404) })
  const tret = new Tret({ baseUrl: BASE, fetch: m.fetch })
  await assert.rejects(tret.runs.get('a/b'), (e: unknown) => e instanceof TretNotFound && e.message === 'Run not found')
})
