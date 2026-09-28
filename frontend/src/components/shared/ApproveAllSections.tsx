import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api, ApiError } from '../../api/client'

/** "Approve all draft sections" for one deliverable.
 *
 *  Deliberately not a bulk endpoint: it calls the same per-finding
 *  `POST /findings/{id}/approval` the Approve button uses, once per section,
 *  so every section still gets its own named decision in the audit record and
 *  the server still enforces the approver role on each one. The sections come
 *  from `GET /deliverables`, which already resolves the latest finding per
 *  section — the same rule export assembly uses — so a superseded older draft
 *  of a section is never swept in. A two-step confirm makes the batch a
 *  deliberate act rather than a stray click. */
export function ApproveAllSections({
  deliverableSlug,
  minDrafts = 1,
}: {
  deliverableSlug: string
  /** Hide the control below this many draft sections (the Approvals page
   *  passes 2: with one draft, its own Approve button already does the job). */
  minDrafts?: number
}) {
  const queryClient = useQueryClient()
  const deliverablesQuery = useQuery({ queryKey: ['deliverables'], queryFn: api.listDeliverables })
  // Same role gate PublishToSharePointButton uses: the server enforces it,
  // this only avoids offering a button that would 403.
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const canApprove = ['owner', 'admin', 'approver'].includes(meQuery.data?.role ?? '')

  const [confirming, setConfirming] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ approved: number; skipped: string[]; failed: string[] } | null>(
    null,
  )

  useEffect(() => {
    setConfirming(false)
    setNote('')
    setResult(null)
  }, [deliverableSlug])

  const deliverable = deliverablesQuery.data?.find((d) => d.slug === deliverableSlug)
  const drafts = deliverable?.sections.filter((s) => s.status === 'draft') ?? []

  const approveAll = async () => {
    setBusy(true)
    const failed: string[] = []
    const skipped: string[] = []
    let approved = 0
    // Tagged so the audit record shows these came from the batch action, not
    // from a section-by-section read.
    const auditNote = note.trim()
      ? `${note.trim()} (via "Approve all sections")`
      : 'Approved via "Approve all sections"'
    // Re-read the deliverable right before deciding: the list shown at confirm
    // time can be stale, and approving a draft that a newer draft of the same
    // section has since superseded would ship the older text (export takes the
    // latest *approved* finding per section). Only sections whose latest draft
    // is still the one the user confirmed are approved; anything that changed
    // is left for a section-by-section review.
    const confirmed = new Map(drafts.map((s) => [s.section, s.finding_id]))
    let fresh = drafts
    try {
      const latest = await queryClient.fetchQuery({
        queryKey: ['deliverables'],
        queryFn: api.listDeliverables,
        staleTime: 0,
      })
      const now = latest.find((d) => d.slug === deliverableSlug)?.sections ?? []
      fresh = now.filter((s) => s.status === 'draft' && confirmed.get(s.section) === s.finding_id)
      for (const [section, id] of confirmed) {
        if (!fresh.some((s) => s.finding_id === id)) skipped.push(`${section} (changed since you confirmed)`)
      }
    } catch {
      // Could not re-check: fall back to exactly what the user confirmed.
    }
    // Sequential, not Promise.all: each decision takes a row lock server-side,
    // and a partial failure should be reported per section, in order.
    for (const s of fresh) {
      try {
        await api.decideFinding(s.finding_id, 'approve', auditNote)
        approved++
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          skipped.push(`${s.section} (already decided)`)
          continue
        }
        const reason =
          e instanceof ApiError && e.status === 403
            ? 'requires approver role'
            : e instanceof Error
              ? e.message
              : String(e)
        failed.push(`${s.section}: ${reason}`)
      }
    }
    setBusy(false)
    setConfirming(false)
    setNote('')
    setResult({ approved, skipped, failed })
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['deliverables'] }),
      queryClient.invalidateQueries({ queryKey: ['deliverable-export', deliverableSlug] }),
      queryClient.invalidateQueries({ queryKey: ['findings'] }),
      queryClient.invalidateQueries({ queryKey: ['finding'] }),
    ])
  }

  const resultLine = result && (
    <div
      className={result.failed.length ? 'error-text' : undefined}
      style={{ marginTop: 8, fontFamily: 'var(--mono)', fontSize: 'var(--fs-xs)' }}
    >
      Approved {result.approved} section{result.approved === 1 ? '' : 's'}
      {result.skipped.length > 0 && ` · skipped: ${result.skipped.join(', ')}`}
      {result.failed.length > 0 && ` · not approved: ${result.failed.join('; ')}`}
    </div>
  )

  if (!canApprove || drafts.length < minDrafts) return resultLine || null

  if (!confirming) {
    return (
      <div style={{ marginTop: 10 }}>
        <button type="button" className="btn btn-sm" onClick={() => setConfirming(true)}>
          Approve all {drafts.length} draft section{drafts.length === 1 ? '' : 's'} of {deliverableSlug}
        </button>
        {resultLine}
      </div>
    )
  }

  return (
    <div className="panel" style={{ marginTop: 10 }}>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Approve {drafts.length} section{drafts.length === 1 ? '' : 's'} of {deliverableSlug}?
      </div>
      <div style={{ fontSize: 'var(--fs-sm)', marginBottom: 8 }}>
        {drafts.map((s) => s.section).join(', ')}. Each is recorded as your own approval, and approved
        sections go into the exported document.
      </div>
      <div className="field">
        <textarea
          rows={2}
          placeholder="Optional note, added to every section's audit record…"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={busy}
        />
      </div>
      <div className="row">
        <button type="button" className="btn btn-approve" onClick={() => void approveAll()} disabled={busy}>
          {busy ? 'Approving…' : `Approve ${drafts.length}`}
        </button>
        <button type="button" className="btn" onClick={() => setConfirming(false)} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  )
}
