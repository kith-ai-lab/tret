import {
  KNOWN_GATE_REASONS,
  TretBudgetRefused,
  TretError,
  TretNotFound,
  TretStreamError,
  errorFromResponse,
} from './errors.js'
import {
  type RoutingDecision,
  type RunEvent,
  type RunEventType,
  type UsageEventData,
  isTerminal,
  toRunEvent,
} from './events.js'
import { type Receipt, buildReceipt } from './receipt.js'
import { parseSse } from './sse.js'
import {
  type AuthConfig,
  type DocumentDetail,
  type DocumentSummary,
  type Finding,
  type FindingDecision,
  type HarnessDetail,
  type HarnessSummary,
  type PackDetail,
  type PackSummary,
  type RunDetail,
  type RunPage,
  TERMINAL_RUN_STATUSES,
  type User,
  type VersionInfo,
} from './types.js'

// ── options ────────────────────────────────────────────────────────────────

export type TretAuth =
  /** `Authorization: Bearer <token>` — an OIDC access token (the backend
   *  accepts these once `TRET_OIDC_API_AUDIENCE` is set). A function is
   *  called before every request (cache/refresh is yours), and once more to
   *  retry a request answered 401. */
  | { kind: 'bearer'; token: string | (() => Promise<string> | string) }
  /** The `tret_session` cookie — browsers and Electron renderers on the same
   *  origin, or any runtime with a cookie jar. Sends `credentials: 'include'`. */
  | { kind: 'session' }
  /** No credentials: `healthz`, `version`, `auth.config`, `auth.login`. */
  | { kind: 'none' }

export interface TretOptions {
  /** e.g. `https://tret.example.com` (no `/api`). `''` = same origin. */
  baseUrl: string
  /** Defaults to `{ kind: 'session' }`. */
  auth?: TretAuth
  /** Defaults to the global `fetch`. */
  fetch?: typeof fetch
  /** Sent as `X-Tret-Workspace` on every request: which of the caller's
   *  workspaces to act in. Needed by bearer callers with more than one
   *  membership (they carry no session cookie to hold the choice); a cookie
   *  session's own selection always wins over it on the server. */
  workspaceId?: string
}

export interface EventsOptions {
  signal?: AbortSignal
  /** Consecutive failed reconnects before giving up. Default 5. */
  maxReconnects?: number
  /** First reconnect delay in ms, doubled per consecutive failure (capped at
   *  10s). Default 500. */
  reconnectDelayMs?: number
}

export interface CreateRunOptions {
  harnessId: string
  /** Defaults to `freeform`. Must be a task type of a pack linked to the
   *  harness (or a generic one), or the run fails before it starts. */
  taskType?: string
  taskInput?: Record<string, unknown>
  documentIds?: string[]
  /** Pin a model id, bypassing the router for this run. */
  modelOverride?: string | null
  /** Defaults to the workspace's project. */
  projectId?: string | null
}

export interface ListRunsOptions {
  /** 1–200, default 50. */
  limit?: number
  cursor?: string | null
  /** Hide delegated child runs. */
  topLevelOnly?: boolean
}

export interface EventsSummary {
  /** Events delivered, by type. */
  counts: Partial<Record<RunEventType, number>>
  /** Every `text_delta` joined. */
  text: string
  routing: RoutingDecision | null
  lastUsage: UsageEventData | null
  findingIds: string[]
  /** The terminal event (`done` or `error`). */
  terminal: Extract<RunEvent, { type: 'done' | 'error' }> | null
}

export interface CompleteOptions {
  /** Called with every event as it arrives. */
  onEvent?: (event: RunEvent) => void | Promise<void>
  signal?: AbortSignal
}

export interface CompleteResult {
  run: RunDetail
  events: EventsSummary
  findings: Finding[]
  receipt: Receipt
}

type Query = Record<string, string | number | boolean | null | undefined>

interface RequestOptions {
  query?: Query
  json?: unknown
  form?: FormData
  signal?: AbortSignal
}

