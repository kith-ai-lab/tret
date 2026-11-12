import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import {
  api,
  ApiError,
  type ConnectedWriteDocumentSource,
  type ConnectedWritePayload,
  type Finding,
  type FindingDetail,
  type PackageMapPayload,
  type PackageResult,
} from '../api/client'
import { VerdictCard } from '../components/findings/VerdictCard'
import { ApproveAllSections } from '../components/shared/ApproveAllSections'
import { formatDateTime } from '../components/shared/format'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { ProvenanceCard } from '../components/shared/ProvenanceCard'
import { StatusBadge } from '../components/shared/StatusBadge'

// `noted` holds findings with nothing to decide (an insufficient_data verdict —
// the data request filed beside it is the actionable item). `superseded` is
// read-only history (a newer draft replaced it) and sits last so deep links to
// one — `?finding=<id>`, "Superseded by" chains — always resolve to a tab.
const TABS = ['draft', 'approved', 'rejected', 'noted', 'superseded'] as const
const TAB_LABEL: Record<(typeof TABS)[number], string> = {
  draft: 'draft',
  approved: 'approved',
  rejected: 'rejected',
  noted: 'Noted (no decision needed)',
  superseded: 'Superseded',
}

export function Approvals() {
  const [tab, setTab] = useState<(typeof TABS)[number]>('draft')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // `?finding=<id>` deep-links one finding (run page, "Superseded by" links):
  // open the tab its status lives in and select it, once.
  const [params] = useSearchParams()
  const focusId = params.get('finding')
  const focusApplied = useRef<string | null>(null)
  const focusQuery = useQuery({
    queryKey: ['finding', focusId],
    queryFn: () => api.getFinding(focusId as string),
    enabled: focusId !== null,
  })
  const focusStatus = focusQuery.data?.status
  useEffect(() => {
    if (focusId === null || focusApplied.current === focusId || !focusStatus) return
    const target = TABS.find((t) => t === focusStatus)
    if (target) setTab(target)
  }, [focusId, focusStatus])

  const findingsQuery = useQuery({
    queryKey: ['findings', tab],
    queryFn: () => api.listFindings({ status: tab }),
  })

  const findings = findingsQuery.data ?? []

  // Keep a valid selection as the list changes.
  useEffect(() => {
    if (findings.length === 0) {
      setSelectedId(null)
    } else if (
      focusId !== null &&
      focusApplied.current !== focusId &&
      findings.some((f) => f.id === focusId)
    ) {
      focusApplied.current = focusId
      setSelectedId(focusId)
    } else if (!findings.some((f) => f.id === selectedId)) {
      setSelectedId(findings[0].id)
    }
  }, [findings, selectedId, focusId])

  return (
    <div>
      <h1 className="view-title">Approvals</h1>
      <div className="view-sub">
        The blessing queue: drafts stay drafts until a named human decides.
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <button key={t} className={`tab${tab === t ? ' active' : ''}`} onClick={() => setTab(t)}>
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>

      {findingsQuery.isLoading ? (
        <div className="empty pulse">Loading findings…</div>
      ) : findings.length === 0 ? (
        <div className="empty">
          {tab === 'draft'
            ? 'No draft findings awaiting review — run a task from the Workbench.'
            : tab === 'noted'
              ? 'No noted findings — insufficient_data verdicts land here, with a data request filed.'
              : `No ${tab} findings yet.`}
        </div>
      ) : (
        <ListDetail
          listWidth={260}
          list={findings.map((f) => (
            <ListItem
              key={f.id}
              active={f.id === selectedId}
              onClick={() => setSelectedId(f.id)}
              title={listTitle(f)}
              sub={`${subjectLine(f.subject)} · ${f.created_at ? formatDateTime(f.created_at) : ''}`}
            />
          ))}
          detail={selectedId ? <FindingDetailPane findingId={selectedId} /> : null}
        />
      )}
    </div>
  )
}

/** The list row's title: the schema slug, except a package_map, which reads
 *  better as "Package map · <document title>". */
