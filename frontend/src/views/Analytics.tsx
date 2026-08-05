import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'

import {
  api,
  type GuardrailHarnessStat,
  type GuardrailMethodError,
  type GuardrailMethodStat,
} from '../api/client'
import { formatTokens } from '../components/shared/format'
import { type Column, MonoTable } from '../components/shared/MonoTable'
import { formatDateTime } from './Runs'

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

  const data = guardrailsQuery.data

  return (
    <div className="stack" style={{ gap: 28 }}>
      <div>
        <h1 className="view-title">Analytics</h1>
        <div className="view-sub">
          Guardrails for code execution: how often the deterministic method lane fails, and how hard
          structured-output validation is pushing back.
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
