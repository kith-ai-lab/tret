/** Large in-app viewer for a batch target's written files (PDF, markdown, JSON
 *  audit records, plain text). Bytes come from the backend's authenticated
 *  `/files` endpoint, never from SharePoint, so the viewer works for anyone who
 *  can see the batch. */
import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'

import { api, ApiError, type BatchWrittenFile } from '../../api/client'
import { formatDateTime } from '../shared/format'
import { MarkdownDoc } from '../shared/MarkdownDoc'
import { Modal } from '../shared/Modal'
import { ExtLink, safeHref, shorten } from './shared'

/** Beyond this many characters of text we show only the head, to keep the
 *  browser responsive. */
const MAX_TEXT_CHARS = 2 * 1024 * 1024
/** Cap on rendered rows / array children in tables and the tree. */
const MAX_ROWS = 500

const BODY_HEIGHT = 'calc(100vh - 190px)'

type Obj = Record<string, unknown>

function isObj(v: unknown): v is Obj {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function basename(path: string): string {
  const i = path.lastIndexOf('/')
  return i >= 0 ? path.slice(i + 1) : path
}

function formatBytes(n: number): string {
  return `${n.toLocaleString('en-US')} B`
}

// ── Viewer shell ─────────────────────────────────────────────────────────

export function WrittenFileViewer({
  batchId,
  targetId,
  files,
  index,
  onIndexChange,
  onClose,
}: {
  batchId: string
  targetId: string
  files: BatchWrittenFile[]
  /** Index into `files`; null = closed. */
  index: number | null
  onIndexChange: (index: number) => void
  onClose: () => void
}) {
  const file = index !== null ? (files[index] ?? null) : null
  const open = file !== null && index !== null

  // ArrowLeft / ArrowRight step through the target's files, unless the reader
  // is typing somewhere.
  useEffect(() => {
    if (!open || index === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
      if (e.altKey || e.metaKey || e.ctrlKey || e.shiftKey || e.defaultPrevented) return
      const el = e.target as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable) return
      const next = e.key === 'ArrowLeft' ? index - 1 : index + 1
      if (next < 0 || next >= files.length) return
      e.preventDefault()
      onIndexChange(next)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, index, files.length, onIndexChange])

  const downloadHref = file ? api.batchTargetFileUrl(batchId, targetId, file.filename, { download: true }) : ''
  const sharepoint = file ? safeHref(file.web_url) : null

  const prevDisabled = index === null || index <= 0
  const nextDisabled = index === null || index >= files.length - 1
  const prevRef = useRef<HTMLButtonElement | null>(null)
  const nextRef = useRef<HTMLButtonElement | null>(null)
  // A button that becomes disabled while focused would drop focus to <body>
  // and take the dialog's Escape/focus trap with it; hand focus to the other.
  useEffect(() => {
    if (document.activeElement === prevRef.current && prevDisabled) nextRef.current?.focus()
    else if (document.activeElement === nextRef.current && nextDisabled) prevRef.current?.focus()
  }, [prevDisabled, nextDisabled])
  // Focus is lost the moment `disabled` lands, before effects run, in some
  // browsers; catch that case through the click handlers too.
  const step = (to: number, other: React.RefObject<HTMLButtonElement | null>) => {
    onIndexChange(to)
    if (to <= 0 || to >= files.length - 1) other.current?.focus()
  }

  const nav = (
    <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      <button
        ref={prevRef}
        type="button"
        className="btn btn-sm"
        aria-label="Previous file"
        disabled={prevDisabled}
        onClick={() => index !== null && step(index - 1, nextRef)}
      >
        ← Prev
      </button>
      <span className="fine-print" aria-live="polite">
        {index !== null ? index + 1 : 0} / {files.length}
      </span>
      <button
        ref={nextRef}
        type="button"
        className="btn btn-sm"
        aria-label="Next file"
        disabled={nextDisabled}
        onClick={() => index !== null && step(index + 1, prevRef)}
      >
        Next →
      </button>
    </div>
  )

  const links = (
    <div className="row" style={{ gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
      {file && (
        <a className="btn btn-sm" href={downloadHref} download={basename(file.filename)}>
          Download
        </a>
      )}
      {sharepoint && (
        <ExtLink url={sharepoint}>
          <span className="fine-print">Open in SharePoint ↗</span>
        </ExtLink>
      )}
    </div>
  )

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={1600}
      title={file ? basename(file.filename) : 'File'}
      subtitle={
        file && (
          <span className="fine-print" style={{ overflowWrap: 'anywhere' }}>
            {file.filename} · {formatBytes(file.byte_size)} · sha256 {shorten(file.sha256, 13)} ·{' '}
            uploaded {formatDateTime(file.uploaded_at)}
          </span>
        )
      }
      footer={
        <div className="row" style={{ gap: 16, justifyContent: 'space-between', width: '100%' }}>
          {nav}
          {links}
        </div>
      }
    >
      {file && (
        <FileBody
          key={`${batchId}/${targetId}/${file.filename}`}
          batchId={batchId}
          targetId={targetId}
          file={file}
          downloadHref={downloadHref}
        />
      )}
    </Modal>
  )
}

function FileBody({
  batchId,
  targetId,
  file,
  downloadHref,
}: {
  batchId: string
  targetId: string
  file: BatchWrittenFile
  downloadHref: string
}) {
  const q = useQuery({
    queryKey: ['batch-target-file', batchId, targetId, file.filename, file.sha256],
    queryFn: ({ signal }) => api.fetchBatchTargetFile(batchId, targetId, file.filename, signal),
    retry: false,
    // Keyed by sha256, so the bytes cannot go stale.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    gcTime: 60_000,
  })

  const box = { minHeight: BODY_HEIGHT, height: BODY_HEIGHT, minWidth: 0 } as const

  if (q.isPending) {
    return (
      <div style={box} className="fine-print">
        Loading {basename(file.filename)}…
      </div>
    )
  }
  if (q.isError) {
    const err = q.error
    const msg = err instanceof Error ? err.message : String(err)
    const status = err instanceof ApiError ? err.status : null
    return (
      <div style={box} className="stack">
        <div className="callout callout-warn">
          <div className="callout-title">Cannot display this file{status ? ` (${status})` : ''}</div>
          <div style={{ overflowWrap: 'anywhere' }}>{msg}</div>
        </div>
        <div>
          <a className="btn btn-sm" href={downloadHref} download={basename(file.filename)}>
            Download
          </a>
        </div>
      </div>
    )
  }

  const lower = file.filename.toLowerCase()
  if (lower.endsWith('.pdf')) {
    return <PdfView blob={q.data} name={basename(file.filename)} downloadHref={downloadHref} box={box} />
  }
  return <TextFileView blob={q.data} filename={file.filename} box={box} />
}

// ── PDF ──────────────────────────────────────────────────────────────────

function PdfView({
  blob,
  name,
  downloadHref,
  box,
}: {
  blob: Blob
  name: string
  downloadHref: string
  box: { minHeight: string; height: string; minWidth: number }
}) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    const u = URL.createObjectURL(new Blob([blob], { type: 'application/pdf' }))
    setUrl(u)
    return () => {
      URL.revokeObjectURL(u)
      setUrl(null)
    }
  }, [blob])
  return (
    <div style={{ ...box, display: 'flex', flexDirection: 'column', gap: 6 }}>
      {url ? (
        <iframe
          title={name}
          src={url}
          style={{
            flex: 1,
            width: '100%',
            minHeight: 0,
            border: '1px solid var(--border)',
            background: 'var(--bg-input)',
          }}
        />
      ) : (
        <div style={{ flex: 1 }} />
      )}
      <div className="fine-print">
        If the PDF does not display,{' '}
        <a href={downloadHref} download={name}>
          download it
        </a>
        .
      </div>
    </div>
  )
}

