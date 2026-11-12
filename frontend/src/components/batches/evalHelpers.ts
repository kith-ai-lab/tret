import type {
  BatchRubric,
  BatchTarget,
  BatchTargetEvaluationBrief,
  RouterGate,
  TargetEvaluationOutlier,
} from '../../api/client'

/** Shown in an empty judge field. A blank judge is sent as null, and the server then
 * takes the first model in TRET_BATCH_EVALUATOR_JUDGE whose provider has a key. */
export const JUDGE_PLACEHOLDER = 'auto: first available judge'

/** "—" for null/undefined/NaN. */
export function dash(v: number | null | undefined, fmt: (n: number) => string): string {
  return v === null || v === undefined || Number.isNaN(v) ? '—' : fmt(v)
}

export function fmtPct(v: number | null | undefined): string {
  return dash(v, (n) => `${(n * 100).toFixed(1)}%`)
}

export function fmtScore(v: number | null | undefined): string {
  return dash(v, (n) => n.toFixed(1))
}

/** Signed number with a true minus sign: "+3.1", "−2.0", "0.0". */
export function fmtSigned(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const fixed = Math.abs(v).toFixed(digits)
  if (Number(fixed) === 0) return (0).toFixed(digits)
  return `${v < 0 ? '−' : '+'}${fixed}`
}

/** "+3.1 (−2.0 to +8.4)"; just the mean when the interval is absent. */
export function fmtMeanInterval(
  mean: number | null | undefined,
  interval: readonly number[] | null | undefined,
  digits = 1,
): string {
  if (mean === null || mean === undefined) return '—'
  if (!interval || interval.length !== 2) return fmtSigned(mean, digits)
  return `${fmtSigned(mean, digits)} (${fmtSigned(interval[0], digits)} to ${fmtSigned(interval[1], digits)})`
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h ${String(m % 60).padStart(2, '0')}m`
}

/** Class name for a signed delta; `goodWhenNegative` for cost-like metrics. */
export function deltaClass(v: number | null | undefined, goodWhenNegative = false): string {
  if (v === null || v === undefined || v === 0) return ''
  const good = goodWhenNegative ? v < 0 : v > 0
  return good ? 'delta-pos' : 'delta-neg'
}

export function flagLabel(rubric: BatchRubric | null | undefined, key: string): string {
  return rubric?.flags.find((f) => f.key === key)?.label ?? key.replace(/_/g, ' ')
}

/** Target ids that merit a human spot check: every flagged scored target plus
 *  every 10th by rank. Pure and client-side; ranks come from the evaluation
 *  brief (1 = best). */
export function suggestedSpotChecks(
  items: { id: string; evaluation?: BatchTargetEvaluationBrief | null }[],
): Set<string> {
  const out = new Set<string>()
  for (const t of items) {
    const e = t.evaluation
    if (!e || e.status !== 'scored') continue
    if ((e.flags ?? []).length > 0) out.add(t.id)
    if (e.rank !== null && e.rank !== undefined && e.rank > 0 && e.rank % 10 === 0) out.add(t.id)
  }
  return out
}

/** Summary of the scored targets for the gate banner. */
export function gateScoreSummary(
  targets: Pick<BatchTarget, 'evaluation'>[],
): { n: number; mean: number | null; flagged: number; flagNames: string[] } | null {
  const scored = targets.filter(
    (t) => t.evaluation?.status === 'scored' && typeof t.evaluation.quality === 'number',
  )
  if (scored.length === 0) return null
  const qs = scored.map((t) => t.evaluation!.quality as number)
  const names = new Set<string>()
  let flagged = 0
  for (const t of scored) {
    const f = t.evaluation!.flags ?? []
    if (f.length > 0) flagged += 1
    f.forEach((x) => names.add(x))
  }
  return {
    n: scored.length,
    mean: qs.reduce((a, b) => a + b, 0) / qs.length,
    flagged,
    flagNames: [...names].sort(),
  }
}

/** Outlier metric names -> readable text. */
export function metricLabel(metric: string): string {
  return (
    (
      {
        cost_usd: 'cost',
        iteration_ratio: 'iterations / max',
        retained_ratio: 'retained / source pages',
        retained_pages: 'retained pages',
      } as Record<string, string>
    )[metric] ?? metric.replace(/_/g, ' ')
  )
}

export function fmtMetric(metric: string, v: number): string {
  if (metric === 'cost_usd') return `$${v.toFixed(2)}`
  if (metric.endsWith('ratio')) return v.toFixed(2)
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

export function outlierText(o: TargetEvaluationOutlier | string): string {
  if (typeof o === 'string') return o
  return `${metricLabel(o.metric)} ${fmtMetric(o.metric, o.value)} vs median ${fmtMetric(o.metric, o.median)} (z ${o.z.toFixed(1)})`
}

/** Derive a rubric criterion/flag key from a label: ^[a-z][a-z0-9_]{1,39}$. */
export function keyFromLabel(label: string, taken: Set<string> = new Set()): string {
  let base = label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 36)
  if (!/^[a-z]/.test(base)) base = `c_${base}`
  if (base.length < 2) base = `${base}_x`
  let key = base
  for (let i = 2; taken.has(key); i += 1) key = `${base}_${i}`.slice(0, 40)
  return key
}

export const DISPUTED_GAP_TITLE =
  "A gap is disputed when the other batch delivered that company's disclosure, judged acceptable."

export const DISPUTED_PRIMARY_TITLE =
  'Another batch packaged a main document for this company that this run did not capture. Re-evaluate with Only unscored to have the judge check it.'

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

export function deliveredElsewhereTitle(n: number, rechecked: boolean | null | undefined): string {
  return `${plural(n, 'other batch', 'other batches')} of this target list delivered this company's disclosure, judged acceptable. Re-checked by the judge: ${rechecked ? 'yes' : 'no'}.`
}

export function otherPrimaryTitle(n: number, rechecked: boolean | null | undefined): string {
  return `${plural(n, 'main document')} packaged by other batches of this target list that this run did not capture. Re-checked by the judge: ${rechecked ? 'yes' : 'no'}.`
}

const wholePct = (v: number) => `${Math.round(v * 100)}%`

export const ROUTER_GATE_TOOLTIP =
  "Evaluator scores feed model selection for this family only after people have spot-checked the judge. Spot-check scored rows from a batch's page."

/** Family-page sentence for the router-learning gate; null when the server sent none. */
export function routerGateLine(g: RouterGate | null | undefined): string | null {
  if (!g) return null
  const head = 'Router learning for this family:'
  if (g.mode === 'off') return `${head} disabled by configuration.`
  if (g.open) return `${head} on. ${g.spot_checks} spot checks, ${wholePct(g.agreement ?? 0)} agreement.`
  if (!g.spot_checks) {
    return `${head} off. No spot checks yet (${g.needed_checks} needed, at ${wholePct(g.needed_agreement)} agreement).`
  }
  const agree = g.agreement == null ? 'no' : wholePct(g.agreement)
  return `${head} off. ${g.spot_checks} of ${g.needed_checks} spot checks, ${agree} agreement (needs ${wholePct(g.needed_agreement)}).`
}

/** Compact scorecard note; null when absent or disabled by configuration. */
export function routerGateNote(g: RouterGate | null | undefined): string | null {
  if (!g || g.mode === 'off') return null
  return g.open ? 'router learning on' : `${g.spot_checks}/${g.needed_checks} spot checks toward router learning`
}
