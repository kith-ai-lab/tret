import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Navigate } from 'react-router-dom'

import {
  api,
  ApiError,
  type CredArmIn,
  type CredArmStat,
  type CredEstimates,
  type CredGate,
  type CredPass,
  type CredReview,
  type CredReviewFilters,
  type CredSelectionIn,
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

type Section = 'tournaments' | 'credibility'

function isGone(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 404 || e.status === 403)
}

export function Evaluation() {
  const listQuery = useQuery({
    queryKey: ['eval-tournaments'],
    queryFn: api.evalTournaments,
    retry: false,
    refetchInterval: (q) => (q.state.data?.tournaments.some((t) => t.status === 'running') ? POLL_MS : false),
  })
  const credQuery = useQuery({
    queryKey: ['eval-cred-passes'],
    queryFn: api.evalCredPasses,
    retry: false,
    refetchInterval: (q) => (q.state.data?.passes.some((p) => p.status === 'running' && !p.stalled) ? POLL_MS : false),
  })
  const [chosen, setChosen] = useState<Section | null>(null)

  if (listQuery.isLoading || credQuery.isLoading) return <div className="empty pulse">Loading evaluation…</div>

  const sections: Section[] = []
  const tournamentsFailed = listQuery.isError && !isGone(listQuery.error)
  if (listQuery.data || tournamentsFailed) sections.push('tournaments')
  if (credQuery.data) sections.push('credibility')
  if (sections.length === 0) {
    // Both features off (404) or not allowed (403): leave rather than render an error.
    if (isGone(listQuery.error) && isGone(credQuery.error)) return <Navigate to="/" replace />
    const failed = !isGone(listQuery.error) ? listQuery : credQuery
    return (
      <div className="eval-page">
        <QueryError error={failed.error} what="evaluation" />
        <button className="btn" onClick={() => void failed.refetch()}>
          Retry
        </button>
      </div>
    )
  }
  const section = chosen && sections.includes(chosen) ? chosen : sections[0]

  return (
    <div className="eval-page">
      <h1 className="view-title">Evaluation</h1>
      {sections.length > 1 && (
        <div className="tabs" role="tablist">
          {sections.map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={section === k}
              className={`tab${section === k ? ' active' : ''}`}
              onClick={() => setChosen(k)}
            >
              {k === 'tournaments' ? 'Tournaments' : 'Credibility'}
            </button>
          ))}
        </div>
      )}
      {section === 'tournaments' && listQuery.data && <TournamentsSection tournaments={listQuery.data.tournaments} />}
      {section === 'tournaments' && !listQuery.data && (
        <>
          <QueryError error={listQuery.error} what="tournaments" />
          <button className="btn" onClick={() => void listQuery.refetch()}>
            Retry
          </button>
        </>
      )}
      {section === 'credibility' && credQuery.data && <CredibilitySection passes={credQuery.data.passes} />}
    </div>
  )
}

