/** A run's water estimate, beside its carbon estimate.
 *
 *  Same commitments as the carbon card: the figure carries its judgment band, the
 *  baseline comparison is signed and never framed as an offset, a run with no
 *  water figure says "Not recorded" (never 0), and nothing is computed here that
 *  the backend did not send. The on-site / off-site split is always visible,
 *  because where the water goes (a data centre's cooling towers or a power plant)
 *  is the first question a reader asks.
 */
import type { EnergyAccounting, WaterAccounting } from '../../api/client'
import { WaterCaveatList, WaterFactorTable } from './FactorProvenance'
import { MethodologyLink } from './MethodologyDialog'
import {
  WATER_BASIS_LABEL,
  WATER_BASIS_NOTE,
  WATER_COUNTERFACTUAL_NOTE,
  WATER_ESTIMATE_NOTE,
  WATER_EVERYDAY_NOTE,
  WATER_HYDRO_NOTE,
  WATER_PART_META,
  WATER_BAND_SHORT,
  BAND_LABEL,
  coarseWaterComparison,
  completeWater,
  share,
  waterAvoidedFraming,
  waterEveryday,
  waterRecord,
  waterBandFactorText,
} from './emissions'
import {
  NO_WATER,
  NO_WATER_HINT,
  formatFactor,
  formatTokens,
  formatWaterAt,
  formatWaterBand,
  formatWaterScaled,
  waterScaleFor,
} from './format'

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

/** The on-site / off-site split as a bar and two labelled figures. Both parts
 *  print in the unit of the total so they read against it. */
export function WaterSplit({ water }: { water: WaterAccounting }) {
  const scale = waterScaleFor(water.water_ml)
  const total = water.onsite_ml + water.offsite_ml
  const parts = [
    { meta: WATER_PART_META[0], ml: water.onsite_ml, color: 'var(--blue)' },
    { meta: WATER_PART_META[1], ml: water.offsite_ml, color: 'var(--amber)' },
  ]
  return (
    <div>
      <div
        className="stacked-bar"
        style={{ height: 10 }}
        role="img"
        aria-label="Water split: on-site cooling and off-site power generation"
      >
        {total > 0 ? (
          parts.map((p) => {
            const pct = share(p.ml, total)
            return pct > 0 ? (
              <span
                key={p.meta.key}
                title={`${p.meta.label}: ${formatWaterAt(p.ml, scale)} (${pct.toFixed(1)}%)`}
                style={{ width: `${pct}%`, background: p.color, opacity: 0.8 }}
              />
            ) : null
          })
        ) : (
          <span style={{ width: '100%', background: 'var(--bg-input)' }} />
        )}
      </div>
      <div className="row" style={{ gap: 16, marginTop: 6, flexWrap: 'wrap' }}>
        {parts.map((p) => (
          <span key={p.meta.key} className="fine-print" title={p.meta.what}>
            <span className="swatch" style={{ background: p.color }} />
            {p.meta.label} {formatWaterAt(p.ml, scale)}
            {total > 0 && ` · ${Math.round(share(p.ml, total))}%`}
          </span>
        ))}
      </div>
    </div>
  )
}

/** "Not recorded" in place of a card's figures. */
function NotRecorded({ what }: { what: string }) {
  return (
    <div className="empty" style={{ padding: '4px 0' }} title={NO_WATER_HINT}>
      {NO_WATER} for {what}. It was recorded before water accounting existed, or only some of its
      calls had water. That is not the same as zero.
    </div>
  )
}

/** The water card: total with band, the split, the baseline comparison, caveats
 *  and factor provenance. Rendered next to the carbon card on a run. */
export function WaterPanel({ energy }: { energy: EnergyAccounting }) {
  const water = completeWater(energy.water)
  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
        <div className="mono-label">Water footprint</div>
        <span className="badge badge-gray" title={WATER_ESTIMATE_NOTE}>
          estimated
        </span>
        {water && (
          <>
            <span className="badge badge-gray" title={WATER_BAND_SHORT}>
              {BAND_LABEL}, not a confidence interval
            </span>
            <span className="badge badge-gray" title={WATER_BASIS_NOTE}>
              consumption
            </span>
          </>
        )}
        <span style={{ flex: 1 }} />
        <span className="fine-print">
          method: <MethodologyLink topic="water" water={water} label="water methodology" />
        </span>
      </div>

      {water ? <WaterBody water={water} /> : <NotRecorded what="this run" />}
    </div>
  )
}

