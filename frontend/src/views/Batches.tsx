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
  type CreateExperimentBody,
  type DisclosureTargetOutcome,
} from '../api/client'
import { AdvicePanel, CostCapField, RowMemoryField, parseCostCap, useBatchAdvice } from '../components/batches/AdvicePanel'
import { ObjectiveSelect, useExperimentForm } from '../components/batches/ExperimentSection'
import { DisclosurePanels } from '../components/batches/DisclosurePanels'
import { EvaluationDrawer } from '../components/batches/EvaluationDrawer'
import { useEvaluationForm } from '../components/batches/EvaluationSection'
import {
  deliveredElsewhereTitle,
  otherPrimaryTitle,
  flagLabel,
  fmtScore,
  fmtSigned,
  gateScoreSummary,
  suggestedSpotChecks,
} from '../components/batches/evalHelpers'
import { Scorecard } from '../components/batches/Scorecard'
import { PendingReviews } from '../components/batches/PendingReviews'
import { WrittenFileViewer } from '../components/batches/WrittenFileViewer'
import { usePromptBatchForm } from '../components/batches/PromptBatchForm'
import { slugify } from '../components/batches/csv'
import { errText, ExtLink, safeHref, shorten, WarningsBox } from '../components/batches/shared'
import { formatCost, formatDateTime } from '../components/shared/format'
import { useConfirm } from '../components/shared/ConfirmDialog'
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

/** "experiment · arm A" (or "· split") linking to the experiment page; nothing for an ordinary batch. */
export function ExperimentChip({ batch }: { batch: Batch }) {
  const ref = batch.experiment ?? batch.settings?.experiment
  if (!ref?.id) return null
  return (
    <Link
      to={`/batches/experiments/${ref.id}`}
      className="badge badge-blue"
      style={{ marginLeft: 8, fontSize: 'var(--fs-2xs)', padding: '1px 6px' }}
      onClick={(e) => e.stopPropagation()}
    >
      experiment · {ref.arm ? `arm ${ref.arm}` : 'split'}
    </Link>
  )
}

