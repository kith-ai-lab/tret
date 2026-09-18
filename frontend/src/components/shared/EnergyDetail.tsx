/** A run's carbon estimate and its measured or modeled energy evidence.
 *
 *  Carbon is shown with its judgment band, because a single figure implies a
 *  precision this model does not have. Money is shown exactly, because per-token
 *  prices are published and that arithmetic is not an estimate — the distinction
 *  is stated on the figures themselves rather than left for the reader to guess.
 */
import type { EnergyAccounting } from '../../api/client'
import { EmissionsCalc, ScopeBar } from './EmissionsCalc'
import { MethodologyLink } from './MethodologyDialog'
import {
  BAND_LABEL,
  BAND_SHORT,
  ESTIMATE_NOTE,
  GRID_NO_INFERENCE_NOTE,
  MONEY_PCT_PRECISION_NOTE,
  SCOPE_META,
  avoidedFraming,
  avoidedMoneyFraming,
  coarseComparison,
  gridBasisLabel,
  gridBasisWhat,
  gridSourceLabel,
  gridSourceWhat,
  moneyPctPhrase,
} from './emissions'
import {
  NO_ESTIMATE,
  formatCo2e,
  formatCo2eBand,
  formatCostSigned,
  formatFactor,
  formatWh,
  formatWhBand,
  orDash,
} from './format'

