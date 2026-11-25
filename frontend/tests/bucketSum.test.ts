// Run with `npm test` (node's built-in runner; no extra dependency).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { bucketsMatchTotal } from '../src/components/shared/bucketSum.ts'

// Mirrors the backend: four exact buckets, each rounded to 6 places, and the
// total rounded on its own (emissions.py `_f`).
const round6 = (x: number) => Math.round(x * 1e6) / 1e6

function seeded(seed: number) {
  let s = seed
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296
    return s / 4294967296
  }
}

test('rounded buckets are not flagged on ordinary random token mixes', () => {
  const rand = seeded(42)
  let legacyFalseAlarms = 0
  for (let i = 0; i < 5000; i++) {
    const exact = [0, 0, 0, 0].map(() => rand() * 50)
    const total = round6(exact.reduce((a, b) => a + b, 0))
    const stored = exact.map(round6)
    assert.ok(bucketsMatchTotal(stored, total), `flagged ${stored} vs ${total}`)
    // The old check: fixed 1e-6 tolerance on the same numbers.
    if (Math.abs(stored.reduce((a, b) => a + b, 0) - total) >= 1e-6) legacyFalseAlarms++
  }
  // Documents the bug: the old tolerance cried wolf on a sizeable share of runs.
  assert.ok(legacyFalseAlarms > 200, `expected the old check to misfire often, got ${legacyFalseAlarms}`)
})

test('a genuinely wrong split is still flagged', () => {
  assert.equal(bucketsMatchTotal([1, 2, 3, 4], 10.001), false)
  assert.equal(bucketsMatchTotal([1, 2, 3, 4], 9.99), false)
  assert.equal(bucketsMatchTotal([1, 2, 3, 4], 10), true)
})
