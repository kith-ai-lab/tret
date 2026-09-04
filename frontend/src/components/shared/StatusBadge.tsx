const STATUS_COLOR: Record<string, string> = {
  // runs
  queued: 'badge-gray',
  running: 'badge-blue',
  completed: 'badge-green',
  // The loop finished but produced no assistant output — not a failure, not a
  // clean success. Amber reads truer than the gray fallback.
  completed_without_output: 'badge-amber',
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
  // egress classes. `on` is green because reaching a provider is the normal,
  // working state — not because open egress is inherently good.
  on: 'badge-green',
  replay: 'badge-amber',
  off: 'badge-gray',
  // marketplace submission / review state machine (Plan Phase B): draft ->
  // submitted -> in_review -> {approved -> listed | changes_requested ->
  // submitted | rejected}; listed -> delisted. `approved`/`rejected` are
  // shared with the findings states above — same colors apply.
  submitted: 'badge-blue',
  in_review: 'badge-amber',
  listed: 'badge-green',
  changes_requested: 'badge-amber',
  delisted: 'badge-gray',
  // workspace connections (Google Drive / Microsoft 365): 'active' shares
  // the same green as 'completed'/'approved'/'fulfilled'; 'error' shares the
  // same red as 'failed'/'rejected' — a dead refresh token is a failure
  // state, not a neutral one.
  active: 'badge-green',
  error: 'badge-red',
}

export function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_COLOR[status] ?? 'badge-gray'
  return (
    <span className={`badge ${cls}${status === 'running' ? ' pulse' : ''}`}>{status}</span>
  )
}
