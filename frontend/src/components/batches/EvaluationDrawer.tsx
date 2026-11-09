import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api, type SpotVerdict, type TargetEvaluation } from '../../api/client'
import { formatCost, formatDateTime } from '../shared/format'
import { Modal } from '../shared/Modal'
import { StatusBadge } from '../shared/StatusBadge'
import { flagLabel, fmtScore, fmtSigned, outlierText } from './evalHelpers'
import { errText } from './shared'

function Dots({ score }: { score: number | null }) {
  if (score === null || score === undefined) return <span className="fine-print">n/a</span>
  const n = Math.max(0, Math.min(5, Math.round(score)))
  return (
    <span className="score-dots" aria-label={`${n} of 5`}>
      {'●'.repeat(n)}
      <span style={{ opacity: 0.25 }}>{'●'.repeat(5 - n)}</span>
    </span>
  )
}

/** Detail of one target's evaluation: per-criterion scores with evidence, the
 *  judge's rationale, relative position and the human spot check. Built on
 *  Modal (the app has no side-drawer primitive). */
export function EvaluationDrawer({
  batchId,
  targetId,
  open,
  onClose,
  suggested = false,
}: {
  batchId: string
  targetId: string | null
  open: boolean
  onClose: () => void
  /** The client-side "check" suggestion for this target. */
  suggested?: boolean
}) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['batch-evaluation', batchId],
    queryFn: () => api.getBatchEvaluation(batchId),
    enabled: open,
    retry: false,
  })
  const detail = query.data
  const te: TargetEvaluation | undefined = detail?.targets.find((t) => t.target_id === targetId)
  const rubric = detail?.rubric ?? null
  const pass = detail?.evaluation ?? null

  const [verdict, setVerdict] = useState<SpotVerdict>('agree')
  const [adjusted, setAdjusted] = useState('')
  const [note, setNote] = useState('')

  const reviewMutation = useMutation({
    mutationFn: () =>
      api.reviewTargetEvaluation(batchId, targetId as string, {
        verdict,
        adjusted_quality: verdict === 'adjust' ? Number(adjusted) : null,
        note: note.trim() || null,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['batch-evaluation', batchId] })
      void queryClient.invalidateQueries({ queryKey: ['batch-scorecard', batchId] })
      void queryClient.invalidateQueries({ queryKey: ['batch', batchId] })
      setNote('')
    },
  })
  const adjustedNum = Number(adjusted)
  const adjustBad = verdict === 'adjust' && (adjusted.trim() === '' || !(adjustedNum >= 0 && adjustedNum <= 100))

  const rel = te?.relative ?? null
  const criteria = rubric?.criteria ?? []
  const scoreKeys = te ? Object.keys(te.scores ?? {}) : []
  // Rubric order first, then any score keys the rubric did not list.
  const rows = [
    ...criteria.map((c) => ({ key: c.key, label: c.label, weight: c.weight as number | null })),
    ...scoreKeys
      .filter((k) => !criteria.some((c) => c.key === k))
      .map((k) => ({ key: k, label: k.replace(/_/g, ' '), weight: null })),
  ].filter((r) => te?.scores?.[r.key])

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={te ? `Evaluation · ${te.label}` : 'Evaluation'}
      width={760}
      headerExtra={te ? <StatusBadge status={te.status} /> : undefined}
    >
      {query.isLoading ? (
        <div className="empty pulse">Loading evaluation…</div>
      ) : query.error ? (
        <div className="error-text">{errText(query.error)}</div>
      ) : !te ? (
        <div className="empty">No evaluation for this target in the current pass.</div>
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          <div className="row" style={{ gap: 18, flexWrap: 'wrap' }}>
            <div>
              <div className="mono-label">Quality</div>
              <div className="stat-value" style={{ fontFamily: 'var(--mono)', fontSize: 20 }}>
                {fmtScore(te.quality)}
              </div>
            </div>
            <div>
              <div className="mono-label">Rank</div>
              <div className="mono-body">{rel ? `${rel.rank}/${rel.of}` : '—'}</div>
            </div>
            <div>
              <div className="mono-label">Residual</div>
              <div className={`mono-body ${(rel?.residual ?? 0) < 0 ? 'delta-neg' : 'delta-pos'}`}>
                {fmtSigned(rel?.residual)}
              </div>
            </div>
            <div>
              <div className="mono-label">Difficulty</div>
              <div className="mono-body">{te.difficulty != null ? `${te.difficulty}/5` : '—'}</div>
            </div>
            <div>
              <div className="mono-label">Confidence</div>
              <div className="mono-body">{te.confidence ?? '—'}</div>
            </div>
            {suggested && (
              <div>
                <span className="chip chip-check">check</span>
              </div>
            )}
            {te.stale && (
              <div>
                <span className="chip">stale: target re-ran since this score</span>
              </div>
            )}
          </div>

          {te.status !== 'scored' && (
            <div className="fine-print">
              {te.status === 'failed'
                ? (te.error ?? 'The evaluator did not submit a score.')
                : (te.skip_reason ?? te.error ?? te.status.replace(/_/g, ' '))}
            </div>
          )}

          {rows.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <table className="mono-table">
                <thead>
                  <tr>
                    <th>Criterion</th>
                    <th className="num">Weight</th>
                    <th>Score</th>
                    <th>Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.key}>
                      <td>{r.label}</td>
                      <td className="num">{r.weight ?? '—'}</td>
                      <td>
                        <Dots score={te.scores[r.key]?.score ?? null} />
                      </td>
                      <td style={{ overflowWrap: 'anywhere', maxWidth: 380 }}>
                        {te.scores[r.key]?.evidence || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {te.flags.length > 0 && (
            <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {te.flags.map((f) => (
                <span key={f} className="chip chip-flag">
                  {flagLabel(rubric, f)}
                </span>
              ))}
            </div>
          )}

          {te.rationale && (
            <div>
              <div className="mono-label">Rationale</div>
              <div className="mono-body" style={{ whiteSpace: 'pre-wrap' }}>
                {te.rationale}
              </div>
            </div>
          )}

          {rel && (
            <div className="fine-print">
              Expected {fmtScore(rel.expected)}
              {rel.expected_basis ? `, from ${rel.expected_basis}` : ''} · residual {fmtSigned(rel.residual)} ·
              percentile {fmtScore(rel.percentile)}
              {(rel.outliers ?? []).length > 0 && (
                <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                  {rel.outliers.map((o, i) => (
                    <li key={i}>Outlier: {outlierText(o)}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="fine-print">
            Judge cost {formatCost(te.cost_usd)}
            {pass ? ` · ${pass.judge_model}` : ''}
            {te.evaluator_run_id && (
              <>
                {' · '}
                <Link to={`/runs/${te.evaluator_run_id}`}>evaluator run {te.evaluator_run_id.slice(0, 8)}</Link>
              </>
            )}
          </div>

          {te.review && (
            <div className="callout callout-note">
              <div className="callout-title">
                Spot check: {te.review.verdict}
                {te.review.adjusted_quality != null ? ` → ${te.review.adjusted_quality}` : ''}
              </div>
              <div className="fine-print">
                {te.review.by_name ?? te.review.by ?? 'reviewer'}
                {te.review.at ? ` · ${formatDateTime(te.review.at)}` : ''}
              </div>
              {te.review.note && <div className="mono-body">{te.review.note}</div>}
            </div>
          )}

          {te.status === 'scored' && !te.stale && (
            <div>
              <div className="mono-label">{te.review ? 'Change spot check' : 'Spot check'}</div>
              <div className="row" style={{ gap: 12, flexWrap: 'wrap', marginTop: 4 }}>
                {(['agree', 'disagree', 'adjust'] as const).map((v) => (
                  <label key={v} className="check-row">
                    <input
                      type="radio"
                      name="spot-verdict"
                      checked={verdict === v}
                      onChange={() => setVerdict(v)}
                    />
                    <span>{v[0].toUpperCase() + v.slice(1)}</span>
                  </label>
                ))}
                {verdict === 'adjust' && (
                  <input
                    type="number"
                    min={0}
                    max={100}
                    aria-label="Adjusted quality (0 to 100)"
                    placeholder="0–100"
                    value={adjusted}
                    onChange={(e) => setAdjusted(e.target.value)}
                    style={{ width: 90 }}
                  />
                )}
              </div>
              <div className="field" style={{ marginTop: 6 }}>
                <input
                  type="text"
                  maxLength={500}
                  placeholder="Note (optional)"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </div>
              <button
                className="btn btn-primary btn-sm"
                disabled={reviewMutation.isPending || adjustBad}
                onClick={() => reviewMutation.mutate()}
              >
                {reviewMutation.isPending ? 'Saving…' : 'Save spot check'}
              </button>
              {reviewMutation.error && <div className="error-text">{errText(reviewMutation.error)}</div>}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
