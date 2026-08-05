/** Compact provenance summary: what produced this artifact, from what inputs,
 *  at what cost — in dollars and in estimated watt-hours. Everything auditable
 *  at a glance. */
import { footprintText, formatCost, formatTokens } from './format'

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
}) {
  const footprint = footprintText(energyWh, co2eG)
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
        {costUsd !== undefined && <Stat label="Cost" value={formatCost(costUsd)} />}
        {footprint && (
          <Stat
            label="Footprint (est.)"
            value={footprint}
            title="Heuristic estimate from token counts and the model's energy class — not a measurement."
          />
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="config-stat">
      <div className="mono-label">{label}</div>
      <div className="mono-body" title={title}>
        {value}
      </div>
    </div>
  )
}
