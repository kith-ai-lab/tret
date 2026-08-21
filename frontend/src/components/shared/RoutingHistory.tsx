import { type RoutingHistoryGroup, type RoutingHistory as RoutingHistoryData } from '../../api/client'
import { shortModelName } from './RoutingBadge'

/** One stable colour per model within a group. Assigned from the group's own
 *  `model_ids`, which the backend sorts, so a model keeps its colour across
 *  every bucket and across reloads.
 *
 *  These are the categorical tokens, deliberately not the semantic ones. An
 *  earlier version of this file drew series in `--green`, `--red` and `--amber`,
 *  which meant model A rendered in the same green as "succeeded" and model B in
 *  the same red as "failed" — a signal that looked meaningful and was not. The
 *  chart tokens are hue-distinct from the status colours for exactly that
 *  reason. Cycle if a group ever has more than six models; do not add a seventh
 *  token here. */
const SERIES = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
  'var(--chart-6)',
]

function colorOf(models: string[], id: string): string {
  const i = models.indexOf(id)
  return SERIES[i >= 0 ? i % SERIES.length : SERIES.length - 1]
}

function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/** Choice share over time as a stacked column per bucket, plus the quality
 *  trend beneath it.
 *
 *  Inline SVG rather than a charting library: the frontend has four runtime
 *  dependencies and this does not warrant a fifth. */
function ShareChart({ group }: { group: RoutingHistoryGroup }) {
  const W = 720
  const H = 120
  const gap = 3
  const n = group.buckets.length || 1
  const bw = Math.max(4, (W - gap * (n - 1)) / n)

  return (
    <svg viewBox={`0 0 ${W} ${H + 18}`} width="100%" role="img"
         aria-label={`Model choice share per ${group.task_shape} bucket`}>
      {group.buckets.map((b, i) => {
        const x = i * (bw + gap)
        let y = 0
        const entries = group.model_ids
          .map((id) => [id, b.models[id]?.share ?? 0] as const)
          .filter(([, share]) => share > 0)
        return (
          <g key={b.start}>
            {entries.map(([id, share]) => {
              const h = share * H
              const rect = (
                <rect key={id} x={x} y={y} width={bw} height={h}
                      fill={colorOf(group.model_ids, id)}>
                  <title>
                    {shortDate(b.start)} — {shortModelName(id)}: {(share * 100).toFixed(0)}% of{' '}
                    {b.runs} run{b.runs === 1 ? '' : 's'}
                  </title>
                </rect>
              )
              y += h
              return rect
            })}
            {/* A run that changed model mid-flight. Marked on the bar it
                happened in, because "the router picked wrong and the engine
                fixed it" is a different event from "the router changed its
                mind", and both belong on the same timeline. */}
            {/* Semantic on purpose, and the one mark here that should be: it
                means "something needed attention", not "series four". */}
            {b.switched_runs > 0 && (
              <circle cx={x + bw / 2} cy={H + 6} r={2.5} fill="var(--amber)">
                <title>
                  {b.switched_runs} run{b.switched_runs === 1 ? '' : 's'} changed model mid-run
                  ({(b.switch_rate * 100).toFixed(0)}%)
                </title>
              </circle>
            )}
            {i % Math.ceil(n / 6) === 0 && (
              <text x={x} y={H + 16} fontSize="8" fill="var(--text-muted)"
                    fontFamily="var(--mono)">
                {shortDate(b.start)}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

function QualityChart({ group }: { group: RoutingHistoryGroup }) {
  const W = 720
  const H = 70
  const n = group.buckets.length || 1
  const step = n > 1 ? W / (n - 1) : W

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
         aria-label={`Mean quality per model over time for ${group.task_shape}`}>
      {/* The delivered-cleanly anchor. Without it the y-axis is unreadable:
          0.70 is what a clean run with no human review scores. */}
      {/* `--border` rather than muted-text-at-40%: this is a guide line, and the
          border token is the one already tuned to read faintly against either
          background. */}
      <line x1={0} y1={H - 0.7 * H} x2={W} y2={H - 0.7 * H} stroke="var(--border)"
            strokeDasharray="2 4" />
      {group.model_ids.map((id) => {
        const points = group.buckets
          .map((b, i) => [i * step, b.models[id]?.mean_quality] as const)
          .filter((p): p is readonly [number, number] => typeof p[1] === 'number')
        if (points.length < 2) return null
        const d = points.map(([x, q], i) => `${i ? 'L' : 'M'}${x},${H - q * H}`).join(' ')
        return (
          <path key={id} d={d} fill="none" stroke={colorOf(group.model_ids, id)}
                strokeWidth={1.5} />
        )
      })}
    </svg>
  )
}

export function RoutingHistoryPanel({ history }: { history: RoutingHistoryData }) {
  if (history.groups.length === 0) {
    return (
      <div className="empty">
        No scored runs yet. Outcomes are recorded as runs finish; run{' '}
        <code>tret outcomes backfill</code> to score the runs already in the database.
      </div>
    )
  }
  return (
    <div className="stack" style={{ gap: 20 }}>
      {history.groups.map((g) => (
        <div key={`${g.task_shape}-${g.objective}`}>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            {g.task_shape} · {g.objective} · {g.runs} runs
          </div>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap', marginBottom: 6 }}>
            {g.model_ids.map((id) => (
              <span key={id} className="mono-label" style={{ color: colorOf(g.model_ids, id) }}>
                ■ {shortModelName(id)}
              </span>
            ))}
          </div>
          <ShareChart group={g} />
          <div className="mono-label" style={{ margin: '6px 0 2px' }}>
            Mean quality (dashed line = 0.70, a clean run with no human review)
          </div>
          <QualityChart group={g} />
          {g.top_pick_changes.length > 0 ? (
            <div style={{ marginTop: 6, fontFamily: 'var(--mono)', fontSize: 10.5 }}>
              {g.top_pick_changes.map((c) => (
                <div key={c.at}>
                  {shortDate(c.at)} — the router changed its mind:{' '}
                  {shortModelName(c.from_model)} → {shortModelName(c.to_model)}
                </div>
              ))}
            </div>
          ) : (
            <div
              style={{
                marginTop: 6,
                fontFamily: 'var(--mono)',
                fontSize: 10.5,
                color: 'var(--text-muted)',
              }}
            >
              The top pick has not changed in this window.
            </div>
          )}
        </div>
      ))}
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--text-muted)' }}>
        {/* The backend's own wording. Share and quality count different things
            on purpose, and reading one as the other is the easy mistake here. */}
        {history.basis.share_counts} {history.basis.quality_counts} Amber dots mark buckets
        where a run changed model mid-flight.
      </div>
    </div>
  )
}
