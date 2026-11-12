import type { FamilyModel } from '../../api/client'

/** Hand-written SVG scatter: x = $ per acceptable result, y = mean quality.
 *  Pareto models are filled in the accent colour; the others are outlined.
 *  Colours are theme tokens, so it follows light and dark. The per-model table
 *  next to it is the accessible equivalent. */
export function FamilyScatter({ models }: { models: FamilyModel[] }) {
  const pts = models.filter((m) => m.cost_per_acceptable_usd != null && m.quality_mean != null)
  if (pts.length === 0) {
    return <div className="fine-print">No model has both a cost per acceptable result and a quality mean yet.</div>
  }
  const W = 560
  const H = 300
  const L = 56
  const R = 24
  const T = 16
  const B = 48
  const xs = pts.map((m) => m.cost_per_acceptable_usd as number)
  const ys = pts.map((m) => m.quality_mean as number)
  const xMin = Math.min(...xs)
  const xMax = Math.max(...xs)
  const yLo = Math.min(...ys)
  const yHi = Math.max(...ys)
  const xPad = (xMax - xMin) * 0.1 || Math.max(xMax * 0.1, 0.01)
  const yPad = (yHi - yLo) * 0.15 || 5
  const x0 = Math.max(0, xMin - xPad)
  const x1 = xMax + xPad
  const y0 = Math.max(0, yLo - yPad)
  const y1 = Math.min(100, yHi + yPad)
  const sx = (v: number) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R)
  const sy = (v: number) => T + (1 - (v - y0) / (y1 - y0 || 1)) * (H - T - B)
  const ticks = (lo: number, hi: number) => [0, 1, 2, 3, 4].map((i) => lo + ((hi - lo) * i) / 4)
  const fmtX = (v: number) => (v < 1 ? `$${v.toFixed(3)}` : `$${v.toFixed(2)}`)
  const label = (m: string) => (m.length > 22 ? `${m.slice(0, 21)}…` : m)
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="Scatter of models: dollars per acceptable result against mean quality. The table beside it lists the same numbers."
      style={{ width: '100%', maxWidth: W, height: 'auto', display: 'block' }}
    >
      {ticks(y0, y1).map((v) => (
        <g key={`y${v}`}>
          <line x1={L} x2={W - R} y1={sy(v)} y2={sy(v)} stroke="var(--border-subtle)" />
          <text x={L - 6} y={sy(v) + 4} textAnchor="end" fontSize="11" fill="var(--text-muted)">
            {v.toFixed(0)}
          </text>
        </g>
      ))}
      {ticks(x0, x1).map((v) => (
        <text key={`x${v}`} x={sx(v)} y={H - B + 16} textAnchor="middle" fontSize="11" fill="var(--text-muted)">
          {fmtX(v)}
        </text>
      ))}
      <line x1={L} x2={W - R} y1={H - B} y2={H - B} stroke="var(--border)" />
      <line x1={L} x2={L} y1={T} y2={H - B} stroke="var(--border)" />
      <text x={(L + W - R) / 2} y={H - 6} textAnchor="middle" fontSize="12" fill="var(--text)">
        $ per acceptable result
      </text>
      <text
        transform={`translate(14 ${(T + H - B) / 2}) rotate(-90)`}
        textAnchor="middle"
        fontSize="12"
        fill="var(--text)"
      >
        Quality (mean)
      </text>
      {pts.map((m) => {
        const cx = sx(m.cost_per_acceptable_usd as number)
        const cy = sy(m.quality_mean as number)
        const flip = cx > W - 150
        return (
          <g key={m.model}>
            <title>
              {m.model}: quality {m.quality_mean?.toFixed(1)}, {fmtX(m.cost_per_acceptable_usd as number)} per
              acceptable result{m.pareto ? ' (Pareto)' : ''}
            </title>
            <circle
              cx={cx}
              cy={cy}
              r={m.pareto ? 6 : 5}
              fill={m.pareto ? 'var(--accent)' : 'var(--bg-panel)'}
              stroke={m.pareto ? 'var(--accent)' : 'var(--text-muted)'}
              strokeWidth={2}
            />
            <text
              x={flip ? cx - 9 : cx + 9}
              y={cy + 4}
              textAnchor={flip ? 'end' : 'start'}
              fontSize="11"
              fill="var(--text)"
              fontWeight={m.pareto ? 700 : 400}
            >
              {label(m.model)}
            </text>
          </g>
        )
      })}
    </svg>
  )
}
