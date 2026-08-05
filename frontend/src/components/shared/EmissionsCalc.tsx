/** A run's carbon figure, derived one checkable step at a time.
 *
 *  The point of this component is that a skeptical reader can redo the
 *  arithmetic by hand: every factor is printed with its value and where it came
 *  from, every intermediate result is shown, and the two invariants the backend
 *  guarantees (energy_wh_total = compute x PUE, co2e_g = scope1+2+3) are checked
 *  on screen instead of asserted.
 *
 *  Nothing here is a measurement, and nothing here is computed from anything the
 *  backend did not send — the only client-side arithmetic is the electricity
 *  figure (total minus embodied) and the scope-sum check, both identities over
 *  returned values. */
import type { EnergyAccounting } from '../../api/client'
import {
  COUNTERFACTUAL_NOTE,
  ESTIMATE_NOTE,
  METHODOLOGY_DOC,
  SCOPE_META,
  avoidedFraming,
  share,
} from './emissions'
import {
  NO_ESTIMATE,
  NO_ESTIMATE_HINT,
  formatCo2e,
  formatFactor,
  formatPct,
  formatTokens,
  formatWh,
  orDash,
} from './format'

interface Step {
  n: number
  step: string
  derivation: string
  result: string
  source: string
  sourceHint?: string
  muted?: boolean
}

/** Where each factor comes from, spelled out once under the table. */
const FACTOR_SOURCES =
  'Factor sources — “catalog” values ship with bench (heuristic energy-class buckets, overridable per model in models.yaml); ' +
  '“instance setting” values are operator-configured (BENCH_GRID_CO2E_G_PER_KWH, BENCH_DATACENTER_PUE, BENCH_LOCAL_PUE, ' +
  'BENCH_EMBODIED_G_PER_RUN) and fall back to bench’s documented defaults when unset. This run shows the values that were in ' +
  'force when it ran — later changes to a setting do not rewrite it.'

function buildSteps(energy: EnergyAccounting): Step[] {
  const weighted = energy.weighted_tokens
  const compute = energy.energy_wh
  const pue = energy.pue
  const total = energy.energy_wh_total
  const grid = energy.grid_co2e_g_per_kwh
  const embodied = energy.embodied_g
  // Identity over returned figures: the run total less amortized hardware is the
  // part that came from electricity.
  const electricity = energy.co2e_g - (embodied ?? 0)
  const energyForGrid = total ?? compute

  const steps: Step[] = [
    {
      n: 1,
      step: 'Weighted tokens',
      derivation: `input + output + ${formatFactor(energy.cache_read_weight)}x cache read + ${formatFactor(
        energy.cache_write_weight,
      )}x cache write`,
      result: `${formatTokens(weighted)} tok`,
      source: 'provider usage, weighted',
      sourceHint:
        'Token counts as reported by the provider, weighted by how much forward-pass work each bucket costs: a cache read re-uses stored state, a cache write is a full pass.',
    },
    {
      n: 2,
      step: 'Energy intensity',
      derivation: `energy class ${energy.energy_class} for ${energy.model}`,
      result: `${formatFactor(energy.energy_wh_per_mtok, 1)} Wh / Mtok`,
      source: 'catalog',
      sourceHint:
        'An order-of-magnitude bucket, not a measurement: no provider publishes per-model energy draw. Uncertainty here is roughly a factor of two to five.',
    },
    {
      n: 3,
      step: 'Compute energy',
      derivation: `${formatTokens(weighted)} / 1,000,000 x ${formatFactor(energy.energy_wh_per_mtok, 1)} Wh`,
      result: orDash(formatWh(compute)),
      source: 'derived',
      sourceHint: 'IT load only — this figure excludes data-centre overhead.',
    },
  ]

  if (pue === undefined || pue === null) {
    steps.push({
      n: 4,
      step: 'Facility overhead (PUE)',
      derivation: 'not recorded — this run predates PUE accounting',
      result: NO_ESTIMATE,
      source: NO_ESTIMATE,
      sourceHint: NO_ESTIMATE_HINT,
      muted: true,
    })
  } else {
    steps.push({
      n: 4,
      step: 'Facility overhead (PUE)',
      derivation: `${formatWh(compute)} x PUE ${formatFactor(pue)}`,
      result: orDash(formatWh(total)),
      source: `instance setting · ${energy.deployment ?? 'deployment not recorded'}`,
      sourceHint:
        'Power Usage Effectiveness: total facility energy divided by IT-load energy. Heuristic — bench cannot see the facility that served the request.',
    })
  }

  steps.push({
    n: 5,
    step: 'Electricity carbon',
    derivation: `${formatWh(energyForGrid)} / 1,000 x ${formatFactor(grid, 2)} gCO₂e/kWh`,
    result: orDash(formatCo2e(electricity)),
    source: 'instance setting',
    sourceHint:
      'Grid intensity as configured when this run happened. The default is a rough world average; a region- or supplier-specific factor is less wrong.',
  })

  if (embodied !== undefined && embodied > 0) {
    steps.push({
      n: 6,
      step: 'Embodied hardware',
      derivation: 'amortized manufacturing carbon per self-hosted run',
      result: orDash(formatCo2e(embodied)),
      source: 'instance setting',
      sourceHint:
        'GHG Protocol Scope 3 Cat. 2 (capital goods). Counted only for self-hosted inference; 0 unless the operator set it, which understates local runs.',
    })
  }

  steps.push({
    n: steps.length + 1,
    step: 'Run total (est.)',
    derivation:
      embodied !== undefined && embodied > 0
        ? 'electricity + embodied hardware'
        : 'electricity carbon',
    result: orDash(formatCo2e(energy.co2e_g)),
    source: 'derived',
    sourceHint: 'The run total, and by construction the sum of the three scopes below.',
  })

  return steps
}

