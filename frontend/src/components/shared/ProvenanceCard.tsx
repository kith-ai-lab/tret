/** Compact provenance summary: what produced this artifact, from what inputs,
 *  at what cost. Everything auditable at a glance. */
export function ProvenanceCard({
  model,
  doctrineSha,
  documentCount,
  retrievedCount,
  inputTokens,
  outputTokens,
  costUsd,
}: {
  model: string | null | undefined
  doctrineSha: string | null | undefined
  documentCount: number
  retrievedCount?: number
  inputTokens?: number
  outputTokens?: number
  costUsd?: number
}) {
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
          <Stat label="Tokens in / out" value={`${fmt(inputTokens)} / ${fmt(outputTokens ?? 0)}`} />
        )}
        {costUsd !== undefined && <Stat label="Cost" value={`$${costUsd.toFixed(4)}`} />}
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

function fmt(n: number): string {
  return n.toLocaleString('en-US')
}
