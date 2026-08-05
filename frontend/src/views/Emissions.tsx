/** Emissions — the estimated carbon footprint of everything bench has run.
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
  type EmissionsByDay,
  type EmissionsByHarness,
  type EmissionsByModel,
  type EmissionsRecordedFactor,
  type EmissionsTotals,
} from '../api/client'
import { ScopeBar } from '../components/shared/EmissionsCalc'
import {
  COUNTERFACTUAL_NOTE,
  ESTIMATE_NOTE,
  METHODOLOGY_DOC,
  MIXED_FACTORS_CAVEAT,
  SCOPE_META,
  avoidedFraming,
  share,
} from '../components/shared/emissions'
import {
  NO_ESTIMATE,
  NO_ESTIMATE_HINT,
  co2eScaleFor,
  formatCo2e,
  formatCo2eAt,
  formatCo2eScaled,
  formatEnergyScaled,
  formatFactor,
  formatPct,
  formatTokens,
  orDash,
} from '../components/shared/format'
import { type Column, MonoTable } from '../components/shared/MonoTable'

const WINDOWS = [7, 30, 90, 365]

/** A rollup's avoided figure, or null when nothing in it had a baseline at all.
 *  A comparison that never happened is not a comparison that came out even, so it
 *  reads as "no figure" rather than as zero. */
function bucketAvoided(b: { baseline_co2e_g: number; avoided_co2e_g: number }): number | null {
  return b.baseline_co2e_g === 0 && b.avoided_co2e_g === 0 ? null : b.avoided_co2e_g
}