const GATE_PREFIX = /^([a-z][a-z0-9_]*): ([\s\S]*)$/

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortReason(signal))
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortReason(signal!))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException('The operation was aborted.', 'AbortError')
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return Boolean(signal?.aborted) || (error instanceof Error && error.name === 'AbortError')
}

// ── HTTP ───────────────────────────────────────────────────────────────────

class Http {
  readonly baseUrl: string
  readonly auth: TretAuth
  readonly fetchImpl: typeof fetch
  workspaceId: string | undefined

  constructor(options: TretOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, '')
    this.auth = options.auth ?? { kind: 'session' }
    const f = options.fetch ?? globalThis.fetch
    if (typeof f !== 'function') {
      throw new Error('@tret/sdk: no fetch available; pass `fetch` in the options (Node >= 18 has one)')
    }
    // Bound, so a browser's window.fetch is never called with the wrong `this`.
    this.fetchImpl = f.bind(globalThis)
    this.workspaceId = options.workspaceId
  }

  url(path: string, query?: Query): string {
    let url = `${this.baseUrl}${path}`
    if (query) {
      const params = new URLSearchParams()
      for (const [key, value] of Object.entries(query)) {
        if (value !== undefined && value !== null) params.set(key, String(value))
      }
      const qs = params.toString()
      if (qs) url += `?${qs}`
    }
    return url
  }

  private async headers(extra: Record<string, string>): Promise<Headers> {
    const headers = new Headers(extra)
    if (this.auth.kind === 'bearer') {
      const { token } = this.auth
      const value = typeof token === 'function' ? await token() : token
      if (!value) throw new TretError(0, '@tret/sdk: the bearer token provider returned no token')
      headers.set('Authorization', `Bearer ${value}`)
    }
    if (this.workspaceId) headers.set('X-Tret-Workspace', this.workspaceId)
    return headers
  }

  /** Raw fetch with auth applied; retries once on 401 when the bearer token is
   *  a function (it may have expired between calls). Throws on non-2xx. */
  async send(
    method: string,
    path: string,
    { query, json, form, signal }: RequestOptions = {},
    accept = 'application/json',
  ): Promise<Response> {
    const canRefresh = this.auth.kind === 'bearer' && typeof this.auth.token === 'function'
    for (let attempt = 0; ; attempt++) {
      const extra: Record<string, string> = { Accept: accept }
      let body: BodyInit | undefined
      if (form) body = form
      else if (json !== undefined) {
        body = JSON.stringify(json)
        extra['Content-Type'] = 'application/json'
      }
      const init: RequestInit = {
        method,
        headers: await this.headers(extra),
        body,
        // A bearer client must not also send a cookie: the server prefers a
        // cookie when both are present, which would silently change identity.
        credentials: this.auth.kind === 'session' ? 'include' : 'omit',
      }
      if (signal) init.signal = signal
      const res = await this.fetchImpl(this.url(path, query), init)
      if (res.ok) return res
      if (res.status === 401 && canRefresh && attempt === 0) {
        await res.body?.cancel().catch(() => undefined)
        continue
      }
      throw await this.toError(res)
    }
  }

  async toError(res: Response): Promise<TretError> {
    let body: unknown
    const text = await res.text().catch(() => '')
    try {
      body = text ? JSON.parse(text) : undefined
    } catch {
      body = text
    }
    return errorFromResponse(res.status, res.statusText, body, res.headers)
  }

  async json<T>(method: string, path: string, options?: RequestOptions): Promise<T> {
    const res = await this.send(method, path, options)
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }
}

// ── resources ──────────────────────────────────────────────────────────────

class AuthResource {
  constructor(private readonly http: Http) {}

  /** Public: which login forms the server offers. */
  config(): Promise<AuthConfig> {
    return this.http.json('GET', '/api/auth/config')
  }

  /** The signed-in user, their role and their workspaces. */
  me(): Promise<User> {
    return this.http.json('GET', '/api/auth/me')
  }

