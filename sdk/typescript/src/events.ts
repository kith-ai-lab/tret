/**
 * The run event stream: `GET /api/runs/{id}/events`.
 *
 * Hand-written, because the stream is Server-Sent Events and OpenAPI has no
 * schema for it. The source of truth is `backend/tret/engine/events.py`
 * (`RunEvent.type`'s documented list) and the `RunEvent("<type>", {...})`
 * constructions in `backend/tret/engine/harness.py`, `engine/tools.py` and
 * `services/budgets.py`. `test/events.test.ts` parses both and fails when this
 * union names a different set, so a backend event cannot be added silently.
 *
 * Wire format: `event: <type>\ndata: <json>\n\n`, where the JSON is the
 * payload with a `ts` (unix seconds, float) merged in. The SDK lifts `ts` out
 * of the payload onto the event itself.
 *
 * Payload fields mirror the backend exactly (snake_case). Index signatures
 * are kept on the larger records so an additive backend field never breaks a
 * consumer's type-check. Every carbon/energy figure is `number | null`:
 * `null` means "no estimate", never zero.
 */

/** Every event type the backend publishes, in `events.py` order. */
export const RUN_EVENT_TYPES = [
  'routing',
  'context_composition',
  'text_delta',
  'tool_call',
  'tool_result',
  'finding_recorded',
  'usage',
  'budget_warning',
  'budget_alert',
  'tools_withheld',
  'context_pressure',
  'compaction',
  'model_switch',
  'switch_refused',
  'effort_raised',
  'provider_ignore_waived',
  'delegation_started',
  'delegation_finished',
  'delegation_progress',
  'lesson_proposed',
  'done',
  'error',
] as const

export type RunEventType = (typeof RUN_EVENT_TYPES)[number]

/** `done` and `error` end a run's stream; nothing follows either. */
export const TERMINAL_EVENT_TYPES = ['done', 'error'] as const

/** Carbon fields carried by `usage` and `done` (services/emissions.py
 *  `emission_event_fields`). Null = no estimate, never 0. */
export interface EmissionEventFields {
  co2e_g: number | null
  scope2_g: number | null
  scope3_g: number | null
  baseline_co2e_g: number | null
  avoided_co2e_g: number | null
  avoided_usd: number | null
  avoided_usd_pct: number | null
  co2e_g_low: number | null
  co2e_g_high: number | null
  water_ml: number | null
}

/** `router_llm/router.py::RoutingDecision`, serialised with `asdict`. */
export interface RoutingDecision {
  router_model: string | null
  routing_prompt_version: string
  candidates: string[]
  chosen_model: string
  reasoning: string
  confidence?: string | null
  objective: string
  task_shape?: string
  max_cost_tier?: string
  effort?: string | null
  fallback_used: boolean
  override?: string | null
  latency_ms?: number
  decided_at?: string
  /** The router call's own spend, kept out of the run's cost. Null when no
   *  router model was contacted. */
  spend?: Record<string, unknown> | null
  context_fit?: Record<string, unknown> | null
  exploration?: Record<string, unknown> | null
  evidence?: Record<string, unknown> | null
  [key: string]: unknown
}

export interface ContextBlock {
  kind: string
  label: string
  chars: number
  est_tokens: number
  sha256?: string
  sections?: string[]
  parts?: Record<string, number>
  note?: string
}

/** `engine/context.py::composition_report`. */
export interface ContextComposition {
  estimator: string
  total_est_tokens: number
  total_chars: number
  by_kind: Record<string, number>
  blocks: ContextBlock[]
  [key: string]: unknown
}

export interface UsageEventData extends EmissionEventFields {
  iteration: number
  /** Cumulative for the run so far, not per iteration. */
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  energy_wh: number | null
  [key: string]: unknown
}

export interface DoneEventData extends EmissionEventFields {
  /** `completed` or `completed_without_output`. */
  status: string
  cost_usd: number
  energy_wh: number | null
  iterations: number | null
  [key: string]: unknown
}

