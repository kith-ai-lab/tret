import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  api,
  type GuardrailEnergyStat,
  type GuardrailHarnessStat,
  type GuardrailMethodError,
  type GuardrailMethodStat,
  type RoutingGroup,
  type RoutingModelPrior,
} from '../api/client'
import {
  formatCo2eScaled,
  formatDateTime,
  formatEnergyScaled,
  formatFactor,
  formatTokens,
  orDash,
} from '../components/shared/format'
import { type Column, MonoTable } from '../components/shared/MonoTable'
import { RoutingHistoryPanel } from '../components/shared/RoutingHistory'

const WINDOWS = [7, 30, 90, 365]

/** Guardrail analytics: are the trust guardrails firing, and how often?
 *  Method reliability (the deterministic lane) plus validation pressure (how
 *  hard structured-output validation is pushing back), per harness. */
export function Analytics() {
  const [days, setDays] = useState(30)
  const guardrailsQuery = useQuery({
    queryKey: ['guardrails', days],
    queryFn: () => api.guardrailAnalytics(days),
  })
  // The routing track record reads a longer window than the guardrail panels: an
  // outcome is one data point about a model, and thirty days of them is rarely
  // enough to say anything. The window selector above deliberately does not
  // drive it, so shortening the guardrail view cannot quietly empty the
  // evidence panel.
  const routingQuery = useQuery({
    queryKey: ['routing-analytics'],
    queryFn: () => api.routingAnalytics(),
  })
  const historyQuery = useQuery({
    queryKey: ['routing-history'],
    queryFn: () => api.routingHistory(),
  })

  const data = guardrailsQuery.data
  const routing = routingQuery.data

  return (
    <div className="stack" style={{ gap: 28 }}>
      <div>
        <h1 className="view-title">Analytics</h1>
        <div className="view-sub">
          Guardrails for code execution: how often the deterministic method lane fails, how hard
          structured-output validation is pushing back, and what the window drew in compute.
        </div>
      </div>

      <div className="row">
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ width: 180 }}>
          {WINDOWS.map((d) => (
            <option key={d} value={d}>
              last {d} days
            </option>
          ))}
        </select>
        {data && (
          <span className="mono-label">
            {formatTokens(data.totals.runs_scanned)} runs scanned (cap{' '}
            {formatTokens(data.totals.runs_scan_limit)})
          </span>
        )}
      </div>

      {guardrailsQuery.isLoading ? (
        <div className="empty pulse">Loading guardrail analytics…</div>
      ) : guardrailsQuery.isError ? (
        <div className="error-text">{(guardrailsQuery.error as Error).message}</div>
      ) : !data ? null : (
        <>
          <div className="panel config-stats">
            <Stat label="Method runs" value={formatTokens(data.totals.method_runs)} />
            <Stat label="Method failures" value={formatTokens(data.totals.method_failures)} />
            <Stat
              label="Method failure rate"
              value={`${data.totals.method_failure_rate_pct}%`}
              color={rateColor(data.totals.method_failure_rate_pct)}
            />
            <Stat label="Validation errors" value={formatTokens(data.totals.validation_errors)} />
            <Stat
              label="Unrecovered"
              value={formatTokens(data.totals.unrecovered_validation_errors)}
              color={data.totals.unrecovered_validation_errors > 0 ? 'var(--red)' : undefined}
            />
          </div>

          <Section
            title="Method reliability"
            hint="Deterministic method runs grouped by slug — a rising failure rate means the code lane, not the model, is the problem."
          >
            <MonoTable
              columns={METHOD_COLUMNS}
              rows={data.methods}
              rowKey={(m) => m.method_slug}
              empty="No method runs in this window."
            />
          </Section>

          <Section
            title="Validation pressure"
            hint="How often structured-output validation rejected a model payload, per harness. Unrecovered errors exhausted the repair budget."
          >
            <MonoTable
              columns={HARNESS_COLUMNS}
              rows={data.harnesses}
              rowKey={(h) => h.harness_id}
              empty="No runs scanned in this window."
            />
          </Section>

          <Section title="Recent method errors" hint="Most recent failures in the deterministic lane, verbatim.">
            <MonoTable
              columns={ERROR_COLUMNS}
              rows={data.recent_method_errors}
              rowKey={(e) => `${e.at ?? ''}-${e.method_slug}-${e.error.slice(0, 24)}`}
              empty="No method errors in this window."
            />
          </Section>

          <Section
            title="Is the router learning?"
            hint="Which model got picked for each shape of task over time, how well each one did, and the moments the router changed its mind. Amber dots mark buckets where a run had to change model mid-flight."
          >
            {historyQuery.isLoading ? (
              <div className="empty pulse">Loading routing history…</div>
            ) : historyQuery.isError ? (
              <div className="error-text">{(historyQuery.error as Error).message}</div>
            ) : historyQuery.data ? (
              <RoutingHistoryPanel history={historyQuery.data} />
            ) : null}
          </Section>

          <Section
            title="Routing track record"
            hint="How each model has actually done, per shape of task and objective — the same evidence the router reads when it chooses. Compare within a group only."
          >
            {routingQuery.isLoading ? (
              <div className="empty pulse">Loading routing evidence…</div>
            ) : routingQuery.isError ? (
              <div className="error-text">{(routingQuery.error as Error).message}</div>
            ) : !routing || routing.groups.length === 0 ? (
              <div className="empty">
                No scored runs yet. Outcomes are recorded as runs finish; run{' '}
                <code>bench outcomes backfill</code> to score the runs already in the database.
              </div>
            ) : (
              <div className="stack" style={{ gap: 16 }}>
                {routing.groups.map((g) => (
                  <RoutingGroupTable key={`${g.task_shape}-${g.objective}`} group={g} />
                ))}
                <div
                  style={{
                    fontFamily: 'var(--mono)',
                    fontSize: 10.5,
                    color: 'var(--text-muted)',
                  }}
                >
                  {/* The backend's own wording, verbatim. These numbers are the
                      easiest thing on this page to over-read, and the caveat has
                      to travel with them rather than living in a doc. */}
                  {routing.basis.note} Evidence halves in weight every{' '}
                  {routing.basis.half_life_days} days; a model needs{' '}
                  {routing.basis.minimum_effective_samples} effective samples before it appears
                  at all. {routing.basis.quality_ignores_cost} ({routing.score_version} /{' '}
                  {routing.priors_version})
                </div>
              </div>
            )}
          </Section>

          {/* The energy rollup this endpoint has always returned. It is NOT the
              same figure as the Emissions view's: this one is compute-only and
              recomputed at today's grid factor, that one sums each run's
              as-recorded carbon. The difference is stated rather than smoothed
              over, and the link points at the authoritative view. */}
          <Section
            title="Compute energy per harness"
            hint="Estimated compute energy over every run in the window — not the bounded scan above. Runs with no estimate are excluded, never counted as zero."
          >
            <MonoTable
              columns={ENERGY_COLUMNS}
              rows={data.energy}
              rowKey={(e) => e.harness_id}
              empty="No run in this window carries an energy estimate."
            />
            <div className="panel config-stats" style={{ marginTop: 10 }}>
              <Stat label="Runs with an estimate" value={formatTokens(data.totals.runs_with_energy)} />
              <Stat
                label="Compute energy (est.)"
                value={orDash(formatEnergyScaled(data.totals.energy_wh))}
              />
              <Stat
                label="Carbon at today's factor (est.)"
                value={orDash(formatCo2eScaled(data.totals.co2e_g))}
              />
              <Stat
                label="Grid factor"
                value={`${formatFactor(data.energy_basis.grid_co2e_g_per_kwh, 1)} g/kWh`}
              />
            </div>
            <div
              style={{
                marginTop: 8,
                fontFamily: 'var(--mono)',
                fontSize: 10.5,
                color: 'var(--text-muted)',
              }}
            >
              {/* The backend's own wording, verbatim — it is the thing that keeps
                  this carbon column from being read as the as-recorded figure. */}
              {data.energy_basis.estimated ? 'Estimated, never metered. ' : ''}
              {data.energy_basis.co2e_basis} — the <Link to="/emissions">Emissions</Link> view is
              that rollup.
            </div>
          </Section>
        </>
      )}
    </div>
  )
}