  /** Password login. Sets the session cookie, so it is only useful with
   *  `auth: { kind: 'session' }` in a runtime that keeps cookies. */
  login(body: { email: string; password: string }): Promise<User> {
    return this.http.json('POST', '/api/auth/login', { json: body })
  }

  logout(): Promise<{ ok: boolean; oidc_logout_url?: string }> {
    return this.http.json('POST', '/api/auth/logout')
  }

  /** Switch the active workspace. Re-mints the session cookie for session
   *  clients, and sets this client's `X-Tret-Workspace` so bearer clients
   *  act in it from the next request on. */
  async selectWorkspace(workspaceId: string): Promise<User> {
    const user = await this.http.json<User>('POST', '/api/auth/workspace', {
      json: { workspace_id: workspaceId },
    })
    this.http.workspaceId = workspaceId
    return user
  }
}

class HarnessesResource {
  constructor(private readonly http: Http) {}

  list(): Promise<HarnessSummary[]> {
    return this.http.json('GET', '/api/harnesses')
  }

  /** Adds the assembled system prompt and every linked pack's task types. */
  get(id: string): Promise<HarnessDetail> {
    return this.http.json('GET', `/api/harnesses/${encodeURIComponent(id)}`)
  }
}

class PacksResource {
  constructor(private readonly http: Http) {}

  list(): Promise<PackSummary[]> {
    return this.http.json('GET', '/api/packs')
  }

  get(id: string): Promise<PackDetail> {
    return this.http.json('GET', `/api/packs/${encodeURIComponent(id)}`)
  }
}

export type UploadData = Blob | ArrayBuffer | ArrayBufferView

class DocumentsResource {
  constructor(private readonly http: Http) {}

  /** Upload one file (multipart field `file`); the server extracts its text.
   *  `data` may be a Blob/File, an ArrayBuffer, or any typed array (a Node
   *  `Buffer` included). */
  upload(data: UploadData, name: string, mime?: string, signal?: AbortSignal): Promise<DocumentSummary> {
    let blob: Blob
    if (typeof Blob !== 'undefined' && data instanceof Blob) {
      blob = mime && data.type !== mime ? new Blob([data], { type: mime }) : data
    } else {
      const bytes =
        data instanceof ArrayBuffer
          ? new Uint8Array(data)
          : new Uint8Array(
              (data as ArrayBufferView).buffer,
              (data as ArrayBufferView).byteOffset,
              (data as ArrayBufferView).byteLength,
            )
      blob = new Blob([bytes as BlobPart], { type: mime ?? 'application/octet-stream' })
    }
    const form = new FormData()
    form.append('file', blob, name)
    return this.http.json('POST', '/api/documents', { form, signal })
  }

  /** The workspace's 200 most recent documents. */
  list(): Promise<DocumentSummary[]> {
    return this.http.json('GET', '/api/documents')
  }

  get(id: string): Promise<DocumentDetail> {
    return this.http.json('GET', `/api/documents/${encodeURIComponent(id)}`)
  }
}

class FindingsResource {
  constructor(private readonly http: Http) {}

  list(params: { runId?: string; status?: string; schemaSlug?: string; limit?: number } = {}): Promise<Finding[]> {
    return this.http.json('GET', '/api/findings', {
      query: {
        run_id: params.runId,
        status: params.status,
        schema_slug: params.schemaSlug,
        limit: params.limit,
      },
    })
  }

  /** Includes the finding's approvals. */
  get(id: string): Promise<Finding> {
    return this.http.json('GET', `/api/findings/${encodeURIComponent(id)}`)
  }

  /** Approve or reject a draft finding. Needs the `approver` role or higher in
   *  the workspace; the approver is whoever is authenticated (the API takes no
   *  approver field). A second decision on the same finding is a 409. */
  decide(id: string, decision: { approved: boolean; comment?: string | null }): Promise<FindingDecision> {
    return this.http.json('POST', `/api/findings/${encodeURIComponent(id)}/approval`, {
      json: { action: decision.approved ? 'approve' : 'reject', note: decision.comment ?? null },
    })
  }
}

class RunsResource {
  constructor(
    private readonly http: Http,
    private readonly findings: FindingsResource,
  ) {}