/** Heavier class, hotter badge. An editorial cue over the backend's own class. */
const CLASS_BADGE: Record<string, string> = {
  S: 'badge-green',
  M: 'badge-blue',
  L: 'badge-amber',
  XL: 'badge-red',
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
          The estimated carbon footprint of bench's own compute: energy, the GHG Protocol scope
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
              <ScopeSplit totals={data.totals} />
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
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="callout callout-note">
        <span className="callout-title">Read before quoting any figure on this page</span>
        {data.disclaimer}
        <div style={{ marginTop: 6 }}>
          Methodology: <code>{METHODOLOGY_DOC}</code>
        </div>
      </div>
      {data.factors.mixed_factors && (
        <div className="callout callout-warn">
          <span className="callout-title">Mixed emission factors in this window</span>
          {MIXED_FACTORS_CAVEAT}
        </div>
      )}
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

function TotalsStrip({ totals }: { totals: EmissionsTotals }) {
  const avoided = bucketAvoided(totals)
  const framing = avoidedFraming(avoided)
  return (
    <div className="panel config-stats" style={{ gap: 34 }}>
      <Stat
        label="Total CO₂e (est.)"
        value={orDash(formatCo2eScaled(totals.co2e_g))}
        title="Summed from each run's as-recorded figure, never recomputed at today's factors."
      />
      <Stat
        label="Total energy (est.)"
        value={orDash(formatEnergyScaled(totals.energy_wh))}
        title={`Includes facility overhead. Compute only: ${orDash(formatEnergyScaled(totals.energy_wh_compute))}.`}
      />
      <Stat
        label="Compute energy (est.)"
        value={orDash(formatEnergyScaled(totals.energy_wh_compute))}
        title="IT load, before data-centre overhead."
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
      <Stat
        label={framing.label}
        value={orDash(formatCo2eScaled(avoided))}
        color={framing.color}
        title={`${framing.note} ${COUNTERFACTUAL_NOTE}`}
      />
    </div>
  )
}

// ── scopes ───────────────────────────────────────────────────────────────

function ScopeSplit({ totals }: { totals: EmissionsTotals }) {
  const scoped = totals.scope1_g + totals.scope2_g + totals.scope3_g
  const gap = totals.co2e_g - scoped
  return (
    <Section
      title="GHG Protocol scopes"
      hint="Where the estimated carbon lands from the bench operator's point of view. Scope 1 is always zero and is shown as an explicit zero — an omitted line would read as an oversight."
    >
      <div className="panel">
        <ScopeBar
          values={{
            scope1_g: totals.scope1_g,
            scope2_g: totals.scope2_g,
            scope3_g: totals.scope3_g,
          }}
          height={12}
        />
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
              const value = totals[meta.key]
              return (
                <tr key={meta.key}>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <span className="swatch" style={{ background: meta.color }} />
                    {meta.label}
                  </td>
                  <td style={{ color: 'var(--text-muted)' }}>{meta.what}</td>
                  <td className="num">{orDash(formatCo2eScaled(value))}</td>
                  <td className="num">{share(value, scoped).toFixed(1)}%</td>
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
          {totals.runs_without_scope_split > 0 ? (
            <>
              {formatTokens(totals.runs_without_scope_split)} run(s) in this window carry a carbon
              figure but no scope split, so the scoped subtotal is{' '}
              {orDash(formatCo2eScaled(gap))} short of the window total (
              {orDash(formatCo2eScaled(totals.co2e_g))}). Those runs are not back-filled with zeros.
            </>
          ) : (
            <>
              The scoped subtotal equals the window total (
              {orDash(formatCo2eScaled(totals.co2e_g))}): every run here carries a split.
            </>
          )}
        </div>
      </div>
    </Section>
  )
}

// ── baseline comparison ──────────────────────────────────────────────────

function BaselineComparison({ data }: { data: EmissionsAnalytics }) {
  const t = data.totals
  const avoided = bucketAvoided(t)
  const framing = avoidedFraming(avoided)
  const baselineModel = data.factors.baseline_model
  const noBaseline = avoided === null
  const scale = Math.max(t.co2e_g, t.baseline_co2e_g)
  // One unit for the pair, so the two bars are read against each other rather
  // than against two different scales.
  const unit = co2eScaleFor(scale)

  return (
    <Section
      title="Actual vs frontier baseline"
      hint="What the same tokens would have cost on the baseline model. An efficiency indicator for model choice — it can come out either way, and a negative result is a surcharge, not a saving."
    >
      <div className="panel stack" style={{ gap: 12 }}>
        {noBaseline ? (
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
                      width: `${share(t.co2e_g, scale)}%`,
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
                      width: `${share(t.baseline_co2e_g, scale)}%`,
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
                label="Difference"
                value={orDash(formatPct(t.avoided_pct))}
                color={framing.color}
                title="Signed share of the baseline total, as reported by the backend."
              />
            </div>
          </>
        )}

        <div className="callout callout-note">
          <span className="callout-title">What this comparison is and is not</span>
          {COUNTERFACTUAL_NOTE}
          {!noBaseline && ` ${framing.note}`}
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
      key: 'co2e',
      header: 'CO₂e (est.)',
      align: 'right',
      render: (r) => orDash(formatCo2eScaled(r.co2e_g)),
    },
    {
      key: 'baseline',
      header: 'Baseline (est.)',
      align: 'right',
      // A bucket with no baseline at all shows no figure, not a zero baseline.
      render: (r) =>
        bucketAvoided(r) === null ? (
          <span title={NO_ESTIMATE_HINT}>{NO_ESTIMATE}</span>
        ) : (
          orDash(formatCo2eScaled(r.baseline_co2e_g))
        ),
    },
    {
      key: 'avoided',
      header: 'Avoided (signed)',
      align: 'right',
      render: (r) => <Avoided grams={bucketAvoided(r)} />,
    },
  ]
  return (
    <Section
      title="By model"
      hint="Model choice moves this number by more than an order of magnitude, which is the whole reason the estimate is worth reporting."
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
      key: 'co2e',
      header: 'CO₂e (est.)',
      align: 'right',
      render: (r) => orDash(formatCo2eScaled(r.co2e_g)),
    },
    {
      key: 'baseline',
      header: 'Baseline (est.)',
      align: 'right',
      // A bucket with no baseline at all shows no figure, not a zero baseline.
      render: (r) =>
        bucketAvoided(r) === null ? (
          <span title={NO_ESTIMATE_HINT}>{NO_ESTIMATE}</span>
        ) : (
          orDash(formatCo2eScaled(r.baseline_co2e_g))
        ),
    },
    {
      key: 'avoided',
      header: 'Avoided (signed)',
      align: 'right',
      render: (r) => <Avoided grams={bucketAvoided(r)} />,
    },
  ]
  return (
    <Section title="By harness" hint="Which workflows account for the window's footprint.">
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
  const maxCo2e = Math.max(...rows.map((r) => r.co2e_g), 0)
  const maxAvoided = Math.max(...rows.map((r) => Math.abs(r.avoided_co2e_g)), 0)
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
              title={`${r.date}: ${orDash(formatCo2e(r.co2e_g))} (est.)`}
            >
              <i style={{ height: `${share(r.co2e_g, maxCo2e)}%` }} />
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
              className={`spark-col ${r.avoided_co2e_g < 0 ? 'neg' : 'pos'}`}
              title={`${r.date}: ${
                r.avoided_co2e_g < 0
                  ? `${orDash(formatCo2e(r.avoided_co2e_g))} — heavier than the baseline`
                  : `${orDash(formatCo2e(r.avoided_co2e_g))} lighter than the baseline`
              } (est.)`}
            >
              <i style={{ height: `${share(Math.abs(r.avoided_co2e_g), maxAvoided)}%` }} />
            </div>
          ))}
        </div>

        <div className="spark-axis">
          <span>{rows[0].date}</span>
          <span>{rows[rows.length - 1].date}</span>
        </div>
        <div className="fine-print" style={{ marginTop: 8 }}>
          Green days ran lighter than the baseline for the same tokens; red days ran heavier. Neither
          is an offset or a credit.
        </div>
      </div>
    </Section>
  )
}

