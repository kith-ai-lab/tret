import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import { api, type BatchComparison, type BatchComparisonRow } from '../api/client'
import { Scorecard } from '../components/batches/Scorecard'
import {
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

function Side({ side }: { side: BatchComparisonRow['a'] }) {
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
      </td>
      <td className="num">{fmtScore(side.quality)}</td>
      <td className="num">{pages}</td>
      <td className="num">{dash(side.cost_usd, (n) => formatCost(n))}</td>
    </>
  )
}

function SummaryStrip({ cmp }: { cmp: BatchComparison }) {
  const s = cmp.summary ?? ({} as BatchComparison['summary'])
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

          <div style={{ overflowX: 'auto' }}>
            <table className="mono-table">
              <thead>
                <tr>
                  <th rowSpan={2}>Row</th>
                  <th colSpan={4}>A</th>
                  <th colSpan={4}>B</th>
                  <th colSpan={3}>Δ (B − A)</th>
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
                {cmp.rows.map((r) => (
                  <tr key={r.row_key}>
                    <td>
                      {r.label}
                      <div className="fine-print">{r.row_key}</div>
                    </td>
                    <Side side={r.a} />
                    <Side side={r.b} />
                    <td className={`num ${deltaClass(r.delta?.quality)}`}>
                      {fmtSigned(r.delta?.quality)}
                    </td>
                    <td className={`num ${deltaClass(r.delta?.cost_usd, true)}`}>
                      {dash(r.delta?.cost_usd, (n) => `${n < 0 ? '−' : '+'}$${Math.abs(n).toFixed(2)}`)}
                    </td>
                    <td className="num">{fmtSigned(r.delta?.retained_pages, 0)}</td>
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
