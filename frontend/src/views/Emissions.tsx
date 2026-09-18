/** Emissions — the estimated carbon footprint of everything tret has run.
 *
 *  This view is a climate claim about a climate product, so it is built under a
 *  hard rule: no number appears here that the backend did not send. Shares and
 *  the max used to scale a bar are arithmetic over returned figures; there is no
 *  extrapolation, no projection, and deliberately no "equivalent to N trees"
 *  conversion. The backend's own disclaimer is rendered verbatim, and the
 *  avoided figure is framed as a same-token counterfactual — never as an offset.
 */
import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useMemo, useState } from 'react'

import {
  api,
  type EmissionsAnalytics,
  type EmissionsByBasis,
  type EmissionsByDay,
  type EmissionsByHarness,
  type EmissionsByModel,
  type EmissionsBucket,
  type EmissionsRecordedFactor,
  type EmissionsTotals,
  type GridBasis,
} from '../api/client'
import { ScopeBar } from '../components/shared/EmissionsCalc'
import { EmissionsScenarioButton } from '../components/shared/EmissionsScenario'
import { MethodologyLink } from '../components/shared/MethodologyDialog'
import {
  BAND_LABEL,
  BAND_SHORT,
  BAND_WHY,
  BASIS_SUBTOTAL_HINT,
  COUNTERFACTUAL_NOTE,
  ESTIMATE_NOTE,
  GRID_NO_INFERENCE_NOTE,
  MIXED_FACTORS_CAVEAT,
  MONEY_PCT_PRECISION_NOTE,
  MONEY_SHORT,
  NOT_SUMMABLE_CELL_HINT,
  NOT_SUMMABLE_TITLE,
  NOT_SUMMABLE_WHY,
  SCOPE_META,
  SUMMABLE_ACROSS_BASES_NOTE,
  avoidedFraming,
  avoidedMoneyFraming,
  bandFactorText,
  coarseComparison,
  gridBasisLabel,
  gridBasisWhat,
  gridSourceLabel,
  gridSourceWhat,
  moneyPctCompact,
  moneyPctPhrase,
  share,
} from '../components/shared/emissions'
import {
  NO_ESTIMATE,
  NO_ESTIMATE_HINT,
  co2eScaleFor,
  formatCo2e,
  formatCo2eAt,
  formatCo2eBand,
  formatCo2eScaled,
  formatCostScaled,
  formatEnergyScaled,
  formatFactor,
  formatTokens,
  orDash,
} from '../components/shared/format'
import { type Column, MonoTable } from '../components/shared/MonoTable'

const WINDOWS = [7, 30, 90, 365]

/** A rollup's avoided figure, or null when nothing in it had a baseline at all.
 *  A comparison that never happened is not a comparison that came out even, so it
 *  reads as "no figure" rather than as zero. Null is also what a basis-mixed
 *  bucket returns: there is no summable avoided figure across two bases. */
function bucketAvoided(b: {
  baseline_co2e_g: number | null
  avoided_co2e_g: number | null
}): number | null {
  if (b.baseline_co2e_g === null || b.avoided_co2e_g === null) return null
  return b.baseline_co2e_g === 0 && b.avoided_co2e_g === 0 ? null : b.avoided_co2e_g
}

/** The bases behind a bucket, in words: "location-based and market-based", or
 *  "location-based, market-based and not recorded" for three. */
function basisList(bases: GridBasis[]): string {
  const labels = bases.map(gridBasisLabel)
  if (labels.length <= 1) return labels[0] ?? NO_ESTIMATE
  return `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`
}

/** Why a carbon cell is empty: two different reasons that must not be conflated —
 *  nothing was recorded, or what was recorded may not be added up. */
function carbonCellHint(bucket: { carbon_is_summable: boolean }): string {
  return bucket.carbon_is_summable ? NO_ESTIMATE_HINT : NOT_SUMMABLE_CELL_HINT
}

/** Heavier class, hotter badge. An editorial cue over the backend's own class.
 *  R is the reasoning tier and is an order of magnitude above XL, not a step. */
const CLASS_BADGE: Record<string, string> = {
  S: 'badge-green',
  M: 'badge-blue',
  L: 'badge-amber',
  XL: 'badge-red',
  R: 'badge-red',
}

/** A rollup's summed band, or null when nothing in it recorded one. The backend
 *  substitutes a run's central figure at both ends when it has no band, so a
 *  window with no bands at all comes back with low == high == central; showing
 *  that as a range would fake a band nobody recorded. */
function bucketBand(b: {
  co2e_g: number | null
  co2e_g_low: number | null
  co2e_g_high: number | null
}): { low: number; high: number } | null {
  if (b.co2e_g_low === null || b.co2e_g_high === null) return null
  if (b.co2e_g_low === b.co2e_g_high) return null
  return { low: b.co2e_g_low, high: b.co2e_g_high }
}

export function Emissions() {
  const [days, setDays] = useState(30)
  const emissionsQuery = useQuery({
    queryKey: ['emissions', days],
    queryFn: () => api.emissionsAnalytics(days),
  })
  // Only to label self-hosted models; no figure is taken from the catalog.
  const modelsQuery = useQuery({ queryKey: ['models'], queryFn: api.listModels })
  const localModels = useMemo(
    () =>
      new Set(
        (modelsQuery.data ?? []).filter((m) => m.provider === 'local').map((m) => m.id),
      ),
    [modelsQuery.data],
  )

  const data = emissionsQuery.data

  return (
    <div className="stack" style={{ gap: 26 }}>
      <div>
        <h1 className="view-title">Emissions</h1>
        <div className="view-sub">
          The estimated carbon footprint of tret's own compute: energy, the GHG Protocol scope
          split, and a same-token comparison against a frontier baseline model. Every figure on this
          page is an estimate derived from token counts — none of it is metered.
        </div>
      </div>

      <div className="row" style={{ flexWrap: 'wrap' }}>
        <select
          aria-label="Reporting window"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          style={{ width: 180 }}
        >
          {WINDOWS.map((d) => (
            <option key={d} value={d}>
              last {d} days
            </option>
          ))}
        </select>
        {data && (
          <span className="mono-label">
            {formatTokens(data.totals.runs)} runs in window · {formatTokens(data.scan.rows_scanned)}{' '}
            scanned (cap {formatTokens(data.scan.limit)})
          </span>
        )}
        {data?.scan.truncated && (
          <span className="badge badge-amber" title="The window holds more runs than the scan cap, so these totals cover the most recent runs only.">
            scan truncated
          </span>
        )}
        <span style={{ marginLeft: 'auto' }}>
          <EmissionsScenarioButton projectId={data?.project_id ?? null} days={days} />
        </span>
      </div>

      {emissionsQuery.isLoading ? (
        <div className="empty pulse">Loading emissions…</div>
      ) : emissionsQuery.isError ? (
        <div className="error-text">{(emissionsQuery.error as Error).message}</div>
      ) : !data ? null : (
        <>
          <Disclaimer data={data} />

          {data.totals.runs === 0 ? (
            <EmptyWindow days={days} />
          ) : data.totals.runs_with_estimate === 0 ? (
            <NoEstimates totals={data.totals} />
          ) : (
            <>
              <TotalsStrip totals={data.totals} />
              <BasisSubtotals rows={data.by_basis} totals={data.totals} />
              <ScopeSplit totals={data.totals} rows={data.by_basis} />
              <BaselineComparison data={data} />
              <ByModel rows={data.by_model} localModels={localModels} />
              <ByHarness rows={data.by_harness} />
              <ByDay rows={data.by_day} />
            </>
          )}

          <Factors data={data} />
        </>
      )}
    </div>
  )
}

