/** A scripted `fetch` for tests: each route answers from a queue or a function. */
export interface Call {
  method: string
  url: string
  path: string
  query: URLSearchParams
  headers: Headers
  credentials: RequestCredentials | undefined
  body: unknown
  signal: AbortSignal | undefined
}

type Responder = (call: Call) => Response | Promise<Response>

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

/** An SSE response. `end: 'close'` ends cleanly, `'error'` drops the
 *  connection mid-stream (what a proxy timeout looks like to fetch). */
export function sse(frames: string[], end: 'close' | 'error' | 'hang' = 'close', signal?: AbortSignal): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      if (end === 'close') controller.close()
      else if (end === 'error') controller.error(new TypeError('terminated'))
      else signal?.addEventListener('abort', () => controller.error(signal.reason), { once: true })
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

export function frame(type: string, data: Record<string, unknown>): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`
}

export function mockFetch(routes: Record<string, Responder | Responder[]>) {
  const calls: Call[] = []
  const queues = new Map<string, Responder[]>()
  for (const [key, value] of Object.entries(routes)) queues.set(key, Array.isArray(value) ? [...value] : [value])

  const fetchImpl = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(String(input))
    const method = (init.method ?? 'GET').toUpperCase()
    let body: unknown = init.body
    if (typeof body === 'string') body = JSON.parse(body)
    const call: Call = {
      method,
      url: url.toString(),
      path: url.pathname,
      query: url.searchParams,
      headers: new Headers(init.headers),
      credentials: init.credentials,
      body,
      signal: init.signal ?? undefined,
    }
    calls.push(call)
    const key = `${method} ${url.pathname}`
    const queue = queues.get(key)
    if (!queue || queue.length === 0) return json({ detail: `no mock for ${key}` }, 599)
    const responder = queue.length > 1 ? queue.shift()! : queue[0]!
    return responder(call)
  }
  return { fetch: fetchImpl as typeof fetch, calls }
}
