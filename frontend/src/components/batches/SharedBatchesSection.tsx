/** The two blocks the batches list shows for sharing: pending incoming shares
 *  (accept or decline) and the batches shared with the current workspace. Both
 *  stay hidden when empty or when the server does not know about sharing. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api, type IncomingBatchShare } from '../../api/client'
import { useConfirm } from '../shared/ConfirmDialog'
import { formatDateTime } from '../shared/format'
import { ROLE_RANK } from '../shared/Lessons'
import { StatusBadge } from '../shared/StatusBadge'
import { fmtScore } from './evalHelpers'
import { errText } from './shared'

function useCanApprove(): boolean {
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  return (ROLE_RANK[meQuery.data?.role ?? 'analyst'] ?? 0) >= ROLE_RANK.approver
}

function person(p: { email: string; name?: string | null } | null): string {
  if (!p) return 'someone'
  return p.name ? `${p.name} (${p.email})` : p.email
}

export function IncomingSharesNotice() {
  const askConfirm = useConfirm()
  const queryClient = useQueryClient()
  const canApprove = useCanApprove()
  const [error, setError] = useState<string | null>(null)
  const query = useQuery({
    queryKey: ['batch-shares-incoming'],
    queryFn: api.listIncomingBatchShares,
    retry: false,
  })
  const done = () => {
    void queryClient.invalidateQueries({ queryKey: ['batch-shares-incoming'] })
    void queryClient.invalidateQueries({ queryKey: ['shared-batches'] })
  }
  const accept = useMutation({
    mutationFn: (id: string) => api.acceptBatchShare(id),
    onMutate: () => setError(null),
    onSuccess: done,
    onError: (e) => setError(errText(e)),
  })
  const decline = useMutation({
    mutationFn: (id: string) => api.declineBatchShare(id),
    onMutate: () => setError(null),
    onSuccess: done,
    onError: (e) => setError(errText(e)),
  })
  const onDecline = async (s: IncomingBatchShare) => {
    const ok = await askConfirm({
      title: 'Decline shared batch',
      message: `Decline "${s.batch.name}" from ${s.from_workspace.name}? It will not appear in this workspace.`,
      confirmLabel: 'Decline',
      destructive: true,
    })
    if (ok) decline.mutate(s.id)
  }

  const shares = query.data ?? []
  if (shares.length === 0) return null
  const busy = accept.isPending || decline.isPending

  return (
    <section className="panel" style={{ margin: '14px 0' }} aria-label="Shared with you">
      <div className="mono-label">Shared with you</div>
      <div className="fine-print">
        Another workspace wants to share a batch with you. Accepting adds it to this workspace as a
        read-only view.
        {!canApprove && ' An approver of this workspace must accept it.'}
      </div>
      <ul style={{ margin: '8px 0 0', padding: 0, listStyle: 'none' }} className="stack">
        {shares.map((s) => (
          <li key={s.id} className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
            <div style={{ flex: '1 1 260px', minWidth: 0 }}>
              <div className="mono-body" style={{ overflowWrap: 'anywhere' }}>
                {s.batch.name}
              </div>
              <div className="fine-print">
                From {s.from_workspace.name}, shared by {person(s.shared_by)} ·{' '}
                {s.batch.counts.total} rows · {formatDateTime(s.created_at)}
              </div>
            </div>
            {canApprove && (
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={busy}
                aria-label={`Accept ${s.batch.name}`}
                onClick={() => accept.mutate(s.id)}
              >
                Accept
              </button>
            )}
            <button
              type="button"
              className="btn btn-sm"
              disabled={busy}
              aria-label={`Decline ${s.batch.name}`}
              onClick={() => void onDecline(s)}
            >
              Decline
            </button>
          </li>
        ))}
      </ul>
      {error && (
        <div className="error-text" role="alert" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
    </section>
  )
}

export function SharedBatchesTable() {
  const query = useQuery({
    queryKey: ['shared-batches'],
    queryFn: api.listSharedBatches,
    retry: false,
  })
  const rows = query.data ?? []
  if (rows.length === 0) return null

  return (
    <section style={{ marginTop: 24 }} aria-label="Shared with this workspace">
      <div className="mono-label" style={{ marginBottom: 6 }}>
        Shared with this workspace ({rows.length})
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="mono-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>From workspace</th>
              <th>Status</th>
              <th className="num">Rows</th>
              <th className="num">Score</th>
              <th className="num">Files</th>
              <th>Shared</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => (
              <tr key={b.id}>
                <td>
                  <Link to={`/batches/shared/${b.id}`}>{b.name}</Link>
                </td>
                <td>{b.share.from_workspace.name}</td>
                <td>
                  <StatusBadge status={b.status} />
                </td>
                <td className="num">{b.counts.total}</td>
                <td className="num">{fmtScore(b.quality_mean)}</td>
                <td className="num">{b.file_count}</td>
                <td>{formatDateTime(b.share.shared_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
