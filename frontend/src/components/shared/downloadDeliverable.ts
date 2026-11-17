/** Download one export of a deliverable.
 *
 *  Fetch first, save only on success: a plain `<a href download>` hands the
 *  browser whatever the server answers with, and a 404/422 JSON `detail` body
 *  would be saved to disk as `tcfd.md` — a file that looks like the document and
 *  contains an error. Throws an `Error` whose message is the server's `detail`
 *  (or the HTTP status line), for the caller to show inline.
 *
 *  `theme` and `accent` are omitted from the URL when not given, so the server's
 *  own default (Report in the cohort edition, Plain otherwise) applies. */
export const EXPORT_FORMATS = [
  { format: 'markdown', label: 'Markdown', extension: 'md' },
  { format: 'html', label: 'HTML', extension: 'html' },
  { format: 'pdf', label: 'PDF', extension: 'pdf' },
  { format: 'docx', label: 'Word (.docx)', extension: 'docx' },
  { format: 'pptx', label: 'PowerPoint (.pptx)', extension: 'pptx' },
] as const

export type ExportFormat = (typeof EXPORT_FORMATS)[number]['format']
export type ExportTheme = 'plain' | 'report'

export function deliverableExportUrl(
  slug: string,
  format: string,
  opts: { includeDraft?: boolean; theme?: ExportTheme; accent?: string } = {},
): string {
  const params = new URLSearchParams({ format })
  if (opts.includeDraft) params.set('include_draft', 'true')
  if (opts.theme) params.set('theme', opts.theme)
  if (opts.accent) params.set('accent', opts.accent)
  return `/api/deliverables/${encodeURIComponent(slug)}/export?${params.toString()}`
}

export async function downloadDeliverable(
  slug: string,
  format: string,
  extension: string,
  opts: { includeDraft?: boolean; theme?: ExportTheme; accent?: string } = {},
): Promise<void> {
  const res = await fetch(deliverableExportUrl(slug, format, opts), { credentials: 'include' })
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
    throw new Error(detail)
  }
  const blob = await res.blob()
  const objectUrl = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = `${slug}.${extension}`
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(objectUrl)
  }
}
