import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError, type Deliverable } from '../api/client'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { StatusBadge } from '../components/shared/StatusBadge'
import { Markdown } from './Packs'
import { formatDateTime } from './Runs'

export function Deliverables() {
  const deliverablesQuery = useQuery({ queryKey: ['deliverables'], queryFn: api.listDeliverables })
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)

  const deliverables = deliverablesQuery.data ?? []

  useEffect(() => {
    if (deliverables.length > 0 && !deliverables.some((d) => d.slug === selectedSlug)) {
      setSelectedSlug(deliverables[0].slug)
    }
  }, [deliverables, selectedSlug])

  const selected = deliverables.find((d) => d.slug === selectedSlug) ?? null

  return (
    <div>
      <h1 className="view-title">Deliverables</h1>
      <div className="view-sub">
        Drafted sections assembled into exportable documents — approved content only, by default.
      </div>

      {deliverablesQuery.isLoading ? (
        <div className="empty pulse">Loading deliverables…</div>
      ) : deliverablesQuery.isError ? (
        <div className="error-text">{(deliverablesQuery.error as Error).message}</div>
      ) : deliverables.length === 0 ? (
        <div className="empty">
          No deliverables yet — draft a section in <Link to="/">Chat</Link> or the{' '}
          <Link to="/workbench">Workbench</Link> (e.g. a TCFD section draft), and it will appear
          here.
        </div>
      ) : (
        <ListDetail
          listWidth={250}
          list={deliverables.map((d) => (
            <ListItem
              key={d.slug}
              active={d.slug === selectedSlug}
              onClick={() => setSelectedSlug(d.slug)}
              title={d.slug}
              sub={
                <span className="row" style={{ gap: 6, display: 'inline-flex' }}>
                  {d.approved_count > 0 && (
                    <span className="badge badge-green" style={{ fontSize: 10, padding: '1px 6px' }}>
                      {d.approved_count} approved
                    </span>
                  )}
                  {d.draft_count > 0 && (
                    <span className="badge badge-amber" style={{ fontSize: 10, padding: '1px 6px' }}>
                      {d.draft_count} draft
                    </span>
                  )}
                  <span>{relativeTime(d.updated_at)}</span>
                </span>
              }
            />
          ))}
          detail={selected ? <DeliverableDetail deliverable={selected} /> : null}
        />
      )}
    </div>
  )
}

function DeliverableDetail({ deliverable }: { deliverable: Deliverable }) {
  const [includeDraft, setIncludeDraft] = useState(false)
  const [pdfError, setPdfError] = useState<string | null>(null)
  const [pdfBusy, setPdfBusy] = useState(false)

  // Reset per-deliverable UI state when switching.
  useEffect(() => {
    setIncludeDraft(false)
    setPdfError(null)
  }, [deliverable.slug])

  const exportQuery = useQuery({
    queryKey: ['deliverable-export', deliverable.slug, includeDraft],
    queryFn: () => api.exportDeliverableJson(deliverable.slug, includeDraft),
    retry: false,
  })

  const exportHref = (format: string) =>
    `/api/deliverables/${encodeURIComponent(deliverable.slug)}/export?format=${format}${
      includeDraft ? '&include_draft=true' : ''
    }`

  const downloadPdf = async () => {
    setPdfError(null)
    setPdfBusy(true)
    try {
      const res = await fetch(exportHref('pdf'), { credentials: 'include' })
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`
        try {
          const data: unknown = await res.json()
          if (data && typeof data === 'object' && 'detail' in data) {
            const d = (data as { detail: unknown }).detail
            detail = typeof d === 'string' ? d : JSON.stringify(d)
          }
        } catch {
          /* non-JSON error body */
        }
        setPdfError(detail)
        return
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${deliverable.slug}.pdf`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setPdfError(e instanceof Error ? e.message : String(e))
    } finally {
      setPdfBusy(false)
    }
  }

  const emptyExport =
    exportQuery.isError && (exportQuery.error as ApiError | undefined)?.status === 404

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: 'wrap' }}>
        <h2 className="view-title" style={{ marginBottom: 0 }}>
          {deliverable.slug}
        </h2>
        <span className="badge badge-green">{deliverable.approved_count} approved</span>
        {deliverable.draft_count > 0 && (
          <span className="badge badge-amber">{deliverable.draft_count} draft</span>
        )}
      </div>

      {/* Sections */}
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Sections
        </div>
        <table className="mono-table">
          <thead>
            <tr>
              <th>Section</th>
              <th>Status</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {deliverable.sections.map((s) => (
              <tr key={s.section}>
                <td>{s.section}</td>
                <td>
                  <StatusBadge status={s.status} />
                </td>
                <td>{s.updated_at ? formatDateTime(s.updated_at) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div
          style={{
            marginTop: 8,
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--text-muted)',
          }}
        >
          Only approved sections are included in exports — drafts go through{' '}
          <Link to="/approvals">Approvals</Link> first.
        </div>
      </div>

      {/* Export controls */}
      <div className="panel">
        <div className="mono-label" style={{ marginBottom: 10 }}>
          Export
        </div>
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <a className="btn" href={exportHref('markdown')} download={`${deliverable.slug}.md`}>
            Markdown
          </a>
          <a className="btn" href={exportHref('html')} download={`${deliverable.slug}.html`}>
            HTML
          </a>
          <button className="btn" onClick={() => void downloadPdf()} disabled={pdfBusy}>
            {pdfBusy ? 'Rendering…' : 'PDF'}
          </button>
          <label className="check-row" style={{ padding: 0, marginLeft: 8 }}>
            <input
              type="checkbox"
              checked={includeDraft}
              onChange={(e) => setIncludeDraft(e.target.checked)}
            />
            <span>include drafts</span>
            <span className="desc">(unapproved-content preview — not for distribution)</span>
          </label>
        </div>
        {pdfError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {pdfError}
          </div>
        )}
      </div>

      {/* Assembled preview */}
      <div>
        <div className="mono-label" style={{ marginBottom: 6 }}>
          Assembled preview{includeDraft ? ' (incl. drafts)' : ''}
        </div>
        {exportQuery.isLoading ? (
          <div className="empty pulse">Assembling…</div>
        ) : emptyExport || (exportQuery.data && exportQuery.data.sections.length === 0) ? (
          <div className="empty">
            No approved sections yet — draft sections in chat or the Workbench, then approve them.
          </div>
        ) : exportQuery.isError ? (
          <div className="error-text">{(exportQuery.error as Error).message}</div>
        ) : exportQuery.data ? (
          <>
            <div className="panel md" style={{ maxHeight: 480, overflowY: 'auto' }}>
              <Markdown source={exportQuery.data.markdown} />
            </div>
            <div className="row" style={{ marginTop: 8, flexWrap: 'wrap', gap: 8 }}>
              {exportQuery.data.sections.map((s) => (
                <span key={s.finding_id} className="chip" title={`finding ${s.finding_id}`}>
                  {s.section} · {s.status}
                  {s.model ? ` · ${s.model}` : ''}
                  {s.doctrine_sha ? ` · ${s.doctrine_sha}` : ''}
                </span>
              ))}
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}

function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}
