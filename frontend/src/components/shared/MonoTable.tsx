import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: ReactNode
  align?: 'left' | 'right'
  render: (row: T) => ReactNode
}

/** Dense mono audit table with optional row click.
 *
 *  **A clickable row is keyboard-operable.** `<tr>` has no native activation
 *  semantics, so one is supplied: `role="button"`, `tabIndex={0}`, and Enter/Space
 *  handlers. Without them the run list — the primary way into a run's audit
 *  record — was reachable only with a mouse. Space also gets `preventDefault` so
 *  activating a row does not scroll the page underneath it.
 *
 *  `rowLabel` gives the row an accessible name; without it a screen reader reads
 *  the concatenated cells, which for a dense audit row is a wall of numbers.
 *
 *  A row's own interactive contents (the routing disclosure, a link) stop their
 *  events from propagating, so they act instead of the row.
 *
 *  `error` outranks `empty`: a failed fetch has no rows either, and rendering the
 *  empty state for it tells the user their data does not exist when in fact it
 *  could not be loaded. */
export function MonoTable<T>({
  columns,
  rows,
  rowKey,
  rowLabel,
  onRowClick,
  empty,
  error,
}: {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string
  rowLabel?: (row: T) => string
  onRowClick?: (row: T) => void
  empty?: ReactNode
  error?: unknown
}) {
  if (error) {
    return <QueryError error={error} />
  }
  if (rows.length === 0) {
    return <div className="empty">{empty ?? 'Nothing here yet.'}</div>
  }
  return (
    <table className="mono-table">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.key} className={c.align === 'right' ? 'num' : undefined}>
              {c.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={rowKey(row)}
            className={onRowClick ? 'clickable' : undefined}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            role={onRowClick ? 'button' : undefined}
            tabIndex={onRowClick ? 0 : undefined}
            aria-label={onRowClick ? rowLabel?.(row) : undefined}
            onKeyDown={
              onRowClick
                ? (e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return
                    // The event may have come from something inside the row that
                    // handles its own keys; only the row itself activates.
                    if (e.target !== e.currentTarget) return
                    e.preventDefault()
                    onRowClick(row)
                  }
                : undefined
            }
          >
            {columns.map((c) => (
              <td key={c.key} className={c.align === 'right' ? 'num' : undefined}>
                {c.render(row)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** One place that turns a react-query error into prose, so "this failed" never
 *  gets rendered by the branch that means "there is nothing here". */
export function QueryError({ error, what }: { error: unknown; what?: string }) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="error-text">
      Could not load {what ?? 'this'} — {message}
    </div>
  )
}
