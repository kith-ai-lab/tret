import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Navigate } from 'react-router-dom'

import {
  api,
  ApiError,
  type EvalDiagnostics,
  type EvalGate,
  type EvalGateState,
  type EvalGateSummary,
  type EvalMatch,
  type EvalRatingPool,
  type EvalRatingRow,
  type EvalTournament,
  type EvalTournamentDetail,
} from '../api/client'
import { useConfirm } from '../components/shared/ConfirmDialog'
import { formatCost, formatDateTime } from '../components/shared/format'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { type Column, MonoTable, QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

/** Staff view of the disclosure evaluation engine's Engine 1 tournaments.
 *  `GET /api/eval/tournaments` 404s while the feature setting is off, so a
 *  direct navigation here leaves rather than rendering an error; the nav item
 *  (App.tsx) is hidden by the same probe. */
const POLL_MS = 10_000
const DIAG_POLL_MS = 30_000

export function Evaluation() {
  const listQuery = useQuery({
    queryKey: ['eval-tournaments'],
    queryFn: api.evalTournaments,
    retry: false,
    refetchInterval: (q) => (q.state.data?.tournaments.some((t) => t.status === 'running') ? POLL_MS : false),
  })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const tournaments = listQuery.data?.tournaments

  useEffect(() => {
    if (!tournaments) return
    if (tournaments.length === 0) setSelectedId(null)
    else if (!tournaments.some((t) => t.id === selectedId)) setSelectedId(tournaments[0].id)
  }, [tournaments, selectedId])

  if (listQuery.isLoading) return <div className="empty pulse">Loading evaluation…</div>
  if (listQuery.isError || !tournaments) {
    const status = listQuery.error instanceof ApiError ? listQuery.error.status : null
    if (status === 404 || status === 403) return <Navigate to="/" replace />
    return (
      <div className="eval-page">
        <QueryError error={listQuery.error} what="evaluation" />
        <button className="btn" onClick={() => void listQuery.refetch()}>
          Retry
        </button>
      </div>
    )
  }

  return (
    <div className="eval-page">
      <h1 className="view-title">Evaluation</h1>
      <div className="view-sub">
        Engine 1 tournaments: pairwise jury matches over peer pools, rated with intervals and checked by the quality gates.
      </div>
      {tournaments.length === 0 ? (
        <div className="empty">No tournaments yet. Create one from the CLI or API; it appears here as a draft.</div>
      ) : (
        <ListDetail
          listWidth={300}
          list={tournaments.map((t) => (
            <ListItem
              key={t.id}
              active={t.id === selectedId}
              onClick={() => setSelectedId(t.id)}
              title={
                <>
                  {t.module} <StatusBadge status={t.status} />
                  <GateDots gates={t.gates} />
                </>
              }
              sub={listSub(t)}
            />
          ))}
          detail={selectedId ? <TournamentPane key={selectedId} id={selectedId} /> : null}
        />
      )}
    </div>
  )
}

function listSub(t: EvalTournament): string {
  const parts = [`${money(t.spent_usd)} / ${t.budget_usd != null ? money(t.budget_usd) : 'no budget'}`]
  parts.push(`round ${t.current_round}/${t.rounds}`)
  if (t.pause_reason) parts.push(t.pause_reason)
  if (t.created_at) parts.push(formatDateTime(t.created_at))
  return parts.join(' · ')
}

function money(n: number | null | undefined): string {
  return n == null ? '—' : `$${n.toFixed(2)}`
}

function num(n: number | null | undefined, digits = 2): string {
  return n == null || Number.isNaN(n) ? '—' : n.toFixed(digits)
}

/** A 403 is the approver-role check; say so rather than showing the bare status. */
function errorText(e: unknown): string {
  if (e instanceof ApiError && e.status === 403) {
    return `Not allowed: this action needs the approver role in this workspace. (${e.message})`
  }
  return e instanceof Error ? e.message : String(e)
}

function GateDots({ gates }: { gates?: EvalGateSummary[] }) {
  if (!gates || gates.length === 0) return null
  return (
    <span className="eval-dots" aria-label="quality gates">
      {gates.map((g) => (
        <span key={g.key} className={`eval-dot ${g.state}`} title={`${gateLabel(g.key)}: ${g.state}`} />
      ))}
    </span>
  )
}

const GATE_LABELS: Record<string, string> = {
  length_bias: 'Length bias',
  flip_rate: 'Flip rate',
  position_bias: 'Position bias',
  juror_agreement: 'Juror agreement',
  anchor_drift: 'Anchor drift',
  parts_vs_whole: 'Parts vs whole',
  score_clumping: 'Score clumping',
}

function gateLabel(key: string): string {
  return GATE_LABELS[key] ?? key
}

// ── Detail pane ──────────────────────────────────────────────────────────

type Tab = 'ratings' | 'diagnostics' | 'matches'

function TournamentPane({ id }: { id: string }) {
  const [tab, setTab] = useState<Tab>('ratings')
  const detailQuery = useQuery({
    queryKey: ['eval-tournament', id],
    queryFn: () => api.evalTournament(id),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? POLL_MS : false),
  })
  const running = detailQuery.data?.status === 'running'

  if (detailQuery.isLoading) return <div className="empty pulse">Loading tournament…</div>
  if (detailQuery.isError || !detailQuery.data) return <QueryError error={detailQuery.error} what="the tournament" />
  const t = detailQuery.data

  return (
    <div>
      <Header t={t} />
      <div className="tabs" role="tablist">
        {(['ratings', 'diagnostics', 'matches'] as Tab[]).map((k) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            className={`tab${tab === k ? ' active' : ''}`}
            onClick={() => setTab(k)}
          >
            {k[0].toUpperCase() + k.slice(1)}
          </button>
        ))}
      </div>
      {tab === 'ratings' && <RatingsTab t={t} />}
      {tab === 'diagnostics' && <DiagnosticsTab id={id} running={running} />}
      {tab === 'matches' && <MatchesTab id={id} running={running} />}
    </div>
  )
}