const METHOD_COLUMNS: Column<GuardrailMethodStat>[] = [
  { key: 'slug', header: 'Method', render: (m) => m.method_slug },
  { key: 'runs', header: 'Runs', align: 'right', render: (m) => formatTokens(m.runs) },
  { key: 'completed', header: 'Completed', align: 'right', render: (m) => formatTokens(m.completed) },
  { key: 'failed', header: 'Failed', align: 'right', render: (m) => formatTokens(m.failed) },
  {
    key: 'rate',
    header: 'Failure rate',
    align: 'right',
    render: (m) => <span style={{ color: rateColor(m.failure_rate_pct) }}>{m.failure_rate_pct}%</span>,
  },
]

const HARNESS_COLUMNS: Column<GuardrailHarnessStat>[] = [
  { key: 'harness', header: 'Harness', render: (h) => h.harness_name },
  { key: 'runs', header: 'Runs', align: 'right', render: (h) => formatTokens(h.runs) },
  {
    key: 'runs_with',
    header: 'Runs w/ error',
    align: 'right',
    render: (h) => formatTokens(h.runs_with_validation_error),
  },
  {
    key: 'errors',
    header: 'Validation errors',
    align: 'right',
    render: (h) => formatTokens(h.validation_errors),
  },
  {
    key: 'unrecovered',
    header: 'Unrecovered',
    align: 'right',
    render: (h) => (
      <span style={{ color: h.unrecovered_validation_errors > 0 ? 'var(--red)' : undefined }}>
        {formatTokens(h.unrecovered_validation_errors)}
      </span>
    ),
  },
  {
    key: 'rate',
    header: 'Run error rate',
    align: 'right',
    render: (h) => (
      <span style={{ color: rateColor(h.run_error_rate_pct) }}>{h.run_error_rate_pct}%</span>
    ),
  },
]

