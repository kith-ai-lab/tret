/** Renders a run's delegated children — the work started by `run_harness_task`,
 *  `delegate_parallel`, or `spawn_subagent` — as a compact list of rows linking
 *  to each child run. Two entry points share the same row rendering:
 *
 *  - `DelegatedWork`, mounted in the *live* streaming turn, fed straight from
 *    `useRunStream`'s `delegations` array so a `delegate_parallel` call that
 *    takes minutes reads as progress rather than a hang.
 *  - `DelegatedWorkDisclosure`, mounted on a *completed* turn's pills, which
 *    lazily fetches `api.listRunChildren` on first expand — the pill summaries
 *    ("delegated 3 tasks in parallel: a, b") are not the only evidence of what
 *    ran. */
import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, type RunSummary } from '../../api/client'
import type { DelegationItem } from '../../api/useRunStream'
import { formatCost } from '../shared/format'
import { StatusBadge } from '../shared/StatusBadge'

/** The shape both entry points reduce down to before rendering — a live
 *  `DelegationItem` and a persisted `RunSummary` carry different fields, but
 *  the row only needs these. */
interface Row {
  childRunId: string
  label: string
  harness: string | null
  status: string
  costUsd: number | null
  batchId: string | null
  index: number | null
  /** Live-only, from `delegation_progress` (useRunStream.ts) — undefined for
   *  `childToRow`'s persisted rows, which have no such event to read. */
  iteration?: number | null
  maxIterations?: number | null
  lastTool?: string | null
  /** R18: epoch ms the child started (live: first seen running; persisted: started_at). */
  startedAt?: number | null
  /** R18: epoch ms the child finished, when known. */
  finishedAt?: number | null
  durationMs?: number | null
  /** R18: why a failed child failed. */
  error?: string | null
  stopReason?: string | null
}

/** Failed = anything that ended and did not complete (failed, cancelled,
 *  unknown, or ran out of steps — the last is labelled amber, not hidden). */
export function isFailedStatus(status: string): boolean {
  return status !== 'running' && status !== 'queued' && status !== 'completed'
}

/** R18 header counts. `queued` counts as running (not finished yet). */
export function countRows(rows: { status: string }[]): { done: number; failed: number; running: number } {
  let done = 0
  let failed = 0
  let running = 0
  for (const r of rows) {
    if (r.status === 'running' || r.status === 'queued') running += 1
    else if (r.status === 'completed') done += 1
    else failed += 1
  }
  return { done, failed, running }
}

export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(total / 60)
  const sec = total % 60
  return m > 0 ? `${m}m ${String(sec).padStart(2, '0')}s` : `${sec}s`
}

/** Re-renders once a second while `active`, returning the current time. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [active])
  return now
}

function resolveLabel(
  label: string | null,
  taskType: string | null,
  delegationKind: 'task' | 'subagent',
): string {
  return label ?? taskType ?? (delegationKind === 'subagent' ? 'subagent' : 'task')
}

/** Groups rows by `batchId`, preserving each group's first arrival position so
 *  a single `delegate_parallel` call's children stay together and in order,
 *  without reshuffling unrelated delegations around them. Rows with no
 *  `batchId` each form their own group of one. */
function groupRows(rows: Row[]): { batchId: string | null; rows: Row[] }[] {
  const order: string[] = []
  const groups = new Map<string, Row[]>()
  rows.forEach((row, i) => {
    const key = row.batchId ?? `__single_${i}`
    if (!groups.has(key)) {
      groups.set(key, [])
      order.push(key)
    }
    groups.get(key)!.push(row)
  })
  return order.map((key) => {
    const groupRows = groups.get(key)!
    const batchId = groupRows[0].batchId
    if (batchId) groupRows.sort((a, b) => (a.index ?? 0) - (b.index ?? 0))
    return { batchId, rows: groupRows }
  })
}

function DoneCount({ rows }: { rows: Row[] }) {
  const { done, failed, running } = countRows(rows)
  const anyRunning = running > 0
  const now = useNow(anyRunning)
  const starts = rows.map((r) => r.startedAt).filter((t): t is number => typeof t === 'number')
  let elapsed: string | null = null
  if (starts.length > 0) {
    const first = Math.min(...starts)
    if (anyRunning) elapsed = formatElapsed(now - first)
    else {
      const ends = rows
        .map((r) => r.finishedAt ?? (r.startedAt != null && r.durationMs != null ? r.startedAt + r.durationMs : null))
        .filter((t): t is number => typeof t === 'number')
      if (ends.length > 0) elapsed = formatElapsed(Math.max(...ends) - first)
    }
  }
  const cost = rows.reduce((sum, r) => sum + (r.costUsd ?? 0), 0)
  const hasCost = rows.some((r) => r.costUsd != null)
  return (
    <span className="dw-count">
      {done} done · {failed} failed · {running} running
      {elapsed && <> · {elapsed}</>}
      {hasCost && <> · {formatCost(cost)}</>}
    </span>
  )
}

