import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { api, type PairwiseScope, type PairwiseStatus, type PairwiseSummary } from '../../api/client'
import { formatCost } from '../shared/format'
import { useConfirm } from '../shared/ConfirmDialog'
import { errText } from './shared'

const ROLES_THAT_APPROVE = ['owner', 'admin', 'approver']

/** One line of tally: "A 3 · B 1 · ties 2 · inconsistent 0 · cost $0.4100". */
export function PairwiseTally({
  a,
  b,
  tie,
  inconsistent,
  cost,
}: {
  a: number
  b: number
  tie: number
  inconsistent: number
  cost: number
}) {
  return (
    <span>
      A {a} · B {b} · ties {tie} · inconsistent {inconsistent} · cost {formatCost(cost)}
    </span>
  )
}

/** Blind pairwise judge control for the compare page. Verdicts sit beside the
 *  rubric scores; they never change them. Polls only while the server says a
 *  pass is running, and refetches the comparison once when it goes idle. */
export function PairwisePanel({
  a,
  b,
  summary,
}: {
  a: string
  b: string
  summary?: PairwiseSummary | null
}) {
  const queryClient = useQueryClient()
  const askConfirm = useConfirm()
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const canRun = ROLES_THAT_APPROVE.includes(meQuery.data?.role ?? '')

  const [scope, setScope] = useState<PairwiseScope>('near_ties')
  const [maxPairs, setMaxPairs] = useState('20')
  const [budget, setBudget] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)

  const statusQuery = useQuery<PairwiseStatus>({
    queryKey: ['batch-pairwise', a, b],
    queryFn: () => api.getPairwise(a, b),
    retry: false,
    // The comparison's own flag lets the first poll start without waiting for this query.
    refetchInterval: (q) => ((q.state.data?.status ?? (summary?.running ? 'running' : 'idle')) === 'running' ? 3000 : false),
  })
  const st = statusQuery.data
  const running = st?.status === 'running'

  // Idle after running: refetch the comparison once so the row column fills in.
  const wasRunning = useRef(false)
  useEffect(() => {
    if (running) {
      wasRunning.current = true
    } else if (wasRunning.current && st) {
      wasRunning.current = false
      void queryClient.invalidateQueries({ queryKey: ['batch-compare', a, b] })
    }
  }, [running, st, queryClient, a, b])

  // POST responses carry no `rows`: keep the cached ones (BatchCompare reads them from the
  // same key) and refetch for fresh ones.
  const setStatus = (s: PairwiseStatus) => {
    queryClient.setQueryData<PairwiseStatus>(['batch-pairwise', a, b], (prev) => ({
      ...s,
      rows: s.rows ?? prev?.rows,
    }))
    void queryClient.invalidateQueries({ queryKey: ['batch-pairwise', a, b] })
    void queryClient.invalidateQueries({ queryKey: ['batch-compare', a, b] })
  }

  const pairs = Math.max(1, Math.min(100, Math.floor(Number(maxPairs)) || 1))
  const eligible = st?.scope_eligible?.[scope] ?? (scope === 'near_ties' ? summary?.eligible_near_ties : undefined)
  const toJudge = eligible != null ? Math.min(pairs, eligible) : pairs
  const budgetNum = budget.trim() === '' ? null : Number(budget)
  const budgetBad = budgetNum !== null && (!Number.isFinite(budgetNum) || budgetNum <= 0)

  const start = useMutation({
    mutationFn: () =>
      api.startPairwise({ a, b, scope, max_pairs: pairs, budget_usd: budgetNum }),
    onSuccess: (s) => {
      setActionError(null)
      setStatus(s)
    },
    onError: (e) => setActionError(errText(e)),
  })
  const cancel = useMutation({
    mutationFn: () => api.cancelPairwise(a, b),
    onSuccess: (s) => {
      setActionError(null)
      setStatus(s)
    },
    onError: (e) => setActionError(errText(e)),
  })

  const run = async () => {
    const calls = toJudge * 2
    const ok = await askConfirm({
      title: 'Run the pairwise judge',
      message: `This asks the judge model to compare up to ${toJudge} ${toJudge === 1 ? 'pair' : 'pairs'} blind, in both presentation orders: up to ${calls} judge calls, billed to this workspace.${budgetNum != null ? ` Spend is capped at ${formatCost(budgetNum)}.` : ' Spend is capped by an automatic budget.'} Rubric scores are not changed.`,
      confirmLabel: 'Run judge',
    })
    if (ok) start.mutate()
  }

  const done = (st?.judged ?? 0) + (st?.failed ?? 0) + (st?.skipped ?? 0)
  const total = done + (st?.pending ?? 0) + (st?.running ?? 0)
  const v = st?.verdicts
  const tallyHas = st != null && (st.judged > 0 || st.stale > 0)

  return (
    <div className="panel" style={{ margin: '14px 0' }}>
      <div className="mono-label">Pairwise judge</div>
      <div className="fine-print" style={{ marginTop: 4 }}>
        A judge sees each pair of outputs side by side, blind to model, cost and batch, in both
        orders, and says which delivers better. It sits beside the rubric scores and never changes
        them.
      </div>
      {
        <>
          {statusQuery.error && !st && (
            <div className="error-text" style={{ marginTop: 6 }}>
              Could not load the pairwise status: {errText(statusQuery.error)}
            </div>
          )}
          <div className="mono-body" style={{ marginTop: 6 }}>
            Eligible pairs: near ties {st?.scope_eligible?.near_ties ?? summary?.eligible_near_ties ?? '—'} ·
            all both-delivered {st?.scope_eligible?.both_delivered ?? '—'}
          </div>
          <div className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'flex-end', marginTop: 8 }}>
            <label className="field" style={{ marginBottom: 0 }}>
              <span className="fine-print">Scope</span>
              <select
                value={scope}
                disabled={running}
                onChange={(e) => setScope(e.target.value as PairwiseScope)}
              >
                <option value="near_ties">Near ties (within 5 points)</option>
                <option value="both_delivered">All both-delivered rows</option>
              </select>
            </label>
            <label className="field" style={{ marginBottom: 0 }}>
              <span className="fine-print">Max pairs (1–100)</span>
              <input
                type="number"
                min={1}
                max={100}
                value={maxPairs}
                disabled={running}
                onChange={(e) => setMaxPairs(e.target.value)}
                style={{ width: 90 }}
              />
            </label>
            <label className="field" style={{ marginBottom: 0 }}>
              <span className="fine-print">Budget, $ (optional)</span>
              <input
                type="number"
                min={0}
                step="0.5"
                value={budget}
                disabled={running}
                placeholder="automatic"
                onChange={(e) => setBudget(e.target.value)}
                style={{ width: 110 }}
              />
            </label>
            {running ? (
              <button className="btn btn-sm" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
                Cancel
              </button>
            ) : (
              <button
                className="btn btn-primary"
                disabled={!canRun || start.isPending || budgetBad || eligible === 0}
                title={
                  !canRun
                    ? 'Requires approver role'
                    : eligible === 0
                      ? 'No eligible pairs in this scope'
                      : undefined
                }
                onClick={run}
              >
                {start.isPending ? 'Starting…' : 'Run pairwise judge'}
              </button>
            )}
          </div>
          {budgetBad && <div className="error-text">Budget must be a positive number.</div>}
          {actionError && <div className="error-text">{actionError}</div>}
          {running && st && (
            <div className="mono-body pulse" style={{ marginTop: 8 }} role="status">
              Judging… {done} of {total} pairs finished
              {st.running > 0 ? `, ${st.running} in flight` : ''}
              {st.budget_usd != null ? ` · budget ${formatCost(st.budget_usd)}` : ''}
            </div>
          )}
          {tallyHas && v && (
            <div className="mono-body" style={{ marginTop: 8 }}>
              <strong>
                <PairwiseTally a={v.a} b={v.b} tie={v.tie} inconsistent={v.inconsistent} cost={st.cost_usd} />
              </strong>
              <div className="fine-print">
                {st.judged} judged
                {st.failed > 0 ? ` · ${st.failed} failed` : ''}
                {st.skipped > 0 ? ` · ${st.skipped} skipped` : ''}
                {st.stale > 0 ? ` · ${st.stale} stale (a target was retried; re-run to refresh)` : ''}
              </div>
            </div>
          )}
        </>
      }
    </div>
  )
}
