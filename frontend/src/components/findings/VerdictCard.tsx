/** Reviewer-facing rendering of a finding's payload.
 *
 *  A `divergence_verdict` reads as a verdict: coloured badge, reason, confidence,
 *  the methodology note as prose, and the cited values — the handful of
 *  retrieved cells the verdict actually rests on — as the primary evidence.
 *  Every other schema falls back to a key/value list of its top-level scalars,
 *  with nested structures collapsed and the raw JSON one click away. */
import { Link } from 'react-router-dom'

import type { Finding } from '../../api/client'

interface CitedValue {
  dataset?: unknown
  row_ref?: unknown
  column?: unknown
  value?: unknown
}

/** Where a finding can be opened by id. */
export function findingHref(id: string): string {
  return `/approvals?finding=${encodeURIComponent(id)}`
}

function verdictBadgeClass(verdict: string): string {
  if (verdict === 'agree') return 'badge-green'
  if (verdict.startsWith('diverge')) return 'badge-amber'
  return 'badge-gray' // insufficient_data and anything unrecognised: muted
}

function humanize(s: string): string {
  return s.replace(/_/g, ' ')
}

function isScalar(v: unknown): v is string | number | boolean | null {
  return v === null || ['string', 'number', 'boolean'].includes(typeof v)
}

function display(v: unknown): string {
  return v === null || v === undefined ? '—' : String(v)
}

function Supersession({ finding }: { finding: Finding }) {
  const n = finding.supersedes?.length ?? 0
  if (n === 0 && !finding.superseded_by) return null
  return (
    <div className="fine-print">
      {n > 0 && (
        <div>
          Supersedes {n} earlier draft{n === 1 ? '' : 's'} for this case.
        </div>
      )}
      {finding.superseded_by && (
        <div>
          Superseded by <Link to={findingHref(finding.superseded_by)}>a newer finding</Link> for this
          case.
        </div>
      )}
    </div>
  )
}

function RawPayload({ payload }: { payload: Record<string, unknown> }) {
  return (
    <details>
      <summary className="mono-label" style={{ cursor: 'pointer' }}>
        Raw payload
      </summary>
      <pre className="code-block" style={{ maxHeight: 320, marginTop: 8 }}>
        {JSON.stringify(payload, null, 2)}
      </pre>
    </details>
  )
}

function DivergenceVerdict({ finding }: { finding: Finding }) {
  const p = finding.payload
  const verdict = typeof p.verdict === 'string' ? p.verdict : 'unknown'
  const reason = typeof p.reason_code === 'string' ? p.reason_code : null
  const confidence = typeof p.confidence === 'string' ? p.confidence : null
  const note = typeof p.methodology_note === 'string' ? p.methodology_note : null
  const cited = Array.isArray(p.cited_values) ? (p.cited_values as CitedValue[]) : []
  const doctrine = Array.isArray(p.doctrine_citations) ? (p.doctrine_citations as unknown[]) : []

  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
        <span className={`badge ${verdictBadgeClass(verdict)}`}>{humanize(verdict)}</span>
        {reason && <span className="chip">{humanize(reason)}</span>}
        {confidence && <span className="mono-body">confidence: {confidence}</span>}
      </div>

      {note && (
        <p className="mono-body" style={{ margin: 0, lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
          {note}
        </p>
      )}

      {cited.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Cited values ({cited.length})
          </div>
          <table className="mono-table">
            <thead>
              <tr>
                <th>Dataset</th>
                <th>Row</th>
                <th>Column</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {cited.map((c, i) => (
                <tr key={i}>
                  <td>{display(c.dataset)}</td>
                  <td>{display(c.row_ref)}</td>
                  <td>{display(c.column)}</td>
                  <td>{display(c.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {doctrine.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Doctrine cited
          </div>
          <ul className="mono-body" style={{ margin: 0, paddingLeft: 18 }}>
            {doctrine.map((d, i) => (
              <li key={i}>{display(d)}</li>
            ))}
          </ul>
        </div>
      )}

      <Supersession finding={finding} />
      <RawPayload payload={p} />
    </div>
  )
}

function GenericPayload({ finding }: { finding: Finding }) {
  const entries = Object.entries(finding.payload)
  const scalars = entries.filter(([, v]) => isScalar(v))
  const nested = entries.filter(([, v]) => !isScalar(v))
  return (
    <div className="stack" style={{ gap: 12 }}>
      {scalars.length > 0 && (
        <table className="mono-table">
          <tbody>
            {scalars.map(([k, v]) => (
              <tr key={k}>
                <td style={{ whiteSpace: 'nowrap', color: 'var(--text-muted)' }}>{k}</td>
                <td style={{ whiteSpace: 'pre-wrap' }}>{display(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {nested.map(([k, v]) => (
        <details key={k}>
          <summary className="mono-body" style={{ cursor: 'pointer' }}>
            {k} {Array.isArray(v) ? `(${v.length})` : ''}
          </summary>
          <pre className="code-block" style={{ maxHeight: 240, marginTop: 6 }}>
            {JSON.stringify(v, null, 2)}
          </pre>
        </details>
      ))}
      <Supersession finding={finding} />
      <RawPayload payload={finding.payload} />
    </div>
  )
}

export function VerdictCard({ finding }: { finding: Finding }) {
  return finding.schema_slug === 'divergence_verdict' ? (
    <DivergenceVerdict finding={finding} />
  ) : (
    <GenericPayload finding={finding} />
  )
}