  /** Start a run. Returns as soon as the run is queued; follow it with
   *  `events()` or use `complete()`. */
  create(options: CreateRunOptions): Promise<{ run_id: string }> {
    return this.http.json('POST', '/api/runs', {
      json: {
        harness_id: options.harnessId,
        project_id: options.projectId ?? null,
        task_type: options.taskType ?? 'freeform',
        task_input: options.taskInput ?? {},
        document_ids: options.documentIds ?? [],
        model_override: options.modelOverride ?? null,
      },
    })
  }

  get(id: string, signal?: AbortSignal): Promise<RunDetail> {
    return this.http.json('GET', `/api/runs/${encodeURIComponent(id)}`, { signal })
  }

  /** Newest first, keyset-paged. Pass `next_cursor` back as `cursor`. */
  list(params: ListRunsOptions = {}): Promise<RunPage> {
    return this.http.json('GET', '/api/runs', {
      query: {
        limit: params.limit ?? 50,
        cursor: params.cursor ?? undefined,
        top_level_only: params.topLevelOnly ? true : undefined,
      },
    })
  }

  cancel(id: string): Promise<{ ok: boolean }> {
    return this.http.json('POST', `/api/runs/${encodeURIComponent(id)}/cancel`)
  }

  /**
   * The run's events, from the start, as they happen. Ends after `done` or
   * `error`.
   *
   * The server replays the whole backlog on every connect (it has no
   * Last-Event-ID), so a dropped connection is resumed by reconnecting and
   * skipping what was already delivered, by position. If the server trimmed
   * the backlog in between (runs past 5000 events), positions no longer line
   * up and the skip falls back to the events' timestamps.
   *
   * A run that finished long ago may have no stream left to replay; the
   * server then only sends keepalives. When that happens (or a reconnect
   * finds the run already finished), the iterator checks the run record and
   * ends with a terminal event built from it, flagged `synthetic: true`.
   */
  events(id: string, options: EventsOptions = {}): AsyncIterable<RunEvent> {
    return { [Symbol.asyncIterator]: () => this.streamEvents(id, options) }
  }

  private async *streamEvents(id: string, options: EventsOptions): AsyncGenerator<RunEvent> {
    const { signal } = options
    const maxReconnects = options.maxReconnects ?? 5
    const baseDelay = options.reconnectDelayMs ?? 500
    const path = `/api/runs/${encodeURIComponent(id)}/events`

    let delivered = 0 // frames consumed so far, pings excluded (unknown types included)
    let firstTs: number | null = null
    let lastTs: number | null = null
    let atLastTs = 0 // how many consumed frames carry exactly `lastTs`
    let failures = 0 // consecutive connections that ended without progress
    let connection = 0
    let lastError: unknown

    for (;;) {
      if (signal?.aborted) throw abortReason(signal)
      // A connection of our own, so leaving the loop early (break/return in
      // the consumer) closes the socket even without a caller signal.
      const controller = new AbortController()
      const onAbort = () => controller.abort(abortReason(signal!))
      signal?.addEventListener('abort', onAbort, { once: true })
      try {
        if (connection > 0) {
          const finished = await this.terminalFromRecord(id, signal)
          if (finished) {
            yield finished
            return
          }
        }
        connection++
        const res = await this.http.send('GET', path, { signal: controller.signal }, 'text/event-stream')
        if (!res.body) throw new TretStreamError('the event stream response had no body')

        let position = 0
        let tsMode = false // the backlog was trimmed: skip by timestamp instead
        let skippedAtTs = 0
        let progressed = false
        for await (const frame of parseSse(res.body)) {
          if (frame.event === 'ping') {
            // Keepalives (every 30s) and nothing else on this connection:
            // either the run is idle, or the server has forgotten a finished
            // run's backlog and will never send `done`.
            if (!progressed) {
              const finished = await this.terminalFromRecord(id, signal)
              if (finished) {
                yield finished
                return
              }
            }
            continue
          }
          const ts = frameTs(frame.data)
          position++
          if (position === 1 && delivered > 0 && ts !== null && firstTs !== null && ts !== firstTs) {
            tsMode = true
          }
          if (tsMode && ts !== null && lastTs !== null) {
            if (ts < lastTs) continue
            if (ts === lastTs && skippedAtTs < atLastTs) {
              skippedAtTs++
              continue
            }
          } else if (!tsMode && position <= delivered) {
            continue
          }
          delivered++
          progressed = true
          failures = 0
          if (ts !== null) {
            if (firstTs === null) firstTs = ts
            if (ts === lastTs) atLastTs++
            else {
              lastTs = ts
              atLastTs = 1
            }
          }
          const event = toRunEvent(frame.event, frame.data)
          if (!event) continue // a type this SDK version does not know
          yield event
          if (isTerminal(event)) return
        }
        // Closed without a terminal event: reconnect below.
      } catch (error) {
        if (isAbort(error, signal) || !isRetryable(error)) throw error
        lastError = error
      } finally {
        signal?.removeEventListener('abort', onAbort)
        controller.abort()
      }
      failures++
      if (failures > maxReconnects) {
        throw new TretStreamError(
          `the event stream for run ${id} ended ${failures} times in a row without progress`,
          lastError,
        )
      }
      await sleep(Math.min(baseDelay * 2 ** (failures - 1), 10_000), signal)
    }
  }

