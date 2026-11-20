/** Settings → Guardrails (R25), and the "Guardrails on this run" block for the
 *  run detail page.
 *
 *  Three tiers, from `GET /api/guardrails`: locked (nothing can change them),
 *  member (this workspace's own settings), operator (set on the server). Member
 *  items get a control when the viewer is a workspace admin/owner; everyone else
 *  — and anyone whose save is refused with a 403 — sees the same values
 *  read-only. Each change saves immediately via `PUT /api/guardrails`. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import {
  api,
  ApiError,
  type GuardrailItem,
  type GuardrailsResponse,
  guardrailsApi,
  type RunGuardrails,
  stopReasonLabel,
} from '../../api/client'
import { formatCost, formatTokens, formatWh } from '../shared/format'
import { QueryError } from '../shared/MonoTable'

function displayValue(item: GuardrailItem): string {
  const v = item.value
  if (typeof v === 'boolean') return v ? 'on' : 'off'
  if (item.id === 'spend_cap_usd') return v == null ? 'no cap' : `$${Number(v).toFixed(2)}`
  if (item.id === 'max_subagents') return v == null ? (item.bounds?.max == null ? 'no limit' : 'instance default') : String(v)
  if (v == null) return item.state || '—'
  if (Array.isArray(v)) return v.length ? v.join(', ') : 'none'
  return String(v)
}

/** One-line overhead summary, or null when nothing has been measured. */
export function overheadText(o: GuardrailItem['overhead']): string | null {
  if (!o || o.runs <= 0) return null
  const parts = [
    `${o.repair_turns} repair ${o.repair_turns === 1 ? 'turn' : 'turns'}`,
    `${formatTokens(o.input_tokens + o.output_tokens)} tokens`,
    formatCost(o.cost_usd),
  ]
  const wh = formatWh(o.energy_wh)
  if (wh) parts.push(wh)
  return `Last ${o.window_days} days: ${parts.join(' · ')} across ${o.runs} ${o.runs === 1 ? 'run' : 'runs'}`
}

function ItemControl({
  item,
  disabled,
  onChange,
}: {
  item: GuardrailItem
  disabled: boolean
  onChange: (value: unknown) => void
}) {
  const v = item.value
  const min = item.bounds?.min ?? 0
  const max = item.bounds?.max
  if (typeof v === 'boolean') {
    return (
      <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <input
          type="checkbox"
          checked={v}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          aria-label={item.title}
        />
        <span className="mono-body">{v ? 'On' : 'Off'}</span>
      </label>
    )
  }
  if (item.id === 'repair_attempts') {
    const n = typeof v === 'number' ? v : 0
    const hi = max ?? 3
    return (
      <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
        <button
          type="button"
          className="btn btn-sm"
          disabled={disabled || n <= min}
          onClick={() => onChange(n - 1)}
          aria-label={`Fewer ${item.title}`}
        >
          −
        </button>
        <span className="mono-body" style={{ minWidth: 16, textAlign: 'center' }}>
          {n}
        </span>
        <button
          type="button"
          className="btn btn-sm"
          disabled={disabled || n >= hi}
          onClick={() => onChange(n + 1)}
          aria-label={`More ${item.title}`}
        >
          +
        </button>
      </span>
    )
  }
  if (item.id === 'spend_cap_usd') {
    const noCap = v == null
    return (
      <span style={{ display: 'inline-flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input
            type="checkbox"
            checked={noCap}
            disabled={disabled}
            onChange={(e) => onChange(e.target.checked ? null : Math.min(1, max ?? 1))}
          />
          <span className="mono-body">No cap</span>
        </label>
        {!noCap && (
          <NumberBox
            value={Number(v)}
            min={item.bounds?.min ?? 0}
            max={max}
            disabled={disabled}
            onCommit={onChange}
            label={item.title}
          />
        )}
      </span>
    )
  }
  if (item.id === 'max_subagents') {
    const opts =
      item.bounds?.options && item.bounds.options.length > 0
        ? item.bounds.options.map(String)
        : Array.from({ length: (max ?? 8) - min + 1 }, (_, i) => String(min + i))
    return (
      <select
        value={v == null ? '' : String(v)}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
        aria-label={item.title}
        style={{ width: 160 }}
      >
        <option value="">Instance default</option>
        {opts.map((o) => (
          <option key={o} value={o}>
            {o === '0' ? '0 (no delegation)' : o}
          </option>
        ))}
      </select>
    )
  }
  return <span className="mono-body">{displayValue(item)}</span>
}

function NumberBox({
  value,
  min,
  max,
  disabled,
  onCommit,
  label,
}: {
  value: number
  min: number
  max?: number
  disabled: boolean
  onCommit: (n: number) => void
  label: string
}) {
  const [draft, setDraft] = useState(String(value))
  const commit = () => {
    const n = Number(draft)
    if (!Number.isFinite(n)) {
      setDraft(String(value))
      return
    }
    const clamped = Math.max(min, max != null ? Math.min(max, n) : n)
    setDraft(String(clamped))
    if (clamped !== value) onCommit(clamped)
  }
  return (
    <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
      <span className="mono-body">$</span>
      <input
        type="number"
        step="0.5"
        min={min}
        max={max}
        value={draft}
        disabled={disabled}
        aria-label={label}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit()
        }}
        style={{ width: 90 }}
      />
    </span>
  )
}

