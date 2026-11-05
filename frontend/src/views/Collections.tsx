import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  ApiError,
  type BatchDecideResult,
  type Collection,
  type CollectionPendingMap,
  type CollectionRecords,
  type CollectionTarget,
  type CreateCollectionBody,
  type PackageMapPayload,
} from '../api/client'
import { formatCost, formatDateTime } from '../components/shared/format'
import { MarkdownDoc } from '../components/shared/MarkdownDoc'
import { Modal } from '../components/shared/Modal'
import { QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

const POLL_MS = 5000
const MARKETS = ['US', 'UK', 'EU'] as const
const CSV_HEADER =
  'company,market,domicile_country,sector,category,expected_form,fiscal_year,known_urls,notes'

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.status === 403 ? 'Requires approver role.' : e.message
  return e instanceof Error ? e.message : String(e)
}

function isLive(c: Collection): boolean {
  if (c.status === 'running') return true
  return (c.targets ?? []).some((t) => t.status === 'running' || t.status === 'packaging')
}

function safeHref(url: string | null | undefined): string | null {
  return url && /^https?:\/\//i.test(url) ? url : null
}

function ExtLink({ url, children }: { url: string | null | undefined; children: React.ReactNode }) {
  const href = safeHref(url)
  if (!href) return <>{children}</>
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

export function Collections() {
  const { id } = useParams<{ id: string }>()
  return id ? <CollectionDetail id={id} /> : <CollectionList />
}

// ── List ─────────────────────────────────────────────────────────────────

function countOf(c: Collection, ...keys: (keyof Collection['counts']['by_status'])[]): number {
  return keys.reduce((n, k) => n + (c.counts.by_status[k] ?? 0), 0)
}

function CollectionList() {
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const listQuery = useQuery({
    queryKey: ['collections'],
    queryFn: api.listCollections,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((c) => c.status === 'running') ? POLL_MS : false,
  })
  const rows = listQuery.data ?? []

  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
        <div>
          <h1 className="view-title">Collections</h1>
          <div className="view-sub">
            Batch disclosure collection: a register of companies, run in waves, with package
            maps approved in bulk and records written to SharePoint.
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          New collection
        </button>
      </div>

      {listQuery.isLoading ? (
        <div className="empty pulse">Loading collections…</div>
      ) : listQuery.error ? (
        <QueryError error={listQuery.error} what="collections" />
      ) : rows.length === 0 ? (
        <div className="empty">No collections yet — create one from a target register CSV.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="mono-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Run slug</th>
                <th>Status</th>
                <th className="num">Total</th>
                <th className="num">Done</th>
                <th className="num">Review</th>
                <th className="num">Gap</th>
                <th className="num">Failed</th>
                <th className="num">Cost</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr
                  key={c.id}
                  className="clickable"
                  role="button"
                  tabIndex={0}
                  aria-label={`Open collection ${c.name}`}
                  onClick={() => navigate(`/collections/${c.id}`)}
                  onKeyDown={(e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return
                    e.preventDefault()
                    navigate(`/collections/${c.id}`)
                  }}
                >
                  <td>{c.name}</td>
                  <td>{c.run_slug}</td>
                  <td>
                    <StatusBadge status={c.status} />
                  </td>
                  <td className="num">{c.counts.total}</td>
                  <td className="num">{countOf(c, 'written', 'completed')}</td>
                  <td className="num">{countOf(c, 'review', 'packaging')}</td>
                  <td className="num">{countOf(c, 'gap')}</td>
                  <td className="num">{countOf(c, 'failed', 'write_failed')}</td>
                  <td className="num">{formatCost(c.cost_usd)}</td>
                  <td>{formatDateTime(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <CreateCollectionModal open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}

// ── Create ───────────────────────────────────────────────────────────────

function todaySlug(): string {
  return `${new Date().toISOString().slice(0, 10)}-batch1`
}

function CreateCollectionModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const targetsQuery = useQuery({
    queryKey: ['m365-write-targets'],
    queryFn: api.m365WriteTargets,
    enabled: open,
    retry: false,
  })
  const targets = targetsQuery.data?.targets ?? []
  const writeEnabled = targetsQuery.data?.write_enabled ?? false

  const [name, setName] = useState('')
  const [runSlug, setRunSlug] = useState(todaySlug)
  const [targetSlug, setTargetSlug] = useState('')
  const [writeRoot, setWriteRoot] = useState('runs')
  const [concurrency, setConcurrency] = useState(3)
  const [firstBatch, setFirstBatch] = useState(12)
  const [fiscalYear, setFiscalYear] = useState(2025)
  const [autoWrite, setAutoWrite] = useState(true)
  const [model, setModel] = useState('')
  const [maxIterations, setMaxIterations] = useState('')
  const [csv, setCsv] = useState(`${CSV_HEADER}\n`)
  const [created, setCreated] = useState<Collection | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)

  const effectiveTarget = targetSlug || targets[0]?.slug || ''

  const createMutation = useMutation({
    mutationFn: (body: CreateCollectionBody) => api.createCollection(body),
    onSuccess: (c) => {
      void queryClient.invalidateQueries({ queryKey: ['collections'] })
      if (c.warnings.length > 0) {
        setCreated(c)
      } else {
        onClose()
        navigate(`/collections/${c.id}`)
      }
    },
  })

  const submit = () => {
    createMutation.mutate({
      name: name.trim(),
      run_slug: runSlug.trim(),
      register_csv: csv,
      target_slug: effectiveTarget,
      write_root: writeRoot.trim() || 'runs',
      concurrency,
      first_batch_size: firstBatch,
      fiscal_year: fiscalYear,
      auto_write: autoWrite,
      model_override: model.trim() || null,
      max_iterations: maxIterations.trim() ? Number(maxIterations) : null,
    })
  }

  const loadFile = (file: File | undefined) => {
    if (!file) return
    setFileError(null)
    file.text().then(setCsv, (e: unknown) => setFileError(`Could not read file: ${errText(e)}`))
  }

  const canSubmit =
    name.trim() !== '' && runSlug.trim() !== '' && effectiveTarget !== '' && !createMutation.isPending
  const createError = createMutation.error

  if (created) {
    return (
      <Modal
        open={open}
        onClose={() => {
          setCreated(null)
          onClose()
        }}
        title="Collection created"
        width={640}
        footer={
          <button
            className="btn btn-primary"
            onClick={() => {
              const id = created.id
              setCreated(null)
              onClose()
              navigate(`/collections/${id}`)
            }}
          >
            Open collection
          </button>
        }
      >
        <WarningsBox warnings={created.warnings} />
      </Modal>
    )
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="New collection"
      width={760}
      footer={
        <div className="row" style={{ justifyContent: 'flex-end', flexWrap: 'wrap' }}>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={submit}>
            {createMutation.isPending ? 'Creating…' : 'Create collection'}
          </button>
        </div>
      }
    >
      <div className="field">
        <label className="mono-label">Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label className="mono-label">Run slug</label>
        <input type="text" value={runSlug} onChange={(e) => setRunSlug(e.target.value)} />
        <span className="fine-print">Lowercase letters, digits and dashes; unique per workspace.</span>
      </div>

      <div className="field">
        <label className="mono-label">Write target</label>
        {targetsQuery.isLoading ? (
          <span className="fine-print pulse">Loading write targets…</span>
        ) : targets.length === 0 || !writeEnabled ? (
          <span className="fine-print">
            Connect SharePoint and choose a write target in Settings → Connections.
          </span>
        ) : (
          <select value={effectiveTarget} onChange={(e) => setTargetSlug(e.target.value)}>
            {targets.map((t) => (
              <option key={t.slug} value={t.slug}>
                {t.label} — {t.path}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="row" style={{ gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div className="field">
          <label className="mono-label">Write root</label>
          <input type="text" value={writeRoot} onChange={(e) => setWriteRoot(e.target.value)} />
        </div>
        <div className="field">
          <label className="mono-label">Concurrency (1–6)</label>
          <input
            type="number"
            min={1}
            max={6}
            value={concurrency}
            onChange={(e) => setConcurrency(Math.min(6, Math.max(1, Number(e.target.value) || 1)))}
          />
        </div>
        <div className="field">
          <label className="mono-label">First batch (0 = no gate)</label>
          <input
            type="number"
            min={0}
            value={firstBatch}
            onChange={(e) => setFirstBatch(Math.max(0, Number(e.target.value) || 0))}
          />
        </div>
        <div className="field">
          <label className="mono-label">Fiscal year</label>
          <input
            type="number"
            value={fiscalYear}
            onChange={(e) => setFiscalYear(Number(e.target.value) || 2025)}
          />
        </div>
        <div className="field">
          <label className="mono-label">Max iterations (1–50, blank = harness)</label>
          <input
            type="number"
            min={1}
            max={50}
            value={maxIterations}
            onChange={(e) => {
              const raw = e.target.value
              setMaxIterations(raw === '' ? '' : String(Math.min(50, Math.max(1, Number(raw) || 1))))
            }}
          />
        </div>
      </div>

      <div className="field">
        <label className="mono-label">Model override (optional)</label>
        <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
      </div>
      <label className="check-row" style={{ marginBottom: 14 }}>
        <input type="checkbox" checked={autoWrite} onChange={(e) => setAutoWrite(e.target.checked)} />
        <span>Upload package files to SharePoint automatically when packaging succeeds</span>
      </label>

      <div className="field">
        <label className="mono-label">Target register (CSV)</label>
        <textarea
          rows={10}
          value={csv}
          onChange={(e) => setCsv(e.target.value)}
          spellCheck={false}
          style={{ whiteSpace: 'pre', overflowX: 'auto' }}
        />
        <span className="fine-print">
          Markets: US, UK, EU. Separate several known_urls with |. Blank cells use defaults.
        </span>
        <input
          type="file"
          accept=".csv,text/csv"
          aria-label="Load register from file"
          onChange={(e) => loadFile(e.target.files?.[0])}
        />
        {fileError && <div className="error-text">{fileError}</div>}
        {createError && <div className="error-text">{errText(createError)}</div>}
      </div>
    </Modal>
  )
}

function WarningsBox({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null
  return (
    <div className="callout callout-warn" style={{ marginBottom: 14 }}>
      <div className="callout-title">Register warnings</div>
      <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
        {warnings.map((w, i) => (
          <li key={i}>{w}</li>
        ))}
      </ul>
    </div>
  )
}

// ── Detail ───────────────────────────────────────────────────────────────

function CollectionDetail({ id }: { id: string }) {
  const queryClient = useQueryClient()
  const [previewing, setPreviewing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const query = useQuery({
    queryKey: ['collection', id],
    queryFn: () => api.getCollection(id),
    refetchInterval: (q) => (q.state.data && isLive(q.state.data) ? POLL_MS : false),
  })
  const c = query.data

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['collection', id] })
    void queryClient.invalidateQueries({ queryKey: ['collections'] })
  }
  const actionMutation = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onMutate: () => setActionError(null),
    onSuccess: refresh,
    onError: (e) => setActionError(errText(e)),
  })
  const act = (fn: () => Promise<unknown>) => actionMutation.mutate(fn)
  const busy = actionMutation.isPending

  if (query.isLoading) return <div className="empty pulse">Loading collection…</div>
  if (query.error || !c) {
    return (
      <div>
        <Link to="/collections">← Collections</Link>
        <QueryError error={query.error ?? new Error('not found')} what="this collection" />
      </div>
    )
  }

  const targets = c.targets ?? []
  const held = c.counts.by_status.held ?? 0
  const s = c.settings

  const hasPendingMaps = (c.pending_maps ?? []).length > 0
  const hasActiveTargets = targets.some(
    (t) => t.status === 'running' || t.status === 'pending' || t.status === 'packaging',
  )
  const finalizeBlock = hasPendingMaps
    ? `Decide the ${(c.pending_maps ?? []).length} pending package maps first`
    : hasActiveTargets
      ? 'Wait for running targets'
      : null
  const canFinalize = ['gate', 'review', 'completed', 'paused', 'finalized'].includes(c.status)
  const canRelease = held > 0 && ['gate', 'review', 'completed', 'paused'].includes(c.status)

  const finalize = () => {
    const where = `${s.target_slug}/tret/${s.write_root}/${c.run_slug}/`
    if (
      window.confirm(
        `Writes manifest, execution, exception log, register, SHA256SUMS and results summary to ${where}`,
      )
    ) {
      act(() => api.finalizeCollection(id))
    }
  }

  return (
    <div>
      <Link to="/collections" className="fine-print">
        ← Collections
      </Link>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap', marginTop: 6 }}>
        <h1 className="view-title" style={{ marginBottom: 0 }}>
          {c.name}
        </h1>
        <StatusBadge status={c.status} />
      </div>
      <div className="view-sub" style={{ marginTop: 6 }}>
        {c.run_slug} · target {s.target_slug} · {s.write_root}/ · concurrency {s.concurrency} ·{' '}
        {s.first_batch_size === 0 ? 'no gate' : `gate after ${s.first_batch_size}`} · FY
        {s.fiscal_year} · auto-write {s.auto_write ? 'on' : 'off'}
        {s.model_override ? ` · model ${s.model_override}` : ''}
        {s.max_iterations ? ` · max iterations ${s.max_iterations}` : ''} · cost {formatCost(c.cost_usd)}
      </div>

      <WarningsBox warnings={c.warnings} />

      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 14 }}>
        {c.status === 'draft' && (
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => act(() => api.startCollection(id))}
          >
            Start
          </button>
        )}
        {c.status === 'running' && (
          <button className="btn" disabled={busy} onClick={() => act(() => api.pauseCollection(id))}>
            Pause
          </button>
        )}
        {c.status === 'paused' && (
          <button className="btn" disabled={busy} onClick={() => act(() => api.resumeCollection(id))}>
            Resume
          </button>
        )}
        {canRelease && (
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => act(() => api.releaseCollection(id))}
          >
            Release remaining ({held} held)
          </button>
        )}
        {canFinalize && (
          <button
            className="btn"
            disabled={busy || finalizeBlock !== null}
            title={finalizeBlock ?? undefined}
            onClick={finalize}
          >
            {c.status === 'finalized' ? 'Re-finalize (overwrites the record files)' : 'Finalize'}
          </button>
        )}
        {canFinalize && finalizeBlock && <span className="fine-print">{finalizeBlock}</span>}
        <button className="btn" onClick={() => setPreviewing(true)}>
          Preview records
        </button>
        {actionError && <span className="error-text">{actionError}</span>}
      </div>

      {c.finalize && (
        <div className="panel" style={{ marginBottom: 14 }}>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            Records written {formatDateTime(c.finalize.written_at)}
          </div>
          {c.finalize.error && <div className="error-text">{c.finalize.error}</div>}
          <ul style={{ margin: 0, paddingLeft: 18 }} className="mono-body">
            {c.finalize.files.map((f) => (
              <li key={f.name}>
                <ExtLink url={f.web_url}>{f.name}</ExtLink>{' '}
                <span className="fine-print">{f.sha256.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'stretch', marginBottom: 18 }}>
        {MARKETS.map((m) => {
          const mc = c.counts.by_market[m] ?? { total: 0, done: 0, gap: 0, failed: 0 }
          return (
            <div key={m} className="panel" style={{ minWidth: 150 }}>
              <div className="mono-label">{m}</div>
              <div className="mono-body" style={{ fontSize: 18 }}>
                {mc.done} / {mc.total}
              </div>
              <div className="fine-print">
                gap {mc.gap} · failed {mc.failed}
              </div>
            </div>
          )
        })}
      </div>

      {(c.pending_maps ?? []).length > 0 && (
        <PendingMaps
          maps={c.pending_maps ?? []}
          targets={targets}
          onDecided={refresh}
        />
      )}

      <TargetsTable collection={c} targets={targets} busy={busy} act={act} />

      <RecordsPreview id={id} open={previewing} onClose={() => setPreviewing(false)} />
    </div>
  )
}

// ── Pending package maps ─────────────────────────────────────────────────

function PendingMaps({
  maps,
  targets,
  onDecided,
}: {
  maps: CollectionPendingMap[]
  targets: CollectionTarget[]
  onDecided: () => void
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [note, setNote] = useState('')
  const [results, setResults] = useState<BatchDecideResult[]>([])
  const companyById = useMemo(() => new Map(targets.map((t) => [t.id, t.company])), [targets])

  const decide = useMutation({
    mutationFn: ({ ids, action }: { ids: string[]; action: 'approve' | 'reject' }) =>
      api.decideFindingsBatch(ids, action, note.trim() || undefined),
    onSuccess: (r) => {
      setResults(r.results)
      setSelected(new Set())
      onDecided()
    },
  })

  const ids = maps.map((m) => m.id)
  const allSelected = ids.length > 0 && ids.every((i) => selected.has(i))
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const chosen = ids.filter((i) => selected.has(i))
  const failures = results.filter((r): r is Extract<BatchDecideResult, { ok: false }> => !r.ok)

  return (
    <div style={{ marginBottom: 20 }}>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Package maps awaiting review ({maps.length})
      </div>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
        <label className="check-row">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => setSelected(allSelected ? new Set() : new Set(ids))}
          />
          <span>Select all</span>
        </label>
        <input
          type="text"
          placeholder="Note (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          style={{ flex: '1 1 160px', minWidth: 0 }}
        />
        <button
          className="btn btn-approve"
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => decide.mutate({ ids: chosen, action: 'approve' })}
        >
          Approve selected ({chosen.length})
        </button>
        <button
          className="btn btn-reject"
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => decide.mutate({ ids: chosen, action: 'reject' })}
        >
          Reject selected
        </button>
      </div>
      {decide.error && <div className="error-text">{errText(decide.error)}</div>}
      {failures.map((f) => (
        <div key={f.id} className="error-text">
          {companyById.get(maps.find((m) => m.id === f.id)?.target_id ?? '') ?? f.id}: {f.error}
        </div>
      ))}

      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            <tr>
              <th />
              <th>Company</th>
              <th>Stem</th>
              <th>Role / scope</th>
              <th>Keep ranges</th>
              <th className="num">Pages</th>
              <th>Source</th>
              <th>Review note</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {maps.map((m) => {
              const p = m.payload as unknown as PackageMapPayload
              const map = p.map
              return (
                <tr key={m.id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(m.id)}
                      onChange={() => toggle(m.id)}
                      aria-label={`Select ${p.stem}`}
                    />
                  </td>
                  <td>{companyById.get(m.target_id) ?? '—'}</td>
                  <td>{p.stem}</td>
                  <td>
                    {map.role} / {map.scope}
                  </td>
                  <td>
                    {map.keep_ranges
                      ? map.keep_ranges.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(', ')
                      : 'whole file'}
                  </td>
                  <td className="num">
                    {p.selected_page_count}/{p.source.page_count ?? '?'}
                  </td>
                  <td style={{ overflowWrap: 'anywhere', maxWidth: 240 }}>
                    <ExtLink url={p.source.url}>{p.source.url ?? p.source.filename}</ExtLink>
                  </td>
                  <td style={{ maxWidth: 280 }}>{map.review_note ?? '—'}</td>
                  <td>
                    <Link to={`/approvals?finding=${m.id}`}>Full view</Link>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Targets ──────────────────────────────────────────────────────────────

function TargetsTable({
  collection,
  targets,
  busy,
  act,
}: {
  collection: Collection
  targets: CollectionTarget[]
  busy: boolean
  act: (fn: () => Promise<unknown>) => void
}) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Targets ({targets.length})
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Company</th>
              <th>Market</th>
              <th>Sector / category / form</th>
              <th>Status</th>
              <th className="num">Tries</th>
              <th>Packages</th>
              <th>Outcome</th>
              <th className="num">Cost</th>
              <th>Model</th>
              <th>Run</th>
              <th>Error</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {targets.map((t) => {
              const expanded = open.has(t.id)
              return (
                <Fragment key={t.id}>
                  <tr>
                    <td>{t.position}</td>
                    <td>
                      <button
                        className="btn btn-sm"
                        aria-expanded={expanded}
                        onClick={() => toggle(t.id)}
                        style={{ marginRight: 6 }}
                      >
                        {expanded ? '−' : '+'}
                      </button>
                      {t.company}
                      <div className="fine-print">{t.slug}</div>
                    </td>
                    <td>{t.market}</td>
                    <td>
                      {[t.sector, t.category, t.expected_form].filter(Boolean).join(' / ') || '—'}
                    </td>
                    <td>
                      <StatusBadge status={t.status} />
                    </td>
                    <td className="num">{t.attempts}</td>
                    <td>
                      {t.packages.length === 0
                        ? '—'
                        : t.packages.map((p) => (
                            <div key={p.finding_id}>
                              {p.stem ?? '—'} <StatusBadge status={p.package_status ?? p.status} />
                              {p.retained_page_count !== null && (
                                <span className="fine-print"> {p.retained_page_count}p</span>
                              )}
                            </div>
                          ))}
                    </td>
                    <td title={t.outcome?.summary ?? undefined} style={{ maxWidth: 240 }}>
                      {t.outcome ? (
                        <>
                          <StatusBadge status={t.outcome.status} />
                          <div className="fine-print">{shorten(t.outcome.summary, 90)}</div>
                        </>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="num">{formatCost(t.cost_usd)}</td>
                    <td>{t.model_used ?? '—'}</td>
                    <td>
                      {t.run_id ? <Link to={`/runs/${t.run_id}`}>{t.run_id.slice(0, 8)}</Link> : '—'}
                    </td>
                    <td className="error-text" style={{ maxWidth: 220 }}>
                      {t.error ?? ''}
                    </td>
                    <td>
                      {(t.status === 'write_failed' ||
                        (t.status === 'completed' &&
                          (t.error ?? '').includes('collection finalized before write'))) && (
                        <button
                          className="btn btn-sm"
                          disabled={busy}
                          onClick={() =>
                            act(() => api.retryCollectionTargetWrite(collection.id, t.id))
                          }
                        >
                          Retry write
                        </button>
                      )}
                      {(t.status === 'failed' ||
                        t.status === 'gap' ||
                        t.status === 'rejected' ||
                        t.status === 'write_failed') && (
                        <button
                          className="btn btn-sm"
                          disabled={busy}
                          onClick={() => act(() => api.retryCollectionTarget(collection.id, t.id))}
                        >
                          Retry
                        </button>
                      )}
                    </td>
                  </tr>
                  {expanded && (
                    <tr>
                      <td />
                      <td colSpan={12}>
                        <TargetExtras target={t} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function shorten(s: string | null, n: number): string {
  if (!s) return ''
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}

function TargetExtras({ target: t }: { target: CollectionTarget }) {
  const o = t.outcome
  const meta: [string, string | null | undefined][] = o
    ? [
        ['Period', o.reporting_period_label],
        ['Published', o.publication_date],
        ['Basis', o.publication_basis],
        ['Type', o.disclosure_type],
        ['Framework', o.framework],
        ['Language', o.language],
        ['Substitution', o.substitution_note],
      ]
    : []
  return (
    <div className="stack" style={{ gap: 8 }}>
      {o?.summary && <div className="mono-body">{o.summary}</div>}
      {meta.some(([, v]) => v) && (
        <div className="row" style={{ gap: 20, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          {meta
            .filter(([, v]) => v)
            .map(([k, v]) => (
              <div key={k}>
                <div className="mono-label">{k}</div>
                <div className="mono-body">{v}</div>
              </div>
            ))}
        </div>
      )}
      <UrlList label="Blocked URLs" urls={o?.blocked_urls} />
      <UrlList label="Candidate URLs" urls={o?.candidate_urls} />
      <UrlList label="Known URLs" urls={t.known_urls} />
      {t.notes && <div className="fine-print">Notes: {t.notes}</div>}
      {(t.history ?? []).length > 0 && (
        <div>
          <div className="mono-label">Previous attempts</div>
          <ul style={{ margin: 0, paddingLeft: 18 }} className="mono-body">
            {t.history.map((h) => (
              <li key={h.attempt}>
                #{h.attempt} <StatusBadge status={h.status} /> {h.model_used ?? '—'} ·{' '}
                {h.cost_usd === null ? '—' : formatCost(h.cost_usd)}
                {h.run_id && (
                  <>
                    {' · '}
                    <Link to={`/runs/${h.run_id}`}>{h.run_id.slice(0, 8)}</Link>
                  </>
                )}
                {h.error && <div className="error-text">{h.error}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {t.written_files.length > 0 && (
        <div>
          <div className="mono-label">Written files</div>
          <ul style={{ margin: 0, paddingLeft: 18 }} className="mono-body">
            {t.written_files.map((f) => (
              <li key={f.filename}>
                <ExtLink url={f.web_url}>{f.filename}</ExtLink>{' '}
                <span className="fine-print">{f.byte_size.toLocaleString('en-US')} B</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {t.finding_ids.length > 0 && (
        <div className="fine-print">
          Findings:{' '}
          {t.finding_ids.map((f) => (
            <Link key={f} to={`/approvals?finding=${f}`} style={{ marginRight: 8 }}>
              {f.slice(0, 8)}
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}

function UrlList({ label, urls }: { label: string; urls: string[] | undefined }) {
  if (!urls || urls.length === 0) return null
  return (
    <div>
      <div className="mono-label">{label}</div>
      <ul style={{ margin: 0, paddingLeft: 18 }} className="fine-print">
        {urls.map((u) => (
          <li key={u} style={{ overflowWrap: 'anywhere' }}>
            <ExtLink url={u}>{u}</ExtLink>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ── Records preview ──────────────────────────────────────────────────────

function RecordsPreview({
  id,
  open,
  onClose,
}: {
  id: string
  open: boolean
  onClose: () => void
}) {
  const recordsQuery = useQuery<CollectionRecords>({
    queryKey: ['collection-records', id],
    queryFn: () => api.collectionRecords(id),
    enabled: open,
    staleTime: 0,
  })
  const r = recordsQuery.data
  return (
    <Modal open={open} onClose={onClose} title="Records preview" width={960}>
      {recordsQuery.isLoading ? (
        <div className="empty pulse">Building records…</div>
      ) : recordsQuery.error ? (
        <QueryError error={recordsQuery.error} what="records" />
      ) : r ? (
        <div className="stack" style={{ gap: 14 }}>
          <MarkdownDoc source={r.results_summary} />
          <div className="mono-label">Manifest</div>
          <pre className="mono-body" style={{ overflowX: 'auto', margin: 0 }}>
            {JSON.stringify(r.manifest, null, 2)}
          </pre>
        </div>
      ) : null}
    </Modal>
  )
}
