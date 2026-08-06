/** A run's estimated footprint at a glance, with the full derivation one
 *  keystroke away. Everything here is a heuristic estimate — the energy class is
 *  calibrated against five models' inferred hardware, not a meter reading, and the
 *  word "measured" appears nowhere.
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
  SCOPE_META,
  avoidedFraming,
  avoidedMoneyFraming,
  coarseComparison,
} from './emissions'
import {
  NO_ESTIMATE,
  formatCo2e,
  formatCo2eBand,
  formatCostSigned,
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
          label="CO₂e total (est.)"
          value={orDash(formatCo2e(energy.co2e_g))}
          sub={formatCo2eBand(band?.co2e_g_low, band?.co2e_g_high)}
          subTitle={BAND_SHORT}
          title="The run total, equal to the sum of its three GHG Protocol scopes."
        />
        <Stat
          label="Energy total (est.)"
          value={orDash(formatWh(totalWh))}
          sub={formatWhBand(band?.energy_wh_total_low, band?.energy_wh_total_high)}
          subTitle={BAND_SHORT}
          title={
            energy.pue === undefined
              ? 'Compute (IT-load) energy. This run carries no PUE, so no facility overhead is included.'
              : `Compute energy x PUE ${energy.pue} — includes facility overhead.`
          }
        />
        <Stat
          label="Compute only (est.)"
          value={orDash(formatWh(energy.energy_wh))}
          title="IT load, before data-centre overhead."
        />
        <Stat
          label="Energy class"
          value={energy.energy_class}
          title="Calibrated order-of-magnitude bucket, not a measurement. Classes are roughly 2-5x apart."
        />
        <Stat label="Deployment" value={energy.deployment ?? NO_ESTIMATE} />
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
          sub={avoidedUsd === null || avoidedUsd === undefined ? undefined : 'exact prices'}
          subTitle={money.note}
          title={money.note}
        />
      </div>

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
