/** The live footprint ticker: a run's cumulative estimated carbon as it streams.
 *
 *  Two constraints shape it. First, it must not move: every numeric cell has a
 *  fixed minimum width and tabular figures, and the whole row renders with
 *  em-dashes before the first `usage` event arrives, so nothing appears,
 *  disappears or reflows mid-stream. Second, a missing figure is an em-dash and
 *  never a zero — the engine reports null when it has no estimate. */
import type { UsageInfo } from '../../api/useRunStream'
import { COUNTERFACTUAL_SHORT, ESTIMATE_NOTE, avoidedFraming } from './emissions'
import { NO_ESTIMATE_HINT, formatCo2e, formatWh, orDash } from './format'

export function LiveFootprint({
  usage,
  label = 'footprint (est.)',
}: {
  usage: UsageInfo | null
  label?: string
}) {
  const framing = avoidedFraming(usage?.avoided_co2e_g)
  const scopeTitle =
    'GHG Protocol split so far: Scope 2 is purchased electricity for self-hosted inference, Scope 3 is cloud inference as a purchased service plus embodied hardware. Scope 1 is always zero.'

  return (
    <div className="ticker" title={`Cumulative for this run. ${ESTIMATE_NOTE}`}>
      <span className="t-key">{label}</span>

      <span className="t-item" title={`Cumulative estimated carbon. ${NO_ESTIMATE_HINT}`}>
        <span className="t-key">co₂e</span>
        <span className="t-val" style={{ minWidth: '11ch' }}>
          {orDash(formatCo2e(usage?.co2e_g))}
        </span>
      </span>

      <span className="t-item" title="Cumulative estimated compute energy (IT load).">
        <span className="t-key">energy</span>
        <span className="t-val" style={{ minWidth: '9ch' }}>
          {orDash(formatWh(usage?.energy_wh))}
        </span>
      </span>

      <span className="t-item" title={scopeTitle}>
        <span className="t-key">s1/s2/s3</span>
        <span className="t-val" style={{ minWidth: '17ch' }}>
          {usage?.co2e_g === null || usage?.co2e_g === undefined
            ? orDash(null)
            : `0 / ${orDash(shortGrams(usage.scope2_g))} / ${orDash(shortGrams(usage.scope3_g))} g`}
        </span>
      </span>

      <span className="t-item" title={`${framing.note} ${COUNTERFACTUAL_SHORT}`}>
        <span className="t-key">
          {framing.tone === 'surcharge' ? 'surcharge vs baseline' : 'avoided vs baseline'}
        </span>
        <span className="t-val" style={{ minWidth: '11ch', color: framing.color }}>
          {orDash(formatCo2e(usage?.avoided_co2e_g))}
        </span>
      </span>
    </div>
  )
}

/** Grams without the unit, for the compact scope triple. */
function shortGrams(grams: number | null | undefined): string | null {
  if (grams === null || grams === undefined) return null
  const text = formatCo2e(grams)
  return text ? text.replace(' g CO₂e', '') : null
}