// ── credibility guardrails ───────────────────────────────────────────────

/** The backend's disclaimer, verbatim, above everything it qualifies — plus the
 *  mixed-factors caveat when the window has no single recording basis. */
function Disclaimer({ data }: { data: EmissionsAnalytics }) {
  const totals = data.totals
  const notSummable = totals.carbon_is_summable === false
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="callout callout-note">
        <span className="callout-title">Read before quoting any figure on this page</span>
        {data.disclaimer}
        <div style={{ marginTop: 6 }}>
          Methodology: <MethodologyLink factorsSlot={<WindowFactors data={data} />} /> — the full
          document, plus the factor combinations behind this window.
        </div>
      </div>
      {notSummable && (
        <div className="callout callout-warn">
          <span className="callout-title">{NOT_SUMMABLE_TITLE}</span>
          This window mixes <strong>{basisList(totals.grid_bases)}</strong> grid factors.{' '}
          {NOT_SUMMABLE_WHY}
          <div style={{ marginTop: 6 }}>
            {SUMMABLE_ACROSS_BASES_NOTE}
            {totals.runs_without_grid_basis > 0 && (
              <>
                {' '}
                {formatTokens(totals.runs_without_grid_basis)} run(s) here recorded no basis at all —
                they predate the label and form their own group rather than being folded in with a
                figure they cannot be shown to share.
              </>
            )}
          </div>
        </div>
      )}
      {data.factors.mixed_factors && (
        <div className="callout callout-warn">
          <span className="callout-title">Mixed emission factors in this window</span>
          {MIXED_FACTORS_CAVEAT}
          {!notSummable && (
            <div style={{ marginTop: 6 }}>
              The factors differ but they share one GHG Protocol basis (
              <strong>{basisList(totals.grid_bases)}</strong>), so the carbon figures below are still
              a legitimate total — they simply do not have a single factor behind them.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** What the methodology dialog shows on this page in place of a run's factor
 *  records: per-factor provenance is recorded per run, so the window-scale view
 *  offers the combinations actually present instead of pretending to have one. */
function WindowFactors({ data }: { data: EmissionsAnalytics }) {
  const f = data.factors
  const t = data.totals
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="callout callout-note">
        <span className="callout-title">Provenance is per run, not per window</span>
        Every constant's value, source, date and confidence marker is recorded on the run that used
        it. Open any run's <em>step-by-step derivation</em> for the full factor table. What follows
        is what this window contains, and the settings in force right now — nothing above was
        computed from them.
      </div>
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Recording bases present in this window
        </div>
        <MonoTable
          columns={RECORDED_COLUMNS}
          rows={f.recorded}
          rowKey={recordedKey}
          empty="No runs with an estimate in this window."
        />
        <div className="fine-print" style={{ marginTop: 6 }}>
          The <em>source</em> column is which configuration rule chose each factor. Precedence:
          per-provider factor (<code>TRET_GRID_FACTORS</code>) → the self-hosted factor
          (<code>TRET_LOCAL_GRID_CO2E_G_PER_KWH</code>, legacy) → the global default
          (<code>TRET_GRID_CO2E_G_PER_KWH</code>). {GRID_NO_INFERENCE_NOTE}
        </div>
      </div>
      <ConfiguredFactors factors={f.grid_factors} />
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Coverage
        </div>
        <div className="fine-print">
          {formatTokens(t.runs_with_estimate)} of {formatTokens(t.runs)} run(s) carry an estimate.{' '}
          {formatTokens(t.runs_without_scope_split)} carry no scope split,{' '}
          {formatTokens(t.runs_without_baseline)} no baseline comparison,{' '}
          {formatTokens(t.runs_without_money_comparison)} no money comparison, and{' '}
          {formatTokens(t.runs_without_uncertainty_band)} no {BAND_LABEL}. None of those are
          back-filled with zeros.
          {f.uncertainty_band_low !== undefined && f.uncertainty_band_high !== undefined && (
            <>
              {' '}
              The band currently configured is{' '}
              {bandFactorText(f.uncertainty_band_low, f.uncertainty_band_high)}. {BAND_SHORT}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function EmptyWindow({ days }: { days: number }) {
  return (
    <div className="panel">
      <div className="mono-label" style={{ marginBottom: 8 }}>
        No runs in the last {days} days
      </div>
      <div className="fine-print" style={{ fontSize: 11.5 }}>
        Once a run finishes, this page fills in with:
        <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
          <li>total estimated gCO₂e and energy for the window, and how many runs carry an estimate</li>
          <li>the GHG Protocol split — Scope 1 (always zero, and why), Scope 2, Scope 3</li>
          <li>
            a same-token comparison against the frontier baseline model, which can come out either
            way
          </li>
          <li>per-model and per-harness rollups, and a per-day bar row</li>
        </ul>
        <div style={{ marginTop: 8 }}>
          Start a run from the Workbench or Chat. A run's own derivation, step by step, is on its run
          page.
        </div>
      </div>
    </div>
  )
}

function NoEstimates({ totals }: { totals: EmissionsTotals }) {
  return (
    <div className="panel">
      <div className="mono-label" style={{ marginBottom: 8 }}>
        No estimates in this window
      </div>
      <div className="fine-print" style={{ fontSize: 11.5 }}>
        All {formatTokens(totals.runs)} run(s) here carry no footprint estimate — they are reported
        as having no figure, not as having emitted nothing. A run has no estimate when it failed
        before any model call, or when it predates emissions accounting.
      </div>
    </div>
  )
}

// ── totals ───────────────────────────────────────────────────────────────

// Exported so the scenario drawer (components/shared/EmissionsScenario.tsx)
// can render a what-if result's `recorded`/`scenario` totals with the exact
// same layout as the page itself — "recorded vs scenario, side by side"
// means literally the same component, not a lookalike.
export function TotalsStrip({ totals }: { totals: EmissionsTotals }) {
  const avoided = bucketAvoided(totals)
  const framing = avoidedFraming(avoided)
  const band = bucketBand(totals)
  const money = avoidedMoneyFraming(totals.avoided_usd)
  const summable = totals.carbon_is_summable !== false
  return (
    <div className="panel stack" style={{ gap: 10 }}>
      <div className="config-stats" style={{ gap: 34 }}>
      <Stat
        label="Total CO₂e (est.)"
        value={summable ? orDash(formatCo2eScaled(totals.co2e_g)) : NO_ESTIMATE}
        color={summable ? undefined : 'var(--text-muted)'}
        sub={
          summable
            ? band
              ? formatCo2eBand(band.low, band.high)
              : undefined
            : `${basisList(totals.grid_bases)} — see subtotals`
        }
        subTitle={
          summable
            ? `${BAND_SHORT} Summed low-with-low and high-with-high, which assumes the factors are wrong in the same direction for every run in the window.`
            : NOT_SUMMABLE_WHY
        }
        title={
          summable
            ? "Summed from each run's as-recorded figure, never recomputed at today's factors."
            : NOT_SUMMABLE_WHY
        }
      />
      <Stat
        label="Energy used for carbon"
        value={orDash(formatEnergyScaled(totals.energy_wh))}
        title="Sum of recorded energy after each run's applicable overhead treatment. Coverage may be incomplete; inspect individual runs."
      />
      <Stat
        label="Recorded energy"
        value={orDash(formatEnergyScaled(totals.energy_wh_compute))}
        title="Sum of energy within each run's recorded boundary, before any additional PUE. GPU, node and facility coverage may differ."
      />
      <Stat
        label="Runs with an estimate"
        value={formatTokens(totals.runs_with_estimate)}
        title="Only these runs are in the totals."
      />
      <Stat
        label="Runs without an estimate"
        value={totals.runs_without_estimate === 0 ? '0' : formatTokens(totals.runs_without_estimate)}
        color={totals.runs_without_estimate > 0 ? 'var(--amber)' : undefined}
        title={`Excluded from every total above. ${NO_ESTIMATE_HINT}`}
      />
      {(totals.runs_without_carbon_total ?? 0) > 0 && (
        <Stat
          label="Runs without a combined carbon figure"
          value={formatTokens(totals.runs_without_carbon_total ?? 0)}
          color="var(--amber)"
          title="Recorded energy and cost are retained; incompatible carbon components are not combined."
        />
      )}
      <Stat
        label={framing.label}
        value={orDash(formatCo2eScaled(avoided))}
        color={summable ? framing.color : 'var(--text-muted)'}
        title={
          summable
            ? `${framing.note} ${COUNTERFACTUAL_NOTE}`
            : `${NOT_SUMMABLE_CELL_HINT} ${COUNTERFACTUAL_NOTE}`
        }
      />
      <Stat
        label={money.label}
        value={formatCostScaled(totals.avoided_usd)}
        color={money.color}
        sub={moneyPctPhrase(totals.avoided_usd_pct)}
        subTitle={MONEY_PCT_PRECISION_NOTE}
        title={`${money.note} ${MONEY_PCT_PRECISION_NOTE}`}
      />
      </div>
      {!summable && (
        <div className="fine-print">
          Carbon is not totalled here: these runs span {basisList(totals.grid_bases)} grid factors.{' '}
          {SUMMABLE_ACROSS_BASES_NOTE} The energy and money figures above cover every run in the
          window.
        </div>
      )}
      <div className="fine-print">
        {BAND_WHY} {BAND_SHORT}
        {totals.runs_without_uncertainty_band > 0 && (
          <>
            {' '}
            {formatTokens(totals.runs_without_uncertainty_band)} run(s) here recorded no range; they
            contribute their central figure to both ends rather than widening or narrowing it.
          </>
        )}
        {totals.runs_without_money_comparison > 0 && (
          <>
            {' '}
            {formatTokens(totals.runs_without_money_comparison)} run(s) recorded no money comparison
            and contribute nothing to the dollar figure.
          </>
        )}
      </div>
    </div>
  )
}

// ── carbon, grouped by GHG Protocol basis ────────────────────────────────

/** The subtotals that replace a single carbon total when the window mixes bases.
 *
 *  Rendered only when there is more than one basis: with one basis the window
 *  totals above *are* this row, and repeating them would imply a distinction that
 *  is not there. Each row may be read as a total; the rows may not be added. */
function BasisSubtotals({
  rows,
  totals,
}: {
  rows: EmissionsByBasis[]
  totals: EmissionsTotals
}) {
  if (!rows || rows.length < 2) return null
  return (
    <Section title="Carbon by GHG Protocol basis" hint={BASIS_SUBTOTAL_HINT}>
      <div className="panel stack" style={{ gap: 10 }}>
        <div className="md-table-wrap">
          <table className="mono-table">
            <thead>
              <tr>
                <th>Basis</th>
                <th className="num">Runs</th>
                <th className="num">Energy (est.)</th>
                <th className="num">CO₂e (est.)</th>
                <th className="num">Scope 2 (est.)</th>
                <th className="num">Scope 3 (est.)</th>
                <th className="num">Avoided (signed)</th>
                <th className="num">Money (signed)</th>
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
                  <td className="num">
                    <Co2eWithBand bucket={row} />
                  </td>
                  <td className="num">{orDash(formatCo2eScaled(row.scope2_g))}</td>
                  <td className="num">{orDash(formatCo2eScaled(row.scope3_g))}</td>
                  <td className="num">
                    <Avoided grams={bucketAvoided(row)} />
                  </td>
                  <td className="num">
                    <AvoidedMoney usd={row.avoided_usd} pct={row.avoided_usd_pct} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="callout callout-note">
          <span className="callout-title">Why these are not added together</span>
          {NOT_SUMMABLE_WHY} {SUMMABLE_ACROSS_BASES_NOTE}
          <div style={{ marginTop: 6 }}>
            Which rows exist is a consequence of how the factors were configured, not of anything
            tret inferred. {GRID_NO_INFERENCE_NOTE}
          </div>
          {totals.runs_without_grid_basis > 0 && (
            <div style={{ marginTop: 6 }}>
              The <strong>{gridBasisLabel(null)}</strong> row is{' '}
              {formatTokens(totals.runs_without_grid_basis)} run(s) recorded before tret stored a
              basis. They keep their carbon figure and are not folded into any other row.
            </div>
          )}
        </div>
      </div>
    </Section>
  )
}

// ── scopes ───────────────────────────────────────────────────────────────

/** The scope split. A scope total *is* carbon, so it follows the basis rule
 *  exactly: with one basis this is the window's split as before, and with several
 *  it is one panel per basis rather than one misleading panel. */
function ScopeSplit({
  totals,
  rows,
}: {
  totals: EmissionsTotals
  rows: EmissionsByBasis[]
}) {
  const summable = totals.carbon_is_summable !== false
  const panels: { key: string; label: string | null; bucket: EmissionsBucket }[] = summable
    ? [{ key: 'window', label: null, bucket: totals }]
    : (rows ?? []).map((row) => ({
        key: row.basis ?? 'unrecorded',
        label: gridBasisLabel(row.basis),
        bucket: row,
      }))
  return (
    <Section
      title="GHG Protocol scopes"
      hint={
        summable
          ? "Where the estimated carbon lands from the tret operator's point of view. Scope 1 is always zero and is shown as an explicit zero — an omitted line would read as an oversight."
          : "Where the estimated carbon lands from the tret operator's point of view, one split per GHG Protocol basis. A scope total is carbon, so it may not be summed across bases either — there is deliberately no combined split below."
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        {panels.map((panel) => (
          <ScopePanel key={panel.key} label={panel.label} bucket={panel.bucket} />
        ))}
      </div>
    </Section>
  )
}

function ScopePanel({ label, bucket }: { label: string | null; bucket: EmissionsBucket }) {
  const scope1 = bucket.scope1_g ?? 0
  const scope2 = bucket.scope2_g ?? 0
  const scope3 = bucket.scope3_g ?? 0
  const scoped = scope1 + scope2 + scope3
  const total = bucket.co2e_g
  const gap = total === null ? null : total - scoped
  return (
    <div className="panel">
      {label && (
        <div className="row" style={{ marginBottom: 8, flexWrap: 'wrap' }}>
          <div className="mono-label">{label}</div>
          <span className="fine-print">
            {formatTokens(bucket.runs)} run(s) · {orDash(formatEnergyScaled(bucket.energy_wh))}
          </span>
        </div>
      )}
      <ScopeBar values={{ scope1_g: scope1, scope2_g: scope2, scope3_g: scope3 }} height={12} />
      <table className="mono-table" style={{ marginTop: 10 }}>
        <thead>
          <tr>
            <th>Scope</th>
            <th>What it covers</th>
            <th className="num">CO₂e (est.)</th>
            <th className="num">Share</th>
          </tr>
        </thead>
        <tbody>
          {SCOPE_META.map((meta) => {
            const value = bucket[meta.key]
            return (
              <tr key={meta.key}>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <span className="swatch" style={{ background: meta.color }} />
                  {meta.label}
                </td>
                <td style={{ color: 'var(--text-muted)' }}>{meta.what}</td>
                <td className="num">{orDash(formatCo2eScaled(value))}</td>
                <td className="num">{share(value ?? 0, scoped).toFixed(1)}%</td>
              </tr>
            )
          })}
          <tr>
            <td style={{ color: 'var(--text-muted)' }}>Scoped subtotal</td>
            <td style={{ color: 'var(--text-muted)' }}>
              Scope 1 + 2 + 3 over the runs that carry a split
            </td>
            <td className="num">{orDash(formatCo2eScaled(scoped))}</td>
            <td className="num">100.0%</td>
          </tr>
        </tbody>
      </table>
      <div className="fine-print" style={{ marginTop: 8 }}>
        {bucket.runs_without_scope_split > 0 ? (
          <>
            {formatTokens(bucket.runs_without_scope_split)} run(s) here carry a carbon figure but no
            scope split, so the scoped subtotal is {orDash(formatCo2eScaled(gap))} short of the total
            ({orDash(formatCo2eScaled(total))}). Those runs are not back-filled with zeros.
          </>
        ) : (
          <>
            The scoped subtotal equals the total ({orDash(formatCo2eScaled(total))}): every run here
            carries a split.
          </>
        )}
      </div>
    </div>
  )
}

// ── baseline comparison ──────────────────────────────────────────────────

function BaselineComparison({ data }: { data: EmissionsAnalytics }) {
  const t = data.totals
  const avoided = bucketAvoided(t)
  const framing = avoidedFraming(avoided)
  const comparison = coarseComparison(t.co2e_g, t.baseline_co2e_g)
  const money = avoidedMoneyFraming(t.avoided_usd)
  const baselineModel = data.factors.baseline_model
  // Two different reasons the carbon comparison is absent, and they read
  // differently: nothing had a baseline, or the window's carbon may not be summed.
  const notSummable = t.carbon_is_summable === false
  const noBaseline = avoided === null
  const scale = Math.max(t.co2e_g ?? 0, t.baseline_co2e_g ?? 0)
  // One unit for the pair, so the two bars are read against each other rather
  // than against two different scales.
  const unit = co2eScaleFor(scale)

  return (
    <Section
      title="Actual vs frontier baseline"
      hint="What the same tokens would have cost on the baseline model. An efficiency indicator for model choice — it can come out either way, and a negative result is a surcharge, not a saving."
    >
      <div className="panel stack" style={{ gap: 12 }}>
        {notSummable ? (
          <div className="stack" style={{ gap: 10 }}>
            <div className="callout callout-warn">
              <span className="callout-title">No window-scale carbon comparison</span>
              This window's runs span {basisList(t.grid_bases)} grid factors, so neither its carbon
              total nor its baseline total is a single figure — and a difference between two
              non-totals is not a comparison. {NOT_SUMMABLE_WHY}
              <div style={{ marginTop: 6 }}>
                The per-basis subtotals above carry the comparison for each basis separately, and each
                run's own page carries its own.
              </div>
            </div>
            <div className="config-stats" style={{ gap: 34 }}>
              <Stat
                label="Baseline model"
                value={baselineModel ?? NO_ESTIMATE}
                title="The counterfactual comparison model in force now. Each run was compared against the baseline resolved when it ran."
              />
              <Stat
                label={money.label}
                value={formatCostScaled(t.avoided_usd)}
                color={money.color}
                sub={moneyPctPhrase(t.avoided_usd_pct)}
                subTitle={MONEY_PCT_PRECISION_NOTE}
                title={`${money.note} Money is unaffected by the basis rule — a price has no Scope 2 accounting method. ${MONEY_PCT_PRECISION_NOTE}`}
              />
            </div>
          </div>
        ) : noBaseline ? (
          <div className="empty" style={{ padding: '4px 0' }}>
            No baseline comparison in this window
            {t.runs_without_baseline > 0
              ? ` — ${formatTokens(t.runs_without_baseline)} run(s) carry no baseline figure.`
              : '.'}
          </div>
        ) : (
          <>
            <div className="stack" style={{ gap: 8 }}>
              <div className="cmp-row">
                <span>actual</span>
                <span className="cmp-track">
                  <span
                    style={{
                      width: `${share(t.co2e_g ?? 0, scale)}%`,
                      background: 'var(--accent)',
                      opacity: 0.8,
                    }}
                  />
                </span>
                <span className="cmp-val">{orDash(formatCo2eAt(t.co2e_g, unit))}</span>
              </div>
              <div className="cmp-row">
                <span title={`Baseline model: ${baselineModel ?? 'not resolved'}`}>baseline</span>
                <span className="cmp-track">
                  <span
                    style={{
                      width: `${share(t.baseline_co2e_g ?? 0, scale)}%`,
                      background: 'var(--gray)',
                      opacity: 0.7,
                    }}
                  />
                </span>
                <span className="cmp-val">{orDash(formatCo2eAt(t.baseline_co2e_g, unit))}</span>
              </div>
            </div>

            <div className="config-stats" style={{ gap: 34 }}>
              <Stat
                label="Baseline model"
                value={baselineModel ?? NO_ESTIMATE}
                title="The counterfactual comparison model in force now. Each run was compared against the baseline resolved when it ran."
              />
              <Stat
                label="Actual total (est.)"
                value={orDash(formatCo2eAt(t.co2e_g, unit))}
              />
              <Stat
                label="Baseline total (est.)"
                value={orDash(formatCo2eAt(t.baseline_co2e_g, unit))}
              />
              <Stat
                label={framing.label}
                value={orDash(formatCo2eScaled(avoided))}
                color={framing.color}
                title={framing.note}
              />
              <Stat
                label="Difference (order of magnitude)"
                value={comparison.text}
                color={comparison.color}
                title={comparison.note}
              />
              <Stat
                label={money.label}
                value={formatCostScaled(t.avoided_usd)}
                color={money.color}
                sub={moneyPctPhrase(t.avoided_usd_pct)}
                subTitle={MONEY_PCT_PRECISION_NOTE}
                title={`${money.note} ${MONEY_PCT_PRECISION_NOTE}`}
              />
            </div>
          </>
        )}

        <div className="callout callout-note">
          <span className="callout-title">What this comparison is and is not</span>
          {COUNTERFACTUAL_NOTE}
          {!noBaseline && !notSummable && ` ${framing.note}`}
          <div style={{ marginTop: 6 }}>
            {!notSummable && <>The difference is stated coarsely on purpose. {comparison.note} </>}
            Money is the firmer of the two figures — {MONEY_SHORT}
          </div>
          {t.runs_without_baseline > 0 && (
            <div style={{ marginTop: 6 }}>
              {formatTokens(t.runs_without_baseline)} run(s) in this window have no baseline figure.
              They are counted in the actual total but not in the baseline total, so the two bars do
              not cover exactly the same runs.
            </div>
          )}
        </div>
      </div>
    </Section>
  )
}

// ── rollups ──────────────────────────────────────────────────────────────

function ByModel({ rows, localModels }: { rows: EmissionsByModel[]; localModels: Set<string> }) {
  const columns: Column<EmissionsByModel>[] = [
    {
      key: 'model',
      header: 'Model',
      render: (r) => (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          {r.model}
          {localModels.has(r.model) && (
            <span
              className="badge badge-violet"
              title="Self-hosted inference: the operator buys the electricity, so it lands in Scope 2 and can carry embodied hardware in Scope 3."
            >
              local
            </span>
          )}
        </span>
      ),
    },
    {
      key: 'class',
      header: 'Energy class',
      render: (r) =>
        r.energy_class ? (
          <span
            className={`badge ${CLASS_BADGE[r.energy_class] ?? 'badge-gray'}`}
            title="Heuristic order-of-magnitude bucket, not a measurement."
          >
            {r.energy_class}
          </span>
        ) : (
          <span title={NO_ESTIMATE_HINT}>{NO_ESTIMATE}</span>
        ),
    },
    { key: 'runs', header: 'Runs', align: 'right', render: (r) => formatTokens(r.runs) },
    {
      key: 'energy',
      header: 'Energy (est.)',
      align: 'right',
      render: (r) => orDash(formatEnergyScaled(r.energy_wh)),
    },
    {
      key: 'basis',
      header: 'GHG basis',
      render: (r) => <BasisCell bucket={r} />,
    },
    {
      key: 'co2e',
      header: 'CO₂e (est.)',
      align: 'right',
      render: (r) => <Co2eWithBand bucket={r} />,
    },
    {
      key: 'baseline',
      header: 'Baseline (est.)',
      align: 'right',
      // A bucket with no baseline at all shows no figure, not a zero baseline.
      render: (r) =>
        bucketAvoided(r) === null ? (
          <span title={carbonCellHint(r)}>{NO_ESTIMATE}</span>
        ) : (
          orDash(formatCo2eScaled(r.baseline_co2e_g))
        ),
    },
    {
      key: 'avoided',
      header: 'Avoided (signed)',
      align: 'right',
      render: (r) => <Avoided grams={bucketAvoided(r)} hint={carbonCellHint(r)} />,
    },
    {
      key: 'money',
      header: 'Money (signed)',
      align: 'right',
      render: (r) => <AvoidedMoney usd={r.avoided_usd} pct={r.avoided_usd_pct} />,
    },
  ]
  return (
    <Section
      title="By model"
      hint="Model choice moves this number by more than an order of magnitude, which is the whole reason the estimate is worth reporting. A model belongs to one provider, so these rows usually keep a carbon figure even where the window as a whole cannot."
    >
      <MonoTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.model}
        empty="No model carried an estimate in this window."
      />
    </Section>
  )
}

function ByHarness({ rows }: { rows: EmissionsByHarness[] }) {
  const columns: Column<EmissionsByHarness>[] = [
    { key: 'harness', header: 'Harness', render: (r) => r.harness_name },
    { key: 'runs', header: 'Runs', align: 'right', render: (r) => formatTokens(r.runs) },
    {
      key: 'energy',
      header: 'Energy (est.)',
      align: 'right',
      render: (r) => orDash(formatEnergyScaled(r.energy_wh)),
    },
    {
      key: 'basis',
      header: 'GHG basis',
      render: (r) => <BasisCell bucket={r} />,
    },
    {
      key: 'co2e',
      header: 'CO₂e (est.)',
      align: 'right',
      render: (r) => <Co2eWithBand bucket={r} />,
    },
    {
      key: 'baseline',
      header: 'Baseline (est.)',
      align: 'right',
      // A bucket with no baseline at all shows no figure, not a zero baseline.
      render: (r) =>
        bucketAvoided(r) === null ? (
          <span title={carbonCellHint(r)}>{NO_ESTIMATE}</span>
        ) : (
          orDash(formatCo2eScaled(r.baseline_co2e_g))
        ),
    },
    {
      key: 'avoided',
      header: 'Avoided (signed)',
      align: 'right',
      render: (r) => <Avoided grams={bucketAvoided(r)} hint={carbonCellHint(r)} />,
    },
    {
      key: 'money',
      header: 'Money (signed)',
      align: 'right',
      render: (r) => <AvoidedMoney usd={r.avoided_usd} pct={r.avoided_usd_pct} />,
    },
  ]
  return (
    <Section
      title="By harness"
      hint="Which workflows account for the window's footprint. A harness that ran two providers on different GHG Protocol bases has an energy figure but no carbon one."
    >
      <MonoTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.harness_id}
        empty="No harness carried an estimate in this window."
      />
    </Section>
  )
}

/** Per-day bars, CSS only. Two rows: carbon as recorded, then the signed
 *  baseline difference, which is red on the days that ran heavier. */
function ByDay({ rows }: { rows: EmissionsByDay[] }) {
  if (rows.length === 0) {
    return (
      <Section title="By day" hint="Daily totals as recorded.">
        <div className="empty">No dated runs with an estimate in this window.</div>
      </Section>
    )
  }
  const maxCo2e = Math.max(...rows.map((r) => r.co2e_g ?? 0), 0)
  const maxAvoided = Math.max(...rows.map((r) => Math.abs(r.avoided_co2e_g ?? 0)), 0)
  const maxEnergy = Math.max(...rows.map((r) => r.energy_wh ?? 0), 0)
  // Days whose runs span two bases have no daily carbon figure. They keep their
  // energy, so the row below plots that rather than dropping the day.
  const withheld = rows.filter((r) => r.carbon_is_summable === false)
  return (
    <Section
      title="By day"
      hint="Daily totals as recorded. Bars are scaled to the largest day in the window; hover a bar for its figures."
    >
      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 6 }}>
          CO₂e per day (est.)
        </div>
        <div
          className="spark"
          role="img"
          aria-label={`Estimated carbon per day across ${rows.length} days, peaking at ${orDash(formatCo2eScaled(maxCo2e))}`}
        >
          {rows.map((r) => (
            <div
              key={r.date}
              className="spark-col"
              title={
                r.co2e_g === null
                  ? `${r.date}: no single carbon figure — this day's runs span ${basisList(
                      r.grid_bases ?? [],
                    )} grid factors. ${orDash(formatEnergyScaled(r.energy_wh))} of energy.`
                  : `${r.date}: ${orDash(formatCo2e(r.co2e_g))} (est.)`
              }
            >
              <i style={{ height: `${share(r.co2e_g ?? 0, maxCo2e)}%` }} />
            </div>
          ))}
        </div>

        <div className="mono-label" style={{ margin: '14px 0 6px' }}>
          Difference vs baseline per day (est., signed)
        </div>
        <div
          className="spark"
          style={{ height: 30 }}
          role="img"
          aria-label="Signed daily difference against the frontier baseline; downward-coloured days ran heavier than the baseline"
        >
          {rows.map((r) => (
            <div
              key={r.date}
              className={`spark-col ${(r.avoided_co2e_g ?? 0) < 0 ? 'neg' : 'pos'}`}
              title={
                r.avoided_co2e_g === null
                  ? `${r.date}: no comparison — this day's carbon may not be summed across bases.`
                  : `${r.date}: ${
                      r.avoided_co2e_g < 0
                        ? `${orDash(formatCo2e(r.avoided_co2e_g))} — heavier than the baseline`
                        : `${orDash(formatCo2e(r.avoided_co2e_g))} lighter than the baseline`
                    } (est.)`
              }
            >
              <i style={{ height: `${share(Math.abs(r.avoided_co2e_g ?? 0), maxAvoided)}%` }} />
            </div>
          ))}
        </div>

        {withheld.length > 0 && (
          <>
            <div className="mono-label" style={{ margin: '14px 0 6px' }}>
              Energy per day (est.) — always summable
            </div>
            <div
              className="spark"
              style={{ height: 30 }}
              role="img"
              aria-label="Estimated energy per day, which is summable across every basis"
            >
              {rows.map((r) => (
                <div
                  key={r.date}
                  className="spark-col"
                  title={`${r.date}: ${orDash(formatEnergyScaled(r.energy_wh))} (est.)`}
                >
                  <i style={{ height: `${share(r.energy_wh ?? 0, maxEnergy)}%` }} />
                </div>
              ))}
            </div>
          </>
        )}

        <div className="spark-axis">
          <span>{rows[0].date}</span>
          <span>{rows[rows.length - 1].date}</span>
        </div>
        <div className="fine-print" style={{ marginTop: 8 }}>
          Green days ran lighter than the baseline for the same tokens; red days ran heavier. Neither
          is an offset or a credit.
          {withheld.length > 0 && (
            <>
              {' '}
              {formatTokens(withheld.length)} day(s) here have no carbon bar at all: their runs span
              more than one GHG Protocol basis, so there is no daily figure to plot. Their energy is
              in the third row.
            </>
          )}
        </div>
      </div>
    </Section>
  )
}

// ── factors ──────────────────────────────────────────────────────────────

/** The (deployment, grid factor, PUE, GHG Protocol basis) combinations a window
 *  actually contains. Shared with the methodology dialog, which shows the same
 *  rows in place of a single run's factor provenance. The basis column is not
 *  decoration: two rows differing only in basis are not summable at all. */
const RECORDED_COLUMNS: Column<EmissionsRecordedFactor>[] = [
  { key: 'deployment', header: 'Deployment', render: (r) => r.deployment ?? NO_ESTIMATE },
  {
    key: 'grid',
    header: 'Grid intensity',
    align: 'right',
    render: (r) =>
      r.grid_co2e_g_per_kwh === null
        ? NO_ESTIMATE
        : `${formatFactor(r.grid_co2e_g_per_kwh, 2)} gCO₂e/kWh`,
  },
  {
    key: 'basis',
    header: 'GHG basis',
    render: (r) => (
      <span
        title={gridBasisWhat(r.grid_co2e_basis)}
        style={r.grid_co2e_basis === 'market_based' ? { color: 'var(--amber)' } : undefined}
      >
        {gridBasisLabel(r.grid_co2e_basis)}
      </span>
    ),
  },
  {
    key: 'source',
    header: 'Source',
    render: (r) => (
      <span title={gridSourceWhat(r.grid_co2e_source)}>
        {gridSourceLabel(r.grid_co2e_source)}
        {r.grid_co2e_label && <span className="band-under">{r.grid_co2e_label}</span>}
      </span>
    ),
  },
  {
    key: 'pue',
    header: 'PUE',
    align: 'right',
    render: (r) => (r.pue === null ? NO_ESTIMATE : formatFactor(r.pue)),
  },
  { key: 'runs', header: 'Runs', align: 'right', render: (r) => formatTokens(r.runs) },
]

/** Row identity for the recorded-factor table: the whole combination, since two
 *  rows can differ only in which rule chose the same number. */
function recordedKey(r: EmissionsRecordedFactor): string {
  return [
    r.deployment,
    r.grid_co2e_g_per_kwh,
    r.pue,
    r.grid_co2e_basis ?? 'none',
    r.grid_co2e_source ?? 'unrecorded',
  ].join('-')
}

/** The per-provider factors configured right now — reference only, like every
 *  other current setting on this page. Rendered so an operator can see what they
 *  configured next to what the window actually recorded, which is how a mismatch
 *  ("I set this last week, why is the window still on the default?") gets found. */
function ConfiguredFactors({
  factors,
}: {
  factors: EmissionsAnalytics['factors']['grid_factors']
}) {
  const entries = Object.entries(factors ?? {})
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Per-provider grid factors configured now
      </div>
      {entries.length === 0 ? (
        <div className="fine-print">
          None configured. Every provider falls back to the self-hosted factor (local runs only) or
          the global default. Set <code>TRET_GRID_FACTORS</code> to give a provider its own factor —
          it is the single biggest improvement available to these numbers.
        </div>
      ) : (
        <div className="md-table-wrap">
          <table className="mono-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th className="num">Grid intensity</th>
                <th>GHG basis</th>
                <th>Label</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([provider, entry]) => (
                <tr key={provider}>
                  <td>{provider}</td>
                  <td className="num">{formatFactor(entry.g_per_kwh, 2)} gCO₂e/kWh</td>
                  <td title={gridBasisWhat(entry.basis)}>{gridBasisLabel(entry.basis)}</td>
                  <td style={{ color: 'var(--text-muted)' }}>{entry.label ?? NO_ESTIMATE}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="fine-print" style={{ marginTop: 6 }}>
        Current configuration, not what these runs used — a run that predates an entry was not
        recorded under it. {GRID_NO_INFERENCE_NOTE}
      </div>
    </div>
  )
}

function Factors({ data }: { data: EmissionsAnalytics }) {
  const f = data.factors
  return (
    <Section
      title="Factors"
      hint="Current settings, for reference only. Nothing above was computed from them — each run carries the factors that were in force when it ran."
    >
      <div className="panel stack" style={{ gap: 12 }}>
        <div className="config-stats" style={{ gap: 34 }}>
          <Stat
            label="Grid intensity"
            value={`${formatFactor(f.grid_co2e_g_per_kwh, 2)} gCO₂e/kWh`}
            title="Applied to cloud inference, and to local inference unless a local factor is set."
          />
          <Stat
            label="Per-provider factors"
            value={
              Object.keys(f.grid_factors ?? {}).length === 0
                ? 'none'
                : Object.keys(f.grid_factors ?? {}).join(', ')
            }
            sub={
              Object.keys(f.grid_factors ?? {}).length === 0
                ? undefined
                : 'highest precedence'
            }
            subTitle="A per-provider factor outranks both the self-hosted setting and the global default for that provider's runs."
            title={`Providers with their own configured grid factor (TRET_GRID_FACTORS). ${GRID_NO_INFERENCE_NOTE}`}
          />
          <Stat
            label="Local grid intensity (legacy)"
            value={
              f.local_grid_co2e_g_per_kwh === null
                ? 'not set'
                : `${formatFactor(f.local_grid_co2e_g_per_kwh, 2)} gCO₂e/kWh`
            }
            title="Optional site- or market-based factor for self-hosted inference (TRET_LOCAL_GRID_CO2E_G_PER_KWH). Still honoured, and superseded by a per-provider factor for 'local'. Unset falls back to the grid intensity."
          />
          <Stat label="Data-centre PUE" value={formatFactor(f.datacenter_pue)} />
          <Stat label="Local PUE" value={formatFactor(f.local_pue)} />
          <Stat label="Baseline model" value={f.baseline_model ?? 'not resolved'} />
        </div>
        <div className="fine-print">{f.note}</div>
        <ConfiguredFactors factors={f.grid_factors} />
        {f.recorded.length > 0 && (
          <details className="tool-row">
            <summary>
              <span className="tool-name">recording bases in this window</span>
              <span style={{ color: 'var(--text-muted)', fontSize: 10.5 }}>
                {f.recorded.length} combination{f.recorded.length === 1 ? '' : 's'} of deployment,
                grid intensity, PUE, basis and factor source
              </span>
            </summary>
            <div style={{ padding: '6px 10px 10px' }}>
              <MonoTable
                columns={RECORDED_COLUMNS}
                rows={f.recorded}
                rowKey={recordedKey}
                empty="Nothing recorded in this window."
              />
            </div>
          </details>
        )}
        <div className="fine-print">{ESTIMATE_NOTE}</div>
      </div>
    </Section>
  )
}

// ── small pieces ─────────────────────────────────────────────────────────

/** A rollup's carbon with its summed range beneath it. The range is the reason a
 *  single number is not printed alone anywhere on this page. */
function Co2eWithBand({
  bucket,
}: {
  bucket: {
    co2e_g: number | null
    co2e_g_low: number | null
    co2e_g_high: number | null
    carbon_is_summable?: boolean
  }
}) {
  const band = bucketBand(bucket)
  if (bucket.co2e_g === null) {
    return (
      <span
        style={{ color: 'var(--text-muted)' }}
        title={bucket.carbon_is_summable === false ? NOT_SUMMABLE_CELL_HINT : NO_ESTIMATE_HINT}
      >
        {NO_ESTIMATE}
      </span>
    )
  }
  return (
    <>
      {orDash(formatCo2eScaled(bucket.co2e_g))}
      {band && (
        <span className="band-under" title={BAND_SHORT}>
          {formatCo2eBand(band.low, band.high)}
        </span>
      )}
    </>
  )
}

/** The bases behind a rollup row. One basis is the ordinary case and reads as a
 *  label; two is the row saying why its carbon column is empty. */
function BasisCell({ bucket }: { bucket: EmissionsBucket }) {
  const bases = bucket.grid_bases ?? []
  if (bases.length === 0) {
    return (
      <span style={{ color: 'var(--text-muted)' }} title={NO_ESTIMATE_HINT}>
        {NO_ESTIMATE}
      </span>
    )
  }
  if (bases.length === 1) {
    return (
      <span
        title={gridBasisWhat(bases[0])}
        style={bases[0] === 'market_based' ? { color: 'var(--amber)' } : undefined}
      >
        {gridBasisLabel(bases[0])}
      </span>
    )
  }
  return (
    <span className="badge badge-amber" title={NOT_SUMMABLE_CELL_HINT}>
      {basisList(bases)}
    </span>
  )
}

/** A rollup's signed money figure. Exact, unlike everything around it, and said
 *  so — but an exact **zero** in a rollup is ambiguous: the runs in it may carry
 *  no money comparison at all, or the comparison may have come out even, and the
 *  summed total cannot tell those apart. Rather than assert "$0 saved", it renders
 *  as no figure and the tooltip names both possibilities.
 *
 *  The percentage alongside it is computed server-side from this bucket's
 *  summed dollars, never by averaging each run's own percentage — see
 *  docs/emissions-methodology.md. It carries a decimal place on purpose: it is
 *  arithmetic on list prices, unlike the coarse carbon comparison. */
function AvoidedMoney({
  usd,
  pct,
}: {
  usd: number | null | undefined
  pct: number | null | undefined
}) {
  if (usd === null || usd === undefined || usd === 0) {
    return (
      <span
        style={{ color: 'var(--text-muted)' }}
        title="No money comparison in this rollup — either these runs recorded none (runs predating it do not), or the comparison came out exactly even. A summed rollup cannot distinguish the two, so it reports no figure rather than claiming zero."
      >
        {NO_ESTIMATE}
      </span>
    )
  }
  const framing = avoidedMoneyFraming(usd)
  return (
    <span
      style={{ color: framing.color }}
      title={`${framing.note} ${moneyPctPhrase(pct)} ${MONEY_PCT_PRECISION_NOTE}`}
    >
      {formatCostScaled(usd)}
      {pct !== null && pct !== undefined && (
        <span className="band-under">{moneyPctCompact(pct)}</span>
      )}
    </span>
  )
}

/** A signed avoided figure with its direction stated, never its absolute. `hint`
 *  distinguishes the two reasons it can be absent: nothing was recorded, or what
 *  was recorded may not be summed across bases. */
function Avoided({
  grams,
  hint = NO_ESTIMATE_HINT,
}: {
  grams: number | null | undefined
  hint?: string
}) {
  const framing = avoidedFraming(grams)
  const missing = grams === null || grams === undefined
  return (
    <span
      style={{ color: missing ? 'var(--text-muted)' : framing.color }}
      title={missing ? hint : framing.note}
    >
      {orDash(formatCo2eScaled(grams))}
    </span>
  )
}

function Section({
  title,
  hint,
  children,
}: {
  title: string
  hint: string
  children: ReactNode
}) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        {title}
      </div>
      <div className="fine-print" style={{ marginBottom: 8 }}>
        {hint}
      </div>
      {children}
    </div>
  )
}

function Stat({
  label,
  value,
  title,
  color,
  sub,
  subTitle,
}: {
  label: string
  value: string
  title?: string
  color?: string
  /** A second line under the figure: the judgment band, or "exact prices". */
  sub?: string | null
  subTitle?: string
}) {
  return (
    <div className="config-stat">
      <div className="mono-label">{label}</div>
      <div className="mono-body" style={{ color }} title={title}>
        {value}
      </div>
      {sub && (
        <span className="band-under" title={subTitle}>
          {sub}
        </span>
      )}
    </div>
  )
}
