import { Fragment } from 'react'
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
 *  figure (total minus embodied), the scope-sum check and the baseline ratio, all
 *  identities over returned values.
 *
 *  Two things this file is careful about, because both were wrong before:
 *
 *  1. **The derivation is generated from the payload, never written out here.**
 *     The token buckets no longer weigh the same — input is 1/20 of an output
 *     token, a cache read a tenth of that again — so a hardcoded
 *     "input + output + …" string was both wrong and unfixable-in-place. The
 *     weights come from the run's own `factors`, the counts from `tokens`, and the
 *     per-bucket energy from `energy_wh_by_bucket`. A run recorded before the
 *     split says so instead of borrowing today's weights.
 *  2. **Carbon appears with its range.** A single figure implies precision this
 *     model does not have; the range is a judgment band and is labelled as one.
 */
import type { EmissionsFactor, EnergyAccounting, TokenBucket } from '../../api/client'
import { CaveatList, FactorTable, SensitivityTable } from './FactorProvenance'
import { MethodologyLink } from './MethodologyDialog'
import {
  BAND_LABEL,
  BAND_SHORT,
  COUNTERFACTUAL_NOTE,
  ESTIMATE_NOTE,
  GRID_NO_INFERENCE_NOTE,
  MONEY_EXACT_NOTE,
  MONEY_PCT_PRECISION_NOTE,
  SCOPE_META,
  TOKEN_BUCKET_LABELS,
  avoidedFraming,
  avoidedMoneyFraming,
  coarseComparison,
  gridBasisLabel,
  gridSourceLabel,
  gridSourceWhat,
  moneyPctPhrase,
  share,
} from './emissions'
import {
  NO_ESTIMATE,
  NO_ESTIMATE_HINT,
  formatCo2e,
  formatCo2eBand,
  formatCostSigned,
  formatFactor,
  formatTokens,
  formatWh,
  formatWhBand,
  orDash,
} from './format'

interface Step {
  n: number
  step: string
  derivation: string
  result: string
  /** The judgment band on this step's result, when the run recorded one. */
  band?: string | null
  source: string
  sourceHint?: string
  muted?: boolean
}

const BUCKETS: TokenBucket[] = ['input', 'output', 'cache_read', 'cache_write']

/** Where each factor comes from, spelled out once under the table. */
const FACTOR_SOURCES =
  'Factor sources — every value below is read from this run’s own stored accounting, with its source, date and confidence marker in the provenance table further down. ' +
  '“catalog” values ship with bench (energy classes calibrated against published per-model measurements, overridable per model in models.yaml); ' +
  '“instance setting” values are operator-configured and fall back to bench’s documented defaults when unset. This run shows the values that were in ' +
  'force when it ran — later changes to a setting do not rewrite it.'

/** A token weight as the run recorded it: preferring the provenance record, then
 *  the top-level key, and reporting nothing rather than a guess. */
function recordedWeight(
  factors: EmissionsFactor[] | undefined,
  key: string,
  fallback: number | undefined,
): number | undefined {
  const factor = factors?.find((f) => f.key === key)
  if (typeof factor?.value === 'number') return factor.value
  return fallback
}

/** The four bucket weights, as recorded. `undefined` where the run has none. */
function bucketWeights(energy: EnergyAccounting): Partial<Record<TokenBucket, number>> {
  const f = energy.factors
  return {
    input: recordedWeight(f, 'token_weight_input', energy.input_weight),
    output: recordedWeight(f, 'token_weight_output', energy.output_weight),
    // ?? undefined, not ?? 0: a multi-model roll-up nulls these where the
    // segments disagreed, and `recordedWeight` already renders "no recorded
    // weight" for undefined. Coercing to 0 would claim these tokens were free.
    cache_read: recordedWeight(f, 'token_weight_cache_read', energy.cache_read_weight ?? undefined),
    cache_write: recordedWeight(
      f,
      'token_weight_cache_write',
      energy.cache_write_weight ?? undefined,
    ),
  }
}

/** The weighted-token derivation, written from the payload.
 *
 *  Reads "0.05 x 1,240 input + 1 x 412 output" — the real weighting, in the
 *  run's own numbers. A run that recorded no input weight predates the split and
 *  says so, because for those runs the buckets genuinely were weighted equally
 *  and printing today's weights would misreport history. */