function Header({ t }: { t: EvalTournamentDetail }) {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [budget, setBudget] = useState('')
  const [note, setNote] = useState('')
  const [ack, setAck] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['eval-tournaments'] })
    void queryClient.invalidateQueries({ queryKey: ['eval-tournament', t.id] })
    void queryClient.invalidateQueries({ queryKey: ['eval-diagnostics', t.id] })
    void queryClient.invalidateQueries({ queryKey: ['eval-ratings', t.id] })
    void queryClient.invalidateQueries({ queryKey: ['eval-matches', t.id] })
  }
  const mutation = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onMutate: () => setError(null),
    onSuccess: () => {
      refresh()
      setAck(new Set())
      setNote('')
    },
    onError: (e) => setError(errorText(e)),
  })
  const busy = mutation.isPending

  const budgetNum = budget.trim() === '' ? null : Number(budget)
  const budgetValid = budgetNum != null && Number.isFinite(budgetNum) && budgetNum > 0
  const budgetBlankOk = budget.trim() === ''
  const pct = t.budget_usd ? Math.min(100, (t.spent_usd / t.budget_usd) * 100) : 0
  const required = t.required_acknowledgements ?? []
  const acknowledged = t.acknowledged_gates ?? {}

  const onStart = async () => {
    if (!budgetValid || budgetNum == null) return
    const c = t.estimates?.cost_usd
    const cov = t.estimates?.gate_coverage
    const covText = cov
      ? ' Gate coverage: ' +
        Object.entries(cov)
          .map(([k, v]) => `${k.replace(/_/g, ' ')} ${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`)
          .join('; ') +
        '.'
      : ''
    const range = c ? `Estimated cost ${money(c.low)} to ${money(c.high)} (the jury rotates).` : 'No cost estimate is available.'
    const ok = await confirm({
      title: 'Start tournament',
      message: `${range}${covText} Spending stops at the budget of ${money(budgetNum)}. Start ${t.module}?`,
      confirmLabel: 'Start',
    })
    if (ok) mutation.mutate(() => api.evalStart(t.id, budgetNum))
  }
  const onCancel = async () => {
    const ok = await confirm({
      title: 'Cancel tournament',
      message: `Stop ${t.module}? Matches already judged are kept and can be refit; unplayed matches are not run.`,
      confirmLabel: 'Cancel tournament',
      cancelLabel: 'Keep running',
      destructive: true,
    })
    if (ok) mutation.mutate(() => api.evalCancel(t.id))
  }
  const onResume = () => {
    const body: { budget_usd?: number; acknowledge_gates?: string[]; note?: string } = {}
    if (budgetValid && budgetNum != null) body.budget_usd = budgetNum
    if (ack.size > 0) body.acknowledge_gates = [...ack]
    if (note.trim()) body.note = note.trim()
    mutation.mutate(() => api.evalResume(t.id, body))
  }
  const onAnchor = async () => {
    if (!budgetValid || budgetNum == null) return
    const ok = await confirm({
      title: 'Create anchor set',
      message: `Judge an anchor set from this tournament's ratings with a budget of ${money(budgetNum)}. This spends jury budget.`,
      confirmLabel: 'Create anchor set',
    })
    if (ok) mutation.mutate(() => api.evalCreateAnchorSet(t.id, budgetNum))
  }

  const juryInfo = useMemo(() => {
    if (t.jury && t.jury.length > 0) {
      return { preview: false, seats: t.jury.map((j) => ({ key: `${j.index}`, label: j.family, title: j.model, dropped: j.dropped })) }
    }
    const picks = t.estimates?.jury?.picks ?? []
    const seen = new Set<string>()
    const seats: Array<{ key: string; label: string; title?: string; dropped: boolean }> = []
    for (const s of picks) {
      const f = (s.family ?? s.model) as string | undefined
      if (f && !seen.has(f)) {
        seen.add(f)
        seats.push({ key: f, label: f, title: s.model, dropped: (t.dropped_families ?? []).includes(f) })
      }
    }
    return { preview: true, seats }
  }, [t])

  const stalledRunning = t.status === 'running' && !!t.stalled
  const canResume = t.status === 'paused' || t.status === 'failed' || stalledRunning
  const needsNote = ack.size > 0 && note.trim() === ''

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <strong className="mono-label">{t.module}</strong>
        {t.module_set && <span className="fine-print">{t.module_set}</span>}
        <StatusBadge status={t.status} />
        {t.stalled && <span className="badge badge-amber">stalled</span>}
        {t.pause_reason && <span className="badge badge-amber">{t.pause_reason}</span>}
        <GateDots gates={t.gates} />
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center', margin: '8px 0' }}>
        <div style={{ minWidth: 200, flex: '0 1 280px' }}>
          <div className="fine-print">
            spend {money(t.spent_usd)} / {t.budget_usd != null ? money(t.budget_usd) : 'no budget'}
          </div>
          <div className={`eval-bar${t.budget_usd && t.spent_usd >= t.budget_usd ? ' over' : ''}`}>
            <span style={{ width: `${pct}%` }} />
          </div>
        </div>
        <span className="fine-print">
          round {t.current_round}/{t.rounds}
        </span>
        {juryInfo.seats.length > 0 && (
          <span className="fine-print">
            jury{juryInfo.preview ? ' (preview)' : ''}:{' '}
            {juryInfo.seats.map((seat, i) => (
              <span key={seat.key}>
                {i > 0 && ', '}
                <span className={seat.dropped ? 'eval-dropped' : undefined} title={seat.title}>
                  {seat.label}
                </span>
              </span>
            ))}
          </span>
        )}
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        {(t.status === 'draft' || canResume || t.status === 'completed') && (
          <input
            type="number"
            min="0"
            step="any"
            style={{ width: 150 }}
            placeholder={t.status === 'draft' ? 'budget USD' : t.status === 'completed' ? 'anchor budget USD' : 'new budget (opt.)'}
            aria-label={t.status === 'completed' ? 'Anchor set budget in USD' : 'Budget in USD'}
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
          />
        )}
        {t.status === 'draft' && (
          <button className="btn btn-primary" disabled={busy || !budgetValid} onClick={onStart}>
            Start
          </button>
        )}
        {(t.status === 'running' || t.status === 'paused') && (
          <button className="btn btn-reject" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        )}
        {canResume && (
          <button
            className="btn btn-primary"
            disabled={busy || (!budgetBlankOk && !budgetValid) || required.some((k) => !ack.has(k)) || needsNote}
            onClick={onResume}
          >
            Resume
          </button>
        )}
        {['completed', 'failed', 'paused', 'cancelled'].includes(t.status) && (
          <button className="btn" disabled={busy} onClick={() => mutation.mutate(() => api.evalRefit(t.id))}>
            Refit
          </button>
        )}
        {t.status === 'completed' && (
          <button className="btn" disabled={busy || !budgetValid} onClick={onAnchor}>
            Create anchor set
          </button>
        )}
        {busy && <span className="fine-print pulse">working…</span>}
      </div>

      {canResume && required.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="fine-print">
            These gates must be acknowledged to resume. A note is required; your name and the note are recorded.
          </div>
          {required.map((key) => (
            <label key={key} className="check-row">
              <input
                type="checkbox"
                checked={ack.has(key)}
                onChange={(e) => {
                  const next = new Set(ack)
                  if (e.target.checked) next.add(key)
                  else next.delete(key)
                  setAck(next)
                }}
              />
              <span>
                {gateLabel(key)}
                {key in acknowledged && <span className="desc"> (acknowledged before)</span>}
              </span>
            </label>
          ))}
          <input
            type="text"
            style={{ marginTop: 6, maxWidth: 480 }}
            placeholder="note (required: why it is safe to continue)"
            aria-label="Acknowledgement note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </div>
      )}
      {error && (
        <div className="error-text" role="alert" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
    </div>
  )
}

