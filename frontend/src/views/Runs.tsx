import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, type RunSummary } from '../api/client'
import {
  BAND_SHORT,
  COUNTERFACTUAL_SHORT,
  MONEY_PCT_PRECISION_NOTE,
  MONEY_SHORT,
  avoidedFraming,
  avoidedMoneyFraming,
  moneyPctPhrase,
} from '../components/shared/emissions'
import {
  NO_ESTIMATE,
  NO_ESTIMATE_HINT,
  footprintText,
  formatDateTime,
  formatCo2e,
  formatCo2eBand,
  formatCost,
  formatCostSigned,
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

const SHOW_DELEGATED_KEY = 'tret.runs.showDelegated'

/** Read the persisted "show delegated runs" choice. Wrapped in try/catch: a
 *  private window, cleared site data or a blocked storage API must never break
 *  the page — it just falls back to the default (off). */
function readShowDelegated(): boolean {
  try {
    return localStorage.getItem(SHOW_DELEGATED_KEY) === '1'
  } catch {
    return false
  }
}

function writeShowDelegated(value: boolean): void {
  try {
    localStorage.setItem(SHOW_DELEGATED_KEY, value ? '1' : '0')
  } catch {
    // Best effort — the toggle still works for the rest of this session.
  }
}

export function Runs() {
  const navigate = useNavigate()
  const [showDelegated, setShowDelegated] = useState(readShowDelegated)
  const runsQuery = useInfiniteQuery({
    queryKey: ['runs', { showDelegated }],
    queryFn: ({ pageParam }) => api.listRuns(50, pageParam, !showDelegated),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: (query) =>
      (query.state.data?.pages ?? []).some((p) =>
        p.items.some((r) => r.status === 'queued' || r.status === 'running'),
      )
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

  const loadedRuns = useMemo(
    () => runsQuery.data?.pages.flatMap((p) => p.items) ?? [],
    [runsQuery.data],
  )

  const rows = useMemo(() => {
    let out = loadedRuns
    if (statusFilter) out = out.filter((r) => r.status === statusFilter)
    if (modelFilter.trim()) {
      const needle = modelFilter.trim().toLowerCase()
      out = out.filter((r) => (r.model_used ?? '').toLowerCase().includes(needle))
    }
    return out
  }, [loadedRuns, statusFilter, modelFilter])

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
    {
      key: 'task',
      header: 'Task',
      render: (r) =>
        r.parent_run_id ? (
          <span>
            <span style={{ color: 'var(--text-muted)' }}>↳ </span>
            {r.task_type}{' '}
            <span className="chip">{r.delegation_kind === 'subagent' ? 'subagent' : 'delegated'}</span>
          </span>
        ) : (
          r.task_type
        ),
    },
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
    {
      key: 'cost',
      header: 'Cost',
      align: 'right',
      render: (r) => (
        <span>
          {formatCost(r.cost_usd)}
          {(r.delegated_cost_usd ?? 0) > 0 && (
            <>
              <br />
              <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                +{formatCost(r.delegated_cost_usd ?? 0)} delegated
              </span>
            </>
          )}
        </span>
      ),
    },
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
        <label className="mono-label runs-delegated-toggle">
          <input
            type="checkbox"
            checked={showDelegated}
            onChange={(e) => {
              setShowDelegated(e.target.checked)
              writeShowDelegated(e.target.checked)
            }}
          />
          show delegated runs
        </label>
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
          rowLabel={(r) =>
            `Open run: ${harnessName.get(r.harness_id) ?? r.harness_id.slice(0, 8)} · ${r.task_type} · ${r.status}`
          }
          onRowClick={(r) => navigate(`/runs/${r.id}`)}
          empty="No runs yet — start one from the Workbench."
        />
      )}

      {runsQuery.hasNextPage && (
        <div className="row" style={{ justifyContent: 'center', marginTop: 14 }}>
          <button
            type="button"
            onClick={() => runsQuery.fetchNextPage()}
            disabled={runsQuery.isFetchingNextPage}
          >
            {runsQuery.isFetchingNextPage ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  )
}

/** The run's estimated footprint, with its scope split, judgment band, signed
 *  baseline difference and signed money in the tooltip. A run with no estimate
 *  shows an em-dash, never 0 — the full derivation is on the run page.
 *
 *  The carbon figure carries its range on a second line: at list density there is
 *  no room for prose, but there is room to not print a bare point estimate. */
function Footprint({ run }: { run: RunSummary }) {
  const text = footprintText(run.energy_wh, run.co2e_g)
  if (!text) {
    return <span title={NO_ESTIMATE_HINT}>{NO_ESTIMATE}</span>
  }
  const framing = avoidedFraming(run.avoided_co2e_g)
  const money = avoidedMoneyFraming(run.avoided_usd)
  const band = formatCo2eBand(run.co2e_g_low, run.co2e_g_high)
  const hasScopes = run.scope2_g !== null || run.scope3_g !== null
  const scopePart = hasScopes
    ? `scope 1 ${formatCo2e(0)} · scope 2 ${orDash(formatCo2e(run.scope2_g))} · scope 3 ${orDash(formatCo2e(run.scope3_g))}. `
    : 'No scope split recorded for this run. '
  const bandPart = band ? `Range ${band} — ${BAND_SHORT} ` : ''
  return (
    <span
      title={`${scopePart}${bandPart}${framing.label} ${orDash(formatCo2e(run.avoided_co2e_g))} — ${COUNTERFACTUAL_SHORT} ${money.label} ${formatCostSigned(run.avoided_usd)} (${moneyPctPhrase(run.avoided_usd_pct)}) — ${MONEY_SHORT} ${MONEY_PCT_PRECISION_NOTE}`}
    >
      {text}
      {band && <span className="band-under">{band}</span>}
    </span>
  )
}

function duration(r: RunSummary): string {
  if (!r.started_at) return '—'
  const end = r.finished_at ? new Date(r.finished_at).getTime() : Date.now()
  const s = (end - new Date(r.started_at).getTime()) / 1000
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`
}