  /** A terminal event built from the run record, when the run is finished. */
  private async terminalFromRecord(id: string, signal?: AbortSignal): Promise<RunEvent | null> {
    const run = await this.get(id, signal)
    if (!TERMINAL_RUN_STATUSES.includes(run.status)) return null
    if (run.status === 'completed' || run.status === 'completed_without_output') {
      return {
        type: 'done',
        ts: null,
        synthetic: true,
        data: {
          status: run.status,
          cost_usd: run.cost_usd,
          energy_wh: run.energy_wh,
          co2e_g: run.co2e_g,
          scope2_g: run.scope2_g,
          scope3_g: run.scope3_g,
          baseline_co2e_g: null,
          avoided_co2e_g: run.avoided_co2e_g,
          avoided_usd: run.avoided_usd,
          avoided_usd_pct: run.avoided_usd_pct,
          co2e_g_low: run.co2e_g_low,
          co2e_g_high: run.co2e_g_high,
          water_ml: run.water_ml,
          iterations: run.iterations,
        },
      }
    }
    return {
      type: 'error',
      ts: null,
      synthetic: true,
      data: { message: run.error ?? run.status, status: run.status },
    }
  }

  /**
   * Run to completion: create, stream every event (to `onEvent`), then read
   * back the final run record and its findings, and build the Receipt.
   *
   * Resolves for any finished run, including `failed` and `cancelled` —
   * check `run.status`. Throws `TretBudgetRefused` (status 0, `runId` set)
   * when a pre-run gate refused the run before it started. Aborting `signal`
   * stops waiting; it does not cancel the run (call `cancel()` for that).
   */
  async complete(create: CreateRunOptions, options: CompleteOptions = {}): Promise<CompleteResult> {
    const { run_id: runId } = await this.create(create)
    const summary: EventsSummary = {
      counts: {},
      text: '',
      routing: null,
      lastUsage: null,
      findingIds: [],
      terminal: null,
    }
    const signal = options.signal
    for await (const event of this.events(runId, signal ? { signal } : {})) {
      summary.counts[event.type] = (summary.counts[event.type] ?? 0) + 1
      switch (event.type) {
        case 'text_delta':
          summary.text += event.data.text
          break
        case 'routing':
          summary.routing = event.data
          break
        case 'usage':
          summary.lastUsage = event.data
          break
        case 'finding_recorded':
          summary.findingIds.push(event.data.finding_id)
          break
        case 'done':
        case 'error':
          summary.terminal = event
          break
      }
      if (options.onEvent) await options.onEvent(event)
    }

    const run = await this.get(runId, signal)
    if (run.status === 'failed' && summary.routing === null) {
      const refusal = refusalFrom(run.error)
      if (refusal) {
        throw new TretBudgetRefused(0, refusal.reason, refusal.detail, refusal, run, runId)
      }
    }
    const findings = await this.findings.list({ runId })
    const receipt = buildReceipt({ run, routing: summary.routing, lastUsage: summary.lastUsage })
    return { run, events: summary, findings, receipt }
  }
}