function isLive(c: Batch): boolean {
  if (c.status === 'running' || c.evaluation_status === 'running') return true
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
  const [selected, setSelected] = useState<string[]>([])
  const [showFamilies, setShowFamilies] = useState(false)
  // Lazy: only asked once the list is opened; an older server just shows the error text.
  const familiesQuery = useQuery({
    queryKey: ['batch-families'],
    queryFn: api.listBatchFamilies,
    enabled: showFamilies,
    retry: false,
  })
  const [showExperiments, setShowExperiments] = useState(false)
  // Lazy, and quiet on an older server: a 404/405 simply hides the list.
  const experimentsQuery = useQuery({
    queryKey: ['batch-experiments'],
    queryFn: api.listExperiments,
    enabled: showExperiments,
    retry: false,
  })
  const experimentsUnavailable =
    experimentsQuery.error instanceof ApiError &&
    (experimentsQuery.error.status === 404 || experimentsQuery.error.status === 405)
  const toggleSelected = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
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
        <div className="row" style={{ gap: 8 }}>
          <button
            className="btn"
            disabled={selected.length !== 2}
            title={selected.length === 2 ? undefined : 'Select exactly two batches to compare'}
            onClick={() => navigate(`/batches/compare?a=${selected[0]}&b=${selected[1]}`)}
          >
            Compare
          </button>
          <button
            className="btn"
            aria-expanded={showFamilies}
            onClick={() => setShowFamilies((v) => !v)}
          >
            Families
          </button>
          <button
            className="btn"
            aria-expanded={showExperiments}
            onClick={() => setShowExperiments((v) => !v)}
          >
            Experiments
          </button>
          <button className="btn btn-primary" onClick={() => setCreating(true)}>
            New batch
          </button>
        </div>
      </div>

      {showFamilies && (
        <div className="panel" style={{ margin: '14px 0' }}>
          <div className="mono-label">Families</div>
          <div className="fine-print">
            Batches of the same target list or prompt template, compared on one leaderboard.
          </div>
          {familiesQuery.isLoading ? (
            <div className="fine-print pulse">Loading…</div>
          ) : familiesQuery.error ? (
            <span className="error-text">{errText(familiesQuery.error)}</span>
          ) : (familiesQuery.data ?? []).length === 0 ? (
            <div className="fine-print">No families yet.</div>
          ) : (
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {(familiesQuery.data ?? []).map((f) => (
                <li key={f.family_key} className="mono-body">
                  <Link to={`/batches/family?key=${encodeURIComponent(f.family_key)}`}>
                    {f.family_label}
                  </Link>{' '}
                  <span className="fine-print">
                    {f.batches} {f.batches === 1 ? 'batch' : 'batches'} · {f.evaluated_batches} evaluated
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {showExperiments && !experimentsUnavailable && (
        <div className="panel" style={{ margin: '14px 0' }}>
          <div className="mono-label">Experiments</div>
          <div className="fine-print">
            Two or three ways of running the same work, side by side. A person starts them and picks a winner.
          </div>
          {experimentsQuery.isLoading ? (
            <div className="fine-print pulse">Loading…</div>
          ) : experimentsQuery.error ? (
            <span className="error-text">{errText(experimentsQuery.error)}</span>
          ) : (experimentsQuery.data ?? []).length === 0 ? (
            <div className="fine-print">No experiments yet. Use New batch, then Run as an experiment.</div>
          ) : (
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {(experimentsQuery.data ?? []).map((x) => (
                <li key={x.id} className="mono-body">
                  <Link to={`/batches/experiments/${x.id}`}>{x.name}</Link>{' '}
                  <span className="fine-print">
                    {x.mode} · {(x.arms ?? []).map((a) => `${a.key} ${a.batch_status ?? '—'}`).join(' · ')}
                    {x.promoted ? ` · promoted arm ${x.promoted.arm}` : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

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
                <th />
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
                <th className="num">Score</th>
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
                  <td onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${c.name} for comparison`}
                      checked={selected.includes(c.id)}
                      onChange={() => toggleSelected(c.id)}
                    />
                  </td>
                  <td>
                    {c.name}
                    <ExperimentChip batch={c} />
                  </td>
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
                  <td className="num">{fmtScore(c.quality_mean)}</td>
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
  const [objective, setObjective] = useState('')
  const [maxIterations, setMaxIterations] = useState('')
  const [costCap, setCostCap] = useState('')
  const [rowMemory, setRowMemory] = useState(false)
  const [csv, setCsv] = useState(`${CSV_HEADER}\n`)
  const [created, setCreated] = useState<Batch | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)

  const prompt = usePromptBatchForm(open && profile === 'prompt', { name, runSlug })
  const disclosureEval = useEvaluationForm(open && profile === 'disclosure', 'disclosure')

  const effectiveTarget = targetSlug || targets[0]?.slug || ''
  const csvRowCount = Math.max(0, csv.split('\n').filter((l) => l.trim() !== '').length - 1)
  // Same key as the disclosure AdvicePanel's request, so this reads its cache.
  const disclosureAdvice = useBatchAdvice(
    open && profile === 'disclosure',
    profile === 'disclosure' ? { profile: 'disclosure', fiscal_year: fiscalYear } : null,
  )
  const experimentHarnessId =
    profile === 'prompt' ? (prompt.body?.harness_id ?? null) : (disclosureAdvice.query.data?.harness_id ?? null)
  const experiment = useExperimentForm(
    open,
    profile === 'prompt' ? prompt.rowCount : csvRowCount,
    experimentHarnessId,
  )

  const experimentMutation = useMutation({
    mutationFn: (body: CreateExperimentBody) => api.createExperiment(body),
    onSuccess: (r) => {
      void queryClient.invalidateQueries({ queryKey: ['batches'] })
      void queryClient.invalidateQueries({ queryKey: ['batch-experiments'] })
      onClose()
      navigate(`/batches/experiments/${r.experiment.id}`)
    },
  })

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

  const submitBody = (body: CreateBatchBody) => {
    if (experiment.enabled) experimentMutation.mutate(experiment.buildBody(body))
    else createMutation.mutate(body)
  }

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
      ...(objective ? { objective } : {}),
      ...(parseCostCap(costCap) != null ? { max_cost_usd: parseCostCap(costCap) as number } : {}),
      ...(rowMemory ? { row_memory: true } : {}),
      ...(disclosureEval.value ? { evaluation: disclosureEval.value } : {}),
    }
    submitBody(body)
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
    !experimentMutation.isPending &&
    experiment.valid &&
    (profile === 'prompt'
      ? prompt.body !== null
      : name.trim() !== '' &&
        runSlug.trim() !== '' &&
        effectiveTarget !== '' &&
        parseCostCap(costCap) !== undefined &&
        disclosureEval.valid)
  const createError = createMutation.error ?? experimentMutation.error

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
                if (prompt.body) submitBody(prompt.body)
              } else {
                submitDisclosure()
              }
            }}
          >
            {createMutation.isPending || experimentMutation.isPending
              ? 'Creating…'
              : experiment.enabled
                ? 'Create experiment'
                : 'Create batch'}
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

          <AdvicePanel
            enabled={open && profile === 'disclosure'}
            body={{ profile: 'disclosure', fiscal_year: fiscalYear }}
            values={{ model, maxIterations, costCap, concurrency }}
            setters={{ setModel, setMaxIterations, setCostCap, setConcurrency }}
          />

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
            <CostCapField value={costCap} onChange={setCostCap} />
          </div>

          <div className="field">
            <label className="mono-label">Model override (optional)</label>
            <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
          </div>
          <div className="field">
            <label className="mono-label">Objective (optional)</label>
            <ObjectiveSelect value={objective} onChange={setObjective} />
            <span className="fine-print">
              What the router optimizes for on every run. Blank uses the harness setting.
            </span>
          </div>
          <RowMemoryField checked={rowMemory} onChange={setRowMemory} />
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

          {disclosureEval.fields}
        </>
      )}
      {experiment.fields}
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
    if (s.max_cost_usd) parts.push(`cost cap $${s.max_cost_usd.toFixed(2)} per target`)
    if (s.row_memory) parts.push('row memory on')
  } else {
    parts.push(`target ${s.target_slug}`)
    parts.push(`${s.write_root}/`)
    parts.push(`concurrency ${s.concurrency}`)
    parts.push(s.first_batch_size === 0 ? 'no gate' : `gate after ${s.first_batch_size}`)
    parts.push(`FY${s.fiscal_year}`)
    parts.push(`auto-write ${s.auto_write ? 'on' : 'off'}`)
    if (s.model_override) parts.push(`model ${s.model_override}`)
    if (s.max_iterations) parts.push(`max iterations ${s.max_iterations}`)
    if (s.max_cost_usd) parts.push(`cost cap $${s.max_cost_usd} per target`)
    if (s.row_memory) parts.push('row memory on')
  }
  parts.push(`cost ${formatCost(c.cost_usd)}`)
  return parts.join(' · ')
}

/** A finalize 409 caused by an infrastructure failure, which "Finalize anyway" can override. */
function isInfraConflict(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409 && e.message.includes('I/O or storage error')
}

function BatchDetail({ id }: { id: string }) {
  const askConfirm = useConfirm()
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
  const gate = c.status === 'gate' ? gateScoreSummary(targets) : null
  const canRelease = held > 0 && ['gate', 'review', 'completed', 'paused'].includes(c.status)

  const finalize = async (force: boolean) => {
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
    const ok = await askConfirm({
      message,
      title: force ? 'Finalize anyway' : 'Finalize batch',
      confirmLabel: force ? 'Finalize anyway' : 'Finalize',
      destructive: force,
    })
    if (ok) finalizeMutation.mutate(force)
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
        <ExperimentChip batch={c} />
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
      {(c.search_outage_targets ?? 0) > 0 && (
        <div className="callout callout-warn" style={{ marginBottom: 14 }}>
          <div className="callout-title">Search was unavailable</div>
          <div style={{ marginTop: 4 }}>
            {c.search_outage_targets} {c.search_outage_targets === 1 ? 'target' : 'targets'} could not
            search the web because the search backend did not answer. They are counted as
            infrastructure failures, not gaps. Retry them once search is working again.
          </div>
        </div>
      )}

      <Scorecard batchId={id} onChanged={refresh} />

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
        {c.status === 'gate' && gate && (
          <span className="fine-print">
            First {gate.n}: mean score {fmtScore(gate.mean)}, {gate.flagged} flagged
            {gate.flagNames.length > 0 &&
              ` (${gate.flagNames.map((f) => flagLabel(null, f)).join(', ')})`}
          </span>
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
  const [drawerTarget, setDrawerTarget] = useState<string | null>(null)
  const anyEvaluation = targets.some((t) => t.evaluation)
  const anyArm = targets.some((t) => t.arm)
  const armHead = anyArm ? <th>Arm</th> : null
  const armCell = (t: BatchTarget) => (anyArm ? <td>{t.arm ?? '—'}</td> : null)
  const evalQuery = useQuery({
    queryKey: ['batch-evaluation', batch.id],
    queryFn: () => api.getBatchEvaluation(batch.id),
    enabled: anyEvaluation,
    retry: false,
  })
  const rubric = evalQuery.data?.rubric ?? null
  const suggested = suggestedSpotChecks(targets)

  const evalHeads = (
    <>
      <th className="num">Score</th>
      <th className="num">Rank</th>
      <th className="num">Resid.</th>
      <th>Flags</th>
    </>
  )
  const evalCells = (t: BatchTarget) => {
    const e = t.evaluation
    if (!e) {
      return (
        <>
          <td className="num">—</td>
          <td className="num">—</td>
          <td className="num">—</td>
          <td />
        </>
      )
    }
    const q = e.quality
    const hasScore = typeof q === 'number'
    return (
      <>
        <td className="num">
          {hasScore || e.status !== 'pending' ? (
            <button
              type="button"
              className="link-button"
              aria-label={`Open evaluation for ${t.label}`}
              onClick={() => setDrawerTarget(t.id)}
            >
              <span className="score-cell">
                {hasScore ? (
                  <>
                    {fmtScore(q)}
                    <span className="score-bar" aria-hidden="true">
                      <span style={{ width: `${Math.max(0, Math.min(100, q))}%` }} />
                    </span>
                  </>
                ) : (
                  e.status.replace(/_/g, ' ')
                )}
              </span>
            </button>
          ) : (
            '—'
          )}
          {e.stale && <span className="chip" style={{ marginLeft: 4 }}>stale</span>}
          {suggested.has(t.id) && <span className="chip chip-check" style={{ marginLeft: 4 }}>check</span>}
          {(e.delivered_elsewhere ?? 0) > 0 && (
            <span
              className="chip"
              style={{ marginLeft: 4 }}
              title={deliveredElsewhereTitle(e.delivered_elsewhere!, e.gap_rechecked)}
            >
              delivered elsewhere
            </span>
          )}
          {(e.primary_elsewhere ?? 0) > 0 && (
            <span
              className="chip"
              style={{ marginLeft: 4 }}
              title={otherPrimaryTitle(e.primary_elsewhere!, e.primary_rechecked)}
            >
              other main document
            </span>
          )}
        </td>
        <td className="num">{e.rank != null && e.of != null ? `${e.rank}/${e.of}` : '—'}</td>
        <td className={`num ${(e.residual ?? 0) < 0 ? 'delta-neg' : (e.residual ?? 0) > 0 ? 'delta-pos' : ''}`}>
          {fmtSigned(e.residual)}
        </td>
        <td>
          <div className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
            {(e.flags ?? []).map((f) => (
              <span key={f} className="chip chip-flag">
                {flagLabel(rubric, f)}
              </span>
            ))}
          </div>
        </td>
      </>
    )
  }

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
                {armHead}
                <th className="num">Tries</th>
                <th>Packages</th>
                <th>Outcome</th>
                {evalHeads}
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
                {armHead}
                <th className="num">Tries</th>
                <th>Outcome</th>
                {evalHeads}
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
                      {armCell(t)}
                      <td className="num">{t.attempts}</td>
                      <td style={{ maxWidth: 320, overflowWrap: 'anywhere' }}>
                        {o ? shorten(o.answer, 120) || '—' : '—'}
                      </td>
                      {evalCells(t)}
                      <td className="num">{formatCost(t.cost_usd)}</td>
                      <td>
                        {t.model_used ?? '—'}
                        {t.explored && <ExplorationChip />}
                      </td>
                      {runCell(t)}
                      <td className="error-text" style={{ maxWidth: 220 }}>
                        {t.error ?? ''}
                      </td>
                      <td>{retryButtons(t)}</td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td />
                        <td colSpan={14}>
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
                    {armCell(t)}
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
                    {evalCells(t)}
                    <td className="num">{formatCost(t.cost_usd)}</td>
                    <td>
                      {t.model_used ?? '—'}
                      {t.explored && <ExplorationChip />}
                    </td>
                    {runCell(t)}
                    <td className="error-text" style={{ maxWidth: 220 }}>
                      {t.error ?? ''}
                    </td>
                    <td>{retryButtons(t)}</td>
                  </tr>
                  {expanded && (
                    <tr>
                      <td />
                      <td colSpan={17}>
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
      <EvaluationDrawer
        batchId={batch.id}
        targetId={drawerTarget}
        open={drawerTarget !== null}
        onClose={() => setDrawerTarget(null)}
        suggested={drawerTarget !== null && suggested.has(drawerTarget)}
        deliveredElsewhere={
          targets.find((t) => t.id === drawerTarget)?.evaluation?.delivered_elsewhere ?? 0
        }
        gapRechecked={targets.find((t) => t.id === drawerTarget)?.evaluation?.gap_rechecked}
        primaryElsewhere={
          targets.find((t) => t.id === drawerTarget)?.evaluation?.primary_elsewhere ?? 0
        }
        primaryRechecked={targets.find((t) => t.id === drawerTarget)?.evaluation?.primary_rechecked}
      />
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
  const [viewFile, setViewFile] = useState<string | null>(null)
  const viewIndex = viewFile === null ? -1 : t.written_files.findIndex((w) => w.filename === viewFile)
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
                <button type="button" className="link-button" onClick={() => setViewFile(f.filename)}>
                  {f.filename}
                </button>{' '}
                <span className="fine-print">{f.byte_size.toLocaleString('en-US')} B</span>
                {f.web_url && (
                  <>
                    {' '}
                    <span className="fine-print">
                      <ExtLink url={f.web_url}>SharePoint ↗</ExtLink>
                    </span>
                  </>
                )}
              </li>
            ))}
          </ul>
          <WrittenFileViewer
            batchId={t.collection_id}
            targetId={t.id}
            files={t.written_files}
            index={viewIndex >= 0 ? viewIndex : null}
            onIndexChange={(i) => setViewFile(t.written_files[i]?.filename ?? null)}
            onClose={() => setViewFile(null)}
          />
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

function ExplorationChip() {
  return (
    <span
      className="chip chip-check"
      style={{ marginLeft: 6 }}
      title="An exploration run: the router tried a model with little record on this agent."
    >
      exploration
    </span>
  )
}
