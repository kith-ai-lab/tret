/**
 * The hand-written event union must name exactly the events the backend
 * publishes. Read from two places in the backend, so neither can drift alone:
 * the documented list on `RunEvent.type` in backend/tret/engine/events.py, and
 * every `RunEvent("<type>", ...)` construction under backend/tret/.
 */
import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { RUN_EVENT_TYPES, type RunEventDataMap, type RunEventType, toRunEvent } from '../src/events.js'

const here = dirname(fileURLToPath(import.meta.url))
const backend = join(here, '..', '..', '..', 'backend', 'tret')
const eventsPy = join(backend, 'engine', 'events.py')
const skip = existsSync(eventsPy) ? false : 'backend/ is not part of this checkout'

// Compile-time: the payload map has exactly one entry per listed type.
type Exact<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false
const mapMatchesList: Exact<keyof RunEventDataMap, RunEventType> = true
void mapMatchesList

/** Not events: the bus's keepalive frame. */
const NOT_EVENTS = new Set(['ping'])

function documentedTypes(source: string): string[] {
  const lines = source.split('\n')
  const start = lines.findIndex((l) => /^\s+type: str\s+#/.test(l))
  assert.notEqual(start, -1, 'could not find `type: str  # ...` in events.py')
  const parts: string[] = [lines[start]!.split('#').slice(1).join('#')]
  for (let i = start + 1; i < lines.length && /^\s+#\s{4,}/.test(lines[i]!); i++) {
    parts.push(lines[i]!.replace(/^\s+#/, ''))
  }
  return parts
    .join('|')
    .split('|')
    .map((t) => t.trim())
    .filter(Boolean)
}

function pyFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (name === '__pycache__') continue
    const full = join(dir, name)
    if (statSync(full).isDirectory()) out.push(...pyFiles(full))
    else if (name.endsWith('.py')) out.push(full)
  }
  return out
}

function publishedTypes(): string[] {
  const found = new Set<string>()
  const pattern = /RunEvent\(\s*(?:type\s*=\s*)?["']([a-z_]+)["']/g
  for (const file of pyFiles(backend)) {
    for (const match of readFileSync(file, 'utf8').matchAll(pattern)) found.add(match[1]!)
  }
  return [...found].filter((t) => !NOT_EVENTS.has(t))
}

const sorted = (xs: Iterable<string>) => [...new Set(xs)].sort()

test('the union matches the documented list in engine/events.py', { skip }, () => {
  const documented = documentedTypes(readFileSync(eventsPy, 'utf8'))
  assert.ok(documented.length >= 20, `parsed only ${documented.length} types: ${documented}`)
  assert.deepEqual(sorted(RUN_EVENT_TYPES), sorted(documented))
})

test('the union matches every RunEvent the backend constructs', { skip }, () => {
  const published = publishedTypes()
  assert.ok(published.includes('done') && published.includes('text_delta'), 'scan found too little')
  assert.deepEqual(sorted(RUN_EVENT_TYPES), sorted(published))
})

test('toRunEvent lifts ts out of the payload and types the event', () => {
  const event = toRunEvent('text_delta', JSON.stringify({ text: 'hi', ts: 1700000000.5 }))
  assert.deepEqual(event, { type: 'text_delta', data: { text: 'hi' }, ts: 1700000000.5 })
  if (event?.type === 'text_delta') {
    const text: string = event.data.text
    assert.equal(text, 'hi')
  }
})

test('toRunEvent drops keepalives, unknown types and malformed frames', () => {
  assert.equal(toRunEvent('ping', '{"ts": 1}'), null)
  assert.equal(toRunEvent('something_new', '{"ts": 1}'), null)
  assert.equal(toRunEvent('done', 'not json'), null)
  assert.equal(toRunEvent('done', '[1,2]'), null)
})