export function EnergyDetail({ energy }: { energy: EnergyAccounting }) {
  const scopes = energy.scopes
  const baseline = energy.baseline
  const band = energy.uncertainty
  const framing = avoidedFraming(baseline?.avoided_co2e_g)
  const comparison = coarseComparison(energy.co2e_g, baseline?.co2e_g)
  const avoidedUsd = energy.cost?.avoided_usd ?? baseline?.avoided_usd
  const avoidedUsdPct = energy.cost?.avoided_pct ?? baseline?.avoided_usd_pct
  const money = avoidedMoneyFraming(avoidedUsd)
  const totalWh = energy.energy_wh_total ?? energy.energy_wh

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
        <div className="mono-label">Carbon footprint</div>
        <span className="badge badge-gray" title={ESTIMATE_NOTE}>
          estimated
        </span>
        {band && (
          <span className="badge badge-gray" title={band.basis}>
            {BAND_LABEL}, not a confidence interval
          </span>
        )}
        <span style={{ flex: 1 }} />
        <span className="fine-print">
          method: <MethodologyLink energy={energy} />
        </span>
      </div>

      <div className="config-stats" style={{ gap: 30 }}>
        <Stat
          label={energy.coverage?.complete_total === null ? 'CO₂e covered subtotal (est.)' : 'CO₂e total (est.)'}
          value={orDash(formatCo2e(energy.co2e_g))}
          sub={formatCo2eBand(band?.co2e_g_low, band?.co2e_g_high)}
          subTitle={BAND_SHORT}
          title="The run total, equal to the sum of its three GHG Protocol scopes."
        />
        <Stat
          label="Energy used for carbon"
          value={orDash(formatWh(totalWh))}
          sub={formatWhBand(band?.energy_wh_total_low, band?.energy_wh_total_high)}
          subTitle={BAND_SHORT}
          title={
            energy.pue_applied === false
              ? 'No additional PUE was applied. Facility readings already include overhead; partial or unknown readings lack a safe conversion boundary.'
              : `Recorded energy with PUE ${energy.pue ?? 'unknown'}. Missing host components remain outside its coverage.`
          }
        />
        <Stat
          label={`Energy (${energy.energy_source ?? 'legacy'})`}
          value={orDash(formatWh(energy.energy_wh))}
          sub={`Coverage: ${(energy.energy_boundary ?? 'unknown').replace(/_/g, ' ')}`}
          title="Energy within the recorded boundary. GPU readings exclude CPU, memory and other host components."
        />
        <Stat
          label="Energy class"
          // Null on a run that used several models with different classes. An
          // em-dash is the honest answer there — asserting one segment's class
          // over the other's tokens would be off by an order of magnitude.
          value={energy.energy_class ?? NO_ESTIMATE}
          title="Calibrated order-of-magnitude bucket, not a measurement. Classes are roughly 2-5x apart."
        />
        <Stat label="Deployment" value={energy.deployment ?? NO_ESTIMATE} />
        <Stat
          label="Grid intensity (est.)"
          value={
            energy.grid_co2e_g_per_kwh === undefined || energy.grid_co2e_g_per_kwh === null
              ? NO_ESTIMATE
              : `${formatFactor(energy.grid_co2e_g_per_kwh, 2)} gCO₂e/kWh`
          }
          // The operator's own label when they gave one, otherwise which rule
          // chose the factor. Never a guessed source on a run that recorded none.
          sub={
            energy.grid_co2e_label ??
            (energy.grid_co2e_source ? gridSourceLabel(energy.grid_co2e_source) : undefined)
          }
          subTitle={`${gridSourceWhat(energy.grid_co2e_source)} ${GRID_NO_INFERENCE_NOTE}`}
          title={`The factor applied to this run's electricity, as recorded when it ran — ${gridBasisLabel(
            energy.grid_co2e_basis,
          )} (${gridBasisWhat(energy.grid_co2e_basis)})`}
        />
        <Stat
          label={framing.label}
          value={orDash(formatCo2e(baseline?.avoided_co2e_g))}
          color={framing.color}
          sub={comparison.tone === 'unknown' ? undefined : comparison.text}
          subTitle={comparison.note}
          title={`${framing.note} Baseline: ${baseline?.model ?? 'none resolved'}.`}
        />
        <Stat
          label={money.label}
          value={formatCostSigned(avoidedUsd)}
          color={money.color}
          sub={avoidedUsd === null || avoidedUsd === undefined ? undefined : moneyPctPhrase(avoidedUsdPct)}
          subTitle={MONEY_PCT_PRECISION_NOTE}
          title={`${money.note} ${MONEY_PCT_PRECISION_NOTE}`}
        />
      </div>

      {energy.coverage && (
        <details className="tool-row" style={{ marginTop: 12 }}>
          <summary>Accounting coverage · {energy.coverage.functional_unit.replace(/_/g, ' ')}</summary>
          <div className="fine-print" style={{ padding: 10 }}>
            {energy.coverage.complete_total === null && (
              <p>A complete lifecycle total is unavailable. Missing: {energy.coverage.missing.map(v => v.replace(/_/g, ' ')).join(', ')}.</p>
            )}
            <ul>{energy.coverage.components.map(component => (
              <li key={component.component_id}>
                {component.component_id.replace(/_/g, ' ')}: {component.status}
                {component.value === null ? ' · unknown amount' : ` · ${component.value.toPrecision(3)} ${component.unit}`}
              </li>
            ))}</ul>
            <p>Grid: {energy.grid_factor_boundary ?? 'unknown boundary'}; {energy.grid_gas_coverage ?? 'unknown gases'};
              {' '}observation year {energy.grid_observation_year ?? 'unknown'}. Standards conformity has not been established.</p>
          </div>
        </details>
      )}
      {energy.energy_method_shadow && (
        <p className="fine-print" style={{ marginTop: 12 }}>
          A legacy estimate is retained for method comparison. Changes caused by the corrected method are not emissions savings.
        </p>
      )}

      {scopes && (
        <div style={{ marginTop: 12 }}>
          <ScopeBar values={scopes} />
          <div className="row" style={{ gap: 16, marginTop: 6, flexWrap: 'wrap' }}>
            {SCOPE_META.map((meta) => (
              <span key={meta.key} className="fine-print" title={meta.what}>
                <span className="swatch" style={{ background: meta.color }} />
                {meta.label} {orDash(formatCo2e(scopes[meta.key]))}
              </span>
            ))}
          </div>
        </div>
      )}

      <details className="tool-row" style={{ marginTop: 12 }}>
        <summary>
          <span className="tool-name">step-by-step derivation</span>
          <span style={{ color: 'var(--text-muted)', fontSize: 10.5 }}>
            weighted tokens → Wh/Mtok → compute Wh → x PUE → x grid intensity → scopes → provenance
          </span>
        </summary>
        <div style={{ padding: '10px 10px 12px' }}>
          <EmissionsCalc energy={energy} />
        </div>
      </details>
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
  /** A second line under the figure — the band, or the coarse comparison. */
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
