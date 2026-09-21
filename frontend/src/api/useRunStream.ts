/** Live run stream: subscribes to /api/runs/{id}/events (SSE) and accumulates
 *  streamed text, tool activity, the routing decision, the context composition,
 *  budget warnings, and usage/done state.
 *
 *  **Two different things are both called "error" here, and conflating them lies
 *  to the user.** The engine publishes a semantic `event: error` frame when a run
 *  actually fails (harness.py — routing unavailable, cost cap, output budget,
 *  provider error). EventSource *also* fires an event named `error` on its own
 *  object for any transport problem: a dropped connection, a proxy timeout, a
 *  laptop lid closing. Both are delivered to an `error` listener, and both invoke
 *  `onerror` — so neither hook alone can tell them apart.
 *
 *  What tells them apart is the payload. A server-sent frame arrives as a
 *  `MessageEvent` carrying a `data` **string** (the engine always sends a JSON
 *  body). A transport failure arrives as a bare `Event` with no `data` at all.
 *  `isServerFrame` is that check, and it is the only thing standing between a
 *  network blip and the UI announcing that a healthy run failed.
 *
 *  Reconnects: EventSource reconnects on its own and the backend replays the run's
 *  full backlog from the start, so accumulated state is dropped at the moment of
 *  the drop rather than duplicated on replay. `connection` reports that as
 *  `reconnecting` — a transport state, never a run verdict, and `error`/`status`
 *  are left untouched. A server that keeps ending the stream without a terminal
 *  event (a finished run whose backlog the bus has already released) would
 *  otherwise reconnect forever, so consecutive silent reconnects are capped. */
import { useEffect, useRef, useState } from 'react'

import type { ContextComposition, RoutingDecision } from './client'

export interface ToolCallItem {
  kind: 'tool_call'
  id: string
  tool: string
  arguments: Record<string, unknown>
}

export interface ToolResultItem {
  kind: 'tool_result'
  id: string
  tool: string
  error: boolean
  result: string
}

export interface FindingRecordedItem {
  kind: 'finding_recorded'
  finding_id: string
}

export type StreamItem = ToolCallItem | ToolResultItem | FindingRecordedItem

/** A child run this run started via `run_harness_task` / `delegate_parallel` /
 *  `spawn_subagent`. Kept as its own array rather than folded into `StreamItem`:
 *  `RunDetail.tsx`'s `ToolItemRow` switches over `StreamItem.kind` assuming only
 *  the three existing members, so adding a fourth there would need edits to a
 *  file this task does not own. */
export interface DelegationItem {
  kind: 'delegation'
  childRunId: string
  harness: string
  taskType: string | null
  delegationKind: 'task' | 'subagent'
  /** Shared by every child of one `delegate_parallel` call; null otherwise. */
  batchId: string | null
  /** Position within a `delegate_parallel` batch; null outside one. */
  index: number | null
  label: string | null
  /** A run status once `delegation_finished` lands, 'unknown' if the engine
   *  could not report one, or 'running' while still in flight. */
  status: 'running' | string
  costUsd: number | null
}

/** The engine crossed a run's soft output budget and told the model to finalize
 *  (harness.py). Not a failure — the run continues — but the user is entitled to
 *  know their answer is being wrapped up early. A run that then keeps going is
 *  stopped by the hard multiple and reports a semantic `error` instead. */
export interface BudgetWarning {
  kind: string // "output_tokens"
  output_tokens: number
  budget: number
}

/** The engine started this run without a web tool (web_search/fetch_url) the
 *  harness or task declared, because the deployment has web research switched
 *  off (TRET_EGRESS_RESEARCH — harness.py, `withheld_web_tools`). Not a
 *  failure — the run continues — but a run silently missing a capability its
 *  author listed is exactly what this event exists to surface. Published at
 *  most once, before the loop starts, so a single value is enough. */
export interface ToolsWithheldNotice {
  tools: string[]
  reason: string
  detail: string
}

/** Transport state of the SSE connection. Says nothing about the run: a run can
 *  be perfectly healthy while this reads `reconnecting`, and `closed` after a
 *  clean `done` is the normal end state. */
export type StreamConnection = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface UsageInfo {
  iteration: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  // Estimated, not metered. null when the engine reported no estimate.
  // Every figure is the run's cumulative total so far, not a per-iteration delta.
  energy_wh: number | null // compute / IT load only
  co2e_g: number | null // run total = scope1 + scope2 + scope3
  scope2_g: number | null
  scope3_g: number | null
  baseline_co2e_g: number | null
  // Signed: negative means heavier than the baseline. Never take its absolute.
  avoided_co2e_g: number | null
  // Money against the same-token baseline, signed the same way. Firmer than the
  // carbon figure: per-token prices are exact arithmetic, not estimation.
  avoided_usd: number | null
  // Share of frontier spend avoided, one decimal place — exact, unlike the
  // carbon comparison's coarse multiple. null (never 0%) until the engine
  // reports one, or when the baseline itself costs nothing.
  avoided_usd_pct: number | null
  // The judgment band around co2e_g — NOT a confidence interval. null until the
  // engine reports one.
  co2e_g_low: number | null
  co2e_g_high: number | null
}