/** Pure view of the tiers; exported so it can be rendered without a network. */
export function GuardrailsView({
  data,
  canEdit,
  saving,
  onChange,
}: {
  data: GuardrailsResponse
  canEdit: boolean
  saving: boolean
  onChange: (id: string, value: unknown) => void
}) {
  return (
    <>
      {data.tiers.map((tier) => {
        const items = data.items.filter((i) => i.tier === tier.id)
        return (
          <div key={tier.id} className="stack" style={{ gap: 8 }} data-tier={tier.id}>
            <div>
              <div className="mono-label">{tier.title}</div>
              <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
                {tier.description}
              </div>
            </div>
            {items.map((item) => {
              const editable = tier.id === 'member' && item.adjustable && canEdit
              const overhead = overheadText(item.overhead)
              return (
                <div
                  key={item.id}
                  className="row"
                  style={{ alignItems: 'flex-start', gap: 12, flexWrap: 'wrap' }}
                  data-item={item.id}
                >
                  <div style={{ flex: 1, minWidth: 220 }}>
                    <div className="mono-body" style={{ fontWeight: 600 }}>
                      {item.title}
                    </div>
                    <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
                      {item.description}
                    </div>
                    {overhead && (
                      <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
                        {overhead}
                      </div>
                    )}
                  </div>
                  <div style={{ minWidth: 140 }}>
                    {editable ? (
                      <ItemControl
                        item={item}
                        disabled={saving}
                        onChange={(value) => onChange(item.id, value)}
                      />
                    ) : (
                      <span className="chip">
                        {tier.id === 'locked' ? 'always on' : displayValue(item)}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )
      })}
    </>
  )
}

export function GuardrailsPanel() {
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const isAdmin = ['owner', 'admin'].includes(meQuery.data?.role ?? '')
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['guardrails'], queryFn: guardrailsApi.get })
  const [forbidden, setForbidden] = useState(false)
  const mutation = useMutation({
    mutationFn: (patch: Record<string, unknown>) => guardrailsApi.set(patch),
    onSuccess: (res) => queryClient.setQueryData(['guardrails'], res),
    onError: (err) => {
      if (err instanceof ApiError && err.status === 403) setForbidden(true)
    },
  })
  const canEdit = isAdmin && !forbidden
  const error = mutation.error as ApiError | null

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Guardrails
      </div>
      <div className="panel stack" style={{ gap: 18 }}>
        <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
          The checks Tret applies to its own work. Some can never be changed, some are yours to
          tune for this workspace, and some are set by whoever runs the server.
          {!canEdit && ' Only a workspace admin can change them.'}
        </div>
        {query.isLoading ? (
          <div className="empty pulse">Loading…</div>
        ) : query.isError ? (
          <QueryError error={query.error} />
        ) : query.data ? (
          <GuardrailsView
            data={query.data}
            canEdit={canEdit}
            saving={mutation.isPending}
            onChange={(id, value) => mutation.mutate({ [id]: value })}
          />
        ) : null}
        {error && !forbidden && (
          <div className="error-text" role="alert">
            {error.message}
          </div>
        )}
        {forbidden && (
          <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
            Only a workspace admin can change these.
          </div>
        )}
      </div>
    </div>
  )
}

/** "Guardrails on this run" for the run detail page, from `run.guardrails`
 *  (what was in force and what the checks found) plus the stop reason. */
export function RunGuardrailsBlock({
  guardrails,
  stopReason,
}: {
  guardrails: RunGuardrails | null | undefined
  stopReason: string | null | undefined
}) {
  const stop = stopReasonLabel(stopReason)
  if (!guardrails && !stop) return null
  const member = (guardrails?.settings?.member ?? null) as Record<string, unknown> | null
  const operator = (guardrails?.settings?.operator ?? null) as Record<string, unknown> | null
  const oh = guardrails?.overhead
  const html = guardrails?.html_check
  const fmt = (v: unknown) =>
    typeof v === 'boolean' ? (v ? 'on' : 'off') : v == null ? 'none' : String(v)
  const labels: Record<string, string> = {
    number_check: 'Number check',
    repair_attempts: 'Repair attempts',
    html_check: 'HTML check',
    web_research: 'Web research',
    spend_cap_usd: 'Spend cap (USD)',
    max_subagents: 'Max subagents',
  }
  return (
    <div className="panel stack" style={{ gap: 8 }} data-testid="run-guardrails">
      <div className="mono-label">Guardrails on this run</div>
      {stop && (
        <div className="mono-body">
          Stopped early: <strong>{stop}</strong>
          {guardrails?.truncated && ` (output limit ${guardrails.truncated.max_output_tokens} tokens on ${guardrails.truncated.model})`}
        </div>
      )}
      {member && (
        <div className="mono-body" style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
          {Object.entries(member).map(([k, v]) => (
            <span key={k}>
              <span style={{ color: 'var(--text-muted)' }}>{labels[k] ?? k}: </span>
              {fmt(v)}
            </span>
          ))}
        </div>
      )}
      {operator && (
        <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
          Server settings: {Object.entries(operator).map(([k, v]) => `${k.replace(/_/g, ' ')} ${fmt(v)}`).join(' · ')}
        </div>
      )}
      {html && html.status && (
        <div className="mono-body">
          HTML check: {html.status}
          {html.repaired ? ' (repaired once)' : ''}
          {html.issues && html.issues.length > 0 && (
            <ul style={{ margin: '4px 0 0 18px' }}>
              {html.issues.map((i, n) => (
                <li key={n}>
                  line {i.line}: {i.kind} — {i.detail}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {oh && (oh.repair_turns ?? 0) > 0 && (
        <div className="mono-body">
          Repair overhead: {oh.repair_turns} {oh.repair_turns === 1 ? 'turn' : 'turns'},{' '}
          {formatTokens((oh.repair_input_tokens ?? 0) + (oh.repair_output_tokens ?? 0))} tokens
        </div>
      )}
      {guardrails?.degenerate && (
        <div className="mono-body" style={{ color: 'var(--amber)' }}>
          Degenerate output detected on {guardrails.degenerate.model} ({guardrails.degenerate.count}×{' '}
          {guardrails.degenerate.pattern})
          {guardrails.degenerate.retried_on ? `; retried on ${guardrails.degenerate.retried_on}` : ''}
        </div>
      )}
    </div>
  )
}
