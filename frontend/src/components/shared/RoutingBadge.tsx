import { useEffect, useRef, useState } from 'react'

import type { RoutingDecision } from '../../api/client'

/** Strip the provider prefix: "anthropic/claude-sonnet-5" → "claude-sonnet-5". */
export function shortModelName(id: string): string {
  const i = id.indexOf('/')
  return i >= 0 ? id.slice(i + 1) : id
}

function tierOf(routing: RoutingDecision): string | null {
  // The decision itself doesn't carry the tier; show override/fallback context
  // instead when present.
  if (routing.override === 'user_pin') return 'pinned'
  if (routing.override === 'run_override') return 'override'
  if (routing.fallback_used) return 'fallback'
  return null
}

/** Chip showing the routed model. Amber when the fallback path was used,
 *  violet when a human override/pin decided; click expands the full,
 *  auditable decision. */
export function RoutingBadge({ routing, tier }: { routing: RoutingDecision | null; tier?: string }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!routing) return <span className="badge badge-gray">unrouted</span>

  const color = routing.fallback_used ? 'badge-amber' : routing.override ? 'badge-violet' : 'badge-blue'
  const suffix = tier ?? tierOf(routing)

  return (
    <span className="routing-wrap" ref={ref}>
      <span
        className={`badge ${color} clickable`}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((o) => !o)
        }}
        title="Routing decision — click for full audit detail"
      >
        {shortModelName(routing.chosen_model)}
        {suffix ? ` · ${suffix}` : ''}
      </span>
      {open && (
        <span className="routing-pop" onClick={(e) => e.stopPropagation()}>
          <div className="mono-label" style={{ marginBottom: 8 }}>
            Routing decision
          </div>
          <div className="kv">
            <span className="k">Chosen</span>
            <span>{routing.chosen_model}</span>
            <span className="k">Router model</span>
            <span>{routing.router_model ?? '— (no router call)'}</span>
            <span className="k">Prompt version</span>
            <span>{routing.routing_prompt_version}</span>
            <span className="k">Override</span>
            <span>{routing.override ?? 'none'}</span>
            <span className="k">Fallback used</span>
            <span>{routing.fallback_used ? 'yes' : 'no'}</span>
            <span className="k">Confidence</span>
            <span>{routing.confidence ?? '—'}</span>
            <span className="k">Latency</span>
            <span>{routing.latency_ms} ms</span>
            <span className="k">Decided at</span>
            <span>{routing.decided_at}</span>
            <span className="k">Candidates</span>
            <span>{routing.candidates.length ? routing.candidates.join(', ') : '—'}</span>
          </div>
          <div className="mono-label" style={{ margin: '10px 0 4px' }}>
            Reasoning (verbatim)
          </div>
          <div className="mono-body" style={{ fontSize: 11.5, whiteSpace: 'pre-wrap' }}>
            {routing.reasoning || '—'}
          </div>
        </span>
      )}
    </span>
  )
}