function TournamentsSection({ tournaments }: { tournaments: EvalTournament[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(null)

  useEffect(() => {
    if (tournaments.length === 0) setSelectedId(null)
    else if (!tournaments.some((t) => t.id === selectedId)) setSelectedId(tournaments[0].id)
  }, [tournaments, selectedId])

  return (
    <>
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
    </>
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
  jury_restore: 'Jury restore',
  juror_agreement: 'Juror agreement',
  anchor_drift: 'Anchor drift',
  parts_vs_whole: 'Parts vs whole',
  score_clumping: 'Score clumping',
  failure_rate: 'Failure rate',
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
    const as = t.estimates?.anchor_set
    const anchorText = as ? ` Anchor set: ${as.pairs} pairs, ${as.matches} matches (included in cost).` : ' No anchor set yet.'
    const range = c ? `Estimated cost ${money(c.low)} to ${money(c.high)} (the jury rotates).` : 'No cost estimate is available.'
    const ok = await confirm({
      title: 'Start tournament',
      message: `${range}${anchorText}${covText} Spending stops at the budget of ${money(budgetNum)}. Start ${t.module}?`,
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
      return { preview: false, seats: t.jury.map((j) => ({ key: `${j.index}`, label: j.family ?? 'unknown', title: j.model, dropped: j.dropped })) }
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
    case 'position_bias':
      return th.flip_drop != null ? `pause > ${th.flip_drop}, needs ≥ ${th.min_swaps_pooled ?? '—'} swaps` : null
    case 'juror_agreement':
      if (th.kappa_pause == null) return null
      return th.agreement_floor != null
        ? `pause < ${th.kappa_pause} and agreement < ${th.agreement_floor}`
        : `pause below κ ${th.kappa_pause}`
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
      {d.notes && d.notes.length > 0 && (
        <ul className="fine-print" style={{ margin: '0 0 8px', paddingLeft: 18 }}>
          {d.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
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

// ── Credibility (Engine 2) ───────────────────────────────────────────────

const CRED_NEW = 'new'

function credSub(p: CredPass): string {
  const c = p.counts
  const parts = [`${c.done}/${c.total - c.skipped} done`]
  if (c.failed > 0) parts.push(`${c.failed} failed`)
  parts.push(`${money(p.spent_usd)} / ${p.budget_usd != null ? money(p.budget_usd) : 'no budget'}`)
  if (p.pause_reason) parts.push(p.pause_reason)
  if (p.created_at) parts.push(formatDateTime(p.created_at))
  return parts.join(' · ')
}

function CredibilitySection({ passes }: { passes: CredPass[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(passes.length > 0 ? passes[0].id : CRED_NEW)

  useEffect(() => {
    if (selectedId !== CRED_NEW && selectedId != null && !passes.some((p) => p.id === selectedId)) {
      setSelectedId(passes.length > 0 ? passes[0].id : CRED_NEW)
    }
  }, [passes, selectedId])

  return (
    <>
      <div className="view-sub">
        Engine 2: one report in, one 1 to 10 Credibility Score and three pushbacks out. The model never sees peers, ratings or
        company metadata. Each arm freezes one router-picked model when the pass starts.
      </div>
      <ListDetail
        listWidth={300}
        list={
          <>
            <ListItem
              active={selectedId === CRED_NEW}
              onClick={() => setSelectedId(CRED_NEW)}
              title={<strong>+ New pass</strong>}
              sub="Preview is free; nothing runs until you start it"
            />
            {passes.map((p) => (
              <ListItem
                key={p.id}
                active={p.id === selectedId}
                onClick={() => setSelectedId(p.id)}
                title={
                  <>
                    {p.name || p.id.slice(0, 8)} <StatusBadge status={p.status} />
                    <GateDots gates={p.gates} />
                  </>
                }
                sub={credSub(p)}
              />
            ))}
          </>
        }
        detail={
          selectedId === CRED_NEW ? (
            <NewPassForm onCreated={(id) => setSelectedId(id)} />
          ) : selectedId ? (
            <PassPane key={selectedId} id={selectedId} />
          ) : null
        }
      />
    </>
  )
}

// ── New pass ─────────────────────────────────────────────────────────────

type OpenWeights = 'require' | 'prefer' | 'any'
interface ArmDraft {
  key: string
  label: string
  weight: string
  openWeights: OpenWeights
}

const DEFAULT_ARMS: ArmDraft[] = [
  { key: 'open', label: 'Open weights', weight: '1', openWeights: 'require' },
  { key: 'closed', label: 'Closed allowed', weight: '1', openWeights: 'any' },
]

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const ARM_KEY_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/

/** The request body, or the first thing wrong with the form. Never carries a model name:
 *  an arm constrains (open weights), the router picks. */
function buildSelection(
  f: { batchId: string; limit: string; seed: string; budget: string; arms: ArmDraft[] },
): { body: CredSelectionIn } | { error: string } {
  const batch = f.batchId.trim()
  if (!UUID_RE.test(batch)) return { error: 'Enter the batch id (a UUID).' }
  const body: CredSelectionIn = { batch_id: batch, arms: [] }
  if (f.limit.trim() !== '') {
    const n = Number(f.limit)
    if (!Number.isInteger(n) || n < 1 || n > 2000) return { error: 'Sample size must be a whole number from 1 to 2000.' }
    body.limit = n
  }
  if (f.seed.trim() !== '') {
    const n = Number(f.seed)
    if (!Number.isInteger(n)) return { error: 'Seed must be a whole number.' }
    if (body.limit == null) return { error: 'A seed only applies with a sample size.' }
    body.sample_seed = n
  }
  if (f.budget.trim() !== '') {
    const n = Number(f.budget)
    if (!Number.isFinite(n) || n <= 0) return { error: 'Budget must be greater than 0.' }
    body.budget_usd = n
  }
  const seen = new Set<string>()
  for (const a of f.arms) {
    if (!ARM_KEY_RE.test(a.key)) return { error: `Arm key "${a.key}": use 1 to 32 characters of a-z, 0-9, - or _.` }
    if (seen.has(a.key)) return { error: `Arm key "${a.key}" is used twice.` }
    seen.add(a.key)
    const w = Number(a.weight)
    if (!Number.isFinite(w) || w <= 0) return { error: `Arm "${a.key}": weight must be greater than 0.` }
    const arm: CredArmIn = { key: a.key, label: a.label.trim() || a.key, weight: w, model_policy: { open_weights: a.openWeights } }
    body.arms.push(arm)
  }
  if (body.arms.length === 0) return { error: 'Add at least one arm.' }
  return { body }
}

function NewPassForm({ onCreated }: { onCreated: (id: string) => void }) {
  const queryClient = useQueryClient()
  const [batchId, setBatchId] = useState('')
  const [limit, setLimit] = useState('50')
  const [seed, setSeed] = useState('1')
  const [name, setName] = useState('')
  const [budget, setBudget] = useState('')
  const [arms, setArms] = useState<ArmDraft[]>(DEFAULT_ARMS)
  const [preview, setPreview] = useState<CredEstimates | null>(null)
  const [error, setError] = useState<string | null>(null)

  const form = { batchId, limit, seed, budget, arms }
  const parsed = buildSelection(form)
  // A preview describes one exact request: any edit makes it stale.
  const formKey = JSON.stringify(form)
  const [previewKey, setPreviewKey] = useState<string | null>(null)

  const previewMutation = useMutation({
    mutationFn: (body: CredSelectionIn) => api.evalCredPreview(body),
    onMutate: () => setError(null),
    onSuccess: (est) => {
      setPreview(est)
      setPreviewKey(formKey)
    },
    onError: (e) => setError(errorText(e)),
  })
  const createMutation = useMutation({
    mutationFn: (body: CredSelectionIn & { name: string }) => api.evalCredCreate(body),
    onMutate: () => setError(null),
    onSuccess: (p) => {
      // Put the new pass in the cached list first, so the list effect finds it and keeps it selected.
      queryClient.setQueryData<{ passes: CredPass[] }>(['eval-cred-passes'], (old) => ({
        passes: [p, ...(old?.passes ?? []).filter((x) => x.id !== p.id)],
      }))
      void queryClient.invalidateQueries({ queryKey: ['eval-cred-passes'] })
      onCreated(p.id)
    },
    onError: (e) => setError(errorText(e)),
  })
  const busy = previewMutation.isPending || createMutation.isPending

  const setArm = (i: number, patch: Partial<ArmDraft>) => setArms((a) => a.map((x, k) => (k === i ? { ...x, ...patch } : x)))

  return (
    <div>
      <strong className="mono-label">New credibility pass</strong>
      <div className="fine-print" style={{ margin: '4px 0 12px' }}>
        Pick a batch, an optional seeded sample and the arms. Arms constrain the router (for example open weights only); you never name
        a model. Preview calls no model and writes nothing.
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        <div className="field" style={{ flex: '1 1 320px' }}>
          <label className="mono-label" htmlFor="cred-batch">
            Batch id
          </label>
          <input id="cred-batch" type="text" placeholder="collection batch id" value={batchId} onChange={(e) => setBatchId(e.target.value)} />
        </div>
        <div className="field" style={{ width: 130 }}>
          <label className="mono-label" htmlFor="cred-limit">
            Sample size
          </label>
          <input id="cred-limit" type="number" min="1" max="2000" placeholder="all" value={limit} onChange={(e) => setLimit(e.target.value)} />
        </div>
        <div className="field" style={{ width: 110 }}>
          <label className="mono-label" htmlFor="cred-seed">
            Seed
          </label>
          <input id="cred-seed" type="number" placeholder="first N" value={seed} onChange={(e) => setSeed(e.target.value)} />
        </div>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        <div className="field" style={{ flex: '1 1 320px' }}>
          <label className="mono-label" htmlFor="cred-name">
            Name
          </label>
          <input id="cred-name" type="text" maxLength={120} placeholder="e.g. 50-report read, open vs closed" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field" style={{ width: 150 }}>
          <label className="mono-label" htmlFor="cred-budget">
            Budget USD (opt.)
          </label>
          <input id="cred-budget" type="number" min="0" step="any" placeholder="set at start" value={budget} onChange={(e) => setBudget(e.target.value)} />
        </div>
      </div>

      <div className="eval-section">Arms</div>
      <div className="fine-print" style={{ marginBottom: 6 }}>
        Reports are split across arms by weight. Each arm freezes the model the router picks under its constraint.
      </div>
      {arms.map((a, i) => (
        <div key={i} style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginBottom: 8 }}>
          <input
            type="text"
            style={{ width: 110 }}
            aria-label={`Arm ${i + 1} key`}
            value={a.key}
            onChange={(e) => setArm(i, { key: e.target.value.toLowerCase() })}
          />
          <input type="text" style={{ width: 170 }} aria-label={`Arm ${i + 1} label`} value={a.label} onChange={(e) => setArm(i, { label: e.target.value })} />
          <input
            type="number"
            min="0"
            step="any"
            style={{ width: 80 }}
            aria-label={`Arm ${i + 1} weight`}
            title="weight"
            value={a.weight}
            onChange={(e) => setArm(i, { weight: e.target.value })}
          />
          <select
            style={{ width: 230 }}
            aria-label={`Arm ${i + 1} open-weights constraint`}
            value={a.openWeights}
            onChange={(e) => setArm(i, { openWeights: e.target.value as OpenWeights })}
          >
            <option value="require">open weights required</option>
            <option value="prefer">open weights preferred</option>
            <option value="any">any model allowed</option>
          </select>
          {arms.length > 1 && (
            <button className="btn btn-sm" onClick={() => setArms((x) => x.filter((_, k) => k !== i))}>
              Remove
            </button>
          )}
        </div>
      ))}
      {arms.length < 4 && (
        <button
          className="btn btn-sm"
          onClick={() => setArms((x) => [...x, { key: `arm${x.length + 1}`, label: `Arm ${x.length + 1}`, weight: '1', openWeights: 'any' }])}
        >
          Add arm
        </button>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginTop: 16 }}>
        <button className="btn" disabled={busy || 'error' in parsed} onClick={() => 'body' in parsed && previewMutation.mutate(parsed.body)}>
          Preview
        </button>
        <button
          className="btn btn-primary"
          disabled={busy || 'error' in parsed || name.trim() === ''}
          title={name.trim() === '' ? 'Give the pass a name' : undefined}
          onClick={() => 'body' in parsed && createMutation.mutate({ ...parsed.body, name: name.trim() })}
        >
          Create draft
        </button>
        {busy && <span className="fine-print pulse">working…</span>}
        {'error' in parsed && batchId.trim() !== '' && <span className="fine-print">{parsed.error}</span>}
      </div>
      <div className="fine-print" style={{ marginTop: 4 }}>
        Creating writes a draft and assigns reports to arms; nothing is spent until you start it with a budget.
      </div>
      {error && (
        <div className="error-text" role="alert" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
      {preview && (
        <div style={{ marginTop: 14 }}>
          {previewKey !== formKey && <div className="badge badge-amber" style={{ marginBottom: 8 }}>Form changed since this preview</div>}
          <PreviewView est={preview} />
        </div>
      )}
    </div>
  )
}

function PreviewView({ est }: { est: CredEstimates }) {
  const skipped = Object.entries(est.skipped_reasons ?? {})
  return (
    <div>
      <div className="eval-section" style={{ marginTop: 0 }}>
        Preview
      </div>
      <div className="fine-print">
        {est.reports} report(s) read ({est.input.whole} whole, {est.input.digest} digest on the arms' models) · {est.skipped} skipped
        {est.cost_usd && (
          <>
            {' '}
            · cost {money(est.cost_usd.low)} to {money(est.cost_usd.high)}, expected {money(est.cost_usd.expected)}
          </>
        )}
        {est.within_budget != null && (
          <span className={`badge ${est.within_budget ? 'badge-green' : 'badge-red'}`} style={{ marginLeft: 6 }}>
            {est.within_budget ? 'within budget' : 'over budget'}
          </span>
        )}
      </div>
      {est.unroutable_arms.length > 0 && (
        <div className="error-text" role="alert" style={{ marginTop: 6 }}>
          No model can serve arm(s): {est.unroutable_arms.join(', ')}. Loosen the constraint or the sample.
        </div>
      )}
      <div className="eval-gates" style={{ marginTop: 8 }}>
        {est.arms.map((a) => (
          <div key={a.key} className={`eval-gate ${a.pick ? 'ok' : 'tripped'}`}>
            <div className="gate-name">
              <span>{a.label}</span>
              <span className="fine-print">weight {a.weight}</span>
            </div>
            {a.pick ? (
              <>
                <div title={a.pick.model}>
                  {a.pick.model}
                  {a.pick.family ? <span className="fine-print"> ({a.pick.family})</span> : null}
                </div>
                {a.pick.why && <div className="gate-line">{a.pick.why}</div>}
              </>
            ) : (
              <div className="gate-line">{a.error ?? 'no pick'}</div>
            )}
            <div className="gate-line">
              ~{a.expected_reports} reports · {a.whole_reports ?? 0} whole, {a.digest_reports ?? 0} digest
            </div>
            <div className="gate-line">
              all reports {money(a.cost_usd_all_reports)} · expected {money(a.expected_cost_usd)}
            </div>
          </div>
        ))}
      </div>
      {skipped.length > 0 && (
        <div className="fine-print">
          Skipped: {skipped.map(([k, n]) => `${n} ${k}`).join('; ')}
          {est.skipped_examples && est.skipped_examples.length > 0 && (
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {est.skipped_examples.slice(0, 5).map((s) => (
                <li key={s.disclosure_report_id}>
                  {s.label ?? s.disclosure_report_id.slice(0, 8)}: {s.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {est.method && <div className="fine-print" style={{ marginTop: 6 }}>{est.method}</div>}
    </div>
  )
}

// ── Pass detail ──────────────────────────────────────────────────────────

type CredTab = 'overview' | 'reviews'

function PassPane({ id }: { id: string }) {
  const [tab, setTab] = useState<CredTab>('overview')
  const q = useQuery({
    queryKey: ['eval-cred-pass', id],
    queryFn: () => api.evalCredPass(id),
    refetchInterval: (query) => (query.state.data?.status === 'running' && !query.state.data.stalled ? POLL_MS : false),
  })
  if (q.isLoading) return <div className="empty pulse">Loading pass…</div>
  if (q.isError || !q.data) return <QueryError error={q.error} what="the pass" />
  const p = q.data
  const running = p.status === 'running' && !p.stalled
  return (
    <div>
      <PassHeader p={p} />
      <div className="tabs" role="tablist">
        {(['overview', 'reviews'] as CredTab[]).map((k) => (
          <button key={k} role="tab" aria-selected={tab === k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            {k[0].toUpperCase() + k.slice(1)}
          </button>
        ))}
      </div>
      {tab === 'overview' && <PassOverview p={p} />}
      {tab === 'reviews' && <ReviewsTab p={p} running={running} />}
    </div>
  )
}

function PassHeader({ p }: { p: CredPass }) {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [budget, setBudget] = useState(p.budget_usd != null ? String(p.budget_usd) : '')
  const [note, setNote] = useState('')
  const [ack, setAck] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['eval-cred-passes'] })
    void queryClient.invalidateQueries({ queryKey: ['eval-cred-pass', p.id] })
    void queryClient.invalidateQueries({ queryKey: ['eval-cred-reviews', p.id] })
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
  const pct = p.budget_usd ? Math.min(100, (p.spent_usd / p.budget_usd) * 100) : 0
  const required = p.required_acknowledgements ?? []
  const stalledRunning = p.status === 'running' && !!p.stalled
  const canResume = p.status === 'paused' || p.status === 'failed' || stalledRunning
  const needsNote = ack.size > 0 && note.trim() === ''
  const label = p.name || p.id.slice(0, 8)

  const onStart = async () => {
    if (!budgetValid || budgetNum == null) return
    const c = p.estimates?.cost_usd
    const range = c ? `Estimated cost ${money(c.low)} to ${money(c.high)}, expected ${money(c.expected)}.` : 'No cost estimate is available.'
    const frozen = p.arms.map((a) => a.label ?? a.key).join(', ')
    const ok = await confirm({
      title: 'Start credibility pass',
      message: `${range} Each arm (${frozen}) freezes the router's pick now. ${p.counts.pending} reports will be read. Spending stops at the budget of ${money(budgetNum)}. Start "${label}"?`,
      confirmLabel: 'Start',
    })
    if (ok) mutation.mutate(() => api.evalCredStart(p.id, budgetNum))
  }
  const onCancel = async () => {
    const ok = await confirm({
      title: 'Cancel pass',
      message: `Stop "${label}" for good? Reviews already done are kept; the remaining reports are not read, and a cancelled pass cannot be resumed.`,
      confirmLabel: 'Cancel pass',
      cancelLabel: 'Keep it',
      destructive: true,
    })
    if (ok) mutation.mutate(() => api.evalCredCancel(p.id))
  }
  const onDelete = async () => {
    const ok = await confirm({
      title: 'Delete pass',
      message: `Delete "${label}" and its reviews? This cannot be undone.`,
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (ok) mutation.mutate(() => api.evalCredDelete(p.id))
  }
  const onResume = () => {
    const body: { budget_usd?: number; acknowledge_gates?: string[]; note?: string } = {}
    if (budgetValid && budgetNum != null) body.budget_usd = budgetNum
    if (ack.size > 0) body.acknowledge_gates = [...ack]
    if (note.trim()) body.note = note.trim()
    mutation.mutate(() => api.evalCredResume(p.id, body))
  }

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <strong className="mono-label">{label}</strong>
        <StatusBadge status={p.status} />
        {p.stalled && <span className="badge badge-amber">stalled</span>}
        {p.pause_reason && <span className="badge badge-amber">{p.pause_reason}</span>}
        <GateDots gates={p.gates} />
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center', margin: '8px 0' }}>
        <div style={{ minWidth: 200, flex: '0 1 280px' }}>
          <div className="fine-print">
            spend {money(p.spent_usd)} / {p.budget_usd != null ? money(p.budget_usd) : 'no budget'}
          </div>
          <div className={`eval-bar${p.budget_usd && p.spent_usd >= p.budget_usd ? ' over' : ''}`}>
            <span style={{ width: `${pct}%` }} />
          </div>
        </div>
        <span className="fine-print">
          {p.counts.done} done · {p.counts.pending + p.counts.running} to go · {p.counts.failed} failed · {p.counts.skipped} skipped
        </span>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        {(p.status === 'draft' || canResume) && (
          <input
            type="number"
            min="0"
            step="any"
            style={{ width: 150 }}
            placeholder={p.status === 'draft' ? 'budget USD' : 'new budget (opt.)'}
            aria-label="Budget in USD"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
          />
        )}
        {p.status === 'draft' && (
          <button className="btn btn-primary" disabled={busy || !budgetValid} onClick={onStart}>
            Start
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
        {(p.status === 'draft' || p.status === 'running' || p.status === 'paused') && (
          <button className="btn btn-reject" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        )}
        {(p.status === 'draft' || p.status === 'cancelled') && (
          <button className="btn btn-reject" disabled={busy} onClick={onDelete}>
            Delete
          </button>
        )}
        {busy && <span className="fine-print pulse">working…</span>}
      </div>

      {canResume && required.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="fine-print">
            These gates are tripped and must be acknowledged to resume. A note is required; your name and the note are recorded.
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
              <span>{gateLabel(key)}</span>
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

function Histogram({ values }: { values: number[] }) {
  const max = Math.max(1, ...values)
  return (
    <div
      role="img"
      aria-label={`Scores 1 to 10: ${values.join(', ')}`}
      style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 54 }}
    >
      {values.map((v, i) => (
        <div key={i} title={`score ${i + 1}: ${v}`} style={{ flex: '0 0 14px', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          <div style={{ width: '100%', height: Math.max(v > 0 ? 2 : 0, (v / max) * 40), background: 'var(--accent)', borderRadius: 2 }} />
          <span className="fine-print" style={{ fontSize: 9 }}>
            {i + 1}
          </span>
        </div>
      ))}
    </div>
  )
}

function CredGateCard({ g }: { g: CredGate }) {
  const thr = g.threshold
  const thrText = thr
    ? g.key === 'score_clumping'
      ? `warn above ${pct0(thr.warn)}, pause above ${pct0(thr.pause)}, needs ${thr.min_n} done`
      : `pause above ${pct0(thr.pause)}, needs ${thr.min_n} attempts`
    : null
  return (
    <div className={`eval-gate ${g.state}`}>
      <div className="gate-name">
        <span>{gateLabel(g.key)}</span>
        <span className={`badge ${STATE_BADGE[g.state]}`}>{STATE_LABEL[g.state]}</span>
      </div>
      <div>
        value {g.value != null ? pct0(g.value) : '—'} · n={g.n}
      </div>
      {thrText && <div className="gate-line">{thrText}</div>}
      {g.detail && <div className="gate-line">{g.detail}</div>}
      {g.acknowledged_by && (
        <div className="gate-line">
          acknowledged by {g.acknowledged_by}
          {g.acknowledged_note ? `: ${g.acknowledged_note}` : ''}
        </div>
      )}
    </div>
  )
}

function pct0(v: number | null | undefined): string {
  return v == null ? '—' : `${(v * 100).toFixed(0)}%`
}

function PassOverview({ p }: { p: CredPass }) {
  const stats: CredArmStat[] = p.arm_stats ?? []
  return (
    <div>
      <div className="eval-section" style={{ marginTop: 4 }}>
        Arms
      </div>
      <div className="eval-scroll">
        <MonoTable
          columns={[
            { key: 'arm', header: 'Arm', render: (a: CredPass['arms'][number]) => `${a.label ?? a.key} (${a.key})` },
            { key: 'w', header: 'Weight', align: 'right', render: (a) => a.weight },
            {
              key: 'policy',
              header: 'Constraint',
              render: (a) => (a.model_policy?.open_weights && a.model_policy.open_weights !== 'any' ? `open weights ${a.model_policy.open_weights}` : 'any model'),
            },
            {
              key: 'model',
              header: 'Frozen model',
              render: (a) =>
                a.frozen && a.model ? (
                  <>
                    {a.model}
                    {a.family ? <span className="fine-print"> ({a.family})</span> : null}
                    {a.max_output_tokens ? <div className="fine-print">max output {a.max_output_tokens.toLocaleString('en-US')} tokens</div> : null}
                  </>
                ) : (
                  <span className="fine-print">not frozen until the pass starts</span>
                ),
            },
            { key: 'why', header: 'Why', render: (a) => <span className="eval-rationale">{a.why ?? '—'}</span> },
          ]}
          rows={p.arms}
          rowKey={(a) => a.key}
        />
      </div>

      <div className="eval-section">Gates</div>
      <div className="eval-gates">
        {p.gates.map((g) => (
          <CredGateCard key={g.key} g={g} />
        ))}
      </div>
      {p.acknowledged_gates.length > 0 && (
        <div className="fine-print">
          Acknowledged: {p.acknowledged_gates.map((a) => `${a.gate ?? '?'} by ${a.by ?? '?'}${a.note ? ` (${a.note})` : ''}`).join('; ')}
        </div>
      )}

      <div className="eval-section">Scores by arm</div>
      {stats.length === 0 ? (
        <div className="empty">No reviews yet.</div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24 }}>
          {stats.map((s) => (
            <div key={s.key} style={{ minWidth: 190 }}>
              <div className="mono-label">{s.label ?? s.key}</div>
              <div className="fine-print">
                mean {num(s.mean, 2)} · median {num(s.median, 1)} · n={s.n_done}
                {s.n_failed > 0 ? ` · ${s.n_failed} failed` : ''} · {money(s.cost_usd)}
                {s.n_digest ? ` · ${s.n_digest} read as digest` : ''}
              </div>
              <div style={{ marginTop: 6 }}>
                <Histogram values={s.histogram} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Reviews ──────────────────────────────────────────────────────────────

function ReviewsTab({ p, running }: { p: CredPass; running: boolean }) {
  const [arm, setArm] = useState('')
  const [status, setStatus] = useState('')
  const [minScore, setMinScore] = useState('')
  const [maxScore, setMaxScore] = useState('')
  const [offset, setOffset] = useState(0)
  const [openId, setOpenId] = useState<string | null>(null)

  const scoreOrUndef = (v: string) => (v === '' ? undefined : Number(v))
  const filters: CredReviewFilters = {
    arm: arm || undefined,
    status: status || undefined,
    min_score: scoreOrUndef(minScore),
    max_score: scoreOrUndef(maxScore),
    limit: PAGE,
    offset,
  }
  const q = useQuery({
    queryKey: ['eval-cred-reviews', p.id, arm, status, minScore, maxScore, offset],
    queryFn: () => api.evalCredReviews(p.id, filters),
    refetchInterval: running ? POLL_MS : false,
  })
  const total = q.data?.total ?? 0
  const reset = (fn: () => void) => {
    fn()
    setOffset(0)
  }

  const columns: Column<CredReview>[] = [
    { key: 'company', header: 'Company', render: (r) => r.company ?? r.disclosure_report_id.slice(0, 8) },
    { key: 'score', header: 'Score', align: 'right', render: (r) => (r.credibility_score != null ? `${r.credibility_score}/10` : '—') },
    { key: 'arm', header: 'Arm', render: (r) => r.arm_key || '—' },
    { key: 'mode', header: 'Input', render: (r) => r.input_mode ?? '—' },
    {
      key: 'status',
      header: 'Status',
      render: (r) =>
        r.status === 'failed' || r.status === 'skipped' ? (
          <span className={`badge ${r.status === 'failed' ? 'badge-red' : 'badge-gray'}`} title={r.error ?? undefined}>
            {r.status}
          </span>
        ) : (
          <StatusBadge status={r.status} />
        ),
    },
    { key: 'model', header: 'Model', render: (r) => <span className="fine-print">{r.model ?? '—'}</span> },
    { key: 'cost', header: 'Cost', align: 'right', render: (r) => formatCost(r.cost_usd) },
  ]

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        <div className="field" style={{ width: 150 }}>
          <label className="mono-label" htmlFor="cred-f-arm">
            Arm
          </label>
          <select id="cred-f-arm" value={arm} onChange={(e) => reset(() => setArm(e.target.value))}>
            <option value="">all</option>
            {p.arms.map((a) => (
              <option key={a.key} value={a.key}>
                {a.label ?? a.key}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ width: 130 }}>
          <label className="mono-label" htmlFor="cred-f-status">
            Status
          </label>
          <select id="cred-f-status" value={status} onChange={(e) => reset(() => setStatus(e.target.value))}>
            <option value="">all</option>
            {['pending', 'running', 'done', 'failed', 'skipped'].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ width: 100 }}>
          <label className="mono-label" htmlFor="cred-f-min">
            Min score
          </label>
          <input id="cred-f-min" type="number" min="1" max="10" value={minScore} onChange={(e) => reset(() => setMinScore(e.target.value))} />
        </div>
        <div className="field" style={{ width: 100 }}>
          <label className="mono-label" htmlFor="cred-f-max">
            Max score
          </label>
          <input id="cred-f-max" type="number" min="1" max="10" value={maxScore} onChange={(e) => reset(() => setMaxScore(e.target.value))} />
        </div>
      </div>

      {openId && <ReviewDetail key={openId} id={openId} onClose={() => setOpenId(null)} />}

      {q.isLoading ? (
        <div className="empty pulse">Loading reviews…</div>
      ) : (
        <div className="eval-scroll">
          <MonoTable
            columns={columns}
            rows={q.data?.reviews ?? []}
            rowKey={(r) => r.id}
            rowLabel={(r) => `Open review of ${r.company ?? r.disclosure_report_id}`}
            onRowClick={(r) => setOpenId(r.id)}
            error={q.error}
            empty="No reviews match."
          />
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

function Hash({ label, h }: { label: string; h: { stored: string | null; rebuilt: string | null; match: boolean } }) {
  return (
    <div className="gate-line" style={{ wordBreak: 'break-all' }}>
      {label}: {h.match ? <span className="badge badge-green">match</span> : <span className="badge badge-red">mismatch</span>}
      <div>stored {h.stored ?? '—'}</div>
      <div>rebuilt {h.rebuilt ?? '—'}</div>
    </div>
  )
}

function ReviewDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ['eval-cred-review', id], queryFn: () => api.evalCredReview(id) })
  const verify = useMutation({ mutationFn: () => api.evalCredVerify(id) })
  const r = q.data

  return (
    <div className="eval-gate ok" style={{ margin: '4px 0 14px', fontFamily: 'inherit' }}>
      <div className="gate-name">
        <span>{r ? (r.company ?? r.disclosure_report_id) : 'Review'}</span>
        <button className="btn btn-sm" onClick={onClose}>
          Close
        </button>
      </div>
      {q.isLoading && <div className="pulse">Loading review…</div>}
      {q.isError && <QueryError error={q.error} what="the review" />}
      {r && (
        <>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
            <strong style={{ fontSize: 22 }}>{r.credibility_score != null ? `${r.credibility_score}/10` : '—'}</strong>
            <StatusBadge status={r.status} />
            <span className="fine-print">
              arm {r.arm_key || '—'} · {r.model ?? 'no model'} · input {r.input_mode ?? '—'} · {formatCost(r.cost_usd)}
            </span>
          </div>
          {r.error && <div className="error-text">{r.error}</div>}
          {r.summary && <p style={{ margin: '8px 0' }}>{r.summary}</p>}
          {r.pushbacks && r.pushbacks.length > 0 && (
            <>
              <div className="eval-section" style={{ marginTop: 10 }}>
                Pushbacks
              </div>
              <ol style={{ margin: 0, paddingLeft: 20 }}>
                {r.pushbacks.map((pb, i) => (
                  <li key={i} style={{ marginBottom: 6 }}>
                    {pb.pushback}
                    <div className="fine-print">Evidence: {pb.evidence}</div>
                  </li>
                ))}
              </ol>
            </>
          )}
          {r.story_breaks && r.story_breaks.length > 0 && (
            <>
              <div className="eval-section" style={{ marginTop: 10 }}>
                Where the story breaks
              </div>
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                {r.story_breaks.map((b, i) => (
                  <li key={i}>{b}</li>
                ))}
              </ul>
            </>
          )}
          <div className="eval-section" style={{ marginTop: 10 }}>
            Payload audit
          </div>
          <div className="gate-line" style={{ wordBreak: 'break-all' }}>
            report sha256 {r.report_sha256 ?? '—'}
          </div>
          <div className="gate-line" style={{ wordBreak: 'break-all' }}>
            prompt sha256 {r.prompt_sha256 ?? '—'}
          </div>
          <div style={{ marginTop: 6 }}>
            <button className="btn btn-sm" disabled={verify.isPending} onClick={() => verify.mutate()}>
              Verify payload
            </button>
            <span className="fine-print"> Rebuilds the prompt from the report and compares the stored hashes.</span>
          </div>
          {verify.isError && (
            <div className="error-text" role="alert">
              {errorText(verify.error)}
            </div>
          )}
          {verify.data && (
            <div style={{ marginTop: 6 }} role="status">
              <span className={`badge ${verify.data.ok ? 'badge-green' : 'badge-red'}`}>{verify.data.ok ? 'match' : 'mismatch'}</span>
              {verify.data.reason && <span className="fine-print"> {verify.data.reason}</span>}
              <Hash label="report" h={verify.data.report_sha256} />
              <Hash label="prompt" h={verify.data.prompt_sha256} />
            </div>
          )}
        </>
      )}
    </div>
  )
}