export interface RunStreamState {
  text: string
  items: StreamItem[]
  /** How many tool_call/tool_result/finding_recorded items have been dropped
   *  from the front of `items` to hold it at MAX_ITEMS. Zero for the common
   *  case of a run that never grows past the cap. */
  truncatedItems: number
  /** Child runs delegated to via `run_harness_task`/`delegate_parallel`/
   *  `spawn_subagent`, in arrival order (see `DelegationItem`). */
  delegations: DelegationItem[]
  routing: RoutingDecision | null
  /** Where the prompt tokens went, as published right after routing — so the
   *  breakdown is available at second one of the run rather than only after it
   *  finishes and the persisted run refetches. */
  composition: ContextComposition | null
  /** The most recent budget nudge, if the run has crossed its soft output budget. */
  budget: BudgetWarning | null
  /** Set once if the engine withheld a declared web tool for this deployment.
   *  See `ToolsWithheldNotice`. */
  toolsWithheld: ToolsWithheldNotice | null
  usage: UsageInfo | null
  status: string | null
  done: boolean
  /** A run-level failure **the engine reported**. Never set by a transport
   *  problem — see the module docstring. */
  error: string | null
  /** Connection health. Purely about the socket; read `error` for the run. */
  connection: StreamConnection
}

const initialState: RunStreamState = {
  text: '',
  items: [],
  truncatedItems: 0,
  delegations: [],
  routing: null,
  composition: null,
  budget: null,
  toolsWithheld: null,
  usage: null,
  status: null,
  done: false,
  error: null,
  connection: 'connecting',
}

/** How many times in a row the transport may drop and reconnect without a single
 *  event arriving in between before we stop trying. Guards the one case that
 *  would otherwise spin forever: the server ends the stream immediately (the run
 *  is over and the event bus has already released its backlog), so every
 *  reconnect is answered with another immediate close. */
const MAX_SILENT_RECONNECTS = 5

/** Cap on `RunStreamState.items`. A long-running run can emit thousands of
 *  tool calls/results; keeping every one of them alive as a growing spread
 *  copy on each event is an unbounded-memory footgun for a tab left open. The
 *  most recent entries matter far more than the earliest ones, so once the
 *  cap is hit the oldest are dropped and counted in `truncatedItems`. */
const MAX_ITEMS = 500

/** Appends one item to `items`, holding the array at MAX_ITEMS by dropping
 *  from the front and folding the drop into `truncatedItems`. */
function appendItem(
  items: StreamItem[],
  truncatedItems: number,
  item: StreamItem,
): { items: StreamItem[]; truncatedItems: number } {
  const next = [...items, item]
  if (next.length <= MAX_ITEMS) return { items: next, truncatedItems }
  const overflow = next.length - MAX_ITEMS
  return { items: next.slice(overflow), truncatedItems: truncatedItems + overflow }
}

/** Is this `error` event a frame the server sent, or EventSource's own transport
 *  failure? The engine's frames always carry a JSON `data` string; a transport
 *  failure is a bare Event with no data. */
function isServerFrame(event: Event): boolean {
  return typeof (event as MessageEvent).data === 'string'
}

/** Estimates are nullable on the wire: a missing estimate is not zero draw. */
function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

