/**
 * Errors thrown by the SDK.
 *
 * tret reports failures as FastAPI does: a JSON body whose `detail` is either
 * a sentence (`{"detail": "Run not found"}`), a validation list (422), or —
 * for an extension gate's refusal — an object `{"reason", "detail"}` whose
 * `reason` is a short machine code (`insufficient_credits`,
 * `subscription_inactive`, `budget_exhausted`, `seat_limit`, ...) and whose
 * inner `detail` is the sentence meant for a person. `message` is always that
 * human sentence; the raw shape stays on `detail` and the whole parsed body on
 * `body`.
 */

/** Any non-2xx response from tret (and a run refused before it started). */
export class TretError extends Error {
  /** HTTP status. `0` when the error did not come from an HTTP response (a
   *  run refused by the pre-run gate reports its refusal on the event stream,
   *  not as a status — see `TretBudgetRefused`). */
  readonly status: number
  /** The response's `detail` field, as sent (string, object or array). */
  readonly detail: unknown
  /** The whole parsed response body (or the raw text when it was not JSON). */
  readonly body: unknown

  constructor(status: number, message: string, detail?: unknown, body?: unknown) {
    super(message)
    this.name = 'TretError'
    this.status = status
    this.detail = detail
    this.body = body
  }
}

/** 401 (no or invalid credentials) or 403 (authenticated, not allowed). */
export class TretAuthError extends TretError {
  constructor(status: number, message: string, detail?: unknown, body?: unknown) {
    super(status, message, detail, body)
    this.name = 'TretAuthError'
  }
}

/** 404 — also what tret answers for anything in another workspace. */
export class TretNotFound extends TretError {
  constructor(status: number, message: string, detail?: unknown, body?: unknown) {
    super(status, message, detail, body)
    this.name = 'TretNotFound'
  }
}

/** 429. `retryAfterSeconds` is the `Retry-After` header, when sent. */
export class TretRateLimited extends TretError {
  readonly retryAfterSeconds: number | null

  constructor(
    status: number,
    message: string,
    detail?: unknown,
    body?: unknown,
    retryAfterSeconds: number | null = null,
  ) {
    super(status, message, detail, body)
    this.name = 'TretRateLimited'
    this.retryAfterSeconds = retryAfterSeconds
  }
}

/**
 * A gate refused the work: the `{reason, detail}` shape.
 *
 * Two ways it arrives:
 * - **HTTP** (`status` 402 or 403): a workspace gate refusing an action —
 *   tret core's routes answer 403 with `{"detail": {"reason", "detail"}}`.
 * - **On the run** (`status` 0, `runId` set): the pre-run gate (core's spend
 *   budget, a hosting extension's credit check) does not refuse the
 *   `POST /api/runs` itself. The run is created, then fails before its first
 *   model call with an `error` event whose message is `"<reason>: <detail>"`.
 *   `tret.runs.complete()` recognises that and throws this error.
 */
export class TretBudgetRefused extends TretError {
  /** Machine code, e.g. `insufficient_credits`, `budget_exhausted`. */
  readonly reason: string
  /** The sentence meant for a person (same as `message`). */
  readonly refusal: string
  /** Set when the refusal arrived on a run rather than an HTTP response. */
  readonly runId: string | null

  constructor(
    status: number,
    reason: string,
    refusal: string,
    detail?: unknown,
    body?: unknown,
    runId: string | null = null,
  ) {
    super(status, refusal || reason, detail, body)
    this.name = 'TretBudgetRefused'
    this.reason = reason
    this.refusal = refusal
    this.runId = runId
  }
}

/** The run's event stream could not be (re)established. */
export class TretStreamError extends TretError {
  constructor(message: string, cause?: unknown) {
    super(0, message, undefined, undefined)
    this.name = 'TretStreamError'
    if (cause !== undefined) (this as { cause?: unknown }).cause = cause
  }
}

/** Reason codes the pre-run gates use: core's spend budget
 *  (services/budgets.py) and the hosted billing extension. `complete()`
 *  recognises only these as a refusal, because the engine writes the same
 *  `"<code>: <detail>"` shape for its own before-start failures
 *  (`unknown_task_type`, `unknown_tool`), which are not. */
export const KNOWN_GATE_REASONS = [
  'budget_exhausted',
  'insufficient_credits',
  'subscription_inactive',
] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `{reason, detail}` when `detail` has the gate-refusal shape, else null. */
export function gateRefusal(detail: unknown): { reason: string; detail: string } | null {
  if (!isRecord(detail) || typeof detail.reason !== 'string' || detail.reason === '') return null
  const text = typeof detail.detail === 'string' ? detail.detail : ''
  return { reason: detail.reason, detail: text }
}

function messageFrom(status: number, statusText: string, detail: unknown): string {
  if (typeof detail === 'string' && detail.trim() !== '') return detail
  const refusal = gateRefusal(detail)
  if (refusal) return refusal.detail || refusal.reason
  if (Array.isArray(detail)) {
    // FastAPI/pydantic validation errors: [{loc, msg, type}, ...]
    const parts = detail
      .map((d) => (isRecord(d) && typeof d.msg === 'string' ? d.msg : null))
      .filter((m): m is string => m !== null)
    if (parts.length) return parts.join('; ')
  }
  if (detail !== undefined) return JSON.stringify(detail)
  return `${status} ${statusText}`.trim()
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null
  const seconds = Number(value)
  if (Number.isFinite(seconds)) return Math.max(0, seconds)
  const date = Date.parse(value)
  return Number.isNaN(date) ? null : Math.max(0, (date - Date.now()) / 1000)
}

/** Build the right `TretError` subclass for a failed response. */
export function errorFromResponse(
  status: number,
  statusText: string,
  body: unknown,
  headers?: { get(name: string): string | null },
): TretError {
  const detail = isRecord(body) && 'detail' in body ? body.detail : undefined
  const message = messageFrom(status, statusText, detail)
  const refusal = gateRefusal(detail)
  if (refusal && (status === 402 || status === 403)) {
    return new TretBudgetRefused(status, refusal.reason, refusal.detail, detail, body)
  }
  if (status === 401 || status === 403) return new TretAuthError(status, message, detail, body)
  if (status === 404) return new TretNotFound(status, message, detail, body)
  if (status === 429) {
    return new TretRateLimited(
      status,
      message,
      detail,
      body,
      parseRetryAfter(headers?.get('retry-after') ?? null),
    )
  }
  return new TretError(status, message, detail, body)
}
