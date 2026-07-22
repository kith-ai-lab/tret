const STATUS_COLOR: Record<string, string> = {
  // runs
  queued: 'badge-gray',
  running: 'badge-blue',
  completed: 'badge-green',
  failed: 'badge-red',
  cancelled: 'badge-gray',
  // findings
  draft: 'badge-amber',
  approved: 'badge-green',
  rejected: 'badge-red',
  // documents
  pending: 'badge-gray',
  done: 'badge-green',
  // data requests
  open: 'badge-amber',
  fulfilled: 'badge-green',
  dismissed: 'badge-gray',
}

export function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_COLOR[status] ?? 'badge-gray'
  return (
    <span className={`badge ${cls}${status === 'running' ? ' pulse' : ''}`}>{status}</span>
  )
}