const ENERGY_COLUMNS: Column<GuardrailEnergyStat>[] = [
  { key: 'harness', header: 'Harness', render: (e) => e.harness_name },
  {
    key: 'runs',
    header: 'Runs w/ estimate',
    align: 'right',
    render: (e) => formatTokens(e.runs_with_energy),
  },
  {
    key: 'energy',
    header: 'Compute energy (est.)',
    align: 'right',
    render: (e) => orDash(formatEnergyScaled(e.energy_wh)),
  },
  {
    key: 'per_run',
    header: 'Per run (est.)',
    align: 'right',
    render: (e) => orDash(formatEnergyScaled(e.energy_wh_per_run)),
  },
  {
    key: 'co2e',
    header: 'Carbon (est.)',
    align: 'right',
    render: (e) => (
      <span title="Compute energy at the CURRENT grid factor — excludes PUE, embodied hardware and the scope split. The Emissions view sums each run's as-recorded figure instead.">
        {orDash(formatCo2eScaled(e.co2e_g))}
      </span>
    ),
  },
]

const ERROR_COLUMNS: Column<GuardrailMethodError>[] = [
  { key: 'at', header: 'When', render: (e) => (e.at ? formatDateTime(e.at) : '—') },
  { key: 'slug', header: 'Method', render: (e) => e.method_slug },
  {
    key: 'error',
    header: 'Error',
    render: (e) => (
      <span style={{ color: 'var(--text-muted)' }} title={e.error}>
        {e.error}
      </span>
    ),
  },
]

