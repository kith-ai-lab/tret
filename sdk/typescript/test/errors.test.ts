import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  TretAuthError,
  TretBudgetRefused,
  TretError,
  TretNotFound,
  TretRateLimited,
  errorFromResponse,
} from '../src/errors.js'

test('401 and 403 are TretAuthError with the detail sentence as message', () => {
  const e = errorFromResponse(401, 'Unauthorized', { detail: 'Not authenticated' })
  assert.ok(e instanceof TretAuthError)
  assert.equal(e.status, 401)
  assert.equal(e.message, 'Not authenticated')
  assert.ok(errorFromResponse(403, 'Forbidden', { detail: "'approver' role or higher" }) instanceof TretAuthError)
})

test('404 is TretNotFound', () => {
  const e = errorFromResponse(404, 'Not Found', { detail: 'Run not found' })
  assert.ok(e instanceof TretNotFound)
  assert.ok(e instanceof TretError)
  assert.equal(e.detail, 'Run not found')
})

test('429 is TretRateLimited and reads Retry-After', () => {
  const e = errorFromResponse(429, 'Too Many Requests', { detail: 'slow down' }, new Headers({ 'Retry-After': '30' }))
  assert.ok(e instanceof TretRateLimited)
  assert.equal(e.retryAfterSeconds, 30)
})

test('a gate refusal {reason, detail} on 403 or 402 is TretBudgetRefused, not an auth error', () => {
  for (const status of [402, 403]) {
    const body = { detail: { reason: 'insufficient_credits', detail: 'Add credits to continue.' } }
    const e = errorFromResponse(status, '', body)
    assert.ok(e instanceof TretBudgetRefused, `status ${status}`)
    assert.ok(!(e instanceof TretAuthError))
    assert.equal(e.reason, 'insufficient_credits')
    assert.equal(e.message, 'Add credits to continue.')
    assert.deepEqual(e.body, body)
    assert.equal(e.runId, null)
  }
})

test('422 validation lists become a readable message; other statuses stay TretError', () => {
  const e = errorFromResponse(422, 'Unprocessable', { detail: [{ loc: ['body'], msg: 'field required' }] })
  assert.equal(e.constructor, TretError)
  assert.equal(e.message, 'field required')
  const plain = errorFromResponse(500, 'Internal Server Error', 'oops')
  assert.equal(plain.message, '500 Internal Server Error')
  assert.equal(plain.body, 'oops')
})