export function useRunStream(runId: string | null): RunStreamState {
  const [state, setState] = useState<RunStreamState>(initialState)
  // Consecutive transport drops with no event in between. Reset by any frame.
  const silentReconnects = useRef(0)

  useEffect(() => {
    if (!runId) {
      setState(initialState)
      return
    }
    setState(initialState)
    silentReconnects.current = 0
    const es = new EventSource(`/api/runs/${runId}/events`)

    // Payloads are typed loosely by design: they are engine-defined JSON.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const on = (type: string, handler: (data: any) => void) => {
      es.addEventListener(type, (e) => {
        // A frame arrived, so the transport is working: forget any earlier drops
        // and stop reporting `reconnecting`.
        silentReconnects.current = 0
        setState((s) => (s.connection === 'open' || s.done ? s : { ...s, connection: 'open' }))
        let data: unknown = {}
        try {
          data = JSON.parse((e as MessageEvent).data as string)
        } catch {
          /* keepalives / malformed frames */
        }
        handler(data)
      })
    }

    es.addEventListener('open', () => {
      silentReconnects.current = 0
      setState((s) => (s.done ? s : { ...s, connection: 'open' }))
    })

    // Never published by the engine today (status travels on `done`/`error`), but
    // handled so a future status frame is not silently dropped.
    on('status', (d) => setState((s) => ({ ...s, status: typeof d.status === 'string' ? d.status : s.status })))
    on('routing', (d) => setState((s) => ({ ...s, routing: d as RoutingDecision })))
    // Published immediately after routing, with the same shape the persisted run
    // carries. Guarded on `blocks` so a malformed frame cannot render a half
    // composition with a NaN total.
    on('context_composition', (d) =>
      setState((s) =>
        Array.isArray(d?.blocks) ? { ...s, composition: d as ContextComposition } : s,
      ),
    )
    on('budget_warning', (d) =>
      setState((s) => ({
        ...s,
        budget: {
          kind: String(d.kind ?? 'output_tokens'),
          output_tokens: Number(d.output_tokens ?? 0),
          budget: Number(d.budget ?? 0),
        },
      })),
    )
    // Published at most once, before the loop starts. Guarded on `tools` so a
    // malformed frame cannot render an empty notice.
    on('tools_withheld', (d) =>
      setState((s) =>
        Array.isArray(d?.tools)
          ? {
              ...s,
              toolsWithheld: {
                tools: d.tools.map(String),
                reason: String(d.reason ?? ''),
                detail: String(d.detail ?? ''),
              },
            }
          : s,
      ),
    )
    on('text_delta', (d) => setState((s) => ({ ...s, text: s.text + (typeof d.text === 'string' ? d.text : '') })))
    on('tool_call', (d) =>
      setState((s) => ({
        ...s,
        ...appendItem(s.items, s.truncatedItems, {
          kind: 'tool_call',
          id: String(d.id ?? ''),
          tool: String(d.tool ?? '?'),
          arguments: d.arguments ?? {},
        }),
      })),
    )
    on('tool_result', (d) =>
      setState((s) => ({
        ...s,
        ...appendItem(s.items, s.truncatedItems, {
          kind: 'tool_result',
          id: String(d.id ?? ''),
          tool: String(d.tool ?? '?'),
          error: Boolean(d.error),
          result: String(d.result ?? ''),
        }),
      })),
    )
    on('finding_recorded', (d) =>
      setState((s) => ({
        ...s,
        ...appendItem(s.items, s.truncatedItems, {
          kind: 'finding_recorded',
          finding_id: String(d.finding_id ?? ''),
        }),
      })),
    )
    on('delegation_started', (d) =>
      setState((s) => {
        const childRunId = String(d.child_run_id ?? '')
        // A duplicate (e.g. a reconnect replaying the backlog) is ignored rather
        // than appended twice.
        if (!childRunId || s.delegations.some((x) => x.childRunId === childRunId)) return s
        const item: DelegationItem = {
          kind: 'delegation',
          childRunId,
          harness: String(d.harness ?? '?'),
          taskType: typeof d.task_type === 'string' ? d.task_type : null,
          delegationKind: d.kind === 'subagent' ? 'subagent' : 'task',
          batchId: typeof d.batch_id === 'string' ? d.batch_id : null,
          index: typeof d.index === 'number' ? d.index : null,
          label: typeof d.label === 'string' ? d.label : null,
          status: 'running',
          costUsd: null,
        }
        return { ...s, delegations: [...s.delegations, item] }
      }),
    )
    on('delegation_finished', (d) =>
      setState((s) => {
        const childRunId = String(d.child_run_id ?? '')
        if (!childRunId) return s
        const status = typeof d.status === 'string' ? d.status : 'unknown'
        const costUsd = numberOrNull(d.cost_usd)
        const idx = s.delegations.findIndex((x) => x.childRunId === childRunId)
        if (idx === -1) {
          // The stream was opened mid-run (or after a reconnect dropped the
          // `delegation_started` frame): the child never appeared as running, so
          // it arrives already finished.
          const item: DelegationItem = {
            kind: 'delegation',
            childRunId,
            harness: String(d.harness ?? '?'),
            taskType: typeof d.task_type === 'string' ? d.task_type : null,
            delegationKind: d.kind === 'subagent' ? 'subagent' : 'task',
            batchId: typeof d.batch_id === 'string' ? d.batch_id : null,
            index: typeof d.index === 'number' ? d.index : null,
            label: typeof d.label === 'string' ? d.label : null,
            status,
            costUsd,
          }
          return { ...s, delegations: [...s.delegations, item] }
        }
        const next = [...s.delegations]
        next[idx] = { ...next[idx], status, costUsd }
        return { ...s, delegations: next }
      }),
    )
    on('usage', (d) =>
      setState((s) => ({
        ...s,
        usage: {
          iteration: Number(d.iteration ?? 0),
          input_tokens: Number(d.input_tokens ?? 0),
          output_tokens: Number(d.output_tokens ?? 0),
          cache_read_tokens: Number(d.cache_read_tokens ?? 0),
          cache_write_tokens: Number(d.cache_write_tokens ?? 0),
          cost_usd: Number(d.cost_usd ?? 0),
          energy_wh: numberOrNull(d.energy_wh),
          co2e_g: numberOrNull(d.co2e_g),
          scope2_g: numberOrNull(d.scope2_g),
          scope3_g: numberOrNull(d.scope3_g),
          baseline_co2e_g: numberOrNull(d.baseline_co2e_g),
          avoided_co2e_g: numberOrNull(d.avoided_co2e_g),
          avoided_usd: numberOrNull(d.avoided_usd),
          avoided_usd_pct: numberOrNull(d.avoided_usd_pct),
          co2e_g_low: numberOrNull(d.co2e_g_low),
          co2e_g_high: numberOrNull(d.co2e_g_high),
        },
      })),
    )
    on('done', (d) => {
      setState((s) => ({
        ...s,
        done: true,
        connection: 'closed',
        status: typeof d.status === 'string' ? d.status : 'completed',
        // `done` carries the final cost/energy totals; keep the last usage
        // frame's token counts, which `done` does not repeat.
        usage: s.usage
          ? {
              ...s.usage,
              cost_usd: Number(d.cost_usd ?? s.usage.cost_usd),
              energy_wh: numberOrNull(d.energy_wh) ?? s.usage.energy_wh,
              co2e_g: numberOrNull(d.co2e_g) ?? s.usage.co2e_g,
              scope2_g: numberOrNull(d.scope2_g) ?? s.usage.scope2_g,
              scope3_g: numberOrNull(d.scope3_g) ?? s.usage.scope3_g,
              baseline_co2e_g: numberOrNull(d.baseline_co2e_g) ?? s.usage.baseline_co2e_g,
              avoided_co2e_g: numberOrNull(d.avoided_co2e_g) ?? s.usage.avoided_co2e_g,
              avoided_usd: numberOrNull(d.avoided_usd) ?? s.usage.avoided_usd,
              avoided_usd_pct: numberOrNull(d.avoided_usd_pct) ?? s.usage.avoided_usd_pct,
              co2e_g_low: numberOrNull(d.co2e_g_low) ?? s.usage.co2e_g_low,
              co2e_g_high: numberOrNull(d.co2e_g_high) ?? s.usage.co2e_g_high,
            }
          : s.usage,
      }))
      es.close()
    })
    on('ping', () => {
      /* keepalive */
    })

    // ── the one listener that has to tell two things apart ──────────────────
    // Registered directly rather than through `on` so the transport branch is
    // reached before anything tries to parse a body that does not exist. Both
    // EventSource's transport failure and the engine's `event: error` frame are
    // delivered here (and to `onerror`, which is therefore left unused).
    es.addEventListener('error', (event) => {
      if (isServerFrame(event)) {
        // The engine says the run failed. This is a verdict.
        silentReconnects.current = 0
        let data: { message?: unknown; status?: unknown } = {}
        try {
          data = JSON.parse((event as MessageEvent).data as string)
        } catch {
          /* malformed frame — fall back to the generic wording below */
        }
        setState((s) => ({
          ...s,
          done: true,
          connection: 'closed',
          error: typeof data.message === 'string' ? data.message : 'run failed',
          status: typeof data.status === 'string' ? data.status : 'failed',
        }))
        es.close()
        return
      }

      // A transport problem. The run is not implicated: say nothing about it.
      if (es.readyState === EventSource.CLOSED) {
        // EventSource gave up (or we closed it after a terminal frame).
        setState((s) => (s.done ? s : { ...s, connection: 'closed' }))
        return
      }
      silentReconnects.current += 1
      if (silentReconnects.current > MAX_SILENT_RECONNECTS) {
        // Reconnecting is not getting us anywhere; stop rather than loop.
        es.close()
        setState((s) => (s.done ? s : { ...s, connection: 'closed' }))
        return
      }
      // EventSource will reconnect and the server replays this run's backlog from
      // the start — drop what we accumulated so the replay cannot duplicate it.
      // `status` survives as the last thing we knew; `error` stays null.
      setState((s) =>
        s.done ? s : { ...initialState, status: s.status, connection: 'reconnecting' },
      )
    })

    return () => es.close()
  }, [runId])

  return state
}
