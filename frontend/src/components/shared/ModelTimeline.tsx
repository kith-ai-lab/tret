import { type CompactionRecord, type ModelSegment } from '../../api/client'
import { formatTokens } from './format'
import { shortModelName } from './RoutingBadge'

const REASONS: Record<string, string> = {
  initial: 'routed at the start',
  context_exhausted: 'ran out of context window with nothing left to elide',
  capability_stall: 'the run had stopped making progress',
}

/** Every model a run used, and what each one actually spent.
 *
 *  Shown only when a run used more than one — which is rare, and exactly why it
 *  needs saying. The run's headline energy and carbon are a sum across these
 *  segments, and its per-model factors (energy class, PUE, grid intensity) are
 *  reported only where the segments agreed; a reader who sees "—" in the
 *  emissions panel finds the un-nulled detail here. */
export function ModelTimeline({ segments }: { segments: ModelSegment[] }) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        Models used ({segments.length})
      </div>
      <div
        style={{
          marginBottom: 8,
          fontFamily: 'var(--mono)',
          fontSize: 10.5,
          color: 'var(--text-muted)',
        }}
      >
        This run changed model part-way. Each segment is accounted against the model that ran
        it — energy class, PUE and grid intensity are properties of the model, not of the run.
      </div>
      <div className="stack" style={{ gap: 6 }}>
        {segments.map((s, i) => (
          <div key={`${s.model}-${s.from_iteration}-${i}`} className="panel" style={{ padding: 10 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <span className="mono-body">{shortModelName(s.model)}</span>
              <span className="mono-label">
                iterations {s.from_iteration}–{s.to_iteration}
              </span>
            </div>
            <div className="mono-label" style={{ marginTop: 4 }}>
              {REASONS[s.reason] ?? s.reason}
            </div>
            <div className="mono-body" style={{ marginTop: 4, fontSize: 11.5 }}>
              {formatTokens(s.input_tokens)} in · {formatTokens(s.output_tokens)} out · $
              {s.cost_usd.toFixed(4)} · {s.energy_wh.toFixed(3)} Wh (est.) ·{' '}
              {s.energy_accounting?.energy_class ?? '—'}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

const KINDS: Record<string, string> = {
  elision: 'elided earlier tool results',
  history_trim: 'dropped the oldest conversation turns',
  no_op: 'over the window with nothing left that may be elided',
}

/** What a run stopped showing the model, and when.
 *
 *  The transcript above this panel is complete and unedited — compaction changes
 *  only what was sent. This is the record of the difference, which is the one
 *  thing a reader cannot recover from the transcript itself. */
export function CompactionLog({ records }: { records: CompactionRecord[] }) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        Context compaction ({records.length})
      </div>
      <div
        style={{
          marginBottom: 8,
          fontFamily: 'var(--mono)',
          fontSize: 10.5,
          color: 'var(--text-muted)',
        }}
      >
        The transcript below is complete and unedited. These are the points at which the run
        stopped <em>showing</em> the model some of it, to stay inside the context window.
        Retrieved values and recorded results are never elided.
      </div>
      <div className="stack" style={{ gap: 6 }}>
        {records.map((r, i) => (
          <div key={`${r.kind}-${r.iteration}-${i}`} className="panel" style={{ padding: 10 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <span className="mono-body">{KINDS[r.kind] ?? r.kind}</span>
              <span className="mono-label">
                {r.iteration ? `iteration ${r.iteration}` : 'before the first call'}
              </span>
            </div>
            <div className="mono-body" style={{ marginTop: 4, fontSize: 11.5 }}>
              {r.before_est_tokens != null && r.after_est_tokens != null
                ? `${formatTokens(r.before_est_tokens)} → ${formatTokens(r.after_est_tokens)} est. tokens`
                : null}
              {r.dropped_history_turns
                ? `${r.dropped_history_turns} prior turn${r.dropped_history_turns === 1 ? '' : 's'} dropped`
                : null}
              {r.elided_tools?.length ? ` · ${r.elided_tools.join(', ')}` : null}
              {r.summarized ? ` · summarized by ${shortModelName(r.summarizer_model ?? '')}` : null}
            </div>
            {r.note && (
              <div className="mono-label" style={{ marginTop: 4, color: 'var(--amber)' }}>
                {r.note}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
