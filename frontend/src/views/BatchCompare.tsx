import { useQuery } from '@tanstack/react-query'
import { type CSSProperties, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import {
  api,
  type BatchComparison,
  type BatchComparisonRow,
  type PairwiseRow,
  type PairwiseStatus,
} from '../api/client'
import { PairwisePanel } from '../components/batches/PairwisePanel'
import { Scorecard } from '../components/batches/Scorecard'
import {
  DISPUTED_GAP_TITLE,
  DISPUTED_PRIMARY_TITLE,
  dash,
  deltaClass,
  fmtMeanInterval,
  fmtScore,
  fmtSigned,
} from '../components/batches/evalHelpers'
import { errText } from '../components/batches/shared'
import { formatCost } from '../components/shared/format'
import { QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

function Side({
  side,
  disputed,
  disputedPrimary,
}: {
  side: BatchComparisonRow['a']
  disputed?: boolean
  disputedPrimary?: boolean
}) {
  if (!side) return <td colSpan={4} className="fine-print">not in this batch</td>
  const pages =
    side.retained_pages != null
      ? side.source_pages != null
        ? `${side.retained_pages}/${side.source_pages}`
        : String(side.retained_pages)
      : '—'
  return (
    <>
      <td>
        <StatusBadge status={side.status} />
        {disputed && (
          <span className="chip" style={{ marginLeft: 4 }} title={DISPUTED_GAP_TITLE}>
            gap disputed
          </span>
        )}
        {disputedPrimary && (
          <span className="chip" style={{ marginLeft: 4 }} title={DISPUTED_PRIMARY_TITLE}>
            main document disputed
          </span>
        )}
      </td>
      <td className="num">{fmtScore(side.quality)}</td>
      <td className="num">{pages}</td>
      <td className="num">{dash(side.cost_usd, (n) => formatCost(n))}</td>
    </>
  )
}

const VERDICT_TEXT: Record<string, string> = { a: 'A', b: 'B', tie: 'tie', inconsistent: 'inconsistent' }

/** Pairwise verdict chip: "lean" is lighter, stale is greyed. Rationales in the title and an expander. */
function PairwiseCell({
  mark,
  detail,
}: {
  mark: BatchComparisonRow['pairwise']
  detail?: PairwiseRow
}) {
  if (!mark) return <span className="fine-print">—</span>
  if (!mark.verdict) {
    return <span className="fine-print">{mark.status === 'judged' ? '—' : mark.status}</span>
  }
  const text = VERDICT_TEXT[mark.verdict] ?? mark.verdict
  const rationales = (detail?.calls ?? [])
    .map((c) => `${c.order === 'a_first' ? 'A shown first' : 'B shown first'}: ${c.rationale ?? 'no rationale'}`)
    .join('\n\n')
  const title =
    [
      mark.strength === 'lean' ? 'Lean: one order preferred it, the other called a tie.' : null,
      mark.stale ? 'Stale: a target was retried since this was judged.' : null,
      rationales || null,
    ]
      .filter(Boolean)
      .join('\n') || undefined
  const style: CSSProperties = {
    opacity: mark.stale ? 0.5 : mark.strength === 'lean' ? 0.75 : 1,
    fontStyle: mark.strength === 'lean' ? 'italic' : undefined,
    textDecoration: mark.stale ? 'line-through' : undefined,
  }
  return (
    <div>
      <span className="chip" style={style} title={title}>
        {text}
        {mark.strength === 'lean' ? ' (lean)' : ''}
        {mark.stale ? ' (stale)' : ''}
      </span>
      {detail && detail.calls.length > 0 && (
        <details className="fine-print" style={{ marginTop: 2 }}>
          <summary>why</summary>
          {detail.calls.map((c) => (
            <div key={c.order} style={{ maxWidth: 360, marginTop: 4 }}>
              <strong>{c.order === 'a_first' ? 'A shown first' : 'B shown first'}</strong>
              {c.confidence ? ` (${c.confidence} confidence)` : ''}: {c.rationale ?? 'no rationale'}
            </div>
          ))}
        </details>
      )}
    </div>
  )
}

function SummaryStrip({ cmp }: { cmp: BatchComparison }) {
  const s = cmp.summary ?? ({} as BatchComparison['summary'])
  const disputed = (s.disputed_gaps_a ?? 0) + (s.disputed_gaps_b ?? 0)
  const disputedMain = (s.disputed_primaries_a ?? 0) + (s.disputed_primaries_b ?? 0)
  const pairs = cmp.rows.filter((r) => r.delta?.quality != null).length
  return (
    <div className="panel" style={{ margin: '14px 0' }}>
      <div className="mono-label">Summary</div>
      <div className="mono-body" style={{ marginTop: 4 }}>
        {cmp.shared} shared rows · {cmp.only_a} only in A · {cmp.only_b} only in B
        {!cmp.same_family && ' · different families'}
      </div>
      <div className="mono-body">
        Over shared rows: cost A {dash(s.cost_a, (n) => formatCost(n))} vs B{' '}
        {dash(s.cost_b, (n) => formatCost(n))} · energy A{' '}
        {dash(s.energy_a, (n) => `${n.toFixed(0)} Wh`)} vs B{' '}
        {dash(s.energy_b, (n) => `${n.toFixed(0)} Wh`)} · acceptable A {s.acceptable_a ?? '—'} vs B{' '}
        {s.acceptable_b ?? '—'}
      </div>
      {cmp.quality_comparable ? (
        <div className="mono-body">
          Quality: A wins {s.wins_a ?? '—'}, ties {s.ties ?? '—'}, B wins {s.wins_b ?? '—'} · mean
          delta (B − A){' '}
          <strong>{fmtMeanInterval(s.mean_quality_delta, s.interval90)}</strong>{' '}
          <span className="fine-print">
            n={pairs}
            {s.interval90 ? ', 90% interval' : ', too few pairs for an interval'}
          </span>
        </div>
      ) : (
        <div className="fine-print">
          Quality is not compared{cmp.quality_note ? `: ${cmp.quality_note}` : '.'}
        </div>
      )}
      {s.delivered && (
        <div style={{ marginTop: 6 }}>
          {s.delivered.n === 0 ? (
            <div className="mono-body">No company was delivered by both batches.</div>
          ) : cmp.quality_comparable && s.delivered.mean_quality_delta != null ? (
            <div className="mono-body">
              Where both delivered ({s.delivered.n}): A wins {s.delivered.wins_a ?? '—'}, ties{' '}
              {s.delivered.ties ?? '—'}, B wins {s.delivered.wins_b ?? '—'} · mean delta (B − A){' '}
              <strong>
                {fmtMeanInterval(s.delivered.mean_quality_delta, s.delivered.interval90)}
              </strong>{' '}
              <span className="fine-print">
                {s.delivered.interval90 ? '90% interval' : 'too few pairs for an interval'}
              </span>
            </div>
          ) : (
            <div className="mono-body">Where both delivered ({s.delivered.n}): not comparable</div>
          )}
          <div className="fine-print">
            The line above counts failed and gap runs with their scores; this one compares delivered
            packages only.
          </div>
        </div>
      )}
      {disputed > 0 && (
        <div className="mono-body" style={{ marginTop: 6 }} title={DISPUTED_GAP_TITLE}>
          Disputed gaps: A {s.disputed_gaps_a ?? 0} · B {s.disputed_gaps_b ?? 0}
        </div>
      )}
      {disputedMain > 0 && (
        <div className="mono-body" style={{ marginTop: 6 }} title={DISPUTED_PRIMARY_TITLE}>
          Rows with a disputed main document: A {s.disputed_primaries_a ?? 0} · B {s.disputed_primaries_b ?? 0}
        </div>
      )}
    </div>
  )
}

export function BatchCompare() {
  const [params] = useSearchParams()
  const a = params.get('a')
  const b = params.get('b')

  const query = useQuery<BatchComparison>({
    queryKey: ['batch-compare', a, b],
    queryFn: () => api.compareBatches(a as string, b as string),
    enabled: !!a && !!b,
    retry: false,
  })
  const cmp = query.data
  const [onlyBoth, setOnlyBoth] = useState(false)
  // An older server sends no `both_delivered`: no filter then, or it would empty the table.
  const canFilterBoth = cmp?.summary.delivered != null
  const hasPairwise = cmp?.summary?.pairwise != null || cmp?.rows.some((r) => r.pairwise != null)
  // Rationales come from the status endpoint; the panel owns the polling, this only reads the cache.
  const pairwiseDetail = useQuery<PairwiseStatus>({
    queryKey: ['batch-pairwise', a, b],
    queryFn: () => api.getPairwise(a as string, b as string),
    enabled: !!a && !!b && !!cmp?.quality_comparable && hasPairwise,
    retry: false,
  })
  const detailByRow = new Map((pairwiseDetail.data?.rows ?? []).map((r) => [r.row_key, r]))
  const showPairwiseColumn = !!cmp?.quality_comparable && cmp.rows.some((r) => r.pairwise != null)
  const rows = cmp ? (onlyBoth && canFilterBoth ? cmp.rows.filter((r) => r.both_delivered) : cmp.rows) : []

  return (
    <div>
      <Link to="/batches" className="fine-print">
        ← Batches
      </Link>
      <h1 className="view-title" style={{ marginTop: 6 }}>
        Compare batches
      </h1>
      {!a || !b ? (
        <div className="empty">Pick two batches from the list to compare.</div>
      ) : query.isLoading ? (
        <div className="empty pulse">Comparing…</div>
      ) : query.error ? (
        <>
          <QueryError error={query.error} what="the comparison" />
          <span className="error-text">{errText(query.error)}</span>
        </>
      ) : cmp ? (
        <>
          <div className="row" style={{ gap: 14, alignItems: 'flex-start', flexWrap: 'wrap' }}>
            <div style={{ flex: '1 1 380px', minWidth: 0 }}>
              <div className="mono-label">A · <Link to={`/batches/${a}`}>{cmp.a.name}</Link></div>
              <Scorecard batchId={a} compact />
            </div>
            <div style={{ flex: '1 1 380px', minWidth: 0 }}>
              <div className="mono-label">B · <Link to={`/batches/${b}`}>{cmp.b.name}</Link></div>
              <Scorecard batchId={b} compact />
            </div>
          </div>

          <SummaryStrip cmp={cmp} />

          {cmp.quality_comparable && cmp.summary?.pairwise != null && (
            <PairwisePanel
              a={a}
              b={b}
              summary={cmp.summary.pairwise}
            />
          )}

          {canFilterBoth && (
            <label className="check-row" style={{ marginBottom: 8 }}>
              <input
                type="checkbox"
                checked={onlyBoth}
                onChange={(e) => setOnlyBoth(e.target.checked)}
              />
              <span>Only companies both delivered</span>
            </label>
          )}

          <div style={{ overflowX: 'auto' }}>
            <table className="mono-table">
              <thead>
                <tr>
                  <th rowSpan={2}>Row</th>
                  <th colSpan={4}>A</th>
                  <th colSpan={4}>B</th>
                  <th colSpan={3}>Δ (B − A)</th>
                  {showPairwiseColumn && <th rowSpan={2}>Pairwise</th>}
                </tr>
                <tr>
                  <th>Status</th>
                  <th className="num">Score</th>
                  <th className="num">Pages</th>
                  <th className="num">Cost</th>
                  <th>Status</th>
                  <th className="num">Score</th>
                  <th className="num">Pages</th>
                  <th className="num">Cost</th>
                  <th className="num">Score</th>
                  <th className="num">Cost</th>
                  <th className="num">Pages</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.row_key}>
                    <td>
                      {r.label}
                      <div className="fine-print">{r.row_key}</div>
                    </td>
                    <Side
                      side={r.a}
                      disputed={r.disputed_gap === 'a'}
                      disputedPrimary={r.disputed_primary === 'a' || r.disputed_primary === 'both'}
                    />
                    <Side
                      side={r.b}
                      disputed={r.disputed_gap === 'b'}
                      disputedPrimary={r.disputed_primary === 'b' || r.disputed_primary === 'both'}
                    />
                    <td className={`num ${deltaClass(r.delta?.quality)}`}>
                      {fmtSigned(r.delta?.quality)}
                    </td>
                    <td className={`num ${deltaClass(r.delta?.cost_usd, true)}`}>
                      {dash(r.delta?.cost_usd, (n) => `${n < 0 ? '−' : '+'}$${Math.abs(n).toFixed(2)}`)}
                    </td>
                    <td className="num">{fmtSigned(r.delta?.retained_pages, 0)}</td>
                    {showPairwiseColumn && (
                      <td>
                        <PairwiseCell mark={r.pairwise} detail={detailByRow.get(r.row_key)} />
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </div>
  )
}