export interface DelegationIdentity {
  child_run_id: string
  harness: string
  kind: string
  batch_id: string | null
  index: number | null
  label: string | null
}

/** Payload of each event type. */
export interface RunEventDataMap {
  routing: RoutingDecision
  context_composition: ContextComposition
  text_delta: { text: string }
  tool_call: { tool: string; id: string; arguments: Record<string, unknown> }
  /** `result` is truncated to 2000 characters on the stream. */
  tool_result: { tool: string; id: string; error: boolean; result: string }
  finding_recorded: { finding_id: string }
  usage: UsageEventData
  budget_warning: {
    kind: 'output_tokens' | 'iterations' | (string & {})
    budget: number
    output_tokens?: number
    iterations?: number
  }
  budget_alert: {
    workspace_id: string
    period: string
    fraction: number
    spent_usd: number
    cap_usd: number
  }
  tools_withheld: { tools: string[]; reason: string; detail: string }
  context_pressure: {
    iteration: number
    est_input_tokens: number
    limit_est_tokens: number
    context_window: number | null
    estimator: string
    basis: string
  }
  compaction: {
    kind: 'elision' | 'no_op' | (string & {})
    iteration: number
    trigger: string
    before_est_tokens: number
    after_est_tokens: number
    [key: string]: unknown
  }
  model_switch: {
    at_iteration: number
    from_model: string | null
    chosen_model: string
    provider: string
    reason: string
    detail: string | null
    evidence: Record<string, unknown> | null
    decided_at: string
  }
  switch_refused: { iteration: number; reason: string; detail: string | null; refused: unknown }
  effort_raised: {
    at_iteration: number
    model: string
    from_effort: string | null
    to_effort: string
    reason: string
    detail: string | null
    evidence: Record<string, unknown> | null
    decided_at: string
  }
  provider_ignore_waived: { iteration: number; error: string }
  delegation_started: DelegationIdentity & { task_type: string }
  delegation_finished: DelegationIdentity & {
    /** The child's final status, or `unknown` when it could not be read. */
    status: string
    cost_usd?: number
  }
  delegation_progress: {
    child_run_id: string
    iteration: number
    max_iterations: number
    tool: string | null
    model: string | null
  }
  lesson_proposed: { lesson_id: string; text: string }
  done: DoneEventData
  /** `status` is absent when the run failed before it started (a refused
   *  gate, an unknown task type, no route). */
  error: { message: string; status?: string }
}

/** One event from a run's stream, discriminated on `type`. */
export type RunEvent = {
  [K in RunEventType]: {
    type: K
    data: RunEventDataMap[K]
    /** Unix seconds (float) when the backend published it; null if absent. */
    ts: number | null
    /** True only for a terminal event the SDK built from the run record,
     *  because the server no longer had the stream to replay (see
     *  `runs.events`). */
    synthetic?: boolean
  }
}[RunEventType]

export type RunEventOf<K extends RunEventType> = Extract<RunEvent, { type: K }>

const KNOWN = new Set<string>(RUN_EVENT_TYPES)

export function isRunEventType(type: string): type is RunEventType {
  return KNOWN.has(type)
}

export function isTerminal(event: { type: string }): boolean {
  return event.type === 'done' || event.type === 'error'
}

/**
 * Turn one SSE frame into a typed event. Returns `null` for a keepalive
 * (`ping`), an empty frame, or a type this SDK version does not know.
 */
export function toRunEvent(eventName: string, data: string): RunEvent | null {
  if (!isRunEventType(eventName)) return null
  let payload: unknown
  try {
    payload = data === '' ? {} : JSON.parse(data)
  } catch {
    return null
  }
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) return null
  const { ts, ...rest } = payload as Record<string, unknown>
  return {
    type: eventName,
    data: rest,
    ts: typeof ts === 'number' ? ts : null,
  } as RunEvent
}