function weightedTokenDerivation(energy: EnergyAccounting): string {
  const tokens = energy.tokens
  const weights = bucketWeights(energy)
  if (!tokens || weights.input === undefined || weights.output === undefined) {
    return energy.input_weight === undefined
      ? 'input + output + weighted cache buckets, as recorded — this run predates the separate input/output weighting'
      : 'as recorded — the per-bucket token counts were not stored on this run'
  }
  const parts = BUCKETS.filter((b) => (tokens[b] ?? 0) > 0).map((b) => {
    const weight = weights[b]
    const shown = weight === undefined ? '?' : formatFactor(weight, 4)
    return `${shown} x ${formatTokens(tokens[b])} ${TOKEN_BUCKET_LABELS[b]}`
  })
  if (parts.length === 0) return 'no tokens recorded on this run'
  return parts.join(' + ')
}

function buildSteps(energy: EnergyAccounting): Step[] {
  const weighted = energy.weighted_tokens
  const compute = energy.energy_wh
  const pue = energy.pue
  const total = energy.energy_wh_total
  const grid = energy.grid_co2e_g_per_kwh
  const embodied = energy.embodied_g
  const band = energy.uncertainty
  // Identity over returned figures: the run total less amortized hardware is the
  // part that came from electricity.
  // Non-null by construction: `EmissionsCalc` renders `CrossBasisNotice`
  // instead when there is no total, and this function only builds the
  // derivation of one.
  const electricity = (energy.co2e_g ?? 0) - (embodied ?? 0)
  const energyForGrid = total ?? compute

  const steps: Step[] = [
    {
      n: 1,
      step: 'Weighted tokens',
      derivation: weightedTokenDerivation(energy),
      result: `${formatTokens(weighted)} tok`,
      source: 'provider usage, weighted',
      sourceHint:
        'Token counts as reported by the provider, weighted by how much forward-pass work each bucket costs. The unit is an output-equivalent token: generation pays a full forward pass per token, prefill processes the prompt in parallel, a cache read re-uses stored state, and a cache write is a full prefill pass.',
    },
    {
      n: 2,
      step: 'Energy intensity',
      derivation: `energy class ${energy.energy_class} for ${energy.model}`,
      result: `${formatFactor(energy.energy_wh_per_mtok, 1)} Wh / Mtok`,
      source: 'catalog',
      sourceHint:
        'Wh per million output-equivalent tokens, calibrated by least-squares against published per-model figures and then generalised beyond them. Being one class out is the expected failure mode; classes are roughly 2-5x apart.',
    },
    {
      n: 3,
      step: 'Compute energy',
      derivation: `${formatTokens(weighted)} / 1,000,000 x ${formatFactor(energy.energy_wh_per_mtok, 1)} Wh`,
      result: orDash(formatWh(compute)),
      band: formatWhBand(band?.energy_wh_low, band?.energy_wh_high),
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
      band: formatWhBand(band?.energy_wh_total_low, band?.energy_wh_total_high),
      source: `instance setting · ${energy.deployment ?? 'deployment not recorded'}${
        energy.pue_profile ? ` · ${energy.pue_profile.replace(/_/g, ' ')}` : ''
      }`,
      sourceHint:
        'Power Usage Effectiveness: total facility energy divided by IT-load energy, resolved per deployment profile. bench cannot see the facility that served the request, so the cloud default sits above every hyperscaler self-report on purpose.',
    })
  }

  steps.push({
    n: 5,
    step: 'Electricity carbon',
    derivation: `${formatWh(energyForGrid)} / 1,000 x ${formatFactor(grid, 2)} gCO₂e/kWh`,
    result: orDash(formatCo2e(electricity)),
    // Which configuration rule chose the factor, and the operator's own label for
    // it, rather than a generic "instance setting" — with several factors
    // configured, which one applied is the question a reader has.
    source: [
      energy.grid_co2e_source ? gridSourceLabel(energy.grid_co2e_source) : 'instance setting',
      gridBasisLabel(energy.grid_co2e_basis),
      energy.grid_co2e_label ? `“${energy.grid_co2e_label}”` : null,
    ]
      .filter((part): part is string => Boolean(part))
      .join(' · '),
    sourceHint: `Grid intensity as configured when this run happened, with its GHG Protocol basis. Location-based and market-based factors answer different questions and must never be summed. A region- or supplier-specific factor is the single biggest improvement available here. ${gridSourceWhat(
      energy.grid_co2e_source,
    )} ${GRID_NO_INFERENCE_NOTE}`,
  })

  if (embodied !== undefined && embodied > 0) {
    steps.push({
      n: 6,
      step: 'Embodied hardware',
      derivation: 'amortized manufacturing carbon per self-hosted run',
      result: orDash(formatCo2e(embodied)),
      source: 'instance setting',
      sourceHint:
        'GHG Protocol Scope 3 Cat. 2 (capital goods). Counted only for self-hosted inference; 0 unless the operator set it, which understates local runs. Its provenance is marked "placeholder" for a reason — see the factor table.',
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
    band: formatCo2eBand(band?.co2e_g_low, band?.co2e_g_high),
    source: 'derived',
    sourceHint: 'The run total, and by construction the sum of the three scopes below.',
  })

  return steps
}