function listTitle(f: Finding): string {
  if (f.schema_slug === 'package_map' && typeof f.subject.title === 'string' && f.subject.title) {
    return `Package map · ${f.subject.title}`
  }
  return f.schema_slug
}

function subjectLine(subject: Record<string, unknown>): string {
  const parts = Object.entries(subject).map(([k, v]) => `${k}=${String(v)}`)
  return parts.join(' ') || '(no subject)'
}

function FindingDetailPane({ findingId }: { findingId: string }) {
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')

  const findingQuery = useQuery({
    queryKey: ['finding', findingId],
    queryFn: () => api.getFinding(findingId),
  })

  const decideMutation = useMutation({
    mutationFn: (action: 'approve' | 'reject') => api.decideFinding(findingId, action, note || undefined),
    onSuccess: () => {
      setNote('')
      queryClient.invalidateQueries({ queryKey: ['findings'] })
      queryClient.invalidateQueries({ queryKey: ['finding', findingId] })
    },
  })

  // The run carries the energy boundary the provenance tooltip needs. Until it
  // resolves (or if it fails) the card gets undefined and stays neutral.
  const runId = findingQuery.data?.run_id
  const runQuery = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api.getRun(runId as string),
    enabled: !!runId,
    staleTime: 5 * 60_000,
  })

  if (findingQuery.isLoading) return <div className="empty pulse">Loading finding…</div>
  if (findingQuery.isError)
    return <div className="error-text">{(findingQuery.error as Error).message}</div>
  const f = findingQuery.data
  if (!f) return null

  const retrieved = f.provenance.retrieved_values ?? []
  const decideError = decideMutation.error as ApiError | null

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: 'wrap' }}>
        <h2 className="view-title" style={{ marginBottom: 0 }}>
          {f.schema_slug}
        </h2>
        <StatusBadge status={f.status} />
        <span className="chip">{subjectLine(f.subject)}</span>
        <span style={{ flex: 1 }} />
        <Link to={`/runs/${f.run_id}`} className="mono-body">
          view run →
        </Link>
      </div>

      {f.schema_slug === 'connected_write' ? (
        <ConnectedWriteBlock finding={f} />
      ) : f.schema_slug === 'package_map' ? (
        <PackageMapBlock finding={f} />
      ) : (
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            {f.schema_slug === 'divergence_verdict' ? 'Verdict' : 'Payload'}
          </div>
          <VerdictCard finding={f} />
        </div>
      )}

      <ProvenanceCard
        model={f.provenance.model}
        doctrineSha={f.provenance.doctrine_sha}
        documentCount={(f.provenance.document_ids ?? []).length}
        retrievedCount={retrieved.length}
        energyBoundary={runQuery.data?.energy?.energy_boundary}
      />

      {retrieved.length > 0 && (
        <details>
          <summary className="mono-label" style={{ cursor: 'pointer' }}>
            All retrieved values ({retrieved.length})
          </summary>
          <div style={{ maxHeight: 240, overflowY: 'auto', marginTop: 6 }}>
            <table className="mono-table">
              <thead>
                <tr>
                  <th>Dataset</th>
                  <th>Row</th>
                  <th>Column</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {retrieved.map((v, i) => (
                  <tr key={i}>
                    <td>{v.dataset}</td>
                    <td>{v.row_ref}</td>
                    <td>{v.column}</td>
                    <td>{v.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {/* Approval history */}
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Approval history
        </div>
        {f.approvals.length === 0 ? (
          <div className="empty" style={{ padding: '4px 0' }}>
            No decisions yet.
          </div>
        ) : (
          <table className="mono-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Action</th>
                <th>Approver</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {f.approvals.map((a, i) => (
                <tr key={i}>
                  <td>{a.created_at ? formatDateTime(a.created_at) : '—'}</td>
                  <td>
                    <StatusBadge status={a.action === 'approve' ? 'approved' : 'rejected'} />
                    {a.purpose === 'packaging' && (
                      <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 6 }}>
                        packaging only
                      </span>
                    )}
                  </td>
                  <td title={a.approver_id}>{a.approver_id.slice(0, 8)}</td>
                  <td>{a.note ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Decision controls */}
      {f.status === 'draft' && (
        <div className="panel">
          <div className="mono-label" style={{ marginBottom: 8 }}>
            Decide
          </div>
          <div className="field">
            <textarea
              rows={2}
              placeholder="Optional note for the audit record…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>
          {decideError && (
            <div className="error-text" style={{ marginBottom: 10 }}>
              {decideError.status === 403 ? 'Requires approver role.' : decideError.message}
            </div>
          )}
          <div className="row">
            <button
              className="btn btn-approve"
              onClick={() => decideMutation.mutate('approve')}
              disabled={decideMutation.isPending}
            >
              Approve
            </button>
            <button
              className="btn btn-reject"
              onClick={() => decideMutation.mutate('reject')}
              disabled={decideMutation.isPending}
            >
              Reject
            </button>
          </div>
        </div>
      )}
      {/* Outside the draft-only panel, so the result line survives this
          finding flipping to approved when the batch finishes. */}
      {f.schema_slug === 'draft_section' && typeof f.subject.deliverable === 'string' && (
        <ApproveAllSections deliverableSlug={f.subject.deliverable} minDrafts={2} />
      )}
    </div>
  )
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const PREVIEW_CHAR_LIMIT = 4000

const PROVENANCE_LABELS: Record<string, string> = {
  web_capture: 'web capture',
  web: 'web fetch',
}

/** The `document` write source: the stored file uploaded exactly as it is, so
 *  what an approver needs is identity, not a text preview — original name,
 *  type, size, the SHA-256 the upload is pinned to, and where the bytes came
 *  from. The hash is shown short; clicking copies the full value. */
function DocumentSourceDetails({
  source,
  destinationPath,
}: {
  source: ConnectedWriteDocumentSource
  destinationPath: string
}) {
  const [copied, setCopied] = useState(false)
  const copyHash = () => {
    navigator.clipboard
      ?.writeText(source.sha256)
      .then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => undefined)
  }
  const provenance = source.provenance
    ? (PROVENANCE_LABELS[source.provenance] ?? source.provenance)
    : null
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="mono-body">Stored document uploaded as-is (the bytes approved are the bytes written)</div>
      <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
        <div>
          <div className="mono-label">Original file</div>
          <div className="mono-body">{source.filename}</div>
        </div>
        <div>
          <div className="mono-label">Type</div>
          <div className="mono-body">{source.content_type}</div>
        </div>
        <div>
          <div className="mono-label">Size</div>
          <div className="mono-body">{formatBytes(source.byte_size)}</div>
        </div>
        <div>
          <div className="mono-label">SHA-256</div>
          <button
            type="button"
            className="mono-body"
            title={`${source.sha256} — click to copy`}
            onClick={copyHash}
            style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer', color: 'inherit' }}
          >
            {source.sha256.slice(0, 12)}…{copied ? ' copied' : ''}
          </button>
        </div>
        {provenance && (
          <div>
            <div className="mono-label">Provenance</div>
            <div className="mono-body">{provenance}</div>
          </div>
        )}
      </div>
      <div>
        <div className="mono-label">Path under tret/</div>
        <div className="mono-body">{destinationPath}</div>
      </div>
    </div>
  )
}

/** Dedicated payload rendering for `schema_slug: "connected_write"` findings
 *  — destination, content type/size, a source preview (inline content or a
 *  deliverable reference), and — once a decision has been made — the upload
 *  result, with a retry affordance on failure. The raw payload stays
 *  available behind a toggle for anyone who wants the JSON tret sent. */
function ConnectedWriteBlock({ finding }: { finding: FindingDetail }) {
  const queryClient = useQueryClient()
  const [showRaw, setShowRaw] = useState(false)
  const payload = finding.payload as unknown as ConnectedWritePayload

  const retryMutation = useMutation({
    mutationFn: () => api.retryFindingUpload(finding.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['finding', finding.id] })
      queryClient.invalidateQueries({ queryKey: ['findings'] })
    },
  })
  const retryError = retryMutation.error as ApiError | null

  const inline = payload.source.kind === 'inline' ? payload.source.content : null
  const truncated = inline !== null && inline.length > PREVIEW_CHAR_LIMIT
  const preview = inline === null ? null : truncated ? `${inline.slice(0, PREVIEW_CHAR_LIMIT)}…` : inline

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <div className="mono-label" style={{ marginBottom: 4 }}>
          Destination
        </div>
        <div className="mono-body">
          {payload.target_label} / tret / {payload.filename}
        </div>
        <div
          className="fine-print"
          style={{ fontFamily: 'var(--mono)', fontSize: 'var(--fs-xs)', marginTop: 2 }}
        >
          {payload.target_path}/tret/{payload.filename}
        </div>
      </div>

      {/* A document source shows its own type/size in the Source block. */}
      {payload.source.kind !== 'document' && (
        <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
          <div>
            <div className="mono-label">Content type</div>
            <div className="mono-body">{payload.content_type}</div>
          </div>
          {/* null for a deliverable-sourced proposal — rendered fresh at
              approval, so there is nothing fixed to size yet. Omit the row
              rather than print "null B". */}
          {payload.size !== null && (
            <div>
              <div className="mono-label">Size</div>
              <div className="mono-body">{formatBytes(payload.size)}</div>
            </div>
          )}
        </div>
      )}

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Source
        </div>
        {payload.source.kind === 'deliverable' ? (
          <div className="mono-body">
            Deliverable {payload.source.slug} exported as {payload.source.format}
          </div>
        ) : payload.source.kind === 'document' ? (
          <DocumentSourceDetails source={payload.source} destinationPath={payload.filename} />
        ) : (
          <>
            <pre className="code-block" style={{ maxHeight: 240 }}>
              {preview}
            </pre>
            {truncated && (
              <div className="fine-print" style={{ marginTop: 4 }}>
                Showing the first {PREVIEW_CHAR_LIMIT.toLocaleString('en-US')} characters.
              </div>
            )}
          </>
        )}
      </div>

      {payload.upload && (
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Upload
          </div>
          {payload.upload.status === 'uploaded' ? (
            <div className="mono-body" style={{ color: 'var(--green)' }}>
              Written to SharePoint
              {payload.upload.web_url && (
                <>
                  {' — '}
                  <a href={payload.upload.web_url} target="_blank" rel="noopener noreferrer">
                    open in SharePoint
                  </a>
                </>
              )}
            </div>
          ) : (
            <div className="stack" style={{ gap: 8 }}>
              <div className="error-text">{payload.upload.error ?? 'Upload failed.'}</div>
              <div>
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={retryMutation.isPending}
                  onClick={() => retryMutation.mutate()}
                >
                  {retryMutation.isPending ? 'Retrying…' : 'Retry upload'}
                </button>
              </div>
              {retryError && (
                <div className="error-text">
                  {retryError.status === 403 ? 'Requires approver role.' : retryError.message}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <div>
        <button type="button" className="btn btn-sm" onClick={() => setShowRaw((s) => !s)}>
          {showRaw ? 'Hide raw' : 'Raw'}
        </button>
        {showRaw && (
          <pre className="code-block" style={{ maxHeight: 320, marginTop: 8 }}>
            {JSON.stringify(finding.payload, null, 2)}
          </pre>
        )}
      </div>
    </div>
  )
}

/** A SHA-256 shown short; clicking copies the full value. */
function CopyHash({ sha256 }: { sha256: string }) {
  const [copied, setCopied] = useState(false)
  const copyHash = () => {
    navigator.clipboard
      ?.writeText(sha256)
      .then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => undefined)
  }
  return (
    <button
      type="button"
      className="mono-body"
      title={`${sha256} — click to copy`}
      onClick={copyHash}
      style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer', color: 'inherit' }}
    >
      {sha256.slice(0, 12)}…{copied ? ' copied' : ''}
    </button>
  )
}

/** "28–143" for a range, "5" for a one-page range (inclusive, 1-based). */
function rangeText([first, last]: [number, number]): string {
  return first === last ? `${first}` : `${first}–${last}`
}

function pageCount(n: number): string {
  return `${n} ${n === 1 ? 'page' : 'pages'}`
}

/** The page selection as one human sentence, e.g. "Pages 28–143, 314–315 ·
 *  118 of 339 physical pages" or "Whole original · 66 pages". */
function selectionText(payload: PackageMapPayload): string {
  const total = payload.source.page_count
  if (payload.full_original || !payload.map.keep_ranges) {
    const n = total ?? payload.selected_page_count
    return `Whole original · ${pageCount(n)}`
  }
  const ranges = payload.map.keep_ranges
  const label = ranges.length === 1 && ranges[0][0] === ranges[0][1] ? 'Page' : 'Pages'
  const kept =
    total !== null && total !== undefined
      ? `${payload.selected_page_count} of ${total} physical pages`
      : pageCount(payload.selected_page_count)
  return `${label} ${ranges.map(rangeText).join(', ')} · ${kept}`
}

// Packaging runs in a background task on the server (approval returns at once),
// so the Approvals page polls the finding while it says "running". The server's
// own deadline is 600 s and it lets a retry take over a "running" record once
// that plus a 60 s grace has passed (PACKAGING_TIMEOUT_SECONDS +
// PACKAGING_STALE_GRACE_SECONDS in api/findings.py); past that the worker is
// presumed dead and the page offers the retry.
const PACKAGING_POLL_MS = 5000
const PACKAGING_STALE_MS = (600 + 60) * 1000

function packagingLooksDead(result: PackageResult): boolean {
  if (result.status !== 'running') return false
  const startedAt = result.started_at ? Date.parse(result.started_at) : NaN
  // No usable start time: the server treats that as stale too.
  return Number.isNaN(startedAt) || Date.now() - startedAt > PACKAGING_STALE_MS
}

/** What the packaging step produced, shown once a package_map is approved:
 *  status, retained pages, how text was extracted, the documents written, and
 *  — on failure — the error with a retry. While packaging is queued or running
 *  it polls the finding every 5 s; a run older than the server's deadline plus
 *  grace is offered a retry. */
function PackageResultBlock({
  findingId,
  result,
}: {
  findingId: string
  /** null: the finding is approved but no packaging outcome was ever recorded. */
  result: PackageResult | null
}) {
  const queryClient = useQueryClient()
  const running = result?.status === 'running'
  // Same key as the detail pane's own query, so each poll refreshes the pane's
  // data (and this component's `result` prop) rather than a second copy. React
  // Query stops the interval when this component unmounts or `running` flips.
  useQuery({
    queryKey: ['finding', findingId],
    queryFn: () => api.getFinding(findingId),
    refetchInterval: running ? PACKAGING_POLL_MS : false,
    enabled: running,
  })
  // The list's badges and counts should catch up once the job leaves "running".
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !running) {
      queryClient.invalidateQueries({ queryKey: ['findings'] })
    }
    wasRunning.current = running
  }, [running, queryClient])
  const retryMutation = useMutation({
    mutationFn: () => api.retryFindingPackage(findingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['finding', findingId] })
      queryClient.invalidateQueries({ queryKey: ['findings'] })
    },
  })
  const retryError = retryMutation.error as ApiError | null

  if (result === null) {
    return (
      <div className="stack" style={{ gap: 8 }}>
        <div className="error-text">Packaging not recorded</div>
        <div className="fine-print">
          This map was approved, but no packaging result was saved (the server may have restarted).
        </div>
        <div>
          <button
            type="button"
            className="btn btn-sm"
            disabled={retryMutation.isPending}
            onClick={() => retryMutation.mutate()}
          >
            {retryMutation.isPending ? 'Retrying…' : 'Retry packaging'}
          </button>
        </div>
        {retryError && (
          <div className="error-text">
            {retryError.status === 403 ? 'Requires approver role.' : retryError.message}
          </div>
        )}
      </div>
    )
  }

  if (result.status === 'running') {
    // A queued job is alive, just waiting for a slot; only a started one can look dead.
    const dead = !result.queued && packagingLooksDead(result)
    return (
      <div className="stack" style={{ gap: 8 }}>
        <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
          <StatusBadge status="running" />
          <span className="fine-print">
            {result.queued ? 'Queued…' : 'Packaging…'}
            {result.started_at && !result.queued && ` · started ${formatDateTime(result.started_at)}`}
          </span>
        </div>
        {result.queued && (
          <div className="fine-print">
            Waiting for a free packaging slot (one or two run at a time, depending on the machine). This page updates on its own.
          </div>
        )}
        {dead && (
          <>
            <div className="fine-print">
              Packaging started more than eleven minutes ago and has not finished; the previous worker
              appears to have died (for example, the server restarted).
            </div>
            <div>
              <button
                type="button"
                className="btn btn-sm"
                disabled={retryMutation.isPending}
                onClick={() => retryMutation.mutate()}
              >
                {retryMutation.isPending ? 'Retrying…' : 'Retry packaging'}
              </button>
            </div>
            {retryError && (
              <div className="error-text">
                {retryError.status === 403 ? 'Requires approver role.' : retryError.message}
              </div>
            )}
          </>
        )}
      </div>
    )
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
        <StatusBadge status={result.status} />
        {result.packaged_at && (
          <span className="fine-print">{formatDateTime(result.packaged_at)}</span>
        )}
      </div>

      {result.status === 'failed' && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="error-text">{result.error ?? 'Packaging failed.'}</div>
          <div>
            <button
              type="button"
              className="btn btn-sm"
              disabled={retryMutation.isPending}
              onClick={() => retryMutation.mutate()}
            >
              {retryMutation.isPending ? 'Retrying…' : 'Retry packaging'}
            </button>
          </div>
          {retryError && (
            <div className="error-text">
              {retryError.status === 403 ? 'Requires approver role.' : retryError.message}
            </div>
          )}
        </div>
      )}

      <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
        {result.retained_page_count !== null && (
          <div>
            <div className="mono-label">Retained pages</div>
            <div className="mono-body">
              {result.retained_page_count}
              {result.source_page_count !== null && ` of ${result.source_page_count}`}
            </div>
          </div>
        )}
        {result.text_mode && (
          <div>
            <div className="mono-label">Text extraction</div>
            <div className="mono-body">
              {result.text_mode}
              {result.text_mode === 'pdftotext' && result.poppler_version
                ? ` (Poppler ${result.poppler_version})`
                : ''}
            </div>
          </div>
        )}
        {result.wall_seconds !== null && (
          <div>
            <div className="mono-label">Wall time</div>
            <div className="mono-body">{result.wall_seconds.toFixed(1)} s</div>
          </div>
        )}
        {result.parser_warning_count !== null && (
          <div>
            <div className="mono-label">Parser warnings</div>
            <div className="mono-body">{result.parser_warning_count}</div>
          </div>
        )}
        {result.retained_sha256 && (
          <div>
            <div className="mono-label">Retained SHA-256</div>
            <CopyHash sha256={result.retained_sha256} />
          </div>
        )}
      </div>

      {result.documents.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            Produced documents
          </div>
          <table className="mono-table">
            <thead>
              <tr>
                <th>Role</th>
                <th>File</th>
                <th>SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {result.documents.map((d) => (
                <tr key={d.document_id}>
                  <td>{d.role}</td>
                  <td title={d.document_id}>{d.filename}</td>
                  <td>
                    <CopyHash sha256={d.sha256} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result.limitations.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            Limitations
          </div>
          <ul className="fine-print" style={{ margin: 0, paddingLeft: 18 }}>
            {result.limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** Dedicated payload rendering for `schema_slug: "package_map"` findings: the
 *  proposed page selection over one captured source PDF — what is kept, what
 *  was pulled in from cited components, what was dropped and why — for a human
 *  to approve, then the packaging result after approval. The raw payload stays
 *  behind a toggle. */
function PackageMapBlock({ finding }: { finding: FindingDetail }) {
  const [showRaw, setShowRaw] = useState(false)
  const payload = finding.payload as unknown as PackageMapPayload
  const { source, map } = payload
  const incorporations = map.incorporations ?? []
  const exclusions = map.exclusions ?? []
  const provenance = source.provenance
    ? (PROVENANCE_LABELS[source.provenance] ?? source.provenance)
    : null

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <div className="mono-label" style={{ marginBottom: 4 }}>
          Proposed package
        </div>
        <div className="mono-body">{map.title}</div>
        <div className="row" style={{ gap: 24, flexWrap: 'wrap', marginTop: 6 }}>
          <div>
            <div className="mono-label">Role</div>
            <div className="mono-body">{map.role}</div>
          </div>
          <div>
            <div className="mono-label">Scope</div>
            <div className="mono-body">{map.scope}</div>
          </div>
          <div>
            <div className="mono-label">Selection</div>
            <div className="mono-body">{selectionText(payload)}</div>
          </div>
        </div>
      </div>

      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Source document
        </div>
        <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
          <div>
            <div className="mono-label">Original file</div>
            <div className="mono-body">{source.filename}</div>
          </div>
          {source.page_count !== null && (
            <div>
              <div className="mono-label">Pages</div>
              <div className="mono-body">{source.page_count}</div>
            </div>
          )}
          <div>
            <div className="mono-label">SHA-256</div>
            <CopyHash sha256={source.sha256} />
          </div>
          {provenance && (
            <div>
              <div className="mono-label">Provenance</div>
              <div className="mono-body">{provenance}</div>
            </div>
          )}
        </div>
        {source.url && (
          <div className="fine-print" style={{ marginTop: 4, overflowWrap: 'anywhere' }}>
            {/^https?:\/\//i.test(source.url) ? (
              <a href={source.url} target="_blank" rel="noopener noreferrer">
                {source.url}
              </a>
            ) : (
              source.url
            )}
          </div>
        )}
      </div>

      {incorporations.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            Incorporated by reference ({incorporations.length})
          </div>
          <table className="mono-table">
            <thead>
              <tr>
                <th>Cited on page</th>
                <th>Component</th>
                <th>Resolved pages</th>
                <th>Status</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {incorporations.map((inc, i) => (
                <tr key={i}>
                  <td>{inc.originating_page}</td>
                  <td>{inc.cited_component}</td>
                  <td>{inc.resolved_range ? rangeText(inc.resolved_range) : '—'}</td>
                  <td>{inc.status}</td>
                  <td>{inc.note ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {exclusions.length > 0 && (
        <div>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            Excluded ({exclusions.length})
          </div>
          <ul className="mono-body" style={{ margin: 0, paddingLeft: 18 }}>
            {exclusions.map((x, i) => (
              <li key={i}>
                Pages {rangeText(x.range)} — {x.reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      {map.review_note && (
        <div>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            Review note
          </div>
          <div className="mono-body" style={{ whiteSpace: 'pre-wrap' }}>
            {map.review_note}
          </div>
        </div>
      )}

      {(payload.package || finding.status === 'approved') && (
        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Package result
          </div>
          <PackageResultBlock findingId={finding.id} result={payload.package} />
        </div>
      )}

      <div>
        <button type="button" className="btn btn-sm" onClick={() => setShowRaw((s) => !s)}>
          {showRaw ? 'Hide raw' : 'Raw'}
        </button>
        {showRaw && (
          <pre className="code-block" style={{ maxHeight: 320, marginTop: 8 }}>
            {JSON.stringify(finding.payload, null, 2)}
          </pre>
        )}
      </div>
    </div>
  )
}
