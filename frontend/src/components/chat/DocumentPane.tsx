/** The chat's right-hand document pane: one finding from the conversation,
 *  readable next to the thread, with the decision and the downloads that apply
 *  to it. Not a modal — the thread stays usable. Esc closes it. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  api,
  type ApiError,
  type ConnectedWritePayload,
  type FindingDetail,
} from '../../api/client'
import { useCanOpen } from '../../edition'
import { VerdictCard, findingHref } from '../findings/VerdictCard'
import { downloadDeliverable } from '../shared/downloadDeliverable'
import { MarkdownDoc } from '../shared/MarkdownDoc'
import { Modal } from '../shared/Modal'
import { ResultStatusChip, resultLabel, resultSubtitle, resultTitle } from './resultMeta'

const APPROVER_ROLES = ['owner', 'admin', 'approver']

export function DocumentPane({ findingId, onClose }: { findingId: string; onClose: () => void }) {
  const queryClient = useQueryClient()
  const paneRef = useRef<HTMLElement | null>(null)
  const [rejecting, setRejecting] = useState(false)
  const [note, setNote] = useState('')
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState<string | null>(null)

  const findingQuery = useQuery({
    queryKey: ['finding', findingId],
    queryFn: () => api.getFinding(findingId),
  })
  // Same cached read App.tsx keeps warm; only used to avoid offering a button
  // the server would 403.
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const canApprove = APPROVER_ROLES.includes(meQuery.data?.role ?? '')
  // The cohort edition hides Deliverables and Approvals; the pane's downloads
  // are then the way out, so the "Open in …" link is left off.
  const canOpen = useCanOpen()

  // Another card was opened, or the pane was just opened: start fresh and move
  // focus into the pane so a keyboard user lands on what they asked for.
  useEffect(() => {
    setRejecting(false)
    setNote('')
    setDownloadError(null)
    paneRef.current?.focus()
  }, [findingId])

  // Esc closes the pane — unless it is already going to close the reject
  // dialog (Modal handles that Escape itself) or the key was meant for a field.
  useEffect(() => {
    if (rejecting) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      const target = event.target as HTMLElement | null
      const tag = target?.tagName
      const inPane = !!target && !!paneRef.current?.contains(target)
      if (!inPane && (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT')) return
      onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [rejecting, onClose])

  const decideMutation = useMutation({
    mutationFn: (v: { action: 'approve' | 'reject'; note?: string }) =>
      api.decideFinding(findingId, v.action, v.note),
    onSuccess: () => {
      setRejecting(false)
      setNote('')
      // The same keys Approvals.tsx invalidates, so the sidebar's Approvals
      // badge (['findings', 'draft']) and Deliverables badge stay right, plus
      // the chat's own per-turn lists.
      queryClient.invalidateQueries({ queryKey: ['findings'] })
      queryClient.invalidateQueries({ queryKey: ['finding', findingId] })
      queryClient.invalidateQueries({ queryKey: ['run-findings'] })
      queryClient.invalidateQueries({ queryKey: ['deliverables'] })
    },
  })
  const decideError = decideMutation.error as ApiError | null

  const f = findingQuery.data
  const auto = !!f?.approvals.some((a) => a.action === 'approve' && a.purpose === 'auto')

  const deliverableSlug =
    f?.schema_slug === 'draft_section' && typeof f.subject.deliverable === 'string'
      ? f.subject.deliverable
      : null

  const download = async (format: 'pdf' | 'docx', extension: string) => {
    if (!deliverableSlug) return
    setDownloadError(null)
    setDownloading(format)
    try {
      // No `theme`: the server applies this workspace's default style.
      await downloadDeliverable(deliverableSlug, format, extension)
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : String(e))
    } finally {
      setDownloading(null)
    }
  }

  return (
    <aside className="doc-pane" ref={paneRef} tabIndex={-1} aria-label="Document">
      <header className="doc-pane-head">
        <div className="doc-pane-headtext">
          {f && <div className="rc-kind">{resultLabel(f)}</div>}
          <h2 className="doc-pane-title">{f ? resultTitle(f) : 'Loading…'}</h2>
          {f && resultSubtitle(f) && <div className="rc-sub">{resultSubtitle(f)}</div>}
        </div>
        {f && <ResultStatusChip status={f.status} auto={auto} />}
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close document pane" title="Close (Esc)">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
            <path d="M3 3l10 10M13 3L3 13" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <div className="doc-pane-body">
        {findingQuery.isLoading && <div className="empty pulse">Loading…</div>}
        {findingQuery.isError && (
          <div className="error-text">Could not load this item — {(findingQuery.error as Error).message}</div>
        )}
        {f && <PaneBody finding={f} />}
      </div>

      {f && (
        <footer className="doc-pane-foot">
          {decideError && (
            <div className="error-text">
              {decideError.status === 403 ? 'Requires approver role.' : decideError.message}
            </div>
          )}
          {downloadError && <div className="error-text">{downloadError}</div>}
          <div className="doc-pane-actions">
            {f.status === 'draft' && canApprove && (
              <>
                <button
                  type="button"
                  className="btn btn-approve"
                  onClick={() => decideMutation.mutate({ action: 'approve' })}
                  disabled={decideMutation.isPending}
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="btn btn-reject"
                  onClick={() => setRejecting(true)}
                  disabled={decideMutation.isPending}
                >
                  Reject
                </button>
              </>
            )}
            {f.status === 'draft' && !canApprove && (
              <span className="doc-pane-hint">An approver of this workspace decides on drafts.</span>
            )}
            {f.status === 'approved' && deliverableSlug && (
              <>
                <button
                  type="button"
                  className="btn"
                  onClick={() => void download('pdf', 'pdf')}
                  disabled={downloading !== null}
                >
                  {downloading === 'pdf' ? 'Preparing…' : 'Download PDF'}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => void download('docx', 'docx')}
                  disabled={downloading !== null}
                >
                  {downloading === 'docx' ? 'Preparing…' : 'Download Word'}
                </button>
              </>
            )}
            <span className="doc-pane-spacer" />
            {deliverableSlug
              ? canOpen('/deliverables') && (
                  <Link className="doc-pane-link" to={`/deliverables?slug=${encodeURIComponent(deliverableSlug)}`}>
                    Open in Deliverables
                  </Link>
                )
              : canOpen(findingHref(f.id)) && (
                  <Link className="doc-pane-link" to={findingHref(f.id)}>
                    Open in Approvals
                  </Link>
                )}
          </div>
        </footer>
      )}

      <Modal
        open={rejecting}
        onClose={() => !decideMutation.isPending && setRejecting(false)}
        title="Reject this draft?"
        width={460}
        footer={
          <div className="confirm-actions">
            <button type="button" className="btn" onClick={() => setRejecting(false)} disabled={decideMutation.isPending}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-reject"
              onClick={() => decideMutation.mutate({ action: 'reject', note: note.trim() || undefined })}
              disabled={decideMutation.isPending}
            >
              Reject
            </button>
          </div>
        }
      >
        <div className="field">
          <label className="mono-label" htmlFor="doc-pane-reject-note">
            Note (optional)
          </label>
          <textarea
            id="doc-pane-reject-note"
            rows={3}
            placeholder="Why is it being rejected? Kept in the audit record."
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </div>
      </Modal>
    </aside>
  )
}

function PaneBody({ finding }: { finding: FindingDetail }) {
  if (finding.schema_slug === 'draft_section') {
    const markdown = typeof finding.payload.markdown === 'string' ? finding.payload.markdown : ''
    return <MarkdownDoc source={markdown || '_(empty section)_'} className="chat-prose md" />
  }
  if (finding.schema_slug === 'connected_write') return <ConnectedWriteBody finding={finding} />
  return <VerdictCard finding={finding} />
}

function ConnectedWriteBody({ finding }: { finding: FindingDetail }) {
  const p = finding.payload as unknown as ConnectedWritePayload
  const upload = p.upload
  const format =
    p.source?.kind === 'deliverable' ? p.source.format : (p.content_type ?? '').split('/').pop() || null
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <div className="mono-label">Destination</div>
        <div className="mono-body">
          {p.target_label || 'Connected drive'} / tret / {p.filename}
        </div>
        {p.target_path && <div className="fine-print">{p.target_path}/tret/{p.filename}</div>}
      </div>
      <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
        {format && (
          <div>
            <div className="mono-label">Format</div>
            <div className="mono-body">{format}</div>
          </div>
        )}
        {p.content_type && (
          <div>
            <div className="mono-label">Content type</div>
            <div className="mono-body">{p.content_type}</div>
          </div>
        )}
      </div>
      {p.source?.kind === 'deliverable' && (
        <div className="fine-print">
          Rendered from the approved sections of <strong>{p.source.slug}</strong> when this is approved.
        </div>
      )}
      <div>
        <div className="mono-label">Upload</div>
        {!upload ? (
          <div className="mono-body">Nothing is written to the drive until this is approved.</div>
        ) : upload.status === 'uploaded' ? (
          <div className="mono-body">
            Uploaded
            {upload.web_url && (
              <>
                {' · '}
                <a href={upload.web_url} target="_blank" rel="noreferrer noopener">
                  open in the drive
                </a>
              </>
            )}
          </div>
        ) : (
          <div className="error-text">Upload failed — {upload.error ?? 'unknown error'}</div>
        )}
      </div>
    </div>
  )
}
