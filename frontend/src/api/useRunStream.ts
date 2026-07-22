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
  cost_usd: number
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
          cost_usd: Number(d.cost_usd ?? 0),
        },
      })),
    )
    on('done', (d) => {
      setState((s) => ({ ...s, done: true, status: typeof d.status === 'string' ? d.status : 'completed' }))
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