/** `"<reason>: <detail>"` (engine/harness.py's pre-run gate refusal) → parts.
 *  Only the known gate reasons count: the engine writes the same
 *  `snake_case: ...` shape for its own before-start failures
 *  (`unknown_task_type`, `unknown_tool`), and those are not refusals. */
function refusalFrom(error: string | null): { reason: string; detail: string } | null {
  if (!error) return null
  const match = GATE_PREFIX.exec(error)
  const reason = match ? match[1]! : error
  if (!(KNOWN_GATE_REASONS as readonly string[]).includes(reason)) return null
  return { reason, detail: match ? match[2]! : '' }
}

/** Worth reconnecting for: a dropped connection (undici and browsers report
 *  those as a TypeError — "terminated", "Failed to fetch"), a stream error,
 *  or a 5xx. A 4xx is an answer (no such run, bad credentials), not an
 *  outage, and reconnecting would not change it. */
function isRetryable(error: unknown): boolean {
  if (error instanceof TretStreamError || error instanceof TypeError) return true
  return error instanceof TretError && error.status >= 500
}

function frameTs(data: string): number | null {
  try {
    const parsed: unknown = JSON.parse(data)
    if (typeof parsed === 'object' && parsed !== null && 'ts' in parsed) {
      const ts = (parsed as { ts: unknown }).ts
      return typeof ts === 'number' ? ts : null
    }
  } catch {
    /* not JSON */
  }
  return null
}

// ── the client ─────────────────────────────────────────────────────────────

/**
 * A tret API client.
 *
 * ```ts
 * const tret = new Tret({ baseUrl: 'https://tret.example.com',
 *   auth: { kind: 'bearer', token: () => getAccessToken() } })
 * const { run, receipt } = await tret.runs.complete({ harnessId, taskType: 'qa_review' })
 * ```
 */
export class Tret {
  readonly auth: AuthResource
  readonly harnesses: HarnessesResource
  readonly packs: PacksResource
  readonly documents: DocumentsResource
  readonly findings: FindingsResource
  readonly runs: RunsResource
  private readonly http: Http

  constructor(options: TretOptions) {
    this.http = new Http(options)
    this.auth = new AuthResource(this.http)
    this.harnesses = new HarnessesResource(this.http)
    this.packs = new PacksResource(this.http)
    this.documents = new DocumentsResource(this.http)
    this.findings = new FindingsResource(this.http)
    this.runs = new RunsResource(this.http, this.findings)
  }

  /** The workspace sent as `X-Tret-Workspace`, if any. */
  get workspaceId(): string | undefined {
    return this.http.workspaceId
  }

  /** Change the workspace sent as `X-Tret-Workspace` without a server call
   *  (`auth.selectWorkspace` also switches a cookie session server-side). */
  setWorkspace(workspaceId: string | undefined): void {
    this.http.workspaceId = workspaceId
  }

  /** `GET /api/version` — `{version, git_sha}`. A server from before that
   *  endpoint existed answers 404; then `/api/healthz` is asked instead and
   *  both fields are null (reachable, version unknown). */
  async version(): Promise<{ version: string | null; git_sha: string | null }> {
    try {
      const info = await this.http.json<VersionInfo>('GET', '/api/version')
      return { version: info.version, git_sha: info.git_sha ?? null }
    } catch (error) {
      if (!(error instanceof TretNotFound)) throw error
      await this.http.json('GET', '/api/healthz')
      return { version: null, git_sha: null }
    }
  }

  /** `GET /api/healthz` — `{ok: true}` when the process is up. */
  healthz(): Promise<{ ok: boolean }> {
    return this.http.json('GET', '/api/healthz')
  }
}
