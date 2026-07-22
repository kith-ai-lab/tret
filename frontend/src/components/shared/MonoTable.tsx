import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: ReactNode
  align?: 'left' | 'right'
  render: (row: T) => ReactNode
}

/** Dense mono audit table with optional row click. */
export function MonoTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  empty,
}: {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string
  onRowClick?: (row: T) => void
  empty?: ReactNode
}) {
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
