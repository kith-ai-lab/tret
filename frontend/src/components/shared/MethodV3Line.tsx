/** A small secondary line under a run's carbon receipt: the parallel estimate
 *  under the revised emissions method. Deliberately subordinate — a preview that
 *  runs beside the reported figure, never a replacement for it. Renders nothing
 *  for a run that predates it. */
import type { MethodV3 } from '../../api/client'
import { METHOD_V3_NOTE, methodV3Parts, methodV3Summary } from './emissions'

export function MethodV3Line({ method }: { method?: MethodV3 | null }) {
  if (!method) return null
  const parts = methodV3Parts(method)
  const detail = [
    parts.map((p) => `${p.label} ${p.value}`).join(' · '),
    `grid: ${method.grid.rung.replace(/_/g, ' ')} — ${method.grid.basis}`,
    `placement: rung ${method.placement.rung}${
      method.placement.flags.length ? ` (${method.placement.flags.join('; ')})` : ''
    }`,
    METHOD_V3_NOTE,
  ].join('\n')
  return (
    <details className="tool-row" style={{ marginTop: 8 }}>
      <summary title={detail}>
        <span className="fine-print">{methodV3Summary(method)}</span>
      </summary>
      <div className="fine-print" style={{ padding: '6px 10px 8px' }}>
        <p>{parts.map((p) => `${p.label} ${p.value}`).join(' · ')}</p>
        <p>
          grid: {method.grid.rung.replace(/_/g, ' ')} — {method.grid.basis}
        </p>
        <p>
          placement: rung {method.placement.rung}
          {method.placement.flags.length > 0 && ` (${method.placement.flags.join('; ')})`}
        </p>
        <p>{METHOD_V3_NOTE}</p>
      </div>
    </details>
  )
}