// ── factors ──────────────────────────────────────────────────────────────

function Factors({ data }: { data: EmissionsAnalytics }) {
  const f = data.factors
  const columns: Column<EmissionsRecordedFactor>[] = [
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
      key: 'pue',
      header: 'PUE',
      align: 'right',
      render: (r) => (r.pue === null ? NO_ESTIMATE : formatFactor(r.pue)),
    },
    { key: 'runs', header: 'Runs', align: 'right', render: (r) => formatTokens(r.runs) },
  ]
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
            label="Local grid intensity"
            value={
              f.local_grid_co2e_g_per_kwh === null
                ? 'not set'
                : `${formatFactor(f.local_grid_co2e_g_per_kwh, 2)} gCO₂e/kWh`
            }
            title="Optional site- or market-based factor for self-hosted inference. Unset falls back to the grid intensity."
          />
          <Stat label="Data-centre PUE" value={formatFactor(f.datacenter_pue)} />
          <Stat label="Local PUE" value={formatFactor(f.local_pue)} />
          <Stat label="Baseline model" value={f.baseline_model ?? 'not resolved'} />
        </div>
        <div className="fine-print">{f.note}</div>
        {f.recorded.length > 0 && (
          <details className="tool-row">
            <summary>
              <span className="tool-name">recording bases in this window</span>
              <span style={{ color: 'var(--text-muted)', fontSize: 10.5 }}>
                {f.recorded.length} combination{f.recorded.length === 1 ? '' : 's'} of deployment,
                grid intensity and PUE
              </span>
            </summary>
            <div style={{ padding: '6px 10px 10px' }}>
              <MonoTable
                columns={columns}
                rows={f.recorded}
                rowKey={(r) => `${r.deployment}-${r.grid_co2e_g_per_kwh}-${r.pue}`}
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

/** A signed avoided figure with its direction stated, never its absolute. */
function Avoided({ grams }: { grams: number | null | undefined }) {
  const framing = avoidedFraming(grams)
  return (
    <span
      style={{ color: framing.color }}
      title={grams === null || grams === undefined ? NO_ESTIMATE_HINT : framing.note}
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
}: {
  label: string
  value: string
  title?: string
  color?: string
}) {
  return (
    <div className="config-stat">
      <div className="mono-label">{label}</div>
      <div className="mono-body" style={{ color }} title={title}>
        {value}
      </div>
    </div>
  )
}
