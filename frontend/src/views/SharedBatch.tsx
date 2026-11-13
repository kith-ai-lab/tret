import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api, ApiError, type CopyFilesResult, type SharedBatch as SharedBatchT, type SharedBatchTarget } from '../api/client'
import { fmtScore } from '../components/batches/evalHelpers'
import { errText } from '../components/batches/shared'
import { WrittenFileViewer } from '../components/batches/WrittenFileViewer'
import { useConfirm } from '../components/shared/ConfirmDialog'
import { formatDateTime } from '../components/shared/format'
import { ROLE_RANK } from '../components/shared/Lessons'
import { QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

function sharer(p: { email: string; name?: string | null } | null): string {
  if (!p) return 'someone'
  return p.name ? `${p.name} (${p.email})` : p.email
}

function rowScore(t: SharedBatchTarget): number | null {
  const e = t.evaluation
  if (!e) return null
  const v = e.score ?? e.quality
  return typeof v === 'number' ? v : null
}

/** What the row's run reported: a disclosure row's summary, a prompt row's answer
 *  (cut short; the files hold the full output). */
function reported(t: SharedBatchTarget): string | null {
  const o = (t.outcome ?? null) as { summary?: unknown; answer?: unknown } | null
  const text = o && (typeof o.summary === 'string' ? o.summary : typeof o.answer === 'string' ? o.answer : null)
  if (!text) return null
  return text.length > 280 ? `${text.slice(0, 280)}…` : text
}

export function SharedBatch() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const askConfirm = useConfirm()
  const queryClient = useQueryClient()
  const [actionError, setActionError] = useState<string | null>(null)
  const [copyResult, setCopyResult] = useState<CopyFilesResult | null>(null)
  // The open file viewer: row id plus the file's name within that row.
  const [view, setView] = useState<{ targetId: string; filename: string } | null>(null)

  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const canApprove = (ROLE_RANK[meQuery.data?.role ?? 'analyst'] ?? 0) >= ROLE_RANK.approver

  const query = useQuery({
    queryKey: ['shared-batch', id],
    queryFn: () => api.getSharedBatch(id),
    retry: false,
  })
  const b: SharedBatchT | undefined = query.data

  const copyMutation = useMutation({
    mutationFn: () => api.copySharedBatchFiles(id),
    onMutate: () => {
      setActionError(null)
      setCopyResult(null)
    },
    onSuccess: (r) => {
      setCopyResult(r)
      // The copies are new documents of this workspace.
      void queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
    onError: (e) => setActionError(errText(e)),
  })
  const removeMutation = useMutation({
    mutationFn: () => api.removeSharedBatch(id),
    onMutate: () => setActionError(null),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: ['shared-batch', id] })
      void queryClient.invalidateQueries({ queryKey: ['shared-batches'] })
      navigate('/batches')
    },
    onError: (e) => setActionError(errText(e)),
  })

  if (query.isLoading) return <div className="empty pulse">Loading shared batch…</div>
  if (query.error instanceof ApiError && query.error.status === 404) {
    return (
      <div>
        <Link to="/batches" className="fine-print">
          ← Batches
        </Link>
        <div className="empty">
          This batch is not shared with this workspace (or the share was revoked).{' '}
          <Link to="/batches">Back to batches</Link>
        </div>
      </div>
    )
  }
  if (query.error || !b) {
    return (
      <div>
        <Link to="/batches" className="fine-print">
          ← Batches
        </Link>
        <QueryError error={query.error ?? new Error('not found')} what="this shared batch" />
      </div>
    )
  }

  const targets = b.targets ?? []
  const isDisclosure = b.profile === 'disclosure'
  const busy = copyMutation.isPending || removeMutation.isPending
  const fileUrl = (tid: string) => (filename: string, o: { download: boolean }) =>
    api.sharedBatchFileUrl(id, tid, filename, o.download ? { download: true } : undefined)
  const viewTarget = view ? targets.find((t) => t.id === view.targetId) : undefined
  const viewIndex = view && viewTarget ? viewTarget.files.findIndex((f) => f.filename === view.filename) : -1

  const copy = async () => {
    const ok = await askConfirm({
      title: 'Copy files to this workspace',
      message: `Add the ${b.file_count} ${b.file_count === 1 ? 'file' : 'files'} of "${b.name}" to this workspace's documents? Files already copied are skipped. The shared batch itself stays read-only.`,
      confirmLabel: 'Copy files',
    })
    if (ok) copyMutation.mutate()
  }
  const remove = async () => {
    const ok = await askConfirm({
      title: 'Remove from this workspace',
      message: `Remove "${b.name}" from this workspace? It disappears from your list. Files you already copied stay in your documents. ${b.share.from_workspace.name} can share it again.`,
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (ok) removeMutation.mutate()
  }

  const statusCounts = Object.entries(b.counts.by_status).filter(([, n]) => (n ?? 0) > 0)
  const colCount = isDisclosure ? 6 : 5

  return (
    <div>
      <Link to="/batches" className="fine-print">
        ← Batches
      </Link>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap', marginTop: 6 }}>
        <h1 className="view-title" style={{ marginBottom: 0 }}>
          {b.name}
        </h1>
        <StatusBadge status={b.status} />
        <span className="badge badge-gray">read-only</span>
      </div>
      <div className="view-sub" style={{ marginTop: 6 }}>
        Shared by {b.share.from_workspace.name} ({sharer(b.share.shared_by)}) on{' '}
        {formatDateTime(b.share.shared_at)}. You can read the rows, scores and files; you cannot
        change the batch.
      </div>

      <div className="row" style={{ gap: 24, flexWrap: 'wrap', margin: '10px 0 14px' }}>
        <div>
          <div className="mono-label">Rows</div>
          <div className="mono-body">{b.counts.total}</div>
        </div>
        {statusCounts.map(([k, n]) => (
          <div key={k}>
            <div className="mono-label">{k.replace(/_/g, ' ')}</div>
            <div className="mono-body">{n}</div>
          </div>
        ))}
        <div>
          <div className="mono-label">Mean score</div>
          <div className="mono-body">{fmtScore(b.quality_mean)}</div>
        </div>
        <div>
          <div className="mono-label">Files</div>
          <div className="mono-body">{b.file_count}</div>
        </div>
      </div>

      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 14, alignItems: 'center' }}>
        {b.file_count > 0 && (
          <a className="btn" href={api.sharedBatchArchiveUrl(id)} download>
            Download all files (.zip)
          </a>
        )}
        {canApprove && b.file_count > 0 && (
          <button className="btn" disabled={busy} onClick={() => void copy()}>
            {copyMutation.isPending ? 'Copying…' : 'Copy files to this workspace'}
          </button>
        )}
        {canApprove && (
          <button className="btn btn-reject" disabled={busy} onClick={() => void remove()}>
            Remove from this workspace
          </button>
        )}
        {!canApprove && (
          <span className="fine-print">An approver of this workspace can copy the files or remove the batch.</span>
        )}
      </div>
      {actionError && (
        <div className="error-text" role="alert" style={{ marginBottom: 10 }}>
          {actionError}
        </div>
      )}
      {copyResult && (
        <div className="callout" role="status" style={{ marginBottom: 14 }}>
          <div className="callout-title">Files copied</div>
          <div style={{ marginTop: 4 }}>
            {copyResult.copied} copied, {copyResult.skipped} already here, {copyResult.missing}{' '}
            missing. Find them under Documents, marked Shared.
          </div>
        </div>
      )}

      <div className="mono-label" style={{ marginBottom: 6 }}>
        Rows ({targets.length})
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            <tr>
              <th>#</th>
              <th>{isDisclosure ? 'Company' : 'Label'}</th>
              {isDisclosure && <th>Market / sector / fiscal year</th>}
              <th>Status</th>
              <th className="num">Score</th>
              <th>Files</th>
            </tr>
          </thead>
          <tbody>
            {targets.length === 0 && (
              <tr>
                <td colSpan={colCount} className="fine-print">
                  No rows.
                </td>
              </tr>
            )}
            {targets.map((t) => {
              const score = rowScore(t)
              const detail = [t.market, t.sector, t.fiscal_year != null ? String(t.fiscal_year) : null]
                .filter(Boolean)
                .join(' / ')
              return (
                <tr key={t.id}>
                  <td>{t.position}</td>
                  <td>
                    {isDisclosure ? (t.company ?? t.label) : t.label}
                    <div className="fine-print">{t.slug}</div>
                  </td>
                  {isDisclosure && <td>{detail || '—'}</td>}
                  <td>
                    <StatusBadge status={t.status} />
                    {reported(t) && (
                      <div className="fine-print" style={{ marginTop: 4, maxWidth: 320, overflowWrap: 'anywhere' }}>
                        {reported(t)}
                      </div>
                    )}
                  </td>
                  <td className="num">{score !== null ? fmtScore(score) : (t.evaluation?.status ?? '—')}</td>
                  <td>
                    {t.files.length === 0 ? (
                      '—'
                    ) : (
                      <ul style={{ margin: 0, padding: 0, listStyle: 'none' }} className="mono-body">
                        {t.files.map((f) => (
                          <li key={f.filename} style={{ overflowWrap: 'anywhere', marginBottom: 4 }}>
                            <button
                              type="button"
                              className="link-button"
                              style={{ textAlign: 'left' }}
                              title={f.filename}
                              onClick={() => setView({ targetId: t.id, filename: f.filename })}
                            >
                              {f.filename.split('/').pop()}
                            </button>{' '}
                            <span className="fine-print">{f.byte_size.toLocaleString('en-US')} B</span>{' '}
                            <a
                              className="fine-print"
                              href={api.sharedBatchFileUrl(id, t.id, f.filename, { download: true })}
                              download={f.filename.split('/').pop()}
                              aria-label={`Download ${f.filename}`}
                            >
                              Download
                            </a>
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {viewTarget && (
        <WrittenFileViewer
          batchId={id}
          targetId={viewTarget.id}
          files={viewTarget.files}
          index={viewIndex >= 0 ? viewIndex : null}
          onIndexChange={(i) => setView({ targetId: viewTarget.id, filename: viewTarget.files[i]?.filename ?? '' })}
          onClose={() => setView(null)}
          fileUrl={fileUrl(viewTarget.id)}
        />
      )}
    </div>
  )
}