export function EmissionsCalc({ energy }: { energy: EnergyAccounting }) {
  const steps = buildSteps(energy)
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Derivation — every figure estimated
        </div>
        <table className="mono-table">
          <thead>
            <tr>
              <th style={{ width: 28 }}>#</th>
              <th>Step</th>
              <th>How it is derived</th>
              <th className="num">Result (est.)</th>
              <th>Factor source</th>
            </tr>
          </thead>
          <tbody>
            {steps.map((s) => (
              <tr key={s.n} style={s.muted ? { color: 'var(--text-muted)' } : undefined}>
                <td style={{ color: 'var(--text-muted)' }}>{s.n}</td>
                <td>{s.step}</td>
                <td style={{ color: 'var(--text-muted)' }}>{s.derivation}</td>
                <td className="num">{s.result}</td>
                <td style={{ color: 'var(--text-muted)' }} title={s.sourceHint}>
                  {s.source}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="fine-print" style={{ marginTop: 6 }}>
          {FACTOR_SOURCES}
        </div>
      </div>

      {energy.scopes && <RunScopes energy={energy} />}
      {energy.baseline && <RunBaseline baseline={energy.baseline} />}

      <div className="fine-print">
        {energy.basis}
        <br />
        {ESTIMATE_NOTE} Method: <code>{METHODOLOGY_DOC}</code>
      </div>
    </div>
  )
}

/** The run's GHG Protocol split, with Scope 1 shown at zero on purpose. */
function RunScopes({ energy }: { energy: EnergyAccounting }) {
  const scopes = energy.scopes
  if (!scopes) return null
  const sum = scopes.scope1_g + scopes.scope2_g + scopes.scope3_g
  // The backend guarantees co2e_g == scope1+2+3; check it rather than trust it.
  const matches = Math.abs(sum - energy.co2e_g) < 1e-6
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        GHG Protocol scopes (est.)
      </div>
      <ScopeBar
        values={{ scope1_g: scopes.scope1_g, scope2_g: scopes.scope2_g, scope3_g: scopes.scope3_g }}
      />
      <table className="mono-table" style={{ marginTop: 8 }}>
        <thead>
          <tr>
            <th>Scope</th>
            <th>What it covers</th>
            <th className="num">gCO₂e (est.)</th>
            <th className="num">Share</th>
          </tr>
        </thead>
        <tbody>
          {SCOPE_META.map((meta) => {
            const value = scopes[meta.key]
            return (
              <tr key={meta.key}>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <span className="swatch" style={{ background: meta.color }} />
                  {meta.label}
                </td>
                <td style={{ color: 'var(--text-muted)' }}>{meta.what}</td>
                <td className="num">{orDash(formatCo2e(value))}</td>
                <td className="num">{share(value, sum).toFixed(1)}%</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="fine-print" style={{ marginTop: 6 }}>
        Sum {orDash(formatCo2e(sum))} ·{' '}
        <span style={{ color: matches ? undefined : 'var(--amber)' }}>
          {matches
            ? 'matches the run total, as the accounting guarantees'
            : 'does NOT match the run total — treat this run’s split as unreliable'}
        </span>
        <br />
        {scopes.basis}
      </div>
    </div>
  )
}

/** The frontier-baseline counterfactual for one run. */
function RunBaseline({ baseline }: { baseline: NonNullable<EnergyAccounting['baseline']> }) {
  const framing = avoidedFraming(baseline.avoided_co2e_g)
  const unavailable = baseline.co2e_g === null || baseline.co2e_g === undefined
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Frontier baseline — same-token counterfactual
      </div>
      {unavailable ? (
        <div className="empty" style={{ padding: '4px 0' }}>
          No baseline comparison for this run.
        </div>
      ) : (
        <div className="config-stats" style={{ gap: 28 }}>
          <Stat label="Baseline model" value={baseline.model ?? NO_ESTIMATE} />
          <Stat label="Baseline class" value={baseline.energy_class ?? NO_ESTIMATE} />
          <Stat
            label="Baseline energy (est.)"
            value={orDash(formatWh(baseline.energy_wh_total ?? baseline.energy_wh))}
            title="The same tokens through the baseline model, including its facility overhead."
          />
          <Stat label="Baseline carbon (est.)" value={orDash(formatCo2e(baseline.co2e_g))} />
          <Stat
            label={framing.label}
            value={orDash(formatCo2e(baseline.avoided_co2e_g))}
            color={framing.color}
            title={framing.note}
          />
          <Stat
            label="Difference"
            value={orDash(formatPct(baseline.avoided_pct))}
            color={framing.color}
            title={framing.note}
          />
        </div>
      )}
      <div className="fine-print" style={{ marginTop: 6 }}>
        {framing.note} {COUNTERFACTUAL_NOTE}
        <br />
        {baseline.basis}
      </div>
    </div>
  )
}

/** Stacked scope bar. Zero-width scopes simply contribute nothing to the bar —
 *  the table beneath is what states their value. */
export function ScopeBar({
  values,
  height = 10,
}: {
  values: { scope1_g: number; scope2_g: number; scope3_g: number }
  height?: number
}) {
  const total = values.scope1_g + values.scope2_g + values.scope3_g
  if (total <= 0) {
    return (
      <div className="stacked-bar" style={{ height }} title="No carbon attributed to any scope.">
        <span style={{ width: '100%', background: 'var(--bg-input)' }} />
      </div>
    )
  }
  return (
    <div className="stacked-bar" style={{ height }} role="img" aria-label="Scope split">
      {SCOPE_META.map((meta) => {
        const pct = share(values[meta.key], total)
        return pct > 0 ? (
          <span
            key={meta.key}
            title={`${meta.label}: ${formatCo2e(values[meta.key])} (${pct.toFixed(1)}%)`}
            style={{ width: `${pct}%`, background: meta.color, opacity: 0.8 }}
          />
        ) : null
      })}
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
