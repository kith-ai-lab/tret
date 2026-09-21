/** Delegation lineage for a run: who delegated to it, what it delegated to,
 *  and the whole tree's totals. Renders nothing at all — no empty panel, no
 *  heading — for an ordinary run that neither has a parent nor delegated to
 *  anything, so the common case looks exactly as it always has.
 *
 *  Cost semantics stay honest here: a run's own `cost_usd`/`delegated_cost_usd`
 *  are never summed into one figure — they are shown side by side, and the
 *  tree total is its own separate line. */
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'

import { api, type RunDetail, type RunSummary } from '../../api/client'
import {
  NO_ESTIMATE,
  NO_ESTIMATE_HINT,
  footprintText,
  formatCo2eBand,
  formatCost,
  formatDateTime,
  formatWh,
} from '../shared/format'
import { type Column, MonoTable, QueryError } from '../shared/MonoTable'
import { RoutingBadge } from '../shared/RoutingBadge'
import { StatusBadge } from '../shared/StatusBadge'

const NON_TERMINAL = ['queued', 'running']

function kindChipText(kind: RunSummary['delegation_kind']): string {
  if (kind === 'subagent') return 'subagent'
  if (kind === 'task') return 'specialist task'
  return '—'
}

export function DelegationPanel({ run }: { run: RunDetail }) {
  const navigate = useNavigate()

  // The run row itself says whether it sits in a delegation tree: `tree` is
  // non-null once any child exists, and a child carries its parent's id.
  const hasLineageSignal =
    !!run.parent_run_id || !!run.tree || (run.delegated_cost_usd ?? 0) > 0
  const runActive = NON_TERMINAL.includes(run.status)

  const childrenQuery = useQuery({
    queryKey: ['run-children', run.id],
    queryFn: () => api.listRunChildren(run.id),
    // Asked only where there can be an answer: a run with lineage, or one still
    // running (its first child may not exist yet). An ordinary finished run
    // never fetches, so a missing or failing endpoint — this frontend deployed
    // ahead of its backend — cannot put an error panel on every run page.
    enabled: !!run.id && (hasLineageSignal || runActive),
    refetchInterval: (query) => {
      const childActive = (query.state.data ?? []).some((c) => NON_TERMINAL.includes(c.status))
      return runActive || childActive ? 3000 : false
    },
  })

  const children = childrenQuery.data ?? []
  // Loading and error states only show where delegation is already known, so a
  // run that delegated nothing never flashes an empty section.
  if (!hasLineageSignal && children.length === 0) return null

  const batchSizes = new Map<string, number>()
  for (const c of children) {
    if (c.delegation_batch_id)
      batchSizes.set(c.delegation_batch_id, (batchSizes.get(c.delegation_batch_id) ?? 0) + 1)
  }

  // First row of each parallel batch gets a subtle "parallel batch" marker.
  const seenBatches = new Set<string>()
  const isFirstOfBatch = new Map<string, boolean>()
  for (const c of children) {
    if (!c.delegation_batch_id) continue
    // A batch of one ran nothing in parallel; say so only when it is true.
    if ((batchSizes.get(c.delegation_batch_id) ?? 0) < 2) continue
    if (seenBatches.has(c.delegation_batch_id)) continue
    seenBatches.add(c.delegation_batch_id)
    isFirstOfBatch.set(c.id, true)
  }

  const columns: Column<RunSummary>[] = [
    {
      key: 'work',
      header: 'Work',
      render: (c) => (
        <>
          {isFirstOfBatch.get(c.id) && (
            <div
              style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}
            >
              parallel batch
            </div>
          )}
          <span className="mono-body">
            {c.delegation_kind === 'subagent' ? 'subagent' : c.task_type}
          </span>
        </>
      ),
    },
    {
      key: 'kind',
      header: 'Kind',
      render: (c) => <span className="chip">{kindChipText(c.delegation_kind)}</span>,
    },
    { key: 'status', header: 'Status', render: (c) => <StatusBadge status={c.status} /> },
    { key: 'model', header: 'Model', render: (c) => <RoutingBadge routing={c.routing} /> },
    {
      key: 'cost',
      header: 'Cost',
      align: 'right',
      render: (c) => (
        <span>
          {formatCost(c.cost_usd)}
          {(c.delegated_cost_usd ?? 0) > 0 && (
            <span style={{ color: 'var(--text-muted)' }}> + {formatCost(c.delegated_cost_usd ?? 0)} delegated</span>
          )}
        </span>
      ),
    },
    {
      key: 'footprint',
      header: 'Footprint',
      align: 'right',
      render: (c) => <ChildFootprint child={c} />,
    },
    {
      key: 'started',
      header: 'Started',
      render: (c) => (c.started_at ? formatDateTime(c.started_at) : '—'),
    },
  ]

  return (
    <div className="panel delegation-panel">
      <div className="mono-label" style={{ marginBottom: 10 }}>
        Delegation
      </div>

      {run.parent_run_id && (
        <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
          <span className="mono-label" style={{ textTransform: 'none', letterSpacing: 0 }}>
            Delegated by
          </span>
          <Link to={`/runs/${run.parent_run_id}`} className="mono-body">
            {run.parent_run_id.slice(0, 8)}
          </Link>
          <span className="chip">{kindChipText(run.delegation_kind)}</span>
          {run.root_run_id && run.root_run_id !== run.parent_run_id && (
            <>
              <span className="mono-label" style={{ textTransform: 'none', letterSpacing: 0 }}>
                Root run
              </span>
              <Link to={`/runs/${run.root_run_id}`} className="mono-body">
                {run.root_run_id.slice(0, 8)}
              </Link>
            </>
          )}
        </div>
      )}

      {childrenQuery.isLoading ? (
        <div className="empty pulse">Loading delegated runs…</div>
      ) : childrenQuery.isError ? (
        <QueryError error={childrenQuery.error} what="delegated runs" />
      ) : (
        <>
          {children.length > 0 && (
            <div className="mono-label" style={{ marginBottom: 6 }}>
              Delegated runs ({children.length})
            </div>
          )}
          <MonoTable
            columns={columns}
            rows={children}
            rowKey={(c) => c.id}
            rowLabel={(c) =>
              `Open run: ${c.delegation_kind === 'subagent' ? 'subagent' : c.task_type || 'task'} · ${c.status}`
            }
            onRowClick={(c) => navigate(`/runs/${c.id}`)}
            empty="No delegated runs."
          />
        </>
      )}

      {run.tree && (
        <div style={{ marginTop: 12 }}>
          <div className="mono-body">
            {run.tree.run_count} runs in this tree · {formatCost(run.tree.cost_usd)} total ·{' '}
            {formatWh(run.tree.energy_wh) ?? NO_ESTIMATE}
          </div>
          <div
            style={{ marginTop: 4, fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--text-muted)' }}
          >
            Each run&rsquo;s own cost covers only its own model calls; delegated work is counted on
            the run that did it.
          </div>
          {(run.delegated_cost_usd ?? 0) > 0 && (
            <div className="mono-body" style={{ marginTop: 6 }}>
              This run: {formatCost(run.cost_usd)} own + {formatCost(run.delegated_cost_usd ?? 0)} delegated
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** Same footprint rendering as the Runs list's own Footprint cell: an em-dash
 *  (never 0) when the run carries no estimate, and the judgment band under the
 *  point figure when the run recorded one. */
function ChildFootprint({ child }: { child: RunSummary }) {
  const text = footprintText(child.energy_wh, child.co2e_g)
  if (!text) return <span title={NO_ESTIMATE_HINT}>{NO_ESTIMATE}</span>
  const band = formatCo2eBand(child.co2e_g_low, child.co2e_g_high)
  return (
    <span>
      {text}
      {band && <span className="band-under">{band}</span>}
    </span>
  )
}
