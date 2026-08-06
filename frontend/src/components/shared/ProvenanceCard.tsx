/** Compact provenance summary: what produced this artifact, from what inputs,
 *  at what cost — in dollars and in estimated watt-hours. Everything auditable
 *  at a glance.
 *
 *  The dollar figure is exact and the carbon figure is not, so the carbon figure
 *  carries its judgment band on a second line wherever the run recorded one. A
 *  bare point estimate is the one thing this card must not show. */
import { BAND_SHORT, MONEY_PCT_PRECISION_NOTE, avoidedMoneyFraming, moneyPctPhrase } from './emissions'
import { footprintText, formatCo2eBand, formatCost, formatCostSigned, formatTokens } from './format'

export function ProvenanceCard({
  model,
  doctrineSha,
  documentCount,
  retrievedCount,
  inputTokens,
  outputTokens,
  cacheReadTokens,
  cacheWriteTokens,
  costUsd,
  energyWh,
  co2eG,
  co2eGLow,
  co2eGHigh,
  avoidedUsd,
  avoidedUsdPct,
}: {
  model: string | null | undefined
  doctrineSha: string | null | undefined
  documentCount: number
  retrievedCount?: number
  inputTokens?: number
  outputTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  costUsd?: number
  energyWh?: number | null
  co2eG?: number | null
  /** The judgment band around co2eG. Absent on runs recorded before it. */
  co2eGLow?: number | null
  co2eGHigh?: number | null
  /** Signed money against the same-token baseline. Exact, unlike the carbon. */
  avoidedUsd?: number | null
  /** Share of frontier spend avoided, one decimal place. null when there is no
   *  baseline spend to compare against — never rendered as 0%. */
  avoidedUsdPct?: number | null
}) {
  const footprint = footprintText(energyWh, co2eG)
  const band = formatCo2eBand(co2eGLow, co2eGHigh)
  const money = avoidedMoneyFraming(avoidedUsd)
  const showCache = cacheReadTokens !== undefined || cacheWriteTokens !== undefined
  return (
    <div className="panel">
      <div className="mono-label" style={{ marginBottom: 10 }}>
        Provenance
      </div>
      <div className="config-stats">
        <Stat label="Model" value={model ?? '—'} />
        <Stat label="Doctrine SHA" value={doctrineSha ? doctrineSha.slice(0, 12) : '—'} title={doctrineSha ?? undefined} />
        <Stat label="Documents" value={String(documentCount)} />
        {retrievedCount !== undefined && <Stat label="Retrieved values" value={String(retrievedCount)} />}
        {inputTokens !== undefined && (
          <Stat label="Tokens in / out" value={`${formatTokens(inputTokens)} / ${formatTokens(outputTokens ?? 0)}`} />
        )}
        {showCache && (
          <Stat
            label="Cache read / write"
            value={`${formatTokens(cacheReadTokens ?? 0)} / ${formatTokens(cacheWriteTokens ?? 0)}`}
            title="Prompt-cache tokens: reads bill at 0.1x the input price, writes at 1.25x."
          />
        )}
        {costUsd !== undefined && (
          <Stat
            label="Cost"
            value={formatCost(costUsd)}
            title="Exact: per-token list prices are published, so this is arithmetic rather than an estimate."
          />
        )}
        {footprint && (
          <Stat
            label="Footprint (est.)"
            value={footprint}
            sub={band}
            subTitle={BAND_SHORT}
            title="Estimated from token counts and the model's calibrated energy class — not a measurement."
          />
        )}
        {avoidedUsd !== null && avoidedUsd !== undefined && (
          <Stat
            label={money.label}
            value={formatCostSigned(avoidedUsd)}
            sub={moneyPctPhrase(avoidedUsdPct)}
            subTitle={MONEY_PCT_PRECISION_NOTE}
            title={`${money.note} ${MONEY_PCT_PRECISION_NOTE}`}
          />
        )}
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  title,
  sub,
  subTitle,
}: {
  label: string
  value: string
  title?: string
  sub?: string | null
  subTitle?: string
}) {
  return (
    <div className="config-stat">
      <div className="mono-label">{label}</div>
      <div className="mono-body" title={title}>
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
