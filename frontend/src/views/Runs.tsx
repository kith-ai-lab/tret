import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, type RunSummary } from '../api/client'
import { COUNTERFACTUAL_SHORT, avoidedFraming } from '../components/shared/emissions'
import {
  NO_ESTIMATE,
  NO_ESTIMATE_HINT,
  footprintText,
  formatCo2e,
  formatCost,
  formatTokens,
  orDash,
} from '../components/shared/format'
import { type Column, MonoTable } from '../components/shared/MonoTable'
import { RoutingBadge } from '../components/shared/RoutingBadge'
import { StatusBadge } from '../components/shared/StatusBadge'

const STATUSES = [
  'queued',
  'running',
  'completed',
  'completed_without_output',
  'failed',
  'cancelled',
]

export function Runs() {
  const navigate = useNavigate()
  const runsQuery = useQuery({
    queryKey: ['runs'],
    queryFn: () => api.listRuns(200),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((r) => r.status === 'queued' || r.status === 'running')
        ? 4000
        : false,
  })
  const harnessesQuery = useQuery({ queryKey: ['harnesses'], queryFn: api.listHarnesses })

  const [statusFilter, setStatusFilter] = useState('')
  const [modelFilter, setModelFilter] = useState('')

  const harnessName = useMemo(() => {
    const map = new Map<string, string>()
    for (const h of harnessesQuery.data ?? []) map.set(h.id, h.name)
    return map
  }, [harnessesQuery.data])

  const rows = useMemo(() => {
    let out = runsQuery.data ?? []
    if (statusFilter) out = out.filter((r) => r.status === statusFilter)
    if (modelFilter.trim()) {
      const needle = modelFilter.trim().toLowerCase()
      out = out.filter((r) => (r.model_used ?? '').toLowerCase().includes(needle))
    }
    return out
  }, [runsQuery.data, statusFilter, modelFilter])

  const columns: Column<RunSummary>[] = [
    {
      key: 'time',
      header: 'Time',
      render: (r) => (r.created_at ? formatDateTime(r.created_at) : '—'),
    },
    {
      key: 'harness',
      header: 'Harness',
      render: (r) => harnessName.get(r.harness_id) ?? r.harness_id.slice(0, 8),
    },
    { key: 'task', header: 'Task', render: (r) => r.task_type },
    {
      key: 'routing',
      header: 'Model',
      render: (r) => <RoutingBadge routing={r.routing} />,
    },
    { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
    {
      key: 'tokens',
      header: 'Tokens',
      align: 'right',
      render: (r) => (
        <span
          title={`cache ${formatTokens(r.cache_read_tokens)} read / ${formatTokens(r.cache_write_tokens)} write`}
        >
          {formatTokens(r.input_tokens)} / {formatTokens(r.output_tokens)}
        </span>
      ),
    },
    { key: 'cost', header: 'Cost', align: 'right', render: (r) => formatCost(r.cost_usd) },
    {
      key: 'footprint',
      header: 'Footprint',
      align: 'right',
      render: (r) => <Footprint run={r} />,
    },
    { key: 'duration', header: 'Duration', align: 'right', render: (r) => duration(r) },
  ]

  return (
    <div>
      <h1 className="view-title">Runs</h1>
      <div className="view-sub">
        Every run, with its auditable routing decision. Footprint figures are estimates, not
        measurements.
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          style={{ width: 180 }}
        >
          <option value="">all statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="filter by model…"
          value={modelFilter}
          onChange={(e) => setModelFilter(e.target.value)}
          style={{ width: 240 }}
        />
        <span className="mono-label">{rows.length} runs</span>
      </div>

      {runsQuery.isLoading ? (
        <div className="empty pulse">Loading runs…</div>
      ) : runsQuery.isError ? (
        <div className="error-text">{(runsQuery.error as Error).message}</div>
      ) : (
        <MonoTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.id}
          onRowClick={(r) => navigate(`/runs/${r.id}`)}
          empty="No runs yet — start one from the Workbench."
        />
      )}
    </div>
  )
}

/** The run's estimated footprint, with its scope split and signed baseline
 *  difference in the tooltip. A run with no estimate shows an em-dash, never 0 —
 *  the full derivation is on the run page. */
function Footprint({ run }: { run: RunSummary }) {
  const text = footprintText(run.energy_wh, run.co2e_g)
  if (!text) {
    return <span title={NO_ESTIMATE_HINT}>{NO_ESTIMATE}</span>
  }
  const framing = avoidedFraming(run.avoided_co2e_g)
  const hasScopes = run.scope2_g !== null || run.scope3_g !== null
  const scopePart = hasScopes
    ? `scope 1 ${formatCo2e(0)} · scope 2 ${orDash(formatCo2e(run.scope2_g))} · scope 3 ${orDash(formatCo2e(run.scope3_g))}. `
    : 'No scope split recorded for this run. '
  return (
    <span
      title={`${scopePart}${framing.label} ${orDash(formatCo2e(run.avoided_co2e_g))} — ${COUNTERFACTUAL_SHORT}`}
    >
      {text}
    </span>
  )
}

function duration(r: RunSummary): string {
  if (!r.started_at) return '—'
  const end = r.finished_at ? new Date(r.finished_at).getTime() : Date.now()
  const s = (end - new Date(r.started_at).getTime()) / 1000
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`
}

export function formatDateTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}