function RowDetail({ row, now }: { row: Row; now: number }) {
  const running = row.status === 'running' || row.status === 'queued'
  if (running) {
    return (
      <>
        {row.iteration != null && (
          <span className="dw-progress">
            · step {row.iteration}
            {row.maxIterations != null ? `/${row.maxIterations}` : ''}
            {row.lastTool ? ` · ${row.lastTool}` : ''}
          </span>
        )}
        {row.startedAt != null && (
          <span className="dw-progress">· {formatElapsed(now - row.startedAt)}</span>
        )}
      </>
    )
  }
  if (!isFailedStatus(row.status)) return null
  const outOfSteps = row.stopReason === 'out_of_steps'
  return (
    <>
      {outOfSteps && <span className="dw-warn">ran out of steps</span>}
      {row.error && !outOfSteps && (
        <span className="dw-error" title={row.error}>
          {row.error}
        </span>
      )}
    </>
  )
}

function DelegatedWorkRows({ rows }: { rows: Row[] }) {
  const groups = groupRows(rows)
  const now = useNow(rows.some((r) => r.status === 'running'))
  return (
    <div className="dw-rows">
      {groups.map((g, gi) => (
        <div className="dw-group" key={g.batchId ?? `single-${gi}`}>
          {g.batchId && g.rows.length > 1 && (
            <div className="dw-parallel-caption">in parallel</div>
          )}
          {g.rows.map((row) => (
            <Link key={row.childRunId} to={`/runs/${row.childRunId}`} className="dw-row">
              <span className="dw-label">{row.label}</span>
              <RowDetail row={row} now={now} />
              {row.harness && <span className="dw-harness">{row.harness}</span>}
              <StatusBadge status={row.status} />
              {row.costUsd != null && <span className="dw-cost">{formatCost(row.costUsd)}</span>}
            </Link>
          ))}
        </div>
      ))}
    </div>
  )
}

/** Mounted in the streaming assistant turn — reads live delegation state
 *  straight off `useRunStream`. Renders nothing until the first child starts. */
export function DelegatedWork({ items }: { items: DelegationItem[] }) {
  if (items.length === 0) return null
  const rows: Row[] = items.map((item) => ({
    childRunId: item.childRunId,
    label: resolveLabel(item.label, item.taskType, item.delegationKind),
    harness: item.harness,
    status: item.status,
    costUsd: item.costUsd,
    batchId: item.batchId,
    index: item.index,
    iteration: item.iteration,
    maxIterations: item.maxIterations,
    lastTool: item.lastTool,
    startedAt: item.startedAt ?? null,
    durationMs: item.durationMs ?? null,
    error: item.error ?? null,
    stopReason: item.stopReason ?? null,
  }))
  return (
    <div className="dw-block">
      <div className="dw-heading">
        Delegated work <DoneCount rows={rows} />
      </div>
      <DelegatedWorkRows rows={rows} />
    </div>
  )
}

function childToRow(child: RunSummary): Row {
  return {
    childRunId: child.id,
    // Prefer the model-written label persisted on the child (survives past
    // the turn that started it) over the generic task-type/"subagent"
    // fallback — otherwise a completed batch's rows are indistinguishable
    // (the live bug: three "divergence_assessment" rows where streaming had
    // shown three different names).
    label:
      child.delegation_label ||
      (child.delegation_kind === 'subagent' ? 'subagent' : child.task_type || 'task'),
    // The persisted run summary has no friendly harness name (only
    // `harness_id`), so it is omitted rather than shown as a raw id.
    harness: null,
    status: child.status,
    costUsd: child.cost_usd,
    batchId: child.delegation_batch_id,
    index: null,
    startedAt: child.started_at ? Date.parse(child.started_at) : null,
    finishedAt: child.finished_at ? Date.parse(child.finished_at) : null,
    error: child.error,
    stopReason: child.stop_reason ?? null,
  }
}

function ChevronIcon() {
  return (
    <svg
      className="dw-caret"
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}

/** Mounted on a completed turn's pills. Collapsed by default; fetches the
 *  run's children only once expanded. */
export function DelegatedWorkDisclosure({ runId }: { runId: string }) {
  const [expanded, setExpanded] = useState(false)
  const { data, isLoading, isError } = useQuery({
    queryKey: ['run-children', runId],
    queryFn: () => api.listRunChildren(runId),
    enabled: expanded,
    staleTime: 60_000,
  })

  return (
    <details
      className="dw-disclosure"
      open={expanded}
      onToggle={(e) => setExpanded((e.target as HTMLDetailsElement).open)}
    >
      <summary>
        <ChevronIcon />
        Delegated work
        {data && data.length > 0 && <DoneCount rows={data.map(childToRow)} />}
      </summary>
      <div className="dw-disclosure-body">
        {isLoading && <div className="dw-muted">Loading…</div>}
        {!isLoading && isError && <div className="dw-muted">Could not load delegated runs.</div>}
        {!isLoading && !isError && (data?.length ?? 0) === 0 && (
          <div className="dw-muted">No delegated runs recorded.</div>
        )}
        {!isLoading && !isError && data && data.length > 0 && (
          <DelegatedWorkRows rows={data.map(childToRow)} />
        )}
      </div>
    </details>
  )
}