/** Why a carbon figure is missing, when it is missing on purpose.
 *
 *  A run whose segments were accounted under different GHG Protocol bases has no
 *  single carbon total, because location-based and market-based grams answer
 *  different questions and adding them is a category error rather than a
 *  rounding one. The subtotals are real and are shown; the total is not offered.
 *  Rendering the derivation chain here would be worse than useless — it would
 *  walk through arithmetic the backend deliberately declined to perform. */
export function CrossBasisNotice({ energy }: { energy: EnergyAccounting }) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Carbon not summed — this run spans two accounting bases
      </div>
      <div
        style={{
          marginBottom: 8,
          fontFamily: 'var(--mono)',
          fontSize: 10.5,
          color: 'var(--text-muted)',
        }}
      >
        Parts of this run were accounted {(energy.grid_bases ?? []).filter(Boolean).join(' and ')}.
        Those answer different questions and may not be added, so there is no run total. Energy,
        tokens and cost are unaffected. Per-basis subtotals:
      </div>
      <div className="kv">
        {(energy.by_basis ?? []).map((row) => (
          <Fragment key={row.grid_co2e_basis ?? 'unspecified'}>
            <span className="k">{row.grid_co2e_basis ?? 'unspecified'}</span>
            <span>
              {row.co2e_g === null ? NO_ESTIMATE : `${formatFactor(row.co2e_g, 3)} gCO₂e`} ·{' '}
              {row.energy_wh === null ? NO_ESTIMATE : `${formatFactor(row.energy_wh, 3)} Wh`} ·{' '}
              {row.models.filter(Boolean).join(', ')}
            </span>
          </Fragment>
        ))}
      </div>
    </div>
  )
}

export function EmissionsCalc({ energy }: { energy: EnergyAccounting }) {
  // No total to derive. Everything below this point walks the arithmetic that
  // produced one.
  if (energy.co2e_g === null) return <CrossBasisNotice energy={energy} />
  const steps = buildSteps(energy)
  const hasProvenance = Boolean(energy.factors?.length || energy.caveats?.length)
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Derivation — every figure estimated
        </div>
        <div className="md-table-wrap">
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
                  <td className="num">
                    {s.result}
                    {s.band && (
                      <span className="band-under" title={BAND_SHORT}>
                        {s.band}
                      </span>
                    )}
                  </td>
                  <td style={{ color: 'var(--text-muted)' }} title={s.sourceHint}>
                    {s.source}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="fine-print" style={{ marginTop: 6 }}>
          {FACTOR_SOURCES}
          {/* Only explain the second line when there is one: a run recorded
              before the band carries no range, and saying otherwise would imply
              a figure it does not have. */}
          {energy.uncertainty && (
            <>
              <br />
              Second figures under a result are the {BAND_LABEL}. {BAND_SHORT}
            </>
          )}
        </div>
      </div>

      <TokenBuckets energy={energy} />

      {energy.scopes && <RunScopes energy={energy} />}
      {energy.baseline && <RunBaseline energy={energy} />}

      {hasProvenance && (
        <details className="tool-row">
          <summary>
            <span className="tool-name">factor provenance, caveats and sensitivity</span>
            <span style={{ color: 'var(--text-muted)', fontSize: 10.5 }}>
              {formatTokens(energy.factors?.length ?? 0)} factors ·{' '}
              {formatTokens(energy.caveats?.length ?? 0)} named biases
            </span>
          </summary>
          <div className="stack" style={{ gap: 16, padding: '10px 10px 12px' }}>
            <div>
              <div className="mono-label" style={{ marginBottom: 4 }}>
                Every constant this run used, and where it came from
              </div>
              <FactorTable factors={energy.factors ?? []} />
            </div>
            {energy.uncertainty && (
              <div>
                <div className="mono-label" style={{ marginBottom: 4 }}>
                  What drives the range
                </div>
                <SensitivityTable uncertainty={energy.uncertainty} />
              </div>
            )}
            <div>
              <div className="mono-label" style={{ marginBottom: 4 }}>
                Known biases on this run
              </div>
              <CaveatList caveats={energy.caveats ?? []} />
            </div>
          </div>
        </details>
      )}

      <div className="fine-print">
        {energy.basis}
        <br />
        {ESTIMATE_NOTE} Method: <MethodologyLink energy={energy} />
      </div>
    </div>
  )
}

