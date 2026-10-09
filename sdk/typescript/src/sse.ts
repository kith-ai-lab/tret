/**
 * A minimal Server-Sent Events parser over a `ReadableStream<Uint8Array>`
 * (a `fetch` response body), following the WHATWG event-stream rules that
 * matter here: lines end in LF, CRLF or CR; `:` starts a comment; `data`
 * lines accumulate (joined by LF); a blank line dispatches; a frame with no
 * `data` field dispatches nothing. No EventSource, so it works the same in
 * Node, Electron's main process and the browser, and can send headers
 * (`Authorization`, `X-Tret-Workspace`) that EventSource cannot.
 */

export interface SseFrame {
  /** The `event:` field; `message` when the frame named none. */
  event: string
  data: string
  id: string | null
}

export async function* parseSse(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SseFrame, void, undefined> {
  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let event = ''
  let data: string[] = []
  let hasData = false
  let id: string | null = null
  // A CR at the very end of a chunk may be the first half of a CRLF.
  let pendingCr = false

  function* takeLines(final: boolean): Generator<string> {
    let start = 0
    for (let i = 0; i < buffer.length; i++) {
      const ch = buffer.charCodeAt(i)
      if (ch === 10 /* \n */) {
        if (pendingCr && i === 0) {
          pendingCr = false
          start = 1
          continue
        }
        pendingCr = false
        yield buffer.slice(start, i)
        start = i + 1
      } else if (ch === 13 /* \r */) {
        yield buffer.slice(start, i)
        if (i + 1 < buffer.length) {
          if (buffer.charCodeAt(i + 1) === 10) i++
        } else {
          pendingCr = true
        }
        start = i + 1
      } else {
        pendingCr = false
      }
    }
    buffer = buffer.slice(start)
    if (final && buffer !== '') {
      yield buffer
      buffer = ''
    }
  }

  function line(text: string): SseFrame | null {
    if (text === '') {
      const frame = hasData ? { event: event || 'message', data: data.join('\n'), id } : null
      event = ''
      data = []
      hasData = false
      return frame
    }
    if (text.startsWith(':')) return null
    const colon = text.indexOf(':')
    const field = colon === -1 ? text : text.slice(0, colon)
    let value = colon === -1 ? '' : text.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') {
      data.push(value)
      hasData = true
    } else if (field === 'id' && !value.includes('\0')) id = value
    return null
  }

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      for (const text of takeLines(false)) {
        const frame = line(text)
        if (frame) yield frame
      }
    }
    buffer += decoder.decode()
    for (const text of takeLines(true)) {
      const frame = line(text)
      if (frame) yield frame
    }
    // An unterminated final frame (no trailing blank line) is discarded, as
    // the spec requires: it may be half of an event the server never finished.
  } finally {
    try {
      await reader.cancel()
    } catch {
      /* already closed or errored */
    }
    reader.releaseLock()
  }
}
