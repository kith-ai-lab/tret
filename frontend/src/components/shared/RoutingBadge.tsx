import { Fragment, useEffect, useId, useRef, useState } from 'react'

import { objectiveDescription, type RoutingDecision } from '../../api/client'

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
 *  violet when a human override/pin decided; activating it expands the full,
 *  auditable decision.
 *
 *  **Keyboard.** The trigger is a real `<button>` with `aria-expanded` and
 *  `aria-controls`, so Tab reaches it, Enter/Space open it, Escape closes it and
 *  returns focus, and a screen reader announces both the control and its state.
 *  It used to be a `<span onClick>`: the routing rationale — the single most
 *  audit-relevant thing on the run list — was unreachable without a mouse.
 *
 *  The badge frequently sits inside a clickable table row, so activation stops
 *  propagating: opening the disclosure must not also navigate away from it. That
 *  applies to keyboard activation too, which arrives as a bubbling click. */
export function RoutingBadge({ routing, tier }: { routing: RoutingDecision | null; tier?: string }) {
  const [open, setOpen] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popId = useId()

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Only claim the key if focus is actually in here, so this never eats an
      // Escape meant for a dialog that happens to contain a routing badge.
      if (!ref.current?.contains(document.activeElement)) return
      e.stopPropagation()
      setOpen(false)
      triggerRef.current?.focus()
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
      <button
        type="button"
        ref={triggerRef}
        className={`badge ${color} badge-button`}
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((o) => !o)
        }}
        title="Routing decision — full audit detail"
      >
        {shortModelName(routing.chosen_model)}
        {suffix ? ` · ${suffix}` : ''}
      </button>
      {open && (
        <span
          className="routing-pop"
          id={popId}
          // The panel sits inside a clickable row too: selecting text in it must
          // not count as clicking the row.
          onClick={(e) => e.stopPropagation()}
        >
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
            <span className="k">Objective</span>
            <span title={objectiveDescription(routing.objective)}>
              {routing.objective ?? '—'}
            </span>
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
          {routing.evidence && (
            <>
              <div className="mono-label" style={{ margin: '10px 0 4px' }}>
                Track record at decision time
              </div>
              <div className="kv">
                {Object.values(routing.evidence.priors)
                  .sort((a, b) => b.quality_mean - a.quality_mean)
                  .map((p) => (
                    <Fragment key={p.model_id}>
                      <span className="k">{shortModelName(p.model_id)}</span>
                      <span>
                        quality {p.quality_mean.toFixed(2)} (floor{' '}
                        {p.quality_ci_low.toFixed(2)}) · {p.runs} runs ·{' '}
                        {(p.delivered_rate * 100).toFixed(0)}% delivered
                        {p.approvals + p.rejections > 0
                          ? ` · ${p.approvals} approved / ${p.rejections} rejected`
                          : ''}
                      </span>
                    </Fragment>
                  ))}
                {routing.evidence.unrecorded.length > 0 && (
                  <>
                    {/* Named, because "no record" and "poor record" are
                        different claims and the difference is what stops this
                        panel reading as a complete ranking. */}
                    <span className="k">No record</span>
                    <span>{routing.evidence.unrecorded.map(shortModelName).join(', ')}</span>
                  </>
                )}
                {routing.evidence.demoted.length > 0 && (
                  <>
                    <span className="k">Demoted</span>
                    <span>{routing.evidence.demoted.map(shortModelName).join(', ')}</span>
                  </>
                )}
                <span className="k">Size band</span>
                <span>{routing.evidence.size_band}</span>
              </div>
            </>
          )}
          <div className="mono-label" style={{ margin: '10px 0 4px' }}>
            Reasoning (verbatim)
          </div>
          <div className="mono-body" style={{ fontSize: 11.5, whiteSpace: 'pre-wrap' }}>
            {routing.reasoning || '—'}
          </div>

          {/* What the router was asked. Collapsed by default — it runs to a few
              KB — but present, because a decision you cannot see the question
              for is a decision you have to take on trust. Null here is
              meaningful rather than missing: no router was consulted. */}
          {routing.router_prompt ? (
            <>
              <button
                type="button"
                className="btn btn-sm"
                style={{ marginTop: 10 }}
                onClick={() => setShowPrompt((v) => !v)}
                aria-expanded={showPrompt}
              >
                {showPrompt ? 'Hide' : 'Show'} the prompt the router was given (
                {routing.router_prompt.length.toLocaleString()} chars)
              </button>
              {showPrompt && (
                <>
                  <pre
                    className="code-block"
                    style={{ maxHeight: 320, marginTop: 8, fontSize: 11 }}
                  >
                    {routing.router_prompt}
                  </pre>
                  <div className="kv" style={{ marginTop: 6 }}>
                    <span className="k">sha256</span>
                    <span style={{ wordBreak: 'break-all' }}>
                      {routing.router_prompt_sha256 ?? '—'}
                    </span>
                    <span className="k">System half</span>
                    <span>
                      pinned by prompt version {routing.routing_prompt_version} — constant
                      for that version, in the source
                    </span>
                  </div>
                </>
              )}
            </>
          ) : (
            <div className="mono-label" style={{ marginTop: 10 }}>
              No prompt: {routing.override ? 'a model was named, so nothing was asked' : 'no router was consulted'}
            </div>
          )}
        </span>
      )}
    </span>
  )
}
