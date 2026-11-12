import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  api,
  ApiError,
  type ExperimentArmResult,
  type ExperimentResult,
} from '../api/client'
import { fmtMeanInterval, fmtScore } from '../components/batches/evalHelpers'
import { errText } from '../components/batches/shared'
import { ROLE_RANK } from '../components/shared/Lessons'
import { useConfirm } from '../components/shared/ConfirmDialog'
import { formatCost, formatDateTime } from '../components/shared/format'
import { StatusBadge } from '../components/shared/StatusBadge'

const POLL_MS = 10_000

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`
}

function num(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits)
}

/** What an arm sets, in words. */
function armSets(a: ExperimentArmResult): string {
  const parts: string[] = []
  if (a.model_override) parts.push(`model ${a.model_override}`)
  if (a.objective) parts.push(`objective ${a.objective}`)
  if (a.max_iterations) parts.push(`max iterations ${a.max_iterations}`)
  return parts.join(' · ') || '—'
}


export function BatchExperiment() {
  const { id = '' } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const askConfirm = useConfirm()
  const [actionError, setActionError] = useState<string | null>(null)

  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const rank = ROLE_RANK[meQuery.data?.role ?? 'analyst'] ?? 0
  const canStart = rank >= ROLE_RANK.approver
  const canPromote = rank >= ROLE_RANK.admin

  const query = useQuery<ExperimentResult>({
    queryKey: ['batch-experiment', id],
    queryFn: () => api.getExperiment(id),
    retry: false,
    refetchInterval: (q) =>
      (q.state.data?.arms ?? []).some((a) => a.batch_status === 'running') ||
      q.state.data?.experiment?.evaluating
        ? POLL_MS
        : false,
  })
  const result = query.data
  const exp = result?.experiment
  const harnessId = exp?.harness_id ?? null

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['batch-experiment', id] })
    void queryClient.invalidateQueries({ queryKey: ['batch-experiments'] })
    void queryClient.invalidateQueries({ queryKey: ['batches'] })
  }
  const startMutation = useMutation({
    mutationFn: () => api.startExperiment(id),
    onMutate: () => setActionError(null),
    onSuccess: refresh,
    onError: (e) => setActionError(errText(e)),
  })
  const promoteMutation = useMutation({
    mutationFn: ({ arm, token }: { arm: string; token?: string }) =>
      api.promoteExperiment(id, arm, token),
    onMutate: () => setActionError(null),
    onSuccess: () => {
      refresh()
      void queryClient.invalidateQueries({ queryKey: ['harnesses'] })
      void queryClient.invalidateQueries({ queryKey: ['harness'] })
      void queryClient.invalidateQueries({ queryKey: ['harness-policy-history'] })
    },
    onError: (e) => {
      // A stale plan (409 included): refetch so it cannot be confirmed again.
      void query.refetch()
      setActionError(
        e instanceof ApiError && e.status === 403 ? 'Making an arm the default requires the admin role.' : errText(e),
      )
    },
  })

  const makeDefault = async (arm: ExperimentArmResult) => {
    // Fresh plan first: polling stops when nothing is running.
    const fresh = (await query.refetch()).data
    const freshArm = fresh?.arms?.find((a) => a.key === arm.key)
    const plan = freshArm?.promotion
    if (!plan || !plan.allowed) return
    const name = exp?.harness_name ?? 'this harness'
    const changes = (plan.changes ?? []).map((c) => `${c.field}: ${c.from} → ${c.to}`)
    const message =
      `This changes the policy of harness "${name}" for every future run of it, not just this experiment. ` +
      (changes.length > 0 ? `${changes.join('; ')}. ` : 'No setting changes. ') +
      (plan.notes ?? []).map((n) => (/[.!?]$/.test(n) ? n : `${n}.`)).join(' ') +
      ' The change is recorded in the harness policy history and can be reverted there.'
    const ok = await askConfirm({
      title: `Make arm ${arm.key} the default`,
      message,
      confirmLabel: `Make arm ${arm.key} the default`,
    })
    if (ok) promoteMutation.mutate({ arm: arm.key, token: plan.token })
  }

  if (query.isLoading) return <div className="empty pulse">Loading experiment…</div>
  if (query.error || !result || !exp) {
    const missing =
      query.error instanceof ApiError && (query.error.status === 404 || query.error.status === 405)
    return (
      <div>
        <Link to="/batches" className="fine-print">
          ← Batches
        </Link>
        <div className="empty">
          {missing ? 'Experiment not found.' : errText(query.error)}
        </div>
      </div>
    )
  }

  const arms = result.arms ?? []
  const comparisons = result.comparisons ?? []
  const anyDraft = arms.some((a) => a.batch_status === 'draft')
  const label = (key: string) => {
    const a = arms.find((x) => x.key === key)
    return a?.label ? `${key} (${a.label})` : key
  }
  const promoted = exp.promoted

  const rows: { name: string; cell: (a: ExperimentArmResult) => React.ReactNode }[] = [
    { name: 'Sets', cell: (a) => armSets(a) },
    {
      name: 'Batch',
      cell: (a) =>
        a.batch_id ? (
          <>
            <Link to={`/batches/${a.batch_id}`}>open</Link>{' '}
            {a.batch_status && <StatusBadge status={a.batch_status} />}
          </>
        ) : (
          '—'
        ),
    },
    { name: 'Settled / targets', cell: (a) => `${a.settled ?? 0} / ${a.targets ?? 0}` },
    { name: 'Delivered', cell: (a) => a.delivered ?? '—' },
    { name: 'Yield', cell: (a) => pct(a.yield) },
    {
      name: 'Quality (90% interval)',
      cell: (a) =>
        a.quality_mean === null || a.quality_mean === undefined
          ? '—'
          : `${fmtScore(a.quality_mean)}${
              a.quality_interval90
                ? ` (${fmtScore(a.quality_interval90[0])} to ${fmtScore(a.quality_interval90[1])})`
                : ''
            } · n=${a.scored ?? 0}`,
    },
    { name: 'Mean residual', cell: (a) => num(a.mean_residual) },
    { name: 'Cost', cell: (a) => formatCost(a.cost_usd) },
    {
      name: '$ per acceptable',
      cell: (a) => (a.cost_per_acceptable_usd == null ? '—' : formatCost(a.cost_per_acceptable_usd)),
    },
    {
      name: 'Wh per acceptable',
      cell: (a) => (a.energy_per_acceptable_wh == null ? '—' : num(a.energy_per_acceptable_wh)),
    },
    { name: 'Mean iterations', cell: (a) => num(a.mean_iterations) },
    ...(arms.some((a) => (a.infra_failures ?? 0) > 0)
      ? [{ name: 'Infrastructure failures', cell: (a: ExperimentArmResult) => a.infra_failures ?? 0 }]
      : []),
  ]

  return (
    <div>
      <Link to="/batches" className="fine-print">
        ← Batches
      </Link>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap', marginTop: 6, justifyContent: 'space-between' }}>
        <h1 className="view-title" style={{ marginBottom: 0 }}>
          {exp.name}
        </h1>
        {canStart && anyDraft && (
          <button
            className="btn btn-primary"
            disabled={startMutation.isPending}
            onClick={() => startMutation.mutate()}
          >
            {startMutation.isPending ? 'Starting…' : 'Start all'}
          </button>
        )}
      </div>
      <div className="view-sub" style={{ marginTop: 6 }}>
        {exp.mode === 'replicate'
          ? 'Replicate: every arm ran the same rows.'
          : 'Split: each row ran once, arms alternated.'}
        {exp.sample_n ? ` Sample of ${exp.sample_n} rows.` : ''}
        {' · '}
        <Link to={`/batches/family?key=${encodeURIComponent(exp.family_key)}`}>Family</Link>
        {exp.harness_name && <> · harness {exp.harness_name}</>}
        {' · '}created {formatDateTime(exp.created_at)}
      </div>
      {!canStart && anyDraft && (
        <div className="fine-print">Starting needs the approver role or higher.</div>
      )}
      {actionError && <div className="error-text">{actionError}</div>}
      {(exp.warnings ?? []).length > 0 && (
        <ul className="fine-print" style={{ margin: '6px 0', paddingLeft: 18 }}>
          {(exp.warnings ?? []).map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      {promoted && (
        <div className="panel" style={{ margin: '12px 0' }}>
          <div className="mono-body">
            Promoted arm {promoted.arm} on {formatDateTime(promoted.at)} by {promoted.by_name ?? 'unknown'}.{' '}
            {exp.current_arm !== undefined && exp.current_arm !== promoted.arm
              ? '(since changed or reverted) '
              : ''}
            {harnessId ? <Link to="/harnesses">Open harnesses</Link> : null}
          </div>
        </div>
      )}

      <div className="mono-label" style={{ margin: '16px 0 6px' }}>
        Arms
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            <tr>
              <th />
              {arms.map((a) => (
                <th key={a.key}>
                  Arm {a.key}
                  {a.label ? <div className="fine-print">{a.label}</div> : null}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name}>
                <td className="mono-label">{r.name}</td>
                {arms.map((a) => (
                  <td key={a.key}>{r.cell(a)}</td>
                ))}
              </tr>
            ))}
            {canPromote && harnessId && arms.every((a) => a.promotion) && (
              <tr>
                <td className="mono-label">Default</td>
                {arms.map((a) => (
                  <td key={a.key}>
                    {exp.current_arm === a.key ? (
                      <span className="fine-print">current default</span>
                    ) : (
                      <>
                        <button
                          className="btn btn-sm"
                          disabled={promoteMutation.isPending || !a.promotion?.allowed}
                          title={a.promotion?.allowed ? undefined : (a.promotion?.blocked_reason ?? undefined)}
                          onClick={() => void makeDefault(a)}
                        >
                          Make default
                        </button>
                        {a.promotion && !a.promotion.allowed && a.promotion.blocked_reason && (
                          <div className="fine-print">{a.promotion.blocked_reason}</div>
                        )}
                      </>
                    )}
                  </td>
                ))}
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {!canPromote && (
        <div className="fine-print" style={{ marginTop: 6 }}>
          Making an arm the default changes the harness policy and needs the admin role.
        </div>
      )}

      <div className="mono-label" style={{ margin: '18px 0 6px' }}>
        Comparisons against arm A
      </div>
      {comparisons.length === 0 ? (
        <div className="fine-print">Nothing to compare yet.</div>
      ) : (
        <table className="mono-table">
          <thead>
            <tr>
              <th>Arm</th>
              <th className="num">n</th>
              <th>Quality difference (90% interval)</th>
              <th>W / L / T</th>
              <th>Note</th>
            </tr>
          </thead>
          <tbody>
            {comparisons.map((c) => (
              <tr key={c.arm}>
                <td>
                  {label(c.arm)} vs {c.vs}
                </td>
                <td className="num">
                  {!c.paired && c.n_vs != null ? `n = ${c.n} vs ${c.n_vs}` : c.n}
                </td>
                <td>{fmtMeanInterval(c.mean_delta, c.interval90)}</td>
                <td>
                  {c.paired && c.wins != null ? `${c.wins} / ${c.losses ?? 0} / ${c.ties ?? 0}` : '—'}
                </td>
                <td className="fine-print">
                  {c.paired ? '' : 'different rows'}
                  {c.note && c.note !== 'different rows' ? `${c.paired ? '' : ' · '}${c.note}` : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {result.quality_note && <div className="fine-print">{result.quality_note}</div>}

      <div className="mono-label" style={{ margin: '18px 0 6px' }}>
        Recommendation
      </div>
      {result.recommendation?.reason ? (
        <div className="mono-body">
          {result.recommendation.arm ? `Arm ${result.recommendation.arm}: ` : ''}
          {result.recommendation.reason}
        </div>
      ) : (
        <div className="fine-print">
          No recommendation yet: arms are still running, not evaluated, or not comparable.
        </div>
      )}
      <div className="fine-print" style={{ marginTop: 4 }}>
        A recommendation never changes anything by itself. A person chooses.
      </div>
    </div>
  )
}