function RoutingGroupTable({ group }: { group: RoutingGroup }) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        {group.task_shape} · {group.objective} · {formatTokens(group.runs)} runs
      </div>
      <MonoTable
        columns={ROUTING_COLUMNS}
        rows={group.models}
        rowKey={(m) => m.model_id}
        empty="No model in this group has enough evidence yet."
      />
      {group.models_below_evidence_floor.length > 0 && (
        <div
          style={{
            marginTop: 6,
            fontFamily: 'var(--mono)',
            fontSize: 10.5,
            color: 'var(--text-muted)',
          }}
        >
          {/* Not the same statement as "did badly". A model with too little
              history is left exactly where it was in the ordering, so it can
              still be tried; saying so here is what stops the table reading as
              a complete list of the models in play. */}
          Too little evidence to rank: {group.models_below_evidence_floor.join(', ')}
        </div>
      )}
    </div>
  )
}

const ROUTING_COLUMNS: Column<RoutingModelPrior>[] = [
  { key: 'model', header: 'Model', render: (m) => m.model_id },
  { key: 'runs', header: 'Runs', align: 'right', render: (m) => formatTokens(m.runs) },
  {
    key: 'n',
    header: 'Eff. n',
    align: 'right',
    // Not the run count: time decay and off-band discounting have already been
    // applied, and the gap between the two columns is the point.
    render: (m) => m.effective_n.toFixed(1),
  },
  {
    key: 'quality',
    header: 'Quality',
    align: 'right',
    render: (m) => (
      <span title={`raw ${m.quality_raw.toFixed(3)}, lower bound ${m.quality_ci_low.toFixed(3)}`}>
        {m.quality_mean.toFixed(3)}
      </span>
    ),
  },
  {
    key: 'floor',
    header: 'Lower bound',
    align: 'right',
    render: (m) => (
      <span style={{ color: 'var(--text-muted)' }}>{m.quality_ci_low.toFixed(3)}</span>
    ),
  },
  {
    key: 'delivered',
    header: 'Delivered',
    align: 'right',
    render: (m) => `${(m.delivered_rate * 100).toFixed(0)}%`,
  },
  {
    key: 'human',
    header: 'Approved / rejected',
    align: 'right',
    render: (m) =>
      m.approvals + m.rejections === 0 ? (
        <span style={{ color: 'var(--text-muted)' }}>—</span>
      ) : (
        <span style={{ color: m.rejections > m.approvals ? 'var(--red)' : undefined }}>
          {m.approvals} / {m.rejections}
        </span>
      ),
  },
  {
    key: 'cost',
    header: 'Mean cost',
    align: 'right',
    render: (m) => `$${m.mean_cost_usd.toFixed(4)}`,
  },
  {
    key: 'iterations',
    header: 'Mean iters',
    align: 'right',
    render: (m) => m.mean_iterations.toFixed(1),
  },
  {
    key: 'seen',
    header: 'Last seen',
    align: 'right',
    render: (m) => (
      <span style={{ color: 'var(--text-muted)' }}>
        {m.last_seen ? formatDateTime(m.last_seen) : orDash(null)}
      </span>
    ),
  },
]

function rateColor(pct: number): string | undefined {
  if (pct >= 25) return 'var(--red)'
  if (pct > 0) return 'var(--amber)'
  return undefined
}

function Section({
  title,
  hint,
  children,
}: {
  title: string
  hint: string
  children: ReactNode
}) {
  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        {title}
      </div>
      <div
        style={{
          marginBottom: 8,
          fontFamily: 'var(--mono)',
          fontSize: 10.5,
          color: 'var(--text-muted)',
        }}
      >
        {hint}
      </div>
      {children}
    </div>
  )
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="config-stat">
      <div className="mono-label">{label}</div>
      <div className="mono-body" style={{ color }}>
        {value}
      </div>
    </div>
  )
}
