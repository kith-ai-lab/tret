/** The billing section's per-period footprint: a month picker over
 *  `GET /api/billing/footprint`, the same `TotalsStrip` the Emissions page
 *  renders (same totals shape, same basis-mixed rule — carbon nulls out and
 *  `carbon_is_summable` goes false rather than summing two GHG Protocol
 *  bases into a meaningless figure), the credits the period consumed, and the
 *  recorded basis as a plain sentence.
 *
 *  404s when tret-cloud is not loaded — hidden entirely, same capability-gate
 *  rule as `BillingSection` and `EmissionsHistory`. Never call the avoided
 *  figure an offset: it is a same-token counterfactual, and the band around it
 *  is a judgment band, not a confidence interval.
 */
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { api, ApiError, type BillingFootprint, type EmissionsByBasis } from '../../api/client'
import {
  gridBasisLabel,
  gridBasisWhat,
  NOT_SUMMABLE_WHY,
  SUMMABLE_ACROSS_BASES_NOTE,
} from '../shared/emissions'
import { NO_ESTIMATE, formatCo2eWithBand, formatCostScaled, formatEnergyScaled, formatTokens, orDash } from '../shared/format'
import { TotalsStrip } from '../../views/Emissions'

/** The current calendar month plus the 12 before it, newest first — as far
 *  back as the backend contract promises footprint data. */
function monthOptions(): { value: string; label: string }[] {
  const now = new Date()
  const opts: { value: string; label: string }[] = []
  for (let i = 0; i < 13; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1)
    const value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    opts.push({ value, label: d.toLocaleString('en-US', { month: 'long', year: 'numeric' }) })
  }
  return opts
}

function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

/** The bases behind a mixed period, in words — the same join the Emissions
 *  page's own (unexported) `basisList` uses, kept local since that helper
 *  isn't exported and this is three lines over `gridBasisLabel`. */
function basisList(bases: (string | null)[]): string {
  const labels = bases.map(gridBasisLabel)
  if (labels.length <= 1) return labels[0] ?? NO_ESTIMATE
  return `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`
}

function BasisRows({ rows }: { rows: EmissionsByBasis[] }) {
  return (
    <div className="md-table-wrap">
      <table className="mono-table">
        <thead>
          <tr>
            <th>Basis</th>
            <th className="num">Runs</th>
            <th className="num">Energy (est.)</th>
            <th className="num">CO₂e (est.)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.basis ?? 'unrecorded'}>
              <td style={{ whiteSpace: 'nowrap' }} title={gridBasisWhat(row.basis)}>
                {gridBasisLabel(row.basis)}
              </td>
              <td className="num">{formatTokens(row.runs)}</td>
              <td className="num">{orDash(formatEnergyScaled(row.energy_wh))}</td>
              <td className="num">{formatCo2eWithBand(row.co2e_g, row.co2e_g_low, row.co2e_g_high)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FootprintBody({ data }: { data: BillingFootprint }) {
  const summable = data.totals.carbon_is_summable !== false
  const mixedBases = data.by_basis && data.by_basis.length > 1

  return (
    <div className="stack" style={{ gap: 10 }}>
      <TotalsStrip totals={data.totals} />

      <div className="config-stats">
        <div className="config-stat">
          <div className="mono-label">Credits consumed</div>
          <div className="mono-body">{formatCostScaled(data.credits_usd_consumed)}</div>
        </div>
      </div>

      <div className="fine-print" title={data.disclaimer}>
        {data.disclaimer}
      </div>

      {/* A mixed window has no single recorded basis to name here — the
          per-basis breakdown below already says which bases it spans, so
          this sentence (and its "no basis was recorded" fallback, which
          would otherwise be wrong: these rows plainly do carry bases) is
          only meaningful for a uniform window. */}
      {!mixedBases && (
        <div className="fine-print" title={gridBasisWhat(data.basis)}>
          Recorded under the {gridBasisLabel(data.basis)} basis. {gridBasisWhat(data.basis)}
        </div>
      )}

      {!summable && mixedBases && data.by_basis && (
        <>
          <div className="fine-print">
            Carbon is not totalled above: this period spans {basisList(data.by_basis.map((r) => r.basis))}{' '}
            grid factors. {NOT_SUMMABLE_WHY} {SUMMABLE_ACROSS_BASES_NOTE}
          </div>
          <BasisRows rows={data.by_basis} />
        </>
      )}
    </div>
  )
}

export function FootprintCard() {
  const [period, setPeriod] = useState(currentPeriod)
  const footprintQuery = useQuery({
    queryKey: ['billing-footprint', period],
    queryFn: () => api.getBillingFootprint(period),
    retry: false,
  })

  const error = footprintQuery.error as ApiError | null
  // Capability gate: tret-cloud isn't loaded on this deployment. Nothing to
  // show at all — same rule every other tret-cloud surface follows.
  if (error && error.status === 404) return null
  // Still loading (the first fetch, before either branch above can have
  // fired yet): say nothing rather than flash the header and month picker
  // for an instant on every OSS deployment, which never has anything to
  // show here once the 404 above lands — same rule EmissionsHistory follows.
  if (footprintQuery.isLoading) return null

  const options = monthOptions()

  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8, alignItems: 'baseline' }}>
        <div className="mono-label">Footprint</div>
        <select
          aria-label="Footprint period"
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          style={{ width: 200 }}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>

      {error ? (
        <div className="error-text">
          {error.status === 422 ? 'That period could not be read.' : error.message}
        </div>
      ) : footprintQuery.data ? (
        <FootprintBody data={footprintQuery.data} />
      ) : (
        <div className="empty pulse">Loading footprint…</div>
      )}
    </div>
  )
}