// ── Ratings ──────────────────────────────────────────────────────────────

type SortKey = 'rating' | 'word_count' | 'claim_density'

function bandLabel(row: EvalRatingRow, pool: EvalRatingPool | undefined): string {
  if (row.band == null) return '—'
  const kind = row.band_kind ?? pool?.band_kind ?? pool?.ratings.find((r) => r.band_kind)?.band_kind
  const prefix = kind === 'quintile' ? 'Q' : kind === 'decile' ? 'D' : ''
  return `${prefix}${row.band}`
}

function RatingsTab({ t }: { t: EvalTournamentDetail }) {
  const ratingsQuery = useQuery({
    queryKey: ['eval-ratings', t.id],
    queryFn: () => api.evalRatings(t.id),
  })
  const [poolKey, setPoolKey] = useState<string | null>(null)
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'rating', dir: -1 })

  const pools = ratingsQuery.data?.pools ?? []
  const pool = pools.find((p) => p.key === poolKey) ?? pools[0]

  const rows = useMemo(() => {
    if (!pool) return []
    const byRating = [...pool.ratings].sort((a, b) => (b.rating ?? -Infinity) - (a.rating ?? -Infinity))
    const ranked = byRating.map((r, i) => ({ r, rank: i + 1 }))
    const val = (x: EvalRatingRow) => (x[sort.key] ?? -Infinity) as number
    return ranked.sort((a, b) => (val(a.r) - val(b.r)) * sort.dir || a.rank - b.rank)
  }, [pool, sort])

  if (ratingsQuery.isLoading) return <div className="empty pulse">Loading ratings…</div>
  if (ratingsQuery.isError) return <QueryError error={ratingsQuery.error} what="ratings" />
  if (!pool) {
    return <div className="empty">Ratings are written when the tournament completes (or on refit).</div>
  }

  const plan = t.pools.find((p) => p.key === pool.key)
  const lo = Math.min(...pool.ratings.map((r) => r.lo90 ?? r.rating ?? 0))
  const hi = Math.max(...pool.ratings.map((r) => r.hi90 ?? r.rating ?? 0))
  const span = hi - lo || 1
  const pct = (v: number) => `${(((v - lo) / span) * 100).toFixed(1)}%`
  const kind = pool.band_kind ?? pool.ratings.find((r) => r.band_kind)?.band_kind

  const sortHead = (label: string, key: SortKey) => (
    <button
      className="tab"
      style={{ padding: 0, border: 'none', color: sort.key === key ? 'var(--accent)' : undefined }}
      onClick={() => setSort((s) => (s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: -1 }))}
    >
      {label}
      {sort.key === key ? (sort.dir === -1 ? ' ↓' : ' ↑') : ''}
    </button>
  )

  type Row = { r: EvalRatingRow; rank: number }
  const columns: Column<Row>[] = [
    { key: 'rank', header: '#', align: 'right', render: ({ rank }) => rank },
    { key: 'company', header: 'Company', render: ({ r }) => r.company ?? r.label ?? r.member_id.slice(0, 8) },
    { key: 'rating', header: sortHead('Rating', 'rating'), align: 'right', render: ({ r }) => num(r.rating, 1) },
    {
      key: 'interval',
      header: '90% interval',
      render: ({ r }) =>
        r.lo90 != null && r.hi90 != null && r.rating != null ? (
          <div
            className="eval-interval"
            title={`${num(r.lo90, 1)} to ${num(r.hi90, 1)}`}
          >
            <span className="rng" style={{ left: pct(r.lo90), width: `calc(${pct(r.hi90)} - ${pct(r.lo90)})` }} />
            <span className="pt" style={{ left: pct(r.rating) }} />
          </div>
        ) : (
          '—'
        ),
    },
    { key: 'band', header: 'Band', render: ({ r }) => bandLabel(r, pool) },
    { key: 'matches', header: 'Matches', align: 'right', render: ({ r }) => r.matches ?? '—' },
    { key: 'wins', header: 'W', align: 'right', render: ({ r }) => r.wins ?? '—' },
    {
      key: 'words',
      header: sortHead('Words', 'word_count'),
      align: 'right',
      render: ({ r }) => (r.word_count != null ? r.word_count.toLocaleString('en-US') : '—'),
    },
    { key: 'density', header: sortHead('Claim density', 'claim_density'), align: 'right', render: ({ r }) => num(r.claim_density, 3) },
  ]

  return (
    <div>
      {(ratingsQuery.data?.partial || (t.summary as { partial?: boolean } | null)?.partial) && (
        <div className="badge badge-amber" style={{ display: 'block', marginBottom: 10, padding: '6px 10px' }} role="status">
          Partial: tournament did not finish
        </div>
      )}
      <div className="field" style={{ maxWidth: 520 }}>
        <label className="mono-label" htmlFor="eval-pool">
          Pool
        </label>
        <select id="eval-pool" value={pool.key} onChange={(e) => setPoolKey(e.target.value)}>
          {pools.map((p) => {
            const pp = t.pools.find((x) => x.key === p.key)
            const k = p.band_kind ?? p.ratings.find((r) => r.band_kind)?.band_kind
            return (
              <option key={p.key} value={p.key}>
                {p.key} · {pp?.level ?? p.level ?? 'level —'} · n={pp?.n ?? p.n ?? p.ratings.length} · {k ?? 'bands —'}
              </option>
            )
          })}
        </select>
      </div>
      <div className="fine-print" style={{ marginBottom: 8 }}>
        {plan?.level ? `level ${plan.level} · ` : ''}n={plan?.n ?? pool.ratings.length}
        {kind ? ` · ${kind} bands` : ''}. Intervals overlap heavily in small pools: read bands and rough order, not exact ranks.
      </div>
      <div className="eval-scroll">
        <MonoTable columns={columns} rows={rows} rowKey={({ r }) => r.member_id} empty="No rated members in this pool." />
      </div>
    </div>
  )
}

