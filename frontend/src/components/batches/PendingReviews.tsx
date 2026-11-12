import { useMutation } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  api,
  type BatchDecideResult,
  type BatchPendingReview,
  type BatchTarget,
  type PackageMapPayload,
} from '../../api/client'
import { errText, ExtLink, shorten } from './shared'

function payloadSummary(payload: Record<string, unknown>): string {
  const v = payload.summary ?? payload.markdown
  const text = typeof v === 'string' ? v : JSON.stringify(payload)
  return shorten(text, 200)
}

/** Draft review findings of a batch with bulk approve / reject. Package-map
 *  findings (disclosure) keep their dedicated columns; anything else shows the
 *  finding type and a payload summary. */
export function PendingReviews({
  reviews,
  targets,
  onDecided,
}: {
  reviews: BatchPendingReview[]
  targets: BatchTarget[]
  onDecided: () => void
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [note, setNote] = useState('')
  const [packagingOnly, setPackagingOnly] = useState(false)
  const [packagingError, setPackagingError] = useState(false)
  const [results, setResults] = useState<BatchDecideResult[]>([])
  const labelById = useMemo(
    () => new Map(targets.map((t) => [t.id, t.label || t.company || t.slug])),
    [targets],
  )
  const packageMaps = reviews.length > 0 && reviews.every((m) => m.schema_slug === 'package_map')

  const hasPackageMap = reviews.some((m) => m.schema_slug === 'package_map')
  // The checkbox is hidden when no page map is pending; never leave it ticked.
  useEffect(() => {
    if (!hasPackageMap) setPackagingOnly(false)
  }, [hasPackageMap])

  const decide = useMutation({
    mutationFn: ({ ids, action }: { ids: string[]; action: 'approve' | 'reject' }) =>
      api.decideFindingsBatch(
        ids,
        action,
        note.trim() || undefined,
        action === 'approve' && packagingOnly && hasPackageMap ? 'packaging' : undefined,
      ),
    onSuccess: (r) => {
      setPackagingOnly(false)
      setPackagingError(false)
      setResults(r.results)
      setSelected(new Set())
      onDecided()
    },
  })

  const ids = reviews.map((m) => m.id)
  const allSelected = ids.length > 0 && ids.every((i) => selected.has(i))
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const chosen = ids.filter((i) => selected.has(i))
  const failures = results.filter((r): r is Extract<BatchDecideResult, { ok: false }> => !r.ok)

  return (
    <div style={{ marginBottom: 20 }}>
      <div className="mono-label" style={{ marginBottom: 6 }}>
        {packageMaps ? 'Package maps' : 'Findings'} awaiting review ({reviews.length})
      </div>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
        <label className="check-row">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => setSelected(allSelected ? new Set() : new Set(ids))}
          />
          <span>Select all</span>
        </label>
        <input
          type="text"
          placeholder="Note (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          style={{ flex: '1 1 160px', minWidth: 0 }}
        />
        <button
          className="btn btn-approve"
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => {
            const packaging = packagingOnly && hasPackageMap
            if (
              packaging &&
              !chosen.every((id) => reviews.find((m) => m.id === id)?.schema_slug === 'package_map')
            ) {
              setPackagingError(true)
              return
            }
            setPackagingError(false)
            decide.mutate({ ids: chosen, action: 'approve' })
          }}
        >
          Approve selected ({chosen.length})
        </button>
        <button
          className="btn btn-reject"
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => decide.mutate({ ids: chosen, action: 'reject' })}
        >
          Reject selected
        </button>
      </div>
      {hasPackageMap && (
        <label
          className="check-row"
          style={{ marginBottom: 8 }}
          title="Use when you are approving page maps only to package them for scoring. These approvals are not counted as quality evidence for the model."
        >
          <input
            type="checkbox"
            checked={packagingOnly}
            onChange={(e) => {
                setPackagingOnly(e.target.checked)
                setPackagingError(false)
              }}
          />
          <span>Approve for packaging only (does not count as a review)</span>
        </label>
      )}
      {packagingError && (
        <div className="error-text">
          Packaging-only approval applies to page maps. Deselect the other items or untick the box.
        </div>
      )}
      {decide.error && <div className="error-text">{errText(decide.error)}</div>}
      {failures.map((f) => (
        <div key={f.id} className="error-text">
          {labelById.get(reviews.find((m) => m.id === f.id)?.target_id ?? '') ?? f.id}: {f.error}
        </div>
      ))}

      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            {packageMaps ? (
              <tr>
                <th />
                <th>Company</th>
                <th>Stem</th>
                <th>Role / scope</th>
                <th>Keep ranges</th>
                <th className="num">Pages</th>
                <th>Source</th>
                <th>Review note</th>
                <th />
              </tr>
            ) : (
              <tr>
                <th />
                <th>Finding type</th>
                <th>Target</th>
                <th>Summary</th>
                <th />
              </tr>
            )}
          </thead>
          <tbody>
            {reviews.map((m) => {
              const checkbox = (
                <td>
                  <input
                    type="checkbox"
                    checked={selected.has(m.id)}
                    onChange={() => toggle(m.id)}
                    aria-label={`Select ${labelById.get(m.target_id) ?? m.id}`}
                  />
                </td>
              )
              const full = (
                <td>
                  <Link to={`/approvals?finding=${m.id}`}>Full view</Link>
                </td>
              )
              if (!packageMaps) {
                return (
                  <tr key={m.id}>
                    {checkbox}
                    <td>{m.schema_slug}</td>
                    <td>{labelById.get(m.target_id) ?? '—'}</td>
                    <td style={{ maxWidth: 480, overflowWrap: 'anywhere' }}>
                      {payloadSummary(m.payload)}
                    </td>
                    {full}
                  </tr>
                )
              }
              const p = m.payload as unknown as PackageMapPayload
              const map = p.map
              return (
                <tr key={m.id}>
                  {checkbox}
                  <td>{labelById.get(m.target_id) ?? '—'}</td>
                  <td>{p.stem}</td>
                  <td>
                    {map.role} / {map.scope}
                  </td>
                  <td>
                    {map.keep_ranges
                      ? map.keep_ranges.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(', ')
                      : 'whole file'}
                  </td>
                  <td className="num">
                    {p.selected_page_count}/{p.source.page_count ?? '?'}
                  </td>
                  <td style={{ overflowWrap: 'anywhere', maxWidth: 240 }}>
                    <ExtLink url={p.source.url}>{p.source.url ?? p.source.filename}</ExtLink>
                  </td>
                  <td style={{ maxWidth: 280 }}>{map.review_note ?? '—'}</td>
                  {full}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
