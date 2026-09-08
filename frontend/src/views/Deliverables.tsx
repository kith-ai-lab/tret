import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError, type Deliverable, type WriteTarget } from '../api/client'
import {
  formatCo2eScaled,
  formatDateTime,
  formatEnergyScaled,
  formatFactor,
  orDash,
} from '../components/shared/format'
import { ListDetail, ListItem } from '../components/shared/ListDetail'
import { MarkdownDoc } from '../components/shared/MarkdownDoc'
import { Modal } from '../components/shared/Modal'
import { StatusBadge } from '../components/shared/StatusBadge'

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

/** The three export formats, and the file each one lands as. */
const EXPORT_FORMATS = [
  { format: 'markdown', label: 'Markdown', extension: 'md' },
  { format: 'html', label: 'HTML', extension: 'html' },
  { format: 'pdf', label: 'PDF', extension: 'pdf' },
] as const

function DeliverableDetail({ deliverable }: { deliverable: Deliverable }) {
  const [includeDraft, setIncludeDraft] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)
  const [busyFormat, setBusyFormat] = useState<string | null>(null)

  // Reset per-deliverable UI state when switching.
  useEffect(() => {
    setIncludeDraft(false)
    setExportError(null)
    setBusyFormat(null)
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

  /** Fetch first, save only on success.
   *
   *  All three formats go through here, and they have to: a plain
   *  `<a href download>` hands the browser whatever the server answers with. When
   *  a deliverable has nothing exportable the endpoint answers 404 with a JSON
   *  `detail` body, and the anchor saved that body to disk as `tcfd.md` — a file
   *  that looks like the document and contains an error. Markdown and HTML used
   *  the anchor; only PDF checked. */
  const download = async (format: string, extension: string) => {
    setExportError(null)
    setBusyFormat(format)
    let objectUrl: string | null = null
    try {
      const res = await fetch(exportHref(format), { credentials: 'include' })
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
        setExportError(detail)
        return
      }
      const blob = await res.blob()
      objectUrl = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = objectUrl
      a.download = `${deliverable.slug}.${extension}`
      document.body.appendChild(a)
      a.click()
      a.remove()
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e))
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl)
      setBusyFormat(null)
    }
  }

  const emptyExport =
    exportQuery.isError && (exportQuery.error as ApiError | undefined)?.status === 404
  // The preview query and the export endpoints agree on what is exportable, so a
  // 404 (or an assembled document with no sections) means every format would
  // 404 too. The buttons say so instead of offering a download that cannot work.
  const nothingToExport = emptyExport || exportQuery.data?.sections.length === 0
  const footprint = exportQuery.data?.energy

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
          {EXPORT_FORMATS.map(({ format, label, extension }) => (
            <button
              key={format}
              className="btn"
              type="button"
              onClick={() => void download(format, extension)}
              disabled={busyFormat !== null || nothingToExport || exportQuery.isLoading}
              title={
                nothingToExport
                  ? 'Nothing to export yet — this deliverable has no approved sections.'
                  : undefined
              }
            >
              {busyFormat === format ? 'Preparing…' : label}
            </button>
          ))}
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
        <PublishToSharePointButton deliverable={deliverable} />
        {nothingToExport && (
          <div
            style={{
              marginTop: 8,
              fontFamily: 'var(--mono)',
              fontSize: 11,
              color: 'var(--text-muted)',
            }}
          >
            Nothing exportable{includeDraft ? '' : ' — approve a section, or tick "include drafts"'}.
          </div>
        )}
        {exportError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {exportError}
          </div>
        )}
        {/* The document's own estimated compute footprint, summed over the
            distinct runs behind it — the same figure the exported file prints in
            its footer, so the UI and the artifact cannot disagree. */}
        {footprint && (
          <div
            style={{
              marginTop: 10,
              fontFamily: 'var(--mono)',
              fontSize: 11,
              color: 'var(--text-muted)',
            }}
            title="Estimated from token counts and each model's energy class — never measured. Summed over distinct runs, so two sections drafted by one run are not counted twice."
          >
            {footprint.energy_wh === null ? (
              // No run behind this document carries an estimate. That is not a
              // footprint of zero, and must not be printed as one.
              <>
                Compute behind this document: no estimate recorded
                {footprint.runs_without_estimate > 0 &&
                  ` — ${footprint.runs_without_estimate} run(s) predate eco accounting`}
                .
              </>
            ) : (
              <>
                Compute behind this document (est.):{' '}
                {orDash(formatEnergyScaled(footprint.energy_wh))} ·{' '}
                {orDash(formatCo2eScaled(footprint.co2e_g))} at{' '}
                {formatFactor(footprint.grid_co2e_g_per_kwh, 1)} g/kWh, over {footprint.runs} run
                {footprint.runs === 1 ? '' : 's'}
                {footprint.runs_without_estimate > 0 &&
                  ` · a further ${footprint.runs_without_estimate} run(s) carry no estimate and are excluded, not counted as zero`}
              </>
            )}
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
            <div className="panel" style={{ maxHeight: 480, overflowY: 'auto' }}>
              <MarkdownDoc source={exportQuery.data.markdown} />
            </div>
            <div className="row" style={{ marginTop: 8, flexWrap: 'wrap', gap: 8 }}>
              {exportQuery.data.sections.map((s) => (
                <span
                  key={s.finding_id}
                  className="chip"
                  title={`finding ${s.finding_id}${s.run_id ? ` · run ${s.run_id}` : ''}${
                    s.energy_wh === null || s.energy_wh === undefined
                      ? ' · no footprint recorded for the drafting run'
                      : ` · drafting run drew ~${orDash(formatEnergyScaled(s.energy_wh))} (est., shared by every section from that run)`
                  }`}
                >
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

// ── publish to SharePoint ────────────────────────────────────────────────
// Only offered once write-back is actually usable: an m365 connection with
// write scopes granted and at least one output folder configured. Both a
// 404 (endpoint not deployed on this backend yet) and a 409 (m365 not
// connected/write-enabled) mean "nothing to publish to" here — same as any
// other error, since there is nothing actionable to show for either.

function PublishToSharePointButton({ deliverable }: { deliverable: Deliverable }) {
  const [open, setOpen] = useState(false)
  const writeTargetsQuery = useQuery({
    queryKey: ['m365-write-targets'],
    queryFn: api.m365WriteTargets,
    retry: false,
  })
  // Publishing needs workspace-approver or higher — the server enforces
  // this (require_workspace_approver in api/findings.py, same dependency
  // POST .../approval uses); this is purely so an analyst isn't shown a
  // button that would just 403. Same ['owner', 'admin'].includes(me.role)
  // shape Harnesses.tsx uses for its own role-gated controls, widened to
  // include 'approver'. Defaults to false (hidden) while `me` is still
  // loading, rather than flashing an enabled button first.
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const canApprove = ['owner', 'admin', 'approver'].includes(meQuery.data?.role ?? '')

  const targets = writeTargetsQuery.data?.targets ?? []
  const eligible = (writeTargetsQuery.data?.write_enabled ?? false) && targets.length > 0

  if (writeTargetsQuery.isLoading || writeTargetsQuery.isError || !eligible || !canApprove) return null

  return (
    <>
      <button type="button" className="btn btn-sm" onClick={() => setOpen(true)}>
        Publish to SharePoint
      </button>
      <PublishToSharePointModal
        deliverable={deliverable}
        targets={targets}
        open={open}
        onClose={() => setOpen(false)}
      />
    </>
  )
}

function PublishToSharePointModal({
  deliverable,
  targets,
  open,
  onClose,
}: {
  deliverable: Deliverable
  targets: WriteTarget[]
  open: boolean
  onClose: () => void
}) {
  const [targetSlug, setTargetSlug] = useState(targets[0]?.slug ?? '')
  const [format, setFormat] = useState<(typeof EXPORT_FORMATS)[number]['format']>('markdown')
  const [filename, setFilename] = useState(`${deliverable.slug}.md`)
  const [filenameTouched, setFilenameTouched] = useState(false)

  // Reset to a fresh form on every open — the modal stays mounted between
  // visits, so without this a prior publish's filename/target would carry
  // over into the next one.
  useEffect(() => {
    if (open) {
      setTargetSlug(targets[0]?.slug ?? '')
      setFormat('markdown')
      setFilename(`${deliverable.slug}.md`)
      setFilenameTouched(false)
    }
  }, [open, deliverable.slug, targets])

  const onFormatChange = (f: (typeof EXPORT_FORMATS)[number]['format']) => {
    setFormat(f)
    if (!filenameTouched) {
      const extension = EXPORT_FORMATS.find((x) => x.format === f)?.extension ?? 'md'
      setFilename(`${deliverable.slug}.${extension}`)
    }
  }

  const publishMutation = useMutation({
    mutationFn: () =>
      api.publishDeliverable(deliverable.slug, {
        target_slug: targetSlug,
        filename,
        format,
      }),
  })
  const publishError = publishMutation.error as ApiError | null
  const result = publishMutation.data

  const close = () => {
    if (publishMutation.isPending) return
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Publish to SharePoint"
      subtitle="Writes this deliverable's export straight into an m365 output folder."
      footer={
        result ? (
          <button type="button" className="btn btn-sm" onClick={close}>
            Close
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn btn-sm"
              onClick={close}
              disabled={publishMutation.isPending}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={publishMutation.isPending || !targetSlug || filename.trim() === ''}
              onClick={() => publishMutation.mutate()}
            >
              {publishMutation.isPending ? 'Publishing…' : 'Publish'}
            </button>
          </>
        )
      }
    >
      {result ? (
        <div className="stack" style={{ gap: 8 }}>
          <div className="mono-body" style={{ color: 'var(--green)' }}>
            Published.
          </div>
          <div className="mono-body">
            <a href={result.web_url} target="_blank" rel="noopener noreferrer">
              {result.name}
            </a>
          </div>
          <div className="fine-print" style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
            {result.path}
          </div>
        </div>
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          <div className="field">
            <label className="mono-label">Output folder</label>
            <select value={targetSlug} onChange={(e) => setTargetSlug(e.target.value)}>
              {targets.map((t) => (
                <option key={t.slug} value={t.slug}>
                  {t.label} — {t.path}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="mono-label">Filename</label>
            <input
              type="text"
              value={filename}
              onChange={(e) => {
                setFilenameTouched(true)
                setFilename(e.target.value)
              }}
            />
          </div>
          <div className="field">
            <label className="mono-label">Format</label>
            <div className="row" style={{ gap: 12 }}>
              {EXPORT_FORMATS.map(({ format: f, label }) => (
                <label key={f} className="check-row" style={{ padding: 0 }}>
                  <input
                    type="radio"
                    name="publish-format"
                    checked={format === f}
                    onChange={() => onFormatChange(f)}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </div>
          {publishError && <div className="error-text">{publishError.message}</div>}
        </div>
      )}
    </Modal>
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