function WaterBody({ water }: { water: WaterAccounting }) {
  const framing = waterAvoidedFraming(water.avoided_water_ml)
  const comparison = coarseWaterComparison(water.water_ml, water.baseline_water_ml)
  const everyday = waterEveryday(water.water_ml)
  const bandRecord = waterRecord(water, 'water_band')?.value
  const bandText =
    bandRecord && typeof bandRecord === 'object'
      ? waterBandFactorText(bandRecord.low, bandRecord.high)
      : null
  const scale = waterScaleFor(water.water_ml)
  const hasBaseline = water.baseline_water_ml !== null && water.baseline_water_ml !== undefined
  return (
    <>
      <div className="config-stats" style={{ gap: 30 }}>
        <Stat
          label="Water consumed (est.)"
          value={formatWaterScaled(water.water_ml) ?? NO_WATER}
          sub={formatWaterBand(water.water_ml_low, water.water_ml_high)}
          subTitle={`${WATER_BAND_SHORT}${bandText ? ` (${bandText})` : ''}`}
          title={`${WATER_BASIS_LABEL}. The run total: on-site cooling plus off-site power generation.`}
        />
        {everyday && (
          <Stat
            label="Roughly"
            value={everyday}
            title={WATER_EVERYDAY_NOTE}
          />
        )}
        <Stat
          label="Baseline water (est.)"
          value={hasBaseline ? (formatWaterAt(water.baseline_water_ml, scale) ?? NO_WATER) : NO_WATER}
          title="The same tokens through the baseline model, priced with its own water factors."
        />
        <Stat
          label={framing.label}
          value={
            water.avoided_water_ml === null || water.avoided_water_ml === undefined
              ? NO_WATER
              : (formatWaterAt(water.avoided_water_ml, scale) ?? NO_WATER)
          }
          color={framing.color}
          sub={comparison.tone === 'unknown' ? undefined : comparison.text}
          subTitle={comparison.note}
          title={`${framing.note} ${WATER_COUNTERFACTUAL_NOTE}`}
        />
      </div>

      <div style={{ marginTop: 12 }}>
        <WaterSplit water={water} />
      </div>

      <div className="fine-print" style={{ marginTop: 8 }}>
        {WATER_BASIS_NOTE} {WATER_HYDRO_NOTE}
      </div>

      {(water.factors?.length > 0 || water.caveats?.length > 0) && (
        <details className="tool-row" style={{ marginTop: 12 }}>
          <summary>
            <span className="tool-name">water factors and caveats</span>
            <span style={{ color: 'var(--text-muted)', fontSize: 'var(--fs-xs)' }}>
              {formatTokens(water.factors?.length ?? 0)} factors ·{' '}
              {formatTokens(water.caveats?.length ?? 0)} caveats
            </span>
          </summary>
          <div className="stack" style={{ gap: 16, padding: '10px 10px 12px' }}>
            <div>
              <div className="mono-label" style={{ marginBottom: 4 }}>
                Every constant behind this figure, and where it came from
              </div>
              <WaterFactorTable factors={water.factors ?? []} />
            </div>
            <div>
              <div className="mono-label" style={{ marginBottom: 4 }}>
                Caveats on this run
              </div>
              <WaterCaveatList caveats={water.caveats ?? []} />
            </div>
          </div>
        </details>
      )}
    </>
  )
}

/** The water rows of the step-by-step derivation (inside `EmissionsCalc`): the
 *  recorded factors and parts, in the order they combine. Values are read from
 *  the run's water block; nothing is recomputed. */
export function WaterDerivation({ energy }: { energy: EnergyAccounting }) {
  const water = completeWater(energy.water)
  const wue = water ? waterRecord(water, 'site_wue_l_per_kwh') : undefined
  const grid = water ? waterRecord(water, 'grid_water_l_per_kwh') : undefined
  const num = (v: unknown) => (typeof v === 'number' ? formatFactor(v, 4) : '—')
  return (
    <div>
      <div className="row" style={{ marginBottom: 6, flexWrap: 'wrap' }}>
        <div className="mono-label">Water: derived from the same energy figure</div>
        <span style={{ flex: 1 }} />
        <span className="fine-print">
          <MethodologyLink topic="water" water={water} label="water methodology" />
        </span>
      </div>
      {!water ? (
        <NotRecorded what="this run" />
      ) : (
        <>
          <div className="md-table-wrap">
            <table className="mono-table">
              <thead>
                <tr>
                  <th>Part</th>
                  <th>How it is derived</th>
                  <th className="num">Result (est.)</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{WATER_PART_META[0].label}</td>
                  <td style={{ color: 'var(--text-muted)' }}>
                    IT energy (kWh) x site WUE {wue ? `${num(wue.value)} ${wue.unit ?? ''}` : ''}
                  </td>
                  <td className="num">{formatWaterScaled(water.onsite_ml)}</td>
                </tr>
                <tr>
                  <td>{WATER_PART_META[1].label}</td>
                  <td style={{ color: 'var(--text-muted)' }}>
                    facility energy (kWh) x grid water {grid ? `${num(grid.value)} ${grid.unit ?? ''}` : ''}
                  </td>
                  <td className="num">{formatWaterScaled(water.offsite_ml)}</td>
                </tr>
                <tr>
                  <td>Total consumed</td>
                  <td style={{ color: 'var(--text-muted)' }}>on-site + off-site</td>
                  <td className="num">
                    {formatWaterScaled(water.water_ml)}
                    <span className="band-under" title={WATER_BAND_SHORT}>
                      {formatWaterBand(water.water_ml_low, water.water_ml_high)}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div className="fine-print" style={{ marginTop: 6 }}>
            {WATER_BASIS_NOTE} Second figure under the total is the {BAND_LABEL}. {WATER_BAND_SHORT}
          </div>
        </>
      )}
    </div>
  )
}
