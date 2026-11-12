import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api, ApiError, type BatchAdvice, type BatchAdviceBody } from '../../api/client'
import { formatCost } from '../shared/format'
import { dash, fmtScore } from './evalHelpers'

/** Debounce a request key so typing in the template does not fire a request per keystroke. */
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** Fetches advice for the family the batch would belong to. `body` is null until the family
 *  is known (disclosure: always; prompt: harness + non-empty template). Keyed only on the
 *  family inputs, so unrelated form edits never refetch. Advice never blocks creating a batch. */
export function useBatchAdvice(enabled: boolean, body: BatchAdviceBody | null) {
  const key = body ? JSON.stringify(body) : ''
  const debounced = useDebounced(key, 500)
  const settled = debounced === key
  const query = useQuery<BatchAdvice>({
    queryKey: ['batch-advice', debounced],
    queryFn: () => api.getBatchAdvice(JSON.parse(debounced) as BatchAdviceBody),
    enabled: enabled && debounced !== '',
    retry: false,
    staleTime: 60_000,
  })
  // An older server has no advice endpoint: hide the panel rather than show an error.
  const unsupported =
    query.error instanceof ApiError && (query.error.status === 404 || query.error.status === 405)
  return { query, pending: enabled && key !== '' && (!settled || query.isFetching), unsupported }
}

export interface AdviceValues {
  model: string
  maxIterations: string
  costCap: string
  concurrency: number
}

export interface AdviceSetters {
  setModel: (v: string) => void
  setMaxIterations: (v: string) => void
  setCostCap: (v: string) => void
  setConcurrency: (v: number) => void
}

interface Suggestion {
  id: 'model' | 'max_iterations' | 'cost_cap' | 'concurrency'
  label: string
  value: string
  basis: string | null
  applied: boolean
  apply: () => void
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n))
}

/** Concurrency input range of the New batch form. */
const MAX_CONCURRENCY = 6

function suggestionsFor(
  advice: BatchAdvice,
  cur: AdviceValues,
  set: AdviceSetters,
): Suggestion[] {
  const r = advice.recommendation
  if (!r) return []
  const out: Suggestion[] = []
  if (r.model) {
    const model = r.model
    out.push({
      id: 'model',
      label: 'Model override',
      value: model,
      basis: r.reason,
      applied: cur.model.trim() === model,
      apply: () => set.setModel(model),
    })
  }
  // No model recommended yet: only concurrency can be applied.
  if (r.model && r.max_iterations != null) {
    const n = clamp(Math.round(r.max_iterations), 1, 50)
    out.push({
      id: 'max_iterations',
      label: 'Max iterations',
      value: String(n),
      basis: r.max_iterations_basis ?? null,
      applied: cur.maxIterations.trim() === String(n),
      apply: () => set.setMaxIterations(String(n)),
    })
  }
  if (r.model && r.cost_cap_usd != null) {
    const capped = Math.max(0.01, r.cost_cap_usd)
    const v = String(capped)
    out.push({
      id: 'cost_cap',
      label: 'Cost cap per target',
      value: `$${capped.toFixed(2)}`,
      basis: r.cost_cap_basis ?? null,
      applied: cur.costCap.trim() !== '' && Number(cur.costCap) === capped,
      apply: () => set.setCostCap(v),
    })
  }
  if (r.concurrency != null) {
    const n = clamp(Math.round(r.concurrency), 1, MAX_CONCURRENCY)
    out.push({
      id: 'concurrency',
      label: 'Concurrency',
      value: String(n),
      basis: r.concurrency_reason ?? null,
      applied: cur.concurrency === n,
      apply: () => set.setConcurrency(n),
    })
  }
  return out
}