// ── Text-ish files ───────────────────────────────────────────────────────

function TextFileView({
  blob,
  filename,
  box,
}: {
  blob: Blob
  filename: string
  box: { minHeight: string; height: string; minWidth: number }
}) {
  const [text, setText] = useState<string | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    blob
      .text()
      .then((t) => {
        if (!live) return
        setTruncated(t.length > MAX_TEXT_CHARS)
        setText(t.length > MAX_TEXT_CHARS ? t.slice(0, MAX_TEXT_CHARS) : t)
      })
      .catch((e: unknown) => live && setFailed(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [blob])

  if (failed) return <div style={box} className="error-text">{failed}</div>
  if (text === null) return <div style={box} className="fine-print">Decoding…</div>

  const lower = filename.toLowerCase()
  return (
    <div style={{ ...box, height: 'auto', overflow: 'auto' }}>
      {truncated && (
        <div className="callout callout-warn" style={{ marginBottom: 8 }}>
          Large file: showing the first 2 MB as source text. Download it for the whole file.
        </div>
      )}
      {lower.endsWith('.md') ? (
        <MarkdownView text={text} forceSource={truncated} />
      ) : lower.endsWith('.json') && !truncated ? (
        <JsonView text={text} filename={lower} />
      ) : (
        <Pre text={text} />
      )}
    </div>
  )
}

function Pre({ text }: { text: string }) {
  return (
    <pre
      className="mono-body"
      style={{
        margin: 0,
        padding: 12,
        whiteSpace: 'pre-wrap',
        overflowWrap: 'anywhere',
        background: 'var(--bg-input)',
        border: '1px solid var(--border-subtle)',
        color: 'var(--text)',
        minHeight: BODY_HEIGHT,
      }}
    >
      {text}
    </pre>
  )
}

function Toggle<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: [T, string][]
  onChange: (v: T) => void
}) {
  return (
    <div className="row" style={{ gap: 6, marginBottom: 10 }} role="group" aria-label="View mode">
      {options.map(([v, label]) => (
        <button
          key={v}
          type="button"
          className="btn btn-sm"
          aria-pressed={value === v}
          onClick={() => onChange(v)}
          style={value === v ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

function MarkdownView({ text, forceSource }: { text: string; forceSource: boolean }) {
  const [mode, setMode] = useState<'rendered' | 'source'>(forceSource ? 'source' : 'rendered')
  return (
    <div>
      {!forceSource && (
        <Toggle
          value={mode}
          onChange={setMode}
          options={[
            ['rendered', 'Rendered'],
            ['source', 'Source'],
          ]}
        />
      )}
      {mode === 'rendered' && !forceSource ? <MarkdownDoc source={text} /> : <Pre text={text} />}
    </div>
  )
}

// ── JSON ─────────────────────────────────────────────────────────────────

type JsonMode = 'summary' | 'tree' | 'raw'

function JsonView({ text, filename }: { text: string; filename: string }) {
  const parsed = useMemo<{ ok: true; value: unknown } | { ok: false }>(() => {
    try {
      return { ok: true, value: JSON.parse(text) as unknown }
    } catch {
      return { ok: false }
    }
  }, [text])
  const kind = filename.endsWith('.page_map.json')
    ? 'page_map'
    : filename.endsWith('.package.json')
      ? 'package'
      : filename.endsWith('.extraction_stats.json')
        ? 'stats'
        : filename.endsWith('.links.json')
          ? 'links'
          : null
  const [mode, setMode] = useState<JsonMode>(kind ? 'summary' : 'tree')

  if (!parsed.ok) {
    return (
      <div>
        <div className="fine-print" style={{ marginBottom: 8 }}>
          Not valid JSON; showing the raw text.
        </div>
        <Pre text={text} />
      </div>
    )
  }
  const v = parsed.value
  const options: [JsonMode, string][] = [
    ...(kind ? ([['summary', 'Summary']] as [JsonMode, string][]) : []),
    ['tree', 'Tree'],
    ['raw', 'Raw'],
  ]
  return (
    <div>
      <Toggle value={mode} onChange={setMode} options={options} />
      {mode === 'raw' ? (
        <Pre text={JSON.stringify(v, null, 2)} />
      ) : mode === 'tree' || !kind ? (
        <Tree value={v} />
      ) : kind === 'page_map' ? (
        <PageMapView v={v} />
      ) : kind === 'package' ? (
        <PackageView v={v} />
      ) : kind === 'stats' ? (
        <StatsView v={v} />
      ) : (
        <LinksView v={v} />
      )}
    </div>
  )
}

// Generic tree ------------------------------------------------------------

function primColor(v: unknown): string {
  if (typeof v === 'string') return 'var(--green)'
  if (typeof v === 'number') return 'var(--blue)'
  if (typeof v === 'boolean') return 'var(--violet)'
  return 'var(--text-muted)'
}

function Prim({ v }: { v: unknown }) {
  const text = typeof v === 'string' ? JSON.stringify(v) : String(v)
  return <span style={{ color: primColor(v), overflowWrap: 'anywhere' }}>{text}</span>
}

function childCount(v: unknown): number {
  return Array.isArray(v) ? v.length : isObj(v) ? Object.keys(v).length : 0
}

function Tree({ value, label, depth = 0 }: { value: unknown; label?: string; depth?: number }) {
  const keyEl = label !== undefined && (
    <span style={{ color: 'var(--text-muted)' }}>{label}: </span>
  )
  if (value === null || typeof value !== 'object') {
    return (
      <div className="mono-body" style={{ paddingLeft: depth ? 16 : 0 }}>
        {keyEl}
        <Prim v={value} />
      </div>
    )
  }
  const isArr = Array.isArray(value)
  const entries: [string, unknown][] = isArr
    ? (value as unknown[]).slice(0, MAX_ROWS).map((x, i) => [String(i), x])
    : Object.entries(value as Obj).slice(0, MAX_ROWS)
  const total = childCount(value)
  const startOpen = depth === 0 || (depth < 2 && total <= 12)
  return (
    <details open={startOpen} style={{ paddingLeft: depth ? 16 : 0 }} className="mono-body">
      <summary style={{ cursor: 'pointer' }}>
        {keyEl}
        <span style={{ color: 'var(--text-muted)' }}>
          {isArr ? `[ ${total} ]` : `{ ${total} }`}
        </span>
      </summary>
      {entries.map(([k, x]) => (
        <Tree key={k} value={x} label={k} depth={depth + 1} />
      ))}
      {total > entries.length && (
        <div className="fine-print" style={{ paddingLeft: 16 }}>
          … {(total - entries.length).toLocaleString('en-US')} more not shown
        </div>
      )}
    </details>
  )
}

// Shared table bits -------------------------------------------------------

function Scroll({ children }: { children: ReactNode }) {
  return <div style={{ overflowX: 'auto', maxWidth: '100%' }}>{children}</div>
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section style={{ marginBottom: 18 }}>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        {title}
      </div>
      {children}
    </section>
  )
}

/** Scalar value as text; objects/arrays are flagged for the tree instead. */
function scalarText(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v
  return String(v)
}

function KeyValueTable({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <Scroll>
      <table className="mono-table">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <td style={{ color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>{k}</td>
              <td style={{ overflowWrap: 'anywhere' }}>{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Scroll>
  )
}

function CellValue({ v }: { v: unknown }) {
  if (v !== null && typeof v === 'object') return <Tree value={v} />
  return <>{scalarText(v)}</>
}

function BulletList({ items }: { items: unknown[] }) {
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }} className="mono-body">
      {items.slice(0, MAX_ROWS).map((x, i) => (
        <li key={i} style={{ overflowWrap: 'anywhere' }}>
          {typeof x === 'string' ? x : <Tree value={x} />}
        </li>
      ))}
    </ul>
  )
}

function ObjectRowsTable({ rows }: { rows: unknown[] }) {
  const objs = rows.slice(0, MAX_ROWS)
  const cols: string[] = []
  for (const r of objs) {
    if (isObj(r)) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k)
  }
  return (
    <Scroll>
      <table className="mono-table">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {objs.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c}>{isObj(r) ? <CellValue v={r[c]} /> : i === 0 ? <CellValue v={r} /> : null}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > objs.length && (
        <div className="fine-print">… {(rows.length - objs.length).toLocaleString('en-US')} more not shown</div>
      )}
    </Scroll>
  )
}

function ObjectTable({ v, empty }: { v: unknown; empty?: string }) {
  if (Array.isArray(v)) {
    if (v.length === 0) return <div className="fine-print">{empty ?? 'None.'}</div>
    return <ObjectRowsTable rows={v} />
  }
  if (v === null || v === undefined) return <div className="fine-print">{empty ?? 'None.'}</div>
  return <Tree value={v} />
}

/** "1–3, 7, 40–46" from a list of page numbers. */
function collapseRanges(nums: number[]): string {
  const sorted = [...new Set(nums)].sort((a, b) => a - b)
  const parts: string[] = []
  let i = 0
  while (i < sorted.length) {
    let j = i
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++
    parts.push(j > i ? `${sorted[i]}–${sorted[j]}` : String(sorted[i]))
    i = j + 1
  }
  return parts.join(', ')
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

// page_map ----------------------------------------------------------------

function PageMapView({ v }: { v: unknown }) {
  if (!isObj(v)) return <Tree value={v} />
  const pages = Array.isArray(v.pages) ? v.pages.filter(isObj) : []
  const src = pages.map((p) => num(p.source_pdf_page)).filter((n): n is number => n !== null)
  const retained = num(v.retained_page_count)
  const total = num(v.source_page_count)
  const rows: [string, ReactNode][] = []
  if (retained !== null && total !== null) rows.push(['Retained', `${retained} of ${total} source pages`])
  if (src.length > 0) rows.push(['Source pages kept', collapseRanges(src)])
  for (const k of ['source_filename', 'source_sha256', 'retained_sha256']) {
    if (v[k] !== undefined) rows.push([k, scalarText(v[k])])
  }
  return (
    <div>
      <Section title="Summary">
        <KeyValueTable rows={rows} />
      </Section>
      <Section title={`Pages (${pages.length.toLocaleString('en-US')})`}>
        {pages.length === 0 ? (
          <div className="fine-print">No page entries.</div>
        ) : (
          <Scroll>
            <table className="mono-table">
              <thead>
                <tr>
                  <th className="num">Retained #</th>
                  <th className="num">Source #</th>
                  <th>Label</th>
                </tr>
              </thead>
              <tbody>
                {pages.slice(0, MAX_ROWS).map((p, i) => (
                  <tr key={i}>
                    <td className="num">{scalarText(p.retained_pdf_page)}</td>
                    <td className="num">{scalarText(p.source_pdf_page)}</td>
                    <td>{scalarText(p.source_page_label)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {pages.length > MAX_ROWS && (
              <div className="fine-print">… {pages.length - MAX_ROWS} more not shown</div>
            )}
          </Scroll>
        )}
      </Section>
    </div>
  )
}

// package -----------------------------------------------------------------

const PACKAGE_HANDLED = new Set([
  'keep_ranges',
  'review_note',
  'incorporations',
  'exclusions',
  'artifacts',
  'limitations',
  'verification',
])

function PackageView({ v }: { v: unknown }) {
  if (!isObj(v)) return <Tree value={v} />
  const scalars: [string, ReactNode][] = []
  const rest: [string, unknown][] = []
  for (const [k, x] of Object.entries(v)) {
    if (PACKAGE_HANDLED.has(k)) continue
    if (x === null || typeof x !== 'object') scalars.push([k, scalarText(x)])
    else rest.push([k, x])
  }
  const ranges = v.keep_ranges
  const rangeText = Array.isArray(ranges)
    ? ranges
        .map((r) =>
          Array.isArray(r) && r.length === 2 ? (r[0] === r[1] ? `${r[0]}` : `${r[0]}–${r[1]}`) : JSON.stringify(r),
        )
        .join(', ') || '—'
    : ranges === null || ranges === undefined
      ? 'Whole document'
      : scalarText(ranges)
  const retained = num(v.retained_page_count)
  const total = num(v.source_page_count)
  const top: [string, ReactNode][] = [
    ['keep_ranges', rangeText],
    ...(retained !== null && total !== null
      ? ([['pages', `Retained ${retained} of ${total} source pages`]] as [string, ReactNode][])
      : []),
  ]
  const artifacts = isObj(v.artifacts) ? Object.entries(v.artifacts) : null
  const verification = isObj(v.verification) ? v.verification : null
  const checked = verification && Array.isArray(verification.checked) ? verification.checked : null
  const vRows: [string, ReactNode][] = verification
    ? Object.entries(verification)
        .filter(([k]) => k !== 'checked')
        .map(([k, x]) => [k, <CellValue key={k} v={x} />])
    : []
  return (
    <div>
      <Section title="Package">
        <KeyValueTable rows={[...top, ...scalars]} />
      </Section>
      {typeof v.review_note === 'string' && v.review_note && (
        <Section title="Review note">
          <div className="mono-body" style={{ overflowWrap: 'anywhere' }}>{v.review_note}</div>
        </Section>
      )}
      {v.incorporations !== undefined && (
        <Section title="Incorporations">
          <ObjectTable v={v.incorporations} />
        </Section>
      )}
      {v.exclusions !== undefined && (
        <Section title="Exclusions">
          <ObjectTable v={v.exclusions} />
        </Section>
      )}
      {artifacts && (
        <Section title="Artifacts">
          <KeyValueTable rows={artifacts.map(([k, x]) => [k, scalarText(x)])} />
        </Section>
      )}
      {Array.isArray(v.limitations) && v.limitations.length > 0 && (
        <Section title="Limitations">
          <BulletList items={v.limitations} />
        </Section>
      )}
      {verification && (
        <Section title="Verification">
          {vRows.length > 0 && <KeyValueTable rows={vRows} />}
          {checked && checked.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div className="fine-print">Checked</div>
              <BulletList items={checked} />
            </div>
          )}
        </Section>
      )}
      {rest.length > 0 && (
        <Section title="Other fields">
          {rest.map(([k, x]) => (
            <Tree key={k} value={x} label={k} />
          ))}
        </Section>
      )}
    </div>
  )
}

// extraction_stats --------------------------------------------------------

function StatsView({ v }: { v: unknown }) {
  if (!isObj(v)) return <Tree value={v} />
  const rows: [string, ReactNode][] = []
  for (const [k, x] of Object.entries(v)) {
    if (isObj(x)) {
      for (const [k2, y] of Object.entries(x)) {
        rows.push([`${k}.${k2}`, y !== null && typeof y === 'object' ? <Tree value={y} /> : scalarText(y)])
      }
    } else if (Array.isArray(x)) {
      rows.push([k, <Tree key={k} value={x} />])
    } else {
      rows.push([k, scalarText(x)])
    }
  }
  return <KeyValueTable rows={rows} />
}

// links -------------------------------------------------------------------

function LinksView({ v }: { v: unknown }) {
  if (!Array.isArray(v)) return <Tree value={v} />
  const entries = v.filter(isObj)
  const counts = new Map<string, number>()
  for (const e of entries) {
    const s = typeof e.status === 'string' ? e.status : '—'
    counts.set(s, (counts.get(s) ?? 0) + 1)
  }
  return (
    <div>
      <Section title={`Links (${entries.length.toLocaleString('en-US')})`}>
        <div className="row" style={{ gap: 16, flexWrap: 'wrap' }}>
          {[...counts.entries()].map(([s, n]) => (
            <span key={s} className="mono-body">
              <span style={{ color: 'var(--text-muted)' }}>{s}</span> {n.toLocaleString('en-US')}
            </span>
          ))}
        </div>
      </Section>
      <Scroll>
        <table className="mono-table">
          <thead>
            <tr>
              <th className="num">Source page</th>
              <th className="num">Retained page</th>
              <th>Type</th>
              <th>Status</th>
              <th>Target</th>
            </tr>
          </thead>
          <tbody>
            {entries.slice(0, MAX_ROWS).map((e, i) => (
              <tr key={i}>
                <td className="num">{scalarText(e.source_pdf_page)}</td>
                <td className="num">{scalarText(e.retained_pdf_page)}</td>
                <td>{scalarText(e.type)}</td>
                <td>{scalarText(e.status)}</td>
                <td style={{ overflowWrap: 'anywhere', maxWidth: 520 }}>
                  <LinkTarget e={e} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {entries.length > MAX_ROWS && (
          <div className="fine-print">… {(entries.length - MAX_ROWS).toLocaleString('en-US')} more not shown</div>
        )}
      </Scroll>
    </div>
  )
}

function LinkTarget({ e }: { e: Obj }) {
  if (typeof e.uri === 'string' && e.uri) {
    return safeHref(e.uri) ? <ExtLink url={e.uri}>{e.uri}</ExtLink> : <>{e.uri}</>
  }
  if (typeof e.remote_file === 'string' && e.remote_file) return <>{e.remote_file}</>
  const retained = num(e.target_retained_pdf_page)
  const source = num(e.target_source_pdf_page)
  if (retained !== null) {
    return <>→ p. {retained}{source !== null ? ` (source ${source})` : ''}</>
  }
  if (source !== null) return <>→ source p. {source}</>
  if (typeof e.detail === 'string' && e.detail) return <>{e.detail}</>
  return <>—</>
}
