/** A run's estimated footprint at a glance, with the full derivation one
 *  keystroke away. Everything here is a heuristic estimate — the energy class is
 *  a bucket, not a meter reading, and the word "measured" appears nowhere. */
import type { EnergyAccounting } from '../../api/client'
import { EmissionsCalc, ScopeBar } from './EmissionsCalc'
import { ESTIMATE_NOTE, METHODOLOGY_DOC, SCOPE_META, avoidedFraming } from './emissions'
import { NO_ESTIMATE, formatCo2e, formatWh, orDash } from './format'

export function EnergyDetail({ energy }: { energy: EnergyAccounting }) {
  const scopes = energy.scopes
  const baseline = energy.baseline
  const framing = avoidedFraming(baseline?.avoided_co2e_g)
  const totalWh = energy.energy_wh_total ?? energy.energy_wh

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
        <div className="mono-label">Carbon footprint</div>
        <span className="badge badge-gray" title={ESTIMATE_NOTE}>
          estimated
        </span>
        <span style={{ flex: 1 }} />
        <span className="fine-print">
          method: <code>{METHODOLOGY_DOC}</code>
        </span>
      </div>

      <div className="config-stats" style={{ gap: 30 }}>
        <Stat
          label="CO₂e total (est.)"
          value={orDash(formatCo2e(energy.co2e_g))}
          title="The run total, equal to the sum of its three GHG Protocol scopes."
        />
        <Stat
          label="Energy total (est.)"
          value={orDash(formatWh(totalWh))}
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
        <Stat label="Energy class" value={energy.energy_class} title="Heuristic bucket, not a measurement." />
        <Stat label="Deployment" value={energy.deployment ?? NO_ESTIMATE} />
        <Stat
          label={framing.label}
          value={orDash(formatCo2e(baseline?.avoided_co2e_g))}
          color={framing.color}
          title={`${framing.note} Baseline: ${baseline?.model ?? 'none resolved'}.`}
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
            weighted tokens → Wh/Mtok → compute Wh → x PUE → x grid intensity → scopes
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
