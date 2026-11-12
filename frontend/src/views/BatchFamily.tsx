import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import {
  api,
  type FamilyBatchRow,
  type FamilyLeaderboard,
  type FamilyModel,
  type FamilyModelsBlock,
} from '../api/client'
import { FamilyScatter } from '../components/batches/FamilyScatter'
import { ROUTER_GATE_TOOLTIP, dash, fmtDuration, fmtPct, fmtScore, routerGateLine } from '../components/batches/evalHelpers'
import { errText } from '../components/batches/shared'
import { formatCost, formatDateTime } from '../components/shared/format'
import { QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

type SortKey =
  | 'created_at'
  | 'name'
  | 'targets'
  | 'yield'
  | 'quality_mean'
  | 'mean_residual'
  | 'cost_usd'
  | 'cost_per_acceptable_usd'
  | 'energy_per_acceptable_wh'
  | 'wall_seconds'

const COLUMNS: { key: SortKey; label: string; num?: boolean }[] = [
  { key: 'created_at', label: 'Created' },
  { key: 'name', label: 'Batch' },
  { key: 'targets', label: 'Targets', num: true },
  { key: 'yield', label: 'Yield', num: true },
  { key: 'quality_mean', label: 'Quality', num: true },
  { key: 'mean_residual', label: 'Residual', num: true },
  { key: 'cost_usd', label: 'Cost', num: true },
  { key: 'cost_per_acceptable_usd', label: '$ / acceptable', num: true },
  { key: 'energy_per_acceptable_wh', label: 'Wh / acceptable', num: true },
  { key: 'wall_seconds', label: 'Wall time', num: true },
]

function modelsBlock(lb: FamilyLeaderboard): FamilyModelsBlock {
  const m = lb.models
  // Flat form is primary: top-level excluded_batches, models is the list.
  if (Array.isArray(m)) {
    return { comparable: lb.comparable ?? null, models: m, excluded_batches: lb.excluded_batches ?? 0 }
  }
  return {
    comparable: m?.comparable ?? lb.comparable ?? null,
    models: m?.models ?? [],
    excluded_batches: lb.excluded_batches ?? m?.excluded_batches ?? 0,
  }
}

function sortValue(b: FamilyBatchRow, key: SortKey): number | string | null {
  const v = b[key]
  if (key === 'created_at') return v ? Date.parse(v as string) : null
  return v as number | string | null
}

export function BatchFamily() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const key = params.get('key')

  const query = useQuery<FamilyLeaderboard>({
    queryKey: ['batch-family', key],
    queryFn: () => api.getBatchFamily(key as string),
    enabled: !!key,
    retry: false,
  })
  const lb = query.data

  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'created_at', dir: 'desc' })
  const [picked, setPicked] = useState<string[]>([])

  const batches = useMemo(() => {
    const list = [...(lb?.batches ?? [])]
    const sign = sort.dir === 'asc' ? 1 : -1
    list.sort((x, y) => {
      const vx = sortValue(x, sort.key)
      const vy = sortValue(y, sort.key)
      if (vx == null && vy == null) return 0
      if (vx == null) return 1 // empty values always last
      if (vy == null) return -1
      if (typeof vx === 'string' || typeof vy === 'string') return sign * String(vx).localeCompare(String(vy))
      return sign * (vx - vy)
    })
    return list
  }, [lb, sort])

  const toggle = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length >= 2 ? [p[1], id] : [...p, id]))

  const block = lb ? modelsBlock(lb) : null
  const models: FamilyModel[] = block?.models ?? []
  const cs = block?.comparable ?? lb?.comparable ?? null

  return (
    <div>
      <Link to="/batches" className="fine-print">
        ← Batches
      </Link>
      <h1 className="view-title" style={{ marginTop: 6 }}>
        {lb?.family_label ?? 'Batch family'}
      </h1>
      {!key ? (
        <div className="empty">Open a family from a batch scorecard or the batch list.</div>
      ) : query.isLoading ? (
        <div className="empty pulse">Loading family…</div>
      ) : query.error ? (
        <>
          <QueryError error={query.error} what="the family" />
          <span className="error-text">{errText(query.error)}</span>
        </>
      ) : lb ? (
        <>
          <div className="view-sub">
            {lb.profile} · {lb.batches.length} {lb.batches.length === 1 ? 'batch' : 'batches'}
            {lb.row_keys ? ` · ${lb.row_keys} rows` : ''}
          </div>
          {routerGateLine(lb.router_gate) && (
            <div className="fine-print" style={{ marginTop: 4 }} title={ROUTER_GATE_TOOLTIP}>
              {routerGateLine(lb.router_gate)}
            </div>
          )}
          <div className="panel" style={{ margin: '14px 0' }}>
            <div className="mono-label">Comparable set</div>
            {cs ? (
              <div className="mono-body" style={{ marginTop: 4 }}>
                Quality is compared only across batches scored with rubric {cs.rubric_key} version{' '}
                {cs.rubric_version} by judge {cs.judge_model}.
              </div>
            ) : (
              <div className="fine-print" style={{ marginTop: 4 }}>
                No evaluated batch yet, so quality is not comparable across this family.
              </div>
            )}
            {(block?.excluded_batches ?? 0) > 0 && (
              <div className="fine-print" style={{ marginTop: 4 }}>
                {block?.excluded_batches} {block?.excluded_batches === 1 ? 'batch is' : 'batches are'} left out of the
                per-model table because {block?.excluded_batches === 1 ? 'its' : 'their'} rubric version, judge or
                evaluation state does not match.
              </div>
            )}
          </div>

          <div className="row" style={{ gap: 8, marginBottom: 8, alignItems: 'center' }}>
            <button
              className="btn"
              disabled={picked.length !== 2}
              title={picked.length === 2 ? undefined : 'Tick exactly two batches to compare'}
              onClick={() => navigate(`/batches/compare?a=${picked[0]}&b=${picked[1]}`)}
            >
              Compare
            </button>
            <span className="fine-print">Tick two batches, then Compare. Click a column header to sort.</span>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="mono-table">
              <thead>
                <tr>
                  <th aria-label="Select to compare" />
                  {COLUMNS.map((c) => (
                    <th
                      key={c.key}
                      className={c.num ? 'num' : undefined}
                      aria-sort={sort.key === c.key ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
                    >
                      <button
                        type="button"
                        className="btn btn-sm"
                        style={{ border: 'none', background: 'none', padding: 0, font: 'inherit', color: 'inherit' }}
                        onClick={() =>
                          setSort((s) =>
                            s.key === c.key
                              ? { key: c.key, dir: s.dir === 'asc' ? 'desc' : 'asc' }
                              : { key: c.key, dir: c.key === 'name' ? 'asc' : 'desc' },
                          )
                        }
                      >
                        {c.label}
                        {sort.key === c.key ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : ''}
                      </button>
                    </th>
                  ))}
                  <th>Models</th>
                </tr>
              </thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.batch_id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Compare ${b.name}`}
                        checked={picked.includes(b.batch_id)}
                        onChange={() => toggle(b.batch_id)}
                      />
                    </td>
                    <td>{b.created_at ? formatDateTime(b.created_at) : '—'}</td>
                    <td>
                      <Link to={`/batches/${b.batch_id}`}>{b.name}</Link>{' '}
                      <StatusBadge status={b.status} />
                      {!b.comparable && (
                        <span
                          className="chip"
                          style={{ marginLeft: 4 }}
                          title="Not scored with this family's comparable rubric and judge."
                        >
                          not comparable
                        </span>
                      )}
                    </td>
                    <td className="num">{b.targets}</td>
                    <td className="num">{fmtPct(b.yield)}</td>
                    <td className="num">{b.comparable ? fmtScore(b.quality_mean) : '—'}</td>
                    <td className="num">{b.comparable ? fmtScore(b.mean_residual) : '—'}</td>
                    <td className="num">{formatCost(b.cost_usd)}</td>
                    <td className="num">{dash(b.cost_per_acceptable_usd, (n) => formatCost(n))}</td>
                    <td className="num">{dash(b.energy_per_acceptable_wh, (n) => n.toFixed(1))}</td>
                    <td className="num">{fmtDuration(b.wall_seconds)}</td>
                    <td className="fine-print">
                      {(b.models ?? []).map((m) => `${m.model} ×${m.targets}`).join(', ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel" style={{ margin: '18px 0 14px' }}>
            <div className="mono-label">Models</div>
            {models.length === 0 ? (
              <div className="fine-print" style={{ marginTop: 4 }}>
                No per-model figures yet: they need evaluated batches in the comparable set.
              </div>
            ) : (
              <>
                <div className="fine-print" style={{ margin: '4px 0 8px' }}>
                  Filled points are on the Pareto front: no other model has both higher quality and a lower cost per
                  acceptable result (models with at least 3 scored rows). The table has the same numbers.
                </div>
                <div style={{ marginBottom: 12 }}>
                  <FamilyScatter models={models} />
                </div>
                <div style={{ overflowX: 'auto' }}>
                  <table className="mono-table">
                    <thead>
                      <tr>
                        <th>Model</th>
                        <th className="num">Targets</th>
                        <th className="num">Delivered</th>
                        <th className="num">Yield</th>
                        <th className="num">Quality (90% interval)</th>
                        <th className="num">Residual</th>
                        <th className="num">$ / acceptable</th>
                        <th className="num">Wh / acceptable</th>
                        <th>Pareto</th>
                      </tr>
                    </thead>
                    <tbody>
                      {models.map((m) => (
                        <tr key={m.model}>
                          <td>{m.model}</td>
                          <td className="num">
                            {m.targets}
                            <span className="fine-print"> in {m.batches}</span>
                          </td>
                          <td className="num">{m.delivered}</td>
                          <td className="num">{fmtPct(m.yield)}</td>
                          <td className="num">
                            {fmtScore(m.quality_mean)}
                            {m.quality_interval90 ? (
                              <span className="fine-print">
                                {' '}
                                ({m.quality_interval90[0].toFixed(1)} to {m.quality_interval90[1].toFixed(1)})
                              </span>
                            ) : m.scored > 0 ? (
                              <span className="fine-print"> (n={m.scored})</span>
                            ) : null}
                          </td>
                          <td className="num">{fmtScore(m.mean_residual)}</td>
                          <td className="num">{dash(m.cost_per_acceptable_usd, (n) => formatCost(n))}</td>
                          <td className="num">{dash(m.energy_per_acceptable_wh, (n) => n.toFixed(1))}</td>
                          <td>{m.pareto ? <span className="chip">Pareto</span> : ''}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        </>
      ) : null}
    </div>
  )
}
