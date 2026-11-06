import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  ApiError,
  isPromptOutcome,
  isPromptSettings,
  type Batch,
  type BatchRecords,
  type BatchTarget,
  type CreateBatchBody,
  type CreateDisclosureBatchBody,
  type DisclosureTargetOutcome,
} from '../api/client'
import { DisclosurePanels } from '../components/batches/DisclosurePanels'
import { PendingReviews } from '../components/batches/PendingReviews'
import { usePromptBatchForm } from '../components/batches/PromptBatchForm'
import { slugify } from '../components/batches/csv'
import { errText, ExtLink, safeHref, shorten, WarningsBox } from '../components/batches/shared'
import { formatCost, formatDateTime } from '../components/shared/format'
import { MarkdownDoc } from '../components/shared/MarkdownDoc'
import { Modal } from '../components/shared/Modal'
import { QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

const POLL_MS = 5000
const CSV_HEADER =
  'company,market,domicile_country,sector,category,expected_form,fiscal_year,known_urls,notes'

const PROFILE_LABEL: Record<string, string> = {
  disclosure: 'Disclosure collection',
  prompt: 'Prompt',
}

function isLive(c: Batch): boolean {
  if (c.status === 'running') return true
  return (c.targets ?? []).some((t) => t.status === 'running' || t.status === 'packaging')
}

export function Batches() {
  const { id } = useParams<{ id: string }>()
  return id ? <BatchDetail id={id} /> : <BatchList />
}

// ── List ─────────────────────────────────────────────────────────────────

function countOf(c: Batch, ...keys: (keyof Batch['counts']['by_status'])[]): number {
  return keys.reduce((n, k) => n + (c.counts.by_status[k] ?? 0), 0)
}

function BatchList() {
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const listQuery = useQuery({
    queryKey: ['batches'],
    queryFn: api.listBatches,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((c) => c.status === 'running') ? POLL_MS : false,
  })
  const rows = listQuery.data ?? []

  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
        <div>
          <h1 className="view-title">Batches</h1>
          <div className="view-sub">
            Run a harness over many rows: a disclosure collection over a register of companies
            (package maps approved in bulk, records written to SharePoint), or a prompt template
            over the rows of a CSV.
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          New batch
        </button>
      </div>

      {listQuery.isLoading ? (
        <div className="empty pulse">Loading batches…</div>
      ) : listQuery.error ? (
        <QueryError error={listQuery.error} what="batches" />
      ) : rows.length === 0 ? (
        <div className="empty">
          No batches yet — create one from a target register or a prompt template and a CSV.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="mono-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Profile</th>
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
                  aria-label={`Open batch ${c.name}`}
                  onClick={() => navigate(`/batches/${c.id}`)}
                  onKeyDown={(e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return
                    e.preventDefault()
                    navigate(`/batches/${c.id}`)
                  }}
                >
                  <td>{c.name}</td>
                  <td>{PROFILE_LABEL[c.profile] ?? c.profile}</td>
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

      <CreateBatchModal open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}

// ── Create ───────────────────────────────────────────────────────────────

function todaySlug(): string {
  return `${new Date().toISOString().slice(0, 10)}-batch1`
}

function CreateBatchModal({ open, onClose }: { open: boolean; onClose: () => void }) {
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

  const [profile, setProfile] = useState<'disclosure' | 'prompt'>('disclosure')
  const [name, setName] = useState('')
  const [runSlug, setRunSlug] = useState(todaySlug)
  const [slugEdited, setSlugEdited] = useState(false)
  const [targetSlug, setTargetSlug] = useState('')
  const [writeRoot, setWriteRoot] = useState('runs')
  const [concurrency, setConcurrency] = useState(3)
  const [firstBatch, setFirstBatch] = useState(12)
  const [fiscalYear, setFiscalYear] = useState(2025)
  const [autoWrite, setAutoWrite] = useState(true)
  const [model, setModel] = useState('')
  const [maxIterations, setMaxIterations] = useState('')
  const [csv, setCsv] = useState(`${CSV_HEADER}\n`)
  const [created, setCreated] = useState<Batch | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)

  const prompt = usePromptBatchForm(open && profile === 'prompt', { name, runSlug })

  const effectiveTarget = targetSlug || targets[0]?.slug || ''

  const createMutation = useMutation({
    mutationFn: (body: CreateBatchBody) => api.createBatch(body),
    onSuccess: (c) => {
      void queryClient.invalidateQueries({ queryKey: ['batches'] })
      if (c.warnings.length > 0) {
        setCreated(c)
      } else {
        onClose()
        navigate(`/batches/${c.id}`)
      }
    },
  })

  const submitDisclosure = () => {
    const body: CreateDisclosureBatchBody = {
      profile: 'disclosure',
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
    }
    createMutation.mutate(body)
  }

  const loadFile = (file: File | undefined) => {
    if (!file) return
    setFileError(null)
    file.text().then(setCsv, (e: unknown) => setFileError(`Could not read file: ${errText(e)}`))
  }

  const onNameChange = (v: string) => {
    setName(v)
    if (profile === 'prompt' && !slugEdited) setRunSlug(slugify(v) || todaySlug())
  }

  const canSubmit =
    !createMutation.isPending &&
    (profile === 'prompt'
      ? prompt.body !== null
      : name.trim() !== '' && runSlug.trim() !== '' && effectiveTarget !== '')
  const createError = createMutation.error

  if (created) {
    return (
      <Modal
        open={open}
        onClose={() => {
          setCreated(null)
          onClose()
        }}
        title="Batch created"
        width={640}
        footer={
          <button
            className="btn btn-primary"
            onClick={() => {
              const id = created.id
              setCreated(null)
              onClose()
              navigate(`/batches/${id}`)
            }}
          >
            Open batch
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
      title="New batch"
      width={760}
      footer={
        <div className="row" style={{ justifyContent: 'flex-end', flexWrap: 'wrap' }}>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={!canSubmit}
            onClick={() => {
              if (profile === 'prompt') {
                if (prompt.body) createMutation.mutate(prompt.body)
              } else {
                submitDisclosure()
              }
            }}
          >
            {createMutation.isPending ? 'Creating…' : 'Create batch'}
          </button>
        </div>
      }
    >
      <div
        className="row"
        role="radiogroup"
        aria-label="Batch profile"
        style={{ gap: 12, alignItems: 'stretch', flexWrap: 'wrap', marginBottom: 14 }}
      >
        {(
          [
            [
              'disclosure',
              'Disclosure collection',
              'Register of companies, Disclosure Collector harness, package maps reviewed, SharePoint write-back.',
            ],
            [
              'prompt',
              'Prompt',
              'Any harness over the rows of a CSV, one run per row.',
            ],
          ] as const
        ).map(([key, title, desc]) => (
          <label
            key={key}
            className="panel"
            style={{
              flex: '1 1 260px',
              cursor: 'pointer',
              outline: profile === key ? '2px solid currentColor' : undefined,
            }}
          >
            <div className="row" style={{ gap: 8 }}>
              <input
                type="radio"
                name="batch-profile"
                checked={profile === key}
                onChange={() => setProfile(key)}
              />
              <span className="mono-label">{title}</span>
            </div>
            <div className="fine-print" style={{ marginTop: 4 }}>
              {desc}
            </div>
          </label>
        ))}
      </div>

      <div className="field">
        <label className="mono-label">Name</label>
        <input type="text" value={name} onChange={(e) => onNameChange(e.target.value)} />
      </div>
      <div className="field">
        <label className="mono-label">Run slug</label>
        <input
          type="text"
          value={runSlug}
          onChange={(e) => {
            setSlugEdited(true)
            setRunSlug(e.target.value)
          }}
        />
        <span className="fine-print">Lowercase letters, digits and dashes; unique per workspace.</span>
      </div>

      {profile === 'prompt' ? (
        prompt.fields
      ) : (
        <>
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
                onChange={(e) =>
                  setConcurrency(Math.min(6, Math.max(1, Number(e.target.value) || 1)))
                }
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
                  setMaxIterations(
                    raw === '' ? '' : String(Math.min(50, Math.max(1, Number(raw) || 1))),
                  )
                }}
              />
            </div>
          </div>

          <div className="field">
            <label className="mono-label">Model override (optional)</label>
            <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
          </div>
          <label className="check-row" style={{ marginBottom: 14 }}>
            <input
              type="checkbox"
              checked={autoWrite}
              onChange={(e) => setAutoWrite(e.target.checked)}
            />
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
          </div>
        </>
      )}
      {createError && <div className="error-text">{errText(createError)}</div>}
    </Modal>
  )
}

// ── Detail ───────────────────────────────────────────────────────────────

function settingsLine(c: Batch): string {
  const s = c.settings
  const parts: string[] = [c.run_slug]
  if (isPromptSettings(s)) {
    parts.push('prompt')
    if (s.harness_name) parts.push(`harness ${s.harness_name}`)
    parts.push(`concurrency ${s.concurrency}`)
    parts.push(s.first_batch_size === 0 ? 'no gate' : `gate after ${s.first_batch_size}`)
    parts.push(s.review_schema ? (s.review_schema === 'any' ? 'review: any finding' : `review: ${s.review_schema}`) : 'no review')
    parts.push(
      s.target_slug
        ? `target ${s.target_slug} · ${s.write_root ?? 'runs'}/`
        : 'records kept as documents',
    )
    if (s.model_override) parts.push(`model ${s.model_override}`)
    if (s.max_iterations) parts.push(`max iterations ${s.max_iterations}`)
  } else {
    parts.push(`target ${s.target_slug}`)
    parts.push(`${s.write_root}/`)
    parts.push(`concurrency ${s.concurrency}`)
    parts.push(s.first_batch_size === 0 ? 'no gate' : `gate after ${s.first_batch_size}`)
    parts.push(`FY${s.fiscal_year}`)
    parts.push(`auto-write ${s.auto_write ? 'on' : 'off'}`)
    if (s.model_override) parts.push(`model ${s.model_override}`)
    if (s.max_iterations) parts.push(`max iterations ${s.max_iterations}`)
  }
  parts.push(`cost ${formatCost(c.cost_usd)}`)
  return parts.join(' · ')
}

/** A finalize 409 caused by an infrastructure failure, which "Finalize anyway" can override. */
function isInfraConflict(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409 && e.message.includes('I/O or storage error')
}

function BatchDetail({ id }: { id: string }) {
  const queryClient = useQueryClient()
  const [previewing, setPreviewing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [forceOffer, setForceOffer] = useState(false)

  const query = useQuery({
    queryKey: ['batch', id],
    queryFn: () => api.getBatch(id),
    refetchInterval: (q) => (q.state.data && isLive(q.state.data) ? POLL_MS : false),
  })
  const c = query.data

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['batch', id] })
    void queryClient.invalidateQueries({ queryKey: ['batches'] })
  }
  const actionMutation = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onMutate: () => {
      setActionError(null)
      setForceOffer(false)
    },
    onSuccess: refresh,
    onError: (e) => setActionError(errText(e)),
  })
  const act = (fn: () => Promise<unknown>) => actionMutation.mutate(fn)
  const finalizeMutation = useMutation({
    mutationFn: (force: boolean) => api.finalizeBatch(id, force ? { force: true } : undefined),
    onMutate: () => {
      setActionError(null)
      setForceOffer(false)
    },
    onSuccess: refresh,
    onError: (e) => {
      setActionError(errText(e))
      setForceOffer(isInfraConflict(e))
    },
  })
  const busy = actionMutation.isPending || finalizeMutation.isPending

  if (query.isLoading) return <div className="empty pulse">Loading batch…</div>
  if (query.error || !c) {
    return (
      <div>
        <Link to="/batches">← Batches</Link>
        <QueryError error={query.error ?? new Error('not found')} what="this batch" />
      </div>
    )
  }

  const targets = c.targets ?? []
  const held = c.counts.by_status.held ?? 0
  const s = c.settings
  const promptSettings = isPromptSettings(s) ? s : null
  const isDisclosure = c.profile === 'disclosure'

  const pendingReviews = c.pending_reviews ?? c.pending_maps ?? []
  const hasActiveTargets = targets.some(
    (t) => t.status === 'running' || t.status === 'pending' || t.status === 'packaging',
  )
  const finalizeBlock =
    pendingReviews.length > 0
      ? isDisclosure
        ? `Decide the ${pendingReviews.length} pending package maps first`
        : `Decide the ${pendingReviews.length} pending reviews first`
      : hasActiveTargets
        ? 'Wait for running targets'
        : null
  const canFinalize = ['gate', 'review', 'completed', 'paused', 'finalized'].includes(c.status)
  const canRelease = held > 0 && ['gate', 'review', 'completed', 'paused'].includes(c.status)

  const finalize = (force: boolean) => {
    let message: string
    if (force) {
      message =
        'Finalize anyway? This writes the records even though some targets failed with an I/O or storage error.'
    } else if (promptSettings && !promptSettings.target_slug) {
      message =
        'Stores manifest.json, results.jsonl and results_summary.md as documents on this batch.'
    } else {
      const slug = promptSettings ? promptSettings.target_slug : (s as { target_slug: string }).target_slug
      const root = promptSettings
        ? (promptSettings.write_root ?? 'runs')
        : (s as { write_root: string }).write_root
      const files = promptSettings
        ? 'manifest, results and results summary'
        : 'manifest, execution, exception log, register, SHA256SUMS and results summary'
      message = `Writes ${files} to ${slug}/tret/${root}/${c.run_slug}/`
    }
    if (window.confirm(message)) finalizeMutation.mutate(force)
  }

  return (
    <div>
      <Link to="/batches" className="fine-print">
        ← Batches
      </Link>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap', marginTop: 6 }}>
        <h1 className="view-title" style={{ marginBottom: 0 }}>
          {c.name}
        </h1>
        <StatusBadge status={c.status} />
      </div>
      <div className="view-sub" style={{ marginTop: 6 }}>
        {settingsLine(c)}
      </div>
      {promptSettings && (
        <details style={{ marginBottom: 10 }}>
          <summary className="fine-print">Prompt template</summary>
          <pre className="mono-body" style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0' }}>
            {promptSettings.template}
          </pre>
        </details>
      )}

      <WarningsBox warnings={c.warnings} />

      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 14 }}>
        {c.status === 'draft' && (
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => act(() => api.startBatch(id))}
          >
            Start
          </button>
        )}
        {c.status === 'running' && (
          <button className="btn" disabled={busy} onClick={() => act(() => api.pauseBatch(id))}>
            Pause
          </button>
        )}
        {c.status === 'paused' && (
          <button className="btn" disabled={busy} onClick={() => act(() => api.resumeBatch(id))}>
            Resume
          </button>
        )}
        {canRelease && (
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => act(() => api.releaseBatch(id))}
          >
            Release remaining ({held} held)
          </button>
        )}
        {canFinalize && (
          <button
            className="btn"
            disabled={busy || finalizeBlock !== null}
            title={finalizeBlock ?? undefined}
            onClick={() => finalize(false)}
          >
            {c.status === 'finalized' ? 'Re-finalize (overwrites the record files)' : 'Finalize'}
          </button>
        )}
        {canFinalize && forceOffer && (
          <button className="btn btn-reject" disabled={busy} onClick={() => finalize(true)}>
            Finalize anyway
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
                {safeHref(f.web_url) ? (
                  <ExtLink url={f.web_url}>{f.name}</ExtLink>
                ) : f.document_id ? (
                  <a href={api.batchRecordUrl(c.id, f.name)} download={f.name}>
                    {f.name}
                  </a>
                ) : (
                  f.name
                )}{' '}
                <span className="fine-print">{f.sha256.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {isDisclosure && <DisclosurePanels batch={c} />}

      {pendingReviews.length > 0 && (
        <PendingReviews reviews={pendingReviews} targets={targets} onDecided={refresh} />
      )}

      <TargetsTable batch={c} targets={targets} busy={busy} act={act} />

      <RecordsPreview id={id} open={previewing} onClose={() => setPreviewing(false)} />
    </div>
  )
}

// ── Targets ──────────────────────────────────────────────────────────────

function TargetsTable({
  batch,
  targets,
  busy,
  act,
}: {
  batch: Batch
  targets: BatchTarget[]
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
  const isDisclosure = batch.profile === 'disclosure'

  const retryButtons = (t: BatchTarget) => (
    <>
      {isDisclosure &&
        (t.status === 'write_failed' ||
          (t.status === 'completed' &&
            (t.error ?? '').includes('collection finalized before write'))) && (
          <button
            className="btn btn-sm"
            disabled={busy}
            onClick={() => act(() => api.retryBatchTargetWrite(batch.id, t.id))}
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
          onClick={() => act(() => api.retryBatchTarget(batch.id, t.id))}
        >
          Retry
        </button>
      )}
    </>
  )

  const expander = (t: BatchTarget, expanded: boolean) => (
    <button
      className="btn btn-sm"
      aria-expanded={expanded}
      onClick={() => toggle(t.id)}
      style={{ marginRight: 6 }}
    >
      {expanded ? '−' : '+'}
    </button>
  )

  const runCell = (t: BatchTarget) => (
    <td>{t.run_id ? <Link to={`/runs/${t.run_id}`}>{t.run_id.slice(0, 8)}</Link> : '—'}</td>
  )

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Targets ({targets.length})
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            {isDisclosure ? (
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
            ) : (
              <tr>
                <th>#</th>
                <th>Label</th>
                <th>Status</th>
                <th className="num">Tries</th>
                <th>Outcome</th>
                <th className="num">Cost</th>
                <th>Model</th>
                <th>Run</th>
                <th>Error</th>
                <th />
              </tr>
            )}
          </thead>
          <tbody>
            {targets.map((t) => {
              const expanded = open.has(t.id)
              if (!isDisclosure) {
                const o = isPromptOutcome(t.outcome) ? t.outcome : null
                return (
                  <Fragment key={t.id}>
                    <tr>
                      <td>{t.position}</td>
                      <td>
                        {expander(t, expanded)}
                        {t.label}
                        <div className="fine-print">{t.slug}</div>
                      </td>
                      <td>
                        <StatusBadge status={t.status} />
                      </td>
                      <td className="num">{t.attempts}</td>
                      <td style={{ maxWidth: 320, overflowWrap: 'anywhere' }}>
                        {o ? shorten(o.answer, 120) || '—' : '—'}
                      </td>
                      <td className="num">{formatCost(t.cost_usd)}</td>
                      <td>{t.model_used ?? '—'}</td>
                      {runCell(t)}
                      <td className="error-text" style={{ maxWidth: 220 }}>
                        {t.error ?? ''}
                      </td>
                      <td>{retryButtons(t)}</td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td />
                        <td colSpan={9}>
                          <PromptTargetExtras target={t} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              }
              const o = isPromptOutcome(t.outcome) ? null : (t.outcome as DisclosureTargetOutcome | null)
              const packages = t.packages ?? []
              return (
                <Fragment key={t.id}>
                  <tr>
                    <td>{t.position}</td>
                    <td>
                      {expander(t, expanded)}
                      {t.company ?? t.label}
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
                      {packages.length === 0
                        ? '—'
                        : packages.map((p) => (
                            <div key={p.finding_id}>
                              {p.stem ?? '—'} <StatusBadge status={p.package_status ?? p.status} />
                              {p.retained_page_count !== null && (
                                <span className="fine-print"> {p.retained_page_count}p</span>
                              )}
                            </div>
                          ))}
                    </td>
                    <td title={o?.summary ?? undefined} style={{ maxWidth: 240 }}>
                      {o ? (
                        <>
                          <StatusBadge status={o.status} />
                          <div className="fine-print">{shorten(o.summary, 90)}</div>
                        </>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="num">{formatCost(t.cost_usd)}</td>
                    <td>{t.model_used ?? '—'}</td>
                    {runCell(t)}
                    <td className="error-text" style={{ maxWidth: 220 }}>
                      {t.error ?? ''}
                    </td>
                    <td>{retryButtons(t)}</td>
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

function HistoryList({ target: t }: { target: BatchTarget }) {
  if ((t.history ?? []).length === 0) return null
  return (
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
  )
}

function FindingLinks({ target: t }: { target: BatchTarget }) {
  if (t.finding_ids.length === 0) return null
  return (
    <div className="fine-print">
      Findings:{' '}
      {t.finding_ids.map((f) => (
        <Link key={f} to={`/approvals?finding=${f}`} style={{ marginRight: 8 }}>
          {f.slice(0, 8)}
        </Link>
      ))}
    </div>
  )
}

function PromptTargetExtras({ target: t }: { target: BatchTarget }) {
  const o = isPromptOutcome(t.outcome) ? t.outcome : null
  const vars = Object.entries(t.task_input ?? {})
  return (
    <div className="stack" style={{ gap: 8 }}>
      {vars.length > 0 && (
        <div className="row" style={{ gap: 20, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          {vars.map(([k, v]) => (
            <div key={k}>
              <div className="mono-label">{k}</div>
              <div className="mono-body" style={{ overflowWrap: 'anywhere' }}>
                {String(v ?? '')}
              </div>
            </div>
          ))}
        </div>
      )}
      {o && (
        <div>
          <div className="mono-label">Answer</div>
          <div className="mono-body" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {o.answer || '—'}
          </div>
        </div>
      )}
      {o?.finding && (
        <div>
          <div className="mono-label">
            Finding {o.finding.schema_slug} <StatusBadge status={o.finding.status} />{' '}
            <Link to={`/approvals?finding=${o.finding.id}`}>{o.finding.id.slice(0, 8)}</Link>
          </div>
          <pre className="mono-body" style={{ overflowX: 'auto', margin: 0 }}>
            {JSON.stringify(o.finding.payload, null, 2)}
          </pre>
        </div>
      )}
      <HistoryList target={t} />
      <FindingLinks target={t} />
    </div>
  )
}

function TargetExtras({ target: t }: { target: BatchTarget }) {
  const o = isPromptOutcome(t.outcome) ? null : (t.outcome as DisclosureTargetOutcome | null)
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
      <HistoryList target={t} />
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
      <FindingLinks target={t} />
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
  const recordsQuery = useQuery<BatchRecords>({
    queryKey: ['batch-records', id],
    queryFn: () => api.batchRecords(id),
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
