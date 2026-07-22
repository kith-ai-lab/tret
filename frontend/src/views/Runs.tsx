import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, type RunSummary } from '../api/client'
import { type Column, MonoTable } from '../components/shared/MonoTable'
import { RoutingBadge } from '../components/shared/RoutingBadge'
import { StatusBadge } from '../components/shared/StatusBadge'

const STATUSES = ['queued', 'running', 'completed', 'failed', 'cancelled']

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
      render: (r) => `${r.input_tokens.toLocaleString()} / ${r.output_tokens.toLocaleString()}`,
    },
    { key: 'cost', header: 'Cost', align: 'right', render: (r) => `$${r.cost_usd.toFixed(4)}` },
    { key: 'duration', header: 'Duration', align: 'right', render: (r) => duration(r) },
  ]

  return (
    <div>
      <h1 className="view-title">Runs</h1>
      <div className="view-sub">Every run, with its auditable routing decision.</div>

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