/** The token buckets and what each contributed, which is the whole reason the
 *  derivation above cannot be a fixed string any more. Rendered only when the run
 *  recorded the split; a legacy run simply does not get this table. */
function TokenBuckets({ energy }: { energy: EnergyAccounting }) {
  const tokens = energy.tokens
  const byBucket = energy.energy_wh_by_bucket
  if (!tokens) return null
  const weights = bucketWeights(energy)
  const bucketSum = byBucket
    ? BUCKETS.reduce((total, b) => total + (byBucket[b] ?? 0), 0)
    : null
  // The backend guarantees the buckets sum to energy_wh; check it, don't trust it.
  const sumMatches = bucketSum === null ? null : Math.abs(bucketSum - energy.energy_wh) < 1e-6
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Token buckets — not equally expensive
      </div>
      <div className="md-table-wrap">
        <table className="mono-table">
          <thead>
            <tr>
              <th>Bucket</th>
              <th className="num">Tokens</th>
              <th className="num">Weight</th>
              <th className="num">Output-equiv.</th>
              <th className="num">Compute Wh (est.)</th>
            </tr>
          </thead>
          <tbody>
            {BUCKETS.map((bucket) => {
              const count = tokens[bucket] ?? 0
              const weight = weights[bucket]
              return (
                <tr key={bucket} style={count === 0 ? { color: 'var(--text-muted)' } : undefined}>
                  <td>{TOKEN_BUCKET_LABELS[bucket]}</td>
                  <td className="num">{formatTokens(count)}</td>
                  <td className="num">{weight === undefined ? NO_ESTIMATE : formatFactor(weight, 4)}</td>
                  <td className="num">
                    {weight === undefined ? NO_ESTIMATE : formatTokens(Math.round(count * weight))}
                  </td>
                  <td className="num">{orDash(formatWh(byBucket?.[bucket]))}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="fine-print" style={{ marginTop: 6 }}>
        {energy.output_to_input_energy_ratio !== undefined && (
          <>
            An output token is treated as {formatFactor(energy.output_to_input_energy_ratio, 0)}x an
            input token: prefill runs in parallel, generation pays a full forward pass per token.{' '}
          </>
        )}
        {sumMatches !== null && (
          <span style={{ color: sumMatches ? undefined : 'var(--amber)' }}>
            {sumMatches
              ? `Buckets sum to ${orDash(formatWh(bucketSum))}, matching compute energy, as the accounting guarantees.`
              : `Buckets sum to ${orDash(formatWh(bucketSum))}, which does NOT match this run’s compute energy — treat the split as unreliable.`}
          </span>
        )}
      </div>
    </div>
  )
}

/** The run's GHG Protocol split, with Scope 1 shown at zero on purpose. */
function RunScopes({ energy }: { energy: EnergyAccounting }) {
  const scopes = energy.scopes
  if (!scopes) return null
  if (
    energy.co2e_g === null ||
    scopes.scope1_g === null ||
    scopes.scope2_g === null ||
    scopes.scope3_g === null
  ) {
    // Scope figures are carbon and follow the same rule as the total.
    return null
  }
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
      <div className="md-table-wrap">
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
                  <td className="num">
                    {value === null ? orDash(null) : `${share(value, sum).toFixed(1)}%`}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
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

/** The frontier-baseline counterfactual for one run, in carbon and in money.
 *
 *  The carbon comparison is stated coarsely — it is the ratio of two estimated
 *  constants, so "98.3% lighter" claimed a precision the inputs never had. The
 *  money comparison is stated exactly, because per-token prices are published and
 *  the arithmetic is not an estimate. Both are signed, and both are efficiency
 *  indicators rather than savings. */
function RunBaseline({ energy }: { energy: EnergyAccounting }) {
  const baseline = energy.baseline
  if (!baseline) return null
  const band = energy.uncertainty
  const framing = avoidedFraming(baseline.avoided_co2e_g)
  const comparison = coarseComparison(energy.co2e_g, baseline.co2e_g)
  const cost = energy.cost
  const avoidedUsd = cost?.avoided_usd ?? baseline.avoided_usd
  const avoidedUsdPct = cost?.avoided_pct ?? baseline.avoided_usd_pct
  const money = avoidedMoneyFraming(avoidedUsd)
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
            label="Difference (order of magnitude)"
            value={comparison.text}
            color={comparison.color}
            title={comparison.note}
          />
          <Stat
            label={money.label}
            value={formatCostSigned(avoidedUsd)}
            color={money.color}
            title={`${money.note} ${MONEY_PCT_PRECISION_NOTE}`}
            sub={
              avoidedUsd === null || avoidedUsd === undefined
                ? undefined
                : `${moneyPctPhrase(avoidedUsdPct)} · ${formatCostSigned(cost?.usd)} vs ${formatCostSigned(
                    cost?.baseline_usd ?? baseline.cost_usd,
                  )}`
            }
            subTitle={MONEY_PCT_PRECISION_NOTE}
          />
        </div>
      )}
      <div className="fine-print" style={{ marginTop: 6 }}>
        {framing.note} {COUNTERFACTUAL_NOTE}
        <br />
        {baseline.basis}
        {(cost?.basis || avoidedUsd !== null) && (
          <>
            <br />
            {cost?.basis ?? MONEY_EXACT_NOTE}
          </>
        )}
        {band && (
          <>
            <br />
            Carbon figures on this panel carry a {BAND_LABEL}: {band.basis}
          </>
        )}
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
  // Nullable, because a roll-up spanning two GHG Protocol bases withholds its
  // scope figures rather than adding grams accounted different ways. A withheld
  // figure is not zero, and the bar must not draw it as an empty slice.
  values: { scope1_g: number | null; scope2_g: number | null; scope3_g: number | null }
  height?: number
}) {
  if (values.scope1_g === null || values.scope2_g === null || values.scope3_g === null) {
    return (
      <div
        className="stacked-bar"
        style={{ height }}
        title="Carbon was not summed for this run: it spans more than one GHG Protocol basis."
      >
        <span style={{ width: '100%', background: 'var(--bg-input)' }} />
      </div>
    )
  }
  // Re-bound after the guard above so the indexed access below is a number.
  // Narrowing a property does not narrow `values[key]`.
  const solid = {
    scope1_g: values.scope1_g,
    scope2_g: values.scope2_g,
    scope3_g: values.scope3_g,
  }
  const total = solid.scope1_g + solid.scope2_g + solid.scope3_g
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
        const pct = share(solid[meta.key], total)
        return pct > 0 ? (
          <span
            key={meta.key}
            title={`${meta.label}: ${formatCo2e(solid[meta.key])} (${pct.toFixed(1)}%)`}
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
  sub,
  subTitle,
}: {
  label: string
  value: string
  title?: string
  color?: string
  sub?: string
  subTitle?: string
}) {
  return (
    <div className="config-stat">
      <div className="mono-label">{label}</div>
      <div className="mono-body" style={{ color }} title={title}>
        {value}
      </div>
      {sub && (
        <div className="band-under" title={subTitle}>
          {sub}
        </div>
      )}
    </div>
  )
}
