/** The emissions-factor change log — a collapsed "Change history" section at
 *  the bottom of the Emissions factors panel.
 *
 *  Same capability-gate pattern as the rest of the tret-cloud surfaces:
 *  `GET /api/billing/emissions/history` 404s when the extension is not
 *  loaded, and that is not an error, it is "this UI does not exist on this
 *  deployment" — the whole section renders nothing. A 403 (the endpoint is
 *  admin/owner only, same as editing the factors themselves) is a different
 *  state: the section header stays, with a one-line note instead of a table.
 *
 *  `changed_keys` are dotted paths into the overrides document (e.g.
 *  `grid.tables.us_west.csv`); the before/after view renders only the values
 *  at those paths, not the whole document, and a `grid.tables[*].csv` value —
 *  replaced server-side by `{ chars, sha256 }` — renders as "csv · N chars"
 *  rather than a hash a person can't read anyway.
 */
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { api, ApiError, type EmissionsFactorHistoryEntry } from '../../api/client'
import { formatDateTime } from '../shared/format'

const PAGE_SIZE = 50
const MAX_LIMIT = 200

/** Reads the value at a dotted path (e.g. "grid.tables.us_west.csv") out of an
 *  overrides document. `undefined` when the path does not exist in this
 *  particular before/after snapshot — a key can be new in `after` and absent
 *  from `before`, or vice versa for a delete. */
function valueAtPath(doc: Record<string, unknown> | null, path: string): unknown {
  if (!doc) return undefined
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc && typeof acc === 'object' && !Array.isArray(acc) && key in (acc as object)) {
      return (acc as Record<string, unknown>)[key]
    }
    return undefined
  }, doc)
}

function isCsvPlaceholder(v: unknown): v is { chars: number; sha256: string } {
  return (
    !!v &&
    typeof v === 'object' &&
    !Array.isArray(v) &&
    typeof (v as Record<string, unknown>).chars === 'number' &&
    typeof (v as Record<string, unknown>).sha256 === 'string'
  )
}

function renderPathValue(v: unknown): string {
  if (v === undefined) return '(unset)'
  if (v === null) return 'null'
  if (isCsvPlaceholder(v)) return `csv · ${v.chars.toLocaleString('en-US')} chars`
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function EntryDetail({ entry }: { entry: EmissionsFactorHistoryEntry }) {
  return (
    <div className="tool-body">
      <table className="mono-table">
        <thead>
          <tr>
            <th>Field</th>
            <th>Before</th>
            <th>After</th>
          </tr>
        </thead>
        <tbody>
          {entry.changed_keys.map((key) => (
            <tr key={key}>
              <td style={{ fontFamily: 'var(--mono)', fontSize: 'var(--fs-xs)' }}>{key}</td>
              <td>{renderPathValue(valueAtPath(entry.before, key))}</td>
              <td>{renderPathValue(valueAtPath(entry.after, key))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function HistoryEntryRow({ entry }: { entry: EmissionsFactorHistoryEntry }) {
  return (
    <details className="tool-row">
      <summary>
        <span style={{ color: 'var(--text-muted)' }}>{formatDateTime(entry.created_at)}</span>
        <span className="tool-name">{entry.user_email ?? entry.user_id ?? 'unknown user'}</span>
        <span style={{ color: 'var(--text-muted)', fontSize: 'var(--fs-xs)' }}>{entry.action}</span>
        <span className="row" style={{ gap: 4, flexWrap: 'wrap', flex: 1 }}>
          {entry.changed_keys.map((key) => (
            <span key={key} className="chip" style={{ fontSize: 'var(--fs-2xs)', padding: '1px 7px' }}>
              {key}
            </span>
          ))}
        </span>
      </summary>
      <EntryDetail entry={entry} />
    </details>
  )
}

export function EmissionsHistory() {
  const [limit, setLimit] = useState(PAGE_SIZE)
  const historyQuery = useQuery({
    queryKey: ['emissions-factor-history', limit],
    queryFn: () => api.getEmissionsFactorHistory(limit),
    retry: false,
  })

  const error = historyQuery.error as ApiError | null
  // Capability gate: the extension isn't loaded on this deployment, so there
  // is no history UI to show at all — same rule BillingSection follows for
  // /billing/status.
  if (error && error.status === 404) return null

  if (error && error.status === 403) {
    return (
      <div style={{ marginTop: 18 }}>
        <div className="mono-label" style={{ marginBottom: 8 }}>
          Change history
        </div>
        <div className="fine-print">Admin only — ask an admin or owner to view this.</div>
      </div>
    )
  }

  // Still loading, or some other failure (network hiccup, 500) — say nothing
  // rather than surface an error for a section nobody asked to see yet.
  if (historyQuery.isLoading || error || !historyQuery.data) return null

  const entries = historyQuery.data.entries
  const canLoadMore = limit < MAX_LIMIT && entries.length >= limit

  return (
    <div style={{ marginTop: 18 }}>
      <details>
        <summary className="mono-label" style={{ cursor: 'pointer' }}>
          Change history
        </summary>
        <div className="stack" style={{ gap: 6, marginTop: 10 }}>
          {entries.length === 0 ? (
            <div className="fine-print">No changes recorded yet.</div>
          ) : (
            entries.map((entry) => <HistoryEntryRow key={entry.id} entry={entry} />)
          )}
          {canLoadMore && (
            <div>
              <button
                type="button"
                className="btn btn-sm"
                disabled={historyQuery.isFetching}
                onClick={() => setLimit((l) => Math.min(MAX_LIMIT, l + PAGE_SIZE))}
              >
                {historyQuery.isFetching ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </div>
      </details>
    </div>
  )
}
