import assert from 'node:assert/strict'
import { test } from 'node:test'

import { parseSse, type SseFrame } from '../src/sse.js'

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

async function collect(chunks: string[]): Promise<SseFrame[]> {
  const out: SseFrame[] = []
  for await (const frame of parseSse(streamOf(chunks))) out.push(frame)
  return out
}

test('parses the backend wire format', async () => {
  const frames = await collect(['event: text_delta\ndata: {"text":"a","ts":1}\n\n'])
  assert.deepEqual(frames, [{ event: 'text_delta', data: '{"text":"a","ts":1}', id: null }])
})

test('frames split across arbitrary chunk boundaries', async () => {
  const wire = 'event: usage\ndata: {"x":1}\n\nevent: done\ndata: {"y":2}\n\n'
  for (let cut = 1; cut < wire.length; cut++) {
    const frames = await collect([wire.slice(0, cut), wire.slice(cut)])
    assert.deepEqual(
      frames.map((f) => [f.event, f.data]),
      [
        ['usage', '{"x":1}'],
        ['done', '{"y":2}'],
      ],
      `cut at ${cut}`,
    )
  }
})

test('CRLF and CR line endings, including a CRLF split across chunks', async () => {
  const frames = await collect(['event: a\r', '\ndata: 1\r\n\r', '\nevent: b\rdata: 2\r\r'])
  assert.deepEqual(
    frames.map((f) => [f.event, f.data]),
    [
      ['a', '1'],
      ['b', '2'],
    ],
  )
})

test('multi-line data, comments, default event name, no-data frames', async () => {
  const frames = await collect([': keepalive\n\n', 'data: one\ndata: two\n\n', 'event: x\n\n', 'id: 7\ndata:3\n\n'])
  assert.deepEqual(frames, [
    { event: 'message', data: 'one\ntwo', id: null },
    { event: 'message', data: '3', id: '7' },
  ])
})

test('multi-byte UTF-8 split across chunks', async () => {
  const bytes = new TextEncoder().encode('data: gCO₂e ✓\n\n')
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const b of bytes) controller.enqueue(new Uint8Array([b]))
      controller.close()
    },
  })
  const frames: SseFrame[] = []
  for await (const f of parseSse(stream)) frames.push(f)
  assert.equal(frames[0]?.data, 'gCO₂e ✓')
})

test('an unterminated final frame is discarded', async () => {
  const frames = await collect(['data: complete\n\n', 'data: half'])
  assert.deepEqual(frames.map((f) => f.data), ['complete'])
})
