/** Toolbar for an HTML deliverable in a chat reply (Learning round R19).
 *
 *  **Safety — the property that must not regress.** A deliverable is
 *  model-authored HTML and may contain scripts. It is only ever shown inside an
 *  `<iframe sandbox="allow-scripts">` WITHOUT `allow-same-origin`, so its
 *  scripts run in an opaque origin: no app cookies, no app storage, no calls to
 *  `/api` as the signed-in user. Do not add `allow-same-origin`, and never hand
 *  the raw HTML to `window.open`/a blob URL as a top-level document — that
 *  would run it with the app's origin. "Open in new tab" therefore opens a
 *  wrapper page that contains nothing but the same sandboxed iframe.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useEffect, useRef, useState } from 'react'

import { chatFilesApi, refusalMessage } from '../../api/client'
import './chat-thread.css'

/** The sandbox tokens for every deliverable frame. Exported for tests. */
export const HTML_SANDBOX = 'allow-scripts'

/** A reply that is itself a whole HTML document (not prose around a fence). */
export function isFullHtmlDocument(source: string): boolean {
  return /^\s*(<!doctype\s+html[^>]*>|<html[\s>])/i.test(source)
}

function escapeAttr(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
}

/** The only document that ever gets its own tab: a sandboxed iframe and nothing else. */
export function buildWrapperPage(html: string, title = 'Preview'): string {
  return (
    '<!doctype html><html><head><meta charset="utf-8">' +
    `<title>${escapeAttr(title)}</title>` +
    '<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%;background:#fff}</style>' +
    '</head><body>' +
    `<iframe sandbox="${HTML_SANDBOX}" srcdoc="${escapeAttr(html)}" title="${escapeAttr(title)}"></iframe>` +
    '</body></html>'
  )
}

/** A filename for the download/save: the document's <title>, else a default. */
export function deliverableFilename(html: string): string {
  const title = /<title[^>]*>([^<]{1,80})<\/title>/i.exec(html)?.[1]?.trim()
  const slug = (title ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return `${slug || 'deliverable'}.html`
}

/** Where the server serves block `block` of assistant message `message` with
 *  its own sandbox CSP (scripts run in an opaque origin). */
export function previewUrl(conversationId: string, message: number, block: number): string {
  return `/api/chat/${encodeURIComponent(conversationId)}/preview?message=${message}&block=${block}`
}

export function HtmlPreview({
  html,
  conversationId,
  messageIndex,
  block,
  serverUrl,
  children,
}: {
  html: string
  /** Chat context: enables "Save to folder". Absent outside chat. */
  conversationId?: string
  runId?: string | null
  /** Which stored assistant message and HTML block this is: "Save to folder"
   *  sends these, and the server reads the bytes itself (never from the client). */
  messageIndex?: number
  block?: number
  /** Persisted messages: the server preview endpoint for this block. Absent for
   *  content the server does not have (shown via srcdoc, scripts inert). */
  serverUrl?: string
  /** The code view of the same source, rendered under the toolbar. */
  children?: ReactNode
}) {
  const [showPreview, setShowPreview] = useState(false)
  const filename = deliverableFilename(html)

  const openInTab = () => {
    if (serverUrl) {
      // The server sets a sandbox CSP on this response; no blob, no app origin.
      window.open(serverUrl, '_blank', 'noopener,noreferrer')
      return
    }
    // Not persisted: a blob of the WRAPPER page, never of the deliverable itself.
    const url = URL.createObjectURL(new Blob([buildWrapperPage(html, filename)], { type: 'text/html' }))
    window.open(url, '_blank', 'noopener,noreferrer')
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }

  const download = () => {
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }))
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
  }

  return (
    <div className="html-preview">
      <div className="html-preview-bar">
        <span className="html-preview-label">HTML</span>
        <button type="button" aria-pressed={showPreview} onClick={() => setShowPreview((v) => !v)}>
          {showPreview ? 'Hide preview' : 'Preview'}
        </button>
        <button type="button" onClick={openInTab}>
          Open in new tab
        </button>
        <button type="button" onClick={download}>
          Download .html
        </button>
        {conversationId && messageIndex !== undefined && (
          <SaveToFolder
            filename={filename}
            conversationId={conversationId}
            messageIndex={messageIndex}
            block={block ?? null}
          />
        )}
      </div>
      <div className="html-preview-status" role="note">
        Preview of model-generated content. Don&apos;t enter passwords or personal details into it.
      </div>
      {showPreview &&
        (serverUrl ? (
          <iframe className="html-preview-frame" title="HTML preview" sandbox={HTML_SANDBOX} src={serverUrl} />
        ) : (
          <>
            <iframe className="html-preview-frame" title="HTML preview" sandbox={HTML_SANDBOX} srcDoc={html} />
            {conversationId && (
              <div className="html-preview-status">Interactive parts run once the reply is saved.</div>
            )}
          </>
        ))}
      {children}
    </div>
  )
}

function SaveToFolder({
  filename,
  conversationId,
  messageIndex,
  block,
}: {
  filename: string
  conversationId: string
  messageIndex: number
  block: number | null
}) {
  const queryClient = useQueryClient()
  const targetsQuery = useQuery({
    queryKey: ['write-targets', conversationId],
    queryFn: () => chatFilesApi.writeTargets(conversationId),
    staleTime: 30_000,
    retry: false,
  })
  const targets = targetsQuery.data?.targets ?? []
  const [slug, setSlug] = useState('')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  // The lock is a ref (not the `saving` state) so a double click cannot post twice.
  const lock = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // Known to have nowhere to write: say why instead of offering a dead button.
  const noTarget = targetsQuery.isSuccess && targets.length === 0
  const reason =
    targetsQuery.data?.reason_if_none ??
    'No output folder set. Attach a folder to this session (+ → Add OneDrive folder) or add an output folder in Settings → Connections.'

  const save = async () => {
    if (lock.current) return
    lock.current = true
    setSaving(true)
    setMessage(null)
    try {
      const result = await chatFilesApi.saveFile(conversationId, {
        filename,
        message: messageIndex,
        block,
        target_slug: slug || targets[0]?.slug,
      })
      if (!mounted.current) return
      if (result.status === 'uploaded') {
        setMessage({ kind: 'ok', text: `Saved ${result.filename} to tret/${result.audit_filename ? ' (audit file written too)' : ''}` })
        void queryClient.invalidateQueries({ queryKey: ['conversation', conversationId] })
      } else {
        setMessage({ kind: 'err', text: result.error ?? 'Could not save the file' })
      }
    } catch (err) {
      if (mounted.current) setMessage({ kind: 'err', text: refusalMessage(err) })
    } finally {
      lock.current = false
      if (mounted.current) setSaving(false)
    }
  }

  return (
    <>
      {targets.length > 1 && (
        <select value={slug || targets[0].slug} onChange={(e) => setSlug(e.target.value)} aria-label="Folder to save to">
          {targets.map((t) => (
            <option key={t.slug} value={t.slug}>
              {t.label}
            </option>
          ))}
        </select>
      )}
      <button
        type="button"
        onClick={() => void save()}
        disabled={saving || noTarget}
        title={noTarget ? reason : 'Save this file into the folder (create-only, in a tret/ subfolder)'}
      >
        {saving ? 'Saving…' : 'Save to folder'}
      </button>
      {noTarget && !message && <span className="html-preview-status">{reason}</span>}
      {message && <span className={`html-preview-status ${message.kind}`}>{message.text}</span>}
    </>
  )
}
