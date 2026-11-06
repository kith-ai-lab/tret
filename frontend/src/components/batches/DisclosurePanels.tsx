import type { Batch } from '../../api/client'

const MARKETS = ['US', 'UK', 'EU'] as const

/** Disclosure-only summary panels on the batch detail page: the per-market board. */
export function DisclosurePanels({ batch: c }: { batch: Batch }) {
  return (
    <div
      className="row"
      style={{ gap: 12, flexWrap: 'wrap', alignItems: 'stretch', marginBottom: 18 }}
    >
      {MARKETS.map((m) => {
        const mc = c.counts.by_market?.[m] ?? { total: 0, done: 0, gap: 0, failed: 0 }
        return (
          <div key={m} className="panel" style={{ minWidth: 150 }}>
            <div className="mono-label">{m}</div>
            <div className="mono-body" style={{ fontSize: 18 }}>
              {mc.done} / {mc.total}
            </div>
            <div className="fine-print">
              gap {mc.gap} · failed {mc.failed}
            </div>
          </div>
        )
      })}
    </div>
  )
}
