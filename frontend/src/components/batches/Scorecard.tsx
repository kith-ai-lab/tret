import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api, type BatchScorecard } from '../../api/client'
import { useConfirm } from '../shared/ConfirmDialog'
import { formatCost } from '../shared/format'
import { Modal } from '../shared/Modal'
import {
  JUDGE_PLACEHOLDER,
  dash,
  fmtDuration,
  fmtPct,
  fmtScore,
  metricLabel,
  fmtMetric,
} from './evalHelpers'
import { errText } from './shared'

const POLL_MS = 5000

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat-tile">
      <div className="mono-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

/** Header panel of the batch detail (and, compact, of the compare view).
 *  Fetches its own scorecard; `refreshKey` lets the parent re-fetch when its
 *  own data changed. Tolerates every field being absent. */
export function Scorecard({
  batchId,
  compact = false,
  onChanged,
}: {
  batchId: string
  /** Compact: no actions, no review/outlier sections. */
  compact?: boolean
  onChanged?: () => void
}) {
  const queryClient = useQueryClient()
  const askConfirm = useConfirm()
  const [dialog, setDialog] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const query = useQuery<BatchScorecard>({
    queryKey: ['batch-scorecard', batchId],
    queryFn: () => api.getBatchScorecard(batchId),
    retry: false,
    refetchInterval: (q) => (q.state.data?.evaluation?.status === 'running' ? POLL_MS : false),
  })
  const sc = query.data

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['batch-scorecard', batchId] })
    void queryClient.invalidateQueries({ queryKey: ['batch-evaluation', batchId] })
    void queryClient.invalidateQueries({ queryKey: ['batch', batchId] })
    onChanged?.()
  }

  const stopMutation = useMutation({
    mutationFn: () => api.cancelBatchEvaluation(batchId),
    onMutate: () => setActionError(null),
    onSuccess: refresh,
    onError: (e) => setActionError(errText(e)),
  })

  if (query.isLoading) return <div className="panel fine-print pulse">Loading scorecard…</div>
  // Older servers have no scorecard route: render nothing rather than an error.
  if (query.error || !sc) return null

  const ev = sc.evaluation
  const running = ev?.status === 'running'
  const counts = ev?.counts ?? {}
  const scored = counts.scored ?? sc.quality?.scored ?? 0
  const totalRows = Object.values(counts).reduce((a, b) => a + (b ?? 0), 0)
  const classes = sc.classes ?? {}
  const rel = sc.reliability

  const stop = async () => {
    const ok = await askConfirm({
      title: 'Stop evaluation',
      message:
        'Stop the evaluation? Pending targets stay unscored, running evaluator runs are cancelled, and scores already given are kept.',
      confirmLabel: 'Stop evaluation',
      destructive: true,
    })
    if (ok) stopMutation.mutate()
  }

  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <div className="mono-label">
          Scorecard
          {compact && sc.name ? ` · ${sc.name}` : ''}
          {sc.family_label ? <span className="fine-print"> · {sc.family_label}</span> : null}
        </div>
        {!compact && (
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            {running ? (
              <button className="btn btn-sm" disabled={stopMutation.isPending} onClick={stop}>
                Stop evaluation
              </button>
            ) : (
              <button className="btn btn-sm" onClick={() => setDialog(true)}>
                {ev ? 'Re-evaluate' : 'Evaluate'}
              </button>
            )}
          </div>
        )}
      </div>

      <div className="stat-tiles" style={{ marginTop: 8 }}>
        <Tile
          label="Yield"
          value={fmtPct(sc.yield)}
          sub={`${sc.acceptable ?? 0} acceptable of ${sc.targets ?? 0}${
            sc.written_share !== null && sc.written_share !== undefined
              ? ` · written ${fmtPct(sc.written_share)}`
              : ''
          }`}
        />
        <Tile
          label="Quality"
          value={sc.quality?.mean != null ? fmtScore(sc.quality.mean) : 'Not evaluated'}
          sub={
            sc.quality
              ? `n=${sc.quality.scored} scored${
                  sc.quality.median != null ? ` · median ${fmtScore(sc.quality.median)}` : ''
                }`
              : undefined
          }
        />
        <Tile
          label="Cost / acceptable"
          value={dash(sc.cost_per_acceptable_usd, (n) => formatCost(n))}
          sub={`total ${formatCost(sc.cost_usd)}`}
        />
        <Tile
          label="Wh / acceptable"
          value={dash(sc.energy_per_acceptable_wh, (n) => n.toFixed(1))}
          sub={sc.energy_wh != null ? `total ${sc.energy_wh.toFixed(0)} Wh` : undefined}
        />
        <Tile label="Wall time" value={fmtDuration(sc.wall_seconds)} />
        <Tile
          label="Reliability"
          value={`${rel?.infra_failures ?? 0} · ${rel?.timeouts ?? 0} · ${rel?.retries ?? 0}`}
          sub="infra · timeouts · retries"
        />
      </div>

      <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
        {(sc.models ?? []).map((m) => (
          <span key={m.model} className="chip" title={m.model}>
            {m.model} × {m.targets}
          </span>
        ))}
        {Object.entries(classes)
          .filter(([, n]) => (n ?? 0) > 0)
          .map(([k, n]) => (
            <span key={k} className="chip">
              {k} {n}
            </span>
          ))}
      </div>

      <div className="fine-print" style={{ marginTop: 8 }}>
        {ev ? (
          <>
            Evaluation <strong>{ev.status}</strong> · judge {ev.judge_model} · scored {scored}
            {totalRows > 0 ? ` / ${totalRows}` : ''} · cost {formatCost(ev.cost_usd)}
            {ev.budget_usd != null ? ` / ${formatCost(ev.budget_usd)}` : ''}
            {ev.agreement != null
              ? ` · spot-check agreement ${fmtPct(ev.agreement)} (${ev.spot_checks} checks)`
              : ` · no spot checks yet`}
            {(counts.not_evaluated_budget ?? 0) > 0 && ` · ${counts.not_evaluated_budget} not evaluated (budget)`}
            {(counts.failed ?? 0) > 0 && ` · ${counts.failed} failed`}
            {ev.error && <span className="error-text"> · {ev.error}</span>}
          </>
        ) : (
          'Not evaluated. Evaluate scores each finished run against a rubric with a judge model.'
        )}
      </div>
      {actionError && <div className="error-text">{actionError}</div>}

      {!compact && ev?.summary && <BatchReview summary={ev.summary} />}

      {!compact && (sc.outliers ?? []).length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="fine-print">Outliers ({sc.outliers.length})</summary>
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }} className="mono-body">
            {sc.outliers.map((o, i) => (
              <li key={`${o.target_id}-${o.metric}-${i}`}>
                {o.label}: {metricLabel(o.metric)} {fmtMetric(o.metric, o.value)}{' '}
                <span className="fine-print">
                  vs median {fmtMetric(o.metric, o.median)} (z {o.z.toFixed(1)})
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {!compact && (
        <EvaluateDialog
          open={dialog}
          onClose={() => setDialog(false)}
          batchId={batchId}
          pass={ev}
          rerun={ev !== null && ev !== undefined}
          onStarted={refresh}
        />
      )}
    </div>
  )
}

function BatchReview({
  summary,
}: {
  summary: { narrative: string; consistency_notes?: string[]; lessons?: string[] }
}) {
  return (
    <details style={{ marginTop: 8 }}>
      <summary className="fine-print">Batch review</summary>
      <div className="stack" style={{ gap: 8, marginTop: 4 }}>
        <div className="mono-body" style={{ whiteSpace: 'pre-wrap' }}>
          {summary.narrative}
        </div>
        {(summary.consistency_notes ?? []).length > 0 && (
          <div>
            <div className="mono-label">Consistency notes</div>
            <ul style={{ margin: 0, paddingLeft: 18 }} className="mono-body">
              {summary.consistency_notes!.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </div>
        )}
        {(summary.lessons ?? []).length > 0 && (
          <div>
            <div className="mono-label">Lessons for future runs</div>
            <ul style={{ margin: 0, paddingLeft: 18 }} className="mono-body">
              {summary.lessons!.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </details>
  )
}

function EvaluateDialog({
  open,
  onClose,
  batchId,
  pass,
  rerun,
  onStarted,
}: {
  open: boolean
  onClose: () => void
  batchId: string
  pass: BatchScorecard['evaluation'] | undefined
  rerun: boolean
  onStarted: () => void
}) {
  const [judge, setJudge] = useState<string | null>(null)
  const [budget, setBudget] = useState<string | null>(null)
  const [onlyMissing, setOnlyMissing] = useState(true)

  // Blank means auto: the server resolves its preference list against this
  // workspace's keys. A hardcoded fallback here sent a model the deployment may
  // have no key for (cloud has no Anthropic key), which the server rejects.
  const judgeValue = judge ?? pass?.judge_model ?? ''
  // No prefill: the previous pass's estimate leaves out the rows it skipped (a
  // stopped pass covers only what it judged), so a prefilled budget would stop
  // the new pass early. Blank lets the server size the budget for this pass.
  const budgetValue = budget ?? ''

  const mutation = useMutation({
    mutationFn: () =>
      api.startBatchEvaluation(batchId, {
        judge_model: judgeValue.trim() || null,
        budget_usd: budgetValue.trim() ? Number(budgetValue) : null,
        only_missing: onlyMissing,
      }),
    onSuccess: () => {
      onStarted()
      onClose()
    },
  })
  const budgetBad = budgetValue.trim() !== '' && !(Number(budgetValue) > 0)

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={rerun ? 'Re-evaluate batch' : 'Evaluate batch'}
      width={520}
      footer={
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={mutation.isPending || budgetBad}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? 'Starting…' : rerun ? 'Re-evaluate' : 'Evaluate'}
          </button>
        </div>
      }
    >
      <div className="field">
        <label className="mono-label">Judge model</label>
        <input
          type="text"
          value={judgeValue}
          placeholder={JUDGE_PLACEHOLDER}
          onChange={(e) => setJudge(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="mono-label">Budget (USD)</label>
        <input
          type="number"
          min={0}
          step="0.1"
          value={budgetValue}
          placeholder="default"
          onChange={(e) => setBudget(e.target.value)}
        />
        <span className="fine-print">
          Blank: enough for every target this pass will judge, at the expected cost, with 50% headroom.
        </span>
      </div>
      {rerun && (
        <label className="check-row">
          <input
            type="checkbox"
            checked={onlyMissing}
            onChange={(e) => setOnlyMissing(e.target.checked)}
          />
          <span>Only unscored targets (keep scores that are still current)</span>
        </label>
      )}
      {mutation.error && <div className="error-text">{errText(mutation.error)}</div>}
    </Modal>
  )
}