// ── Diagnostics ──────────────────────────────────────────────────────────

function thresholdText(g: EvalGate, th: EvalDiagnostics['thresholds']): string | null {
  switch (g.key) {
    case 'length_bias':
      return th.length_r_warn != null ? `warn at |r| > ${th.length_r_warn}, pause at > ${th.length_r_pause ?? '—'}` : null
    case 'flip_rate':
      return th.flip_drop != null ? `drop a family at flip rate > ${th.flip_drop}` : null
    case 'juror_agreement':
      return th.kappa_pause != null ? `pause below κ ${th.kappa_pause}` : null
    case 'anchor_drift':
      return th.anchor_changes_pause != null ? `pause at > ${th.anchor_changes_pause} changed` : null
    default:
      return null
  }
}

const STATE_LABEL: Record<EvalGateState, string> = {
  ok: 'ok',
  warn: 'warn',
  tripped: 'tripped',
  insufficient_data: 'not enough data',
  not_applicable: 'n/a',
}
const STATE_BADGE: Record<EvalGateState, string> = {
  ok: 'badge-green',
  warn: 'badge-amber',
  tripped: 'badge-red',
  insufficient_data: 'badge-gray',
  not_applicable: 'badge-gray',
}

function Sparkline({ values, label }: { values: Array<number | null>; label: string }) {
  const pts = values.map((v, i) => ({ v, i })).filter((p): p is { v: number; i: number } => p.v != null)
  if (pts.length < 2) {
    return (
      <div className="fine-print">
        {label}: {pts.length === 1 ? num(pts[0].v) : '—'}
      </div>
    )
  }
  const min = Math.min(...pts.map((p) => p.v))
  const max = Math.max(...pts.map((p) => p.v))
  const rng = max - min || 1
  const w = 120
  const h = 28
  const d = pts
    .map((p, k) => {
      const x = (p.i / Math.max(values.length - 1, 1)) * w
      const y = h - 3 - ((p.v - min) / rng) * (h - 6)
      return `${k === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <div className="fine-print">
      <div>
        {label}: {num(pts[pts.length - 1].v)}
      </div>
      <svg width={w} height={h} role="img" aria-label={`${label} by round`}>
        <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      </svg>
    </div>
  )
}

function DiagnosticsTab({ id, running }: { id: string; running: boolean }) {
  const q = useQuery({
    queryKey: ['eval-diagnostics', id],
    queryFn: () => api.evalDiagnostics(id),
    retry: false,
    refetchInterval: running ? DIAG_POLL_MS : false,
  })
  if (q.isLoading) return <div className="empty pulse">Loading diagnostics…</div>
  if (q.isError || !q.data) return <QueryError error={q.error} what="diagnostics" />
  const d = q.data
  const lb = d.length_bias

  return (
    <div>
      {d.computed_at && <div className="fine-print">computed {formatDateTime(d.computed_at)}</div>}
      <div className="eval-gates" style={{ marginTop: 8 }}>
        {d.gates.map((g) => {
          const th = thresholdText(g, d.thresholds)
          return (
            <div key={g.key} className={`eval-gate ${g.state}`}>
              <div className="gate-name">
                <span>{gateLabel(g.key)}</span>
                <span className={`badge ${STATE_BADGE[g.state]}`}>{STATE_LABEL[g.state]}</span>
              </div>
              <div>
                value {num(g.value, 3)} · n={g.n}
              </div>
              {th && <div className="gate-line">{th}</div>}
              {g.detail && <div className="gate-line">{g.detail}</div>}
              {g.acknowledged_by && (
                <div className="gate-line">
                  acknowledged by {g.acknowledged_by}
                  {g.acknowledged_at ? ` at ${formatDateTime(g.acknowledged_at)}` : ''}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="eval-section">Length bias</div>
      <div className="eval-scroll">
        <MonoTable
          columns={[
            { key: 'pool', header: 'Pool', render: (r: { pool: string }) => r.pool },
            { key: 'n', header: 'n', align: 'right', render: (r) => r.n },
            { key: 'p', header: 'Words r (Pearson)', align: 'right', render: (r) => num(r.pearson) },
            { key: 's', header: 'Words ρ (Spearman)', align: 'right', render: (r) => num(r.spearman) },
            { key: 'pd', header: 'Density r (Pearson)', align: 'right', render: (r) => num(r.pearson_density) },
            { key: 'sd', header: 'Density ρ (Spearman)', align: 'right', render: (r) => num(r.spearman_density) },
          ]}
          rows={[
            ...(lb?.overall ? [{ pool: 'overall', ...lb.overall }] : []),
            ...(lb?.pools ?? []).map((p) => ({ ...p, pool: p.pool_key })),
          ]}
          rowKey={(r) => r.pool}
          empty="No length-bias data yet."
        />
      </div>

      <div className="eval-section">Flip rate by family</div>
      <div className="eval-scroll">
        <MonoTable
          columns={[
            { key: 'f', header: 'Family', render: (r: EvalDiagnostics['flip_rates'][number]) => <span className={r.dropped ? 'eval-dropped' : undefined}>{r.family}</span> },
            { key: 'sw', header: 'Swaps', align: 'right', render: (r) => r.swaps },
            { key: 'fl', header: 'Flips', align: 'right', render: (r) => r.flips },
            { key: 'rate', header: 'Rate', align: 'right', render: (r) => num(r.rate, 3) },
            { key: 'dr', header: '', render: (r) => (r.dropped ? <span className="badge badge-red">dropped</span> : null) },
          ]}
          rows={d.flip_rates}
          rowKey={(r) => r.family}
          empty="No swap matches judged yet."
        />
      </div>

      <div className="eval-section">Juror agreement</div>
      <div className="eval-scroll">
        <MonoTable
          columns={[
            { key: 'pair', header: 'Family pair', render: (r: EvalDiagnostics['agreement'][number]) => `${r.family_a} / ${r.family_b}` },
            { key: 'n', header: 'n', align: 'right', render: (r) => r.n },
            { key: 'k', header: 'Cohen κ', align: 'right', render: (r) => num(r.kappa, 3) },
            { key: 'raw', header: 'Raw agreement', align: 'right', render: (r) => num(r.raw_agreement, 3) },
          ]}
          rows={d.agreement}
          rowKey={(r) => `${r.family_a}|${r.family_b}`}
          empty="No overlapping judgements between families yet."
        />
      </div>

      <div className="eval-section">Anchors</div>
      {d.anchors ? (
        <div>
          <div className="fine-print" style={{ marginBottom: 6 }}>
            set {d.anchors.anchor_set_id.slice(0, 8)} · {d.anchors.judged}/{d.anchors.n} judged · {d.anchors.changed} changed
          </div>
          <div className="eval-scroll">
            <table className="mono-table">
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>Baseline</th>
                  <th>Current</th>
                </tr>
              </thead>
              <tbody>
                {d.anchors.items.map((it) => (
                  <tr key={it.index} className={it.changed ? 'eval-changed' : undefined}>
                    <td className="num">{it.index}</td>
                    <td>{it.baseline ?? '—'}</td>
                    <td>
                      {it.baseline ?? '—'} → {it.current ?? '—'}
                      {it.changed && <span className="badge badge-amber" style={{ marginLeft: 6 }}>changed</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div className="empty">No anchor set attached to this tournament.</div>
      )}

      <div className="eval-section">History by round</div>
      {d.history.length === 0 ? (
        <div className="empty">No rounds recorded yet.</div>
      ) : (
        <>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20, marginBottom: 8 }}>
            <Sparkline label="length ρ" values={d.history.map((h) => h.length_spearman)} />
            <Sparkline label="max flip" values={d.history.map((h) => h.max_flip)} />
            <Sparkline label="min κ" values={d.history.map((h) => h.min_kappa)} />
          </div>
          <div className="eval-scroll">
            <MonoTable
              columns={[
                { key: 'r', header: 'Round', align: 'right', render: (h: EvalDiagnostics['history'][number]) => h.round },
                { key: 'l', header: 'Length ρ', align: 'right', render: (h) => num(h.length_spearman) },
                { key: 'f', header: 'Max flip', align: 'right', render: (h) => num(h.max_flip, 3) },
                { key: 'k', header: 'Min κ', align: 'right', render: (h) => num(h.min_kappa, 3) },
                { key: 'rt', header: 'Rated', align: 'right', render: (h) => h.rated ?? '—' },
              ]}
              rows={d.history}
              rowKey={(h) => String(h.round)}
            />
          </div>
        </>
      )}
    </div>
  )
}

// ── Matches ──────────────────────────────────────────────────────────────

const PAGE = 50

function MatchesTab({ id, running }: { id: string; running: boolean }) {
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const q = useQuery({
    queryKey: ['eval-matches', id, status, offset],
    queryFn: () => api.evalMatches(id, { status: status || undefined, limit: PAGE, offset }),
    refetchInterval: running ? POLL_MS : false,
  })
  const total = q.data?.total ?? 0

  const toggle = (mid: string) =>
    setOpen((s) => {
      const n = new Set(s)
      if (n.has(mid)) n.delete(mid)
      else n.add(mid)
      return n
    })

  const columns: Column<EvalMatch>[] = [
    { key: 'round', header: 'Round', align: 'right', render: (m) => m.round },
    { key: 'kind', header: 'Kind', render: (m) => m.kind },
    { key: 'pool', header: 'Pool', render: (m) => m.pool_key },
    {
      key: 'juror',
      header: 'Juror',
      render: (m) => (
        <>
          {m.juror_family ?? '—'}
          {m.juror_model && <span className="fine-print"> {m.juror_model}</span>}
        </>
      ),
    },
    {
      key: 'verdict',
      header: 'Verdict',
      render: (m) =>
        m.status === 'failed' ? (
          <span className="badge badge-red" title={m.error ?? undefined}>
            failed
          </span>
        ) : m.verdict ? (
          `${m.verdict}${m.strength != null ? ` (${m.strength})` : ''}`
        ) : (
          <StatusBadge status={m.status} />
        ),
    },
    {
      key: 'rationale',
      header: 'Rationale',
      render: (m) => {
        const text = m.rationale ?? ''
        if (!text) return '—'
        const expanded = open.has(m.id)
        return (
          <div
            className="eval-rationale"
            role="button"
            tabIndex={0}
            aria-expanded={expanded}
            onClick={() => toggle(m.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                toggle(m.id)
              }
            }}
          >
            {expanded || text.length <= 120 ? text : `${text.slice(0, 120)}…`}
          </div>
        )
      },
    },
    { key: 'cost', header: 'Cost', align: 'right', render: (m) => formatCost(m.cost_usd) },
  ]

  return (
    <div>
      <div className="field" style={{ maxWidth: 200 }}>
        <label className="mono-label" htmlFor="eval-status">
          Status
        </label>
        <select
          id="eval-status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value)
            setOffset(0)
          }}
        >
          <option value="">all</option>
          <option value="pending">pending</option>
          <option value="running">running</option>
          <option value="done">done</option>
          <option value="failed">failed</option>
        </select>
      </div>
      {q.isLoading ? (
        <div className="empty pulse">Loading matches…</div>
      ) : (
        <div className="eval-scroll">
          <MonoTable columns={columns} rows={q.data?.matches ?? []} rowKey={(m) => m.id} error={q.error} empty="No matches." />
        </div>
      )}
      {total > 0 && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 10 }}>
          <button className="btn btn-sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            Previous
          </button>
          <span className="fine-print">
            {offset + 1}–{Math.min(offset + PAGE, total)} of {total}
          </span>
          <button className="btn btn-sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
            Next
          </button>
        </div>
      )}
    </div>
  )
}
