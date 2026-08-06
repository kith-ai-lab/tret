/** Live run stream: subscribes to /api/runs/{id}/events (SSE) and accumulates
 *  streamed text, tool activity, the routing decision, and usage/done state.
 *  The backend replays the full backlog on (re)connect, so on transport error
 *  we reset accumulated state before the automatic EventSource reconnect. */
import { useEffect, useState } from 'react'

import type { RoutingDecision } from './client'

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
  // The judgment band around co2e_g — NOT a confidence interval. null until the
  // engine reports one.
  co2e_g_low: number | null
  co2e_g_high: number | null
}

export interface RunStreamState {
  text: string
  items: StreamItem[]
  routing: RoutingDecision | null
  usage: UsageInfo | null
  status: string | null
  done: boolean
  error: string | null
}

const initialState: RunStreamState = {
  text: '',
  items: [],
  routing: null,
  usage: null,
  status: null,
  done: false,
  error: null,
}

/** Estimates are nullable on the wire: a missing estimate is not zero draw. */
function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

export function useRunStream(runId: string | null): RunStreamState {
  const [state, setState] = useState<RunStreamState>(initialState)

  useEffect(() => {
    if (!runId) {
      setState(initialState)
      return
    }
    setState(initialState)
    const es = new EventSource(`/api/runs/${runId}/events`)

    // Payloads are typed loosely by design: they are engine-defined JSON.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const on = (type: string, handler: (data: any) => void) => {
      es.addEventListener(type, (e) => {
        let data: unknown = {}
        try {
          data = JSON.parse((e as MessageEvent).data as string)
        } catch {
          /* keepalives / malformed frames */
        }
        handler(data)
      })
    }

    on('status', (d) => setState((s) => ({ ...s, status: typeof d.status === 'string' ? d.status : s.status })))
    on('routing', (d) => setState((s) => ({ ...s, routing: d as RoutingDecision })))
    on('text_delta', (d) => setState((s) => ({ ...s, text: s.text + (typeof d.text === 'string' ? d.text : '') })))
    on('tool_call', (d) =>
      setState((s) => ({
        ...s,
        items: [
          ...s.items,
          { kind: 'tool_call', id: String(d.id ?? ''), tool: String(d.tool ?? '?'), arguments: d.arguments ?? {} },
        ],
      })),
    )
    on('tool_result', (d) =>
      setState((s) => ({
        ...s,
        items: [
          ...s.items,
          {
            kind: 'tool_result',
            id: String(d.id ?? ''),
            tool: String(d.tool ?? '?'),
            error: Boolean(d.error),
            result: String(d.result ?? ''),
          },
        ],
      })),
    )
    on('finding_recorded', (d) =>
      setState((s) => ({
        ...s,
        items: [...s.items, { kind: 'finding_recorded', finding_id: String(d.finding_id ?? '') }],
      })),
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
          co2e_g_low: numberOrNull(d.co2e_g_low),
          co2e_g_high: numberOrNull(d.co2e_g_high),
        },
      })),
    )
    on('done', (d) => {
      setState((s) => ({
        ...s,
        done: true,
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
              co2e_g_low: numberOrNull(d.co2e_g_low) ?? s.usage.co2e_g_low,
              co2e_g_high: numberOrNull(d.co2e_g_high) ?? s.usage.co2e_g_high,
            }
          : s.usage,
      }))
      es.close()
    })
    on('error', (d) => {
      setState((s) => ({
        ...s,
        done: true,
        error: typeof d.message === 'string' ? d.message : 'run failed',
        status: typeof d.status === 'string' ? d.status : 'failed',
      }))
      es.close()
    })
    on('ping', () => {
      /* keepalive */
    })

    es.onerror = () => {
      // EventSource will reconnect and the server replays the backlog from the
      // start — drop what we accumulated so nothing is duplicated.
      if (es.readyState !== EventSource.CLOSED) {
        setState((s) => (s.done ? s : { ...initialState, status: s.status }))
      }
    }

    return () => es.close()
  }, [runId])

  return state
}
