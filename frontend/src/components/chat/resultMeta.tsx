/** Shared vocabulary for chat result cards and the document pane: what a
 *  finding is called, what to title it, and how its status reads. Kept apart
 *  from both components so neither has to import the other. */
import type { ConnectedWritePayload, Finding } from '../../api/client'

/** Finding schemas that are machinery rather than a result a reader opens:
 *  a package map is a page selection approved only to be packaged, and has its
 *  own flow on the Approvals page. */
const NOT_A_RESULT = new Set(['package_map'])

export function isResult(f: Finding): boolean {
  return !NOT_A_RESULT.has(f.schema_slug)
}

function humanize(s: string): string {
  const spaced = s.replace(/[_-]+/g, ' ').trim()
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : s
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null
}

/** "Drafted section" | "Finding" | "Verdict" | "File for <drive>". */
export function resultLabel(f: Finding): string {
  if (f.schema_slug === 'draft_section') return 'Drafted section'
  if (f.schema_slug === 'connected_write') {
    const label = str((f.payload as Partial<ConnectedWritePayload>).target_label)
    return `File for ${label ?? 'connected drive'}`
  }
  if (f.schema_slug.includes('verdict')) return 'Verdict'
  return 'Finding'
}

export function resultTitle(f: Finding): string {
  if (f.schema_slug === 'draft_section') {
    return humanize(str(f.subject.section) ?? str(f.subject.deliverable) ?? 'Untitled section')
  }
  if (f.schema_slug === 'connected_write') {
    const name = str((f.payload as Partial<ConnectedWritePayload>).filename) ?? str(f.subject.filename)
    return name ? (name.split('/').pop() ?? name) : 'Untitled file'
  }
  const parts = Object.values(f.subject).filter((v): v is string => typeof v === 'string' && !!v)
  return parts.length > 0 ? parts.join(' · ') : humanize(f.schema_slug)
}

/** The secondary line: the deliverable a section belongs to, or where a file
 *  is going. Null when there is nothing useful to add. */
export function resultSubtitle(f: Finding): string | null {
  if (f.schema_slug === 'draft_section') return str(f.subject.deliverable)
  if (f.schema_slug === 'connected_write') {
    const p = f.payload as Partial<ConnectedWritePayload>
    return str(p.relative_dir) ? `in ${p.relative_dir}` : null
  }
  return f.schema_slug.includes('verdict') ? humanize(f.schema_slug) : null
}

export interface StatusChipSpec {
  label: string
  badge: string
}

/** `auto` is a finding approved by workspace policy (approval purpose
 *  "auto"), which reads differently from a person's decision. */
export function statusChipSpec(status: string, auto: boolean): StatusChipSpec {
  switch (status) {
    case 'draft':
      return { label: 'Draft', badge: 'badge-amber' }
    case 'approved':
      return auto
        ? { label: 'Approved automatically', badge: 'badge-green' }
        : { label: 'Approved', badge: 'badge-green' }
    case 'rejected':
      return { label: 'Rejected', badge: 'badge-red' }
    case 'noted':
      return { label: 'Noted', badge: 'badge-gray' }
    case 'superseded':
      return { label: 'Superseded', badge: 'badge-gray' }
    default:
      return { label: humanize(status), badge: 'badge-gray' }
  }
}

export function ResultStatusChip({ status, auto }: { status: string; auto: boolean }) {
  const { label, badge } = statusChipSpec(status, auto)
  return <span className={`badge ${badge}`}>{label}</span>
}