/** Per-model figures from the family's comparable set, the recommended model highlighted. */
function ModelTable({ advice }: { advice: BatchAdvice }) {
  const models = advice.evidence?.models ?? []
  if (models.length === 0) return null
  const rec = advice.recommendation?.model ?? null
  return (
    <div style={{ overflowX: 'auto', margin: '8px 0' }}>
      <table className="mono-table">
        <thead>
          <tr>
            <th>Model</th>
            <th className="num">Rows scored</th>
            <th className="num">Quality (90% interval)</th>
            <th className="num">Residual</th>
            <th className="num">$ / acceptable</th>
            <th className="num">Wh / acceptable</th>
            <th>Pareto</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => (
            <tr key={m.model} style={m.model === rec ? { fontWeight: 600 } : undefined}>
              <td>
                {m.model}
                {m.model === rec && (
                  <span className="chip" style={{ marginLeft: 6 }}>
                    recommended
                  </span>
                )}
              </td>
              <td className="num">{m.scored}</td>
              <td className="num">
                {fmtScore(m.quality_mean)}
                {m.quality_interval90 ? (
                  <span className="fine-print">
                    {' '}
                    ({m.quality_interval90[0].toFixed(1)} to {m.quality_interval90[1].toFixed(1)})
                  </span>
                ) : null}
                {m.batches > 1 && m.latest_batch && m.latest_batch.quality_mean !== null && (
                  <div className="fine-print">
                    latest batch {fmtScore(m.latest_batch.quality_mean)} (n={m.latest_batch.scored})
                  </div>
                )}
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
  )
}

/** "Advice from earlier batches". Read-only until the person clicks Apply: an applied value is
 *  written into the matching input, which stays editable. */
export function AdvicePanel({
  enabled,
  body,
  values,
  setters,
}: {
  enabled: boolean
  body: BatchAdviceBody | null
  values: AdviceValues
  setters: AdviceSetters
}) {
  const { query, pending, unsupported } = useBatchAdvice(enabled, body)
  if (!enabled || body === null || unsupported) return null
  const advice = query.data
  const suggestions = advice ? suggestionsFor(advice, values, setters) : []
  const todo = suggestions.filter((s) => !s.applied)

  return (
    <div className="panel" style={{ margin: '12px 0 14px' }} aria-label="Advice from earlier batches">
      <div className="row" style={{ gap: 8, alignItems: 'center', justifyContent: 'space-between' }}>
        <div className="mono-label">Advice from earlier batches</div>
        {suggestions.length > 1 && (
          <button
            type="button"
            className="btn btn-sm"
            disabled={todo.length === 0}
            onClick={() => todo.forEach((s) => s.apply())}
          >
            Apply all
          </button>
        )}
      </div>
      {pending && !advice ? (
        <div className="fine-print pulse" style={{ marginTop: 4 }}>
          Looking at earlier batches of this kind…
        </div>
      ) : query.error ? (
        <div className="fine-print" style={{ marginTop: 4 }}>
          Advice is not available right now. You can still create the batch.
        </div>
      ) : advice && advice.recommendation === null ? (
        <>
          <div className="fine-print" style={{ marginTop: 4 }}>
            No evidence yet: nothing to recommend until earlier batches of this kind have been scored.
          </div>
          {(advice.notes ?? []).map((n) => (
            <div key={n} className="fine-print" style={{ marginTop: 2 }}>
              {n}
            </div>
          ))}
        </>
      ) : advice ? (
        <>
          <div className="fine-print" style={{ marginTop: 4 }}>
            {advice.family_label} · objective {advice.objective} · {advice.evidence?.batches ?? 0}{' '}
            earlier {(advice.evidence?.batches ?? 0) === 1 ? 'batch' : 'batches'}
            {advice.evidence?.comparable
              ? ` · compared by rubric ${advice.evidence.comparable.rubric_key} ${advice.evidence.comparable.rubric_version}, judge ${advice.evidence.comparable.judge_model}`
              : ''}
          </div>
          <ModelTable advice={advice} />
          {advice.recommendation && (
            <>
              {advice.recommendation.model === null && (
                <div className="fine-print" style={{ margin: '4px 0' }}>
                  {advice.recommendation.reason}
                </div>
              )}
              {suggestions.map((s) => (
                <div
                  key={s.id}
                  className="row"
                  style={{ gap: 10, alignItems: 'flex-start', margin: '6px 0', flexWrap: 'wrap' }}
                >
                  <div style={{ flex: '1 1 320px' }}>
                    <div className="mono-body">
                      {s.label}: <strong>{s.value}</strong>
                    </div>
                    {s.basis && <div className="fine-print">{s.basis}</div>}
                  </div>
                  <button type="button" className="btn btn-sm" disabled={s.applied} onClick={s.apply}>
                    {s.applied ? 'Applied' : 'Apply'}
                  </button>
                </div>
              ))}
            </>
          )}
          {(advice.notes ?? []).map((n) => (
            <div key={n} className="fine-print" style={{ marginTop: 2 }}>
              {n}
            </div>
          ))}
          <div className="fine-print" style={{ marginTop: 4 }}>
            Suggestions only: nothing changes until you apply it, and every field stays editable.
          </div>
        </>
      ) : null}
    </div>
  )
}

/** Optional per-target cost cap input. `value` is the raw text ('' = none). */
export function CostCapField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="field">
      <label className="mono-label">Cost cap per target (USD, blank = harness)</label>
      <input
        type="number"
        min={0.01}
        max={1000}
        step="0.01"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  )
}

/** Parse the cost cap text: null when blank, undefined when present but not usable. */
export function parseCostCap(raw: string): number | null | undefined {
  if (raw.trim() === '') return null
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 && n <= 1000 ? n : undefined
}

export function RowMemoryField({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <label className="check-row">
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        <span>Use notes from earlier attempts (row memory)</span>
      </label>
      <span className="fine-print">
        Adds a short block to each target&rsquo;s task: where earlier runs of this list found, or failed to find,
        the same target. Leads to verify, not instructions. Off by default.
      </span>
    </div>
  )
}
