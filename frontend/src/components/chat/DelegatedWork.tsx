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
import { useState } from 'react'
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
  const done = rows.filter((r) => r.status !== 'running').length
  return (
    <span className="dw-count">
      {done} of {rows.length} done
    </span>
  )
}

function DelegatedWorkRows({ rows }: { rows: Row[] }) {
  const groups = groupRows(rows)
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
    label: child.delegation_kind === 'subagent' ? 'subagent' : child.task_type || 'task',
    // The persisted run summary has no friendly harness name (only
    // `harness_id`), so it is omitted rather than shown as a raw id.
    harness: null,
    status: child.status,
    costUsd: child.cost_usd,
    batchId: child.delegation_batch_id,
    index: null,
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
