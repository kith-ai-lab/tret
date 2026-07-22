import type { ReactNode } from 'react'

/** Two-pane shell: narrow list on the left, detail on the right. */
export function ListDetail({
  list,
  detail,
  listWidth = 240,
}: {
  list: ReactNode
  detail: ReactNode
  listWidth?: number
}) {
  return (
    <div className="list-detail">
      <div className="list-detail-list" style={{ width: listWidth }}>
        {list}
      </div>
      <div className="list-detail-detail">{detail}</div>
    </div>
  )
}

export function ListItem({
  active,
  onClick,
  title,
  sub,
}: {
  active: boolean
  onClick: () => void
  title: ReactNode
  sub?: ReactNode
}) {
  return (
    <button className={`list-item${active ? ' active' : ''}`} onClick={onClick}>
      {title}
      {sub != null && <span className="sub">{sub}</span>}
    </button>
  )
}
