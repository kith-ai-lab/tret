/** Share a batch, read-only, with a person (by email) or with another workspace
 *  of the current user. Every member can see the list of shares; creating and
 *  revoking is approver and above. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { api, type BatchShare } from '../../api/client'
import { useConfirm } from '../shared/ConfirmDialog'
import { formatDateTime } from '../shared/format'
import { ROLE_RANK } from '../shared/Lessons'
import { Modal } from '../shared/Modal'
import { errText } from './shared'

const SHARE_STATUS_COLOR: Record<string, string> = {
  pending: 'badge-amber',
  active: 'badge-green',
  declined: 'badge-gray',
  revoked: 'badge-gray',
}

function recipientLabel(s: BatchShare): string {
  if (s.recipient_workspace) return `${s.recipient_workspace.name} (workspace)`
  return s.recipient_email ?? 'Unknown recipient'
}

export function ShareBatchDialog({
  batchId,
  batchName,
  open,
  onClose,
}: {
  batchId: string
  batchName: string
  open: boolean
  onClose: () => void
}) {
  const askConfirm = useConfirm()
  const queryClient = useQueryClient()
  const emailId = useId()
  const wsId = useId()
  const [mode, setMode] = useState<'email' | 'workspace'>('email')
  const [email, setEmail] = useState('')
  const [workspaceId, setWorkspaceId] = useState('')
  const [error, setError] = useState<string | null>(null)

  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const me = meQuery.data
  const canManage = (ROLE_RANK[me?.role ?? 'analyst'] ?? 0) >= ROLE_RANK.approver
  // Sharing straight into a workspace needs the approver role there (the server answers
  // "Workspace not found" otherwise), so only those are offered.
  const otherWorkspaces = (me?.workspaces ?? []).filter(
    (w) => w.id !== me?.current_workspace_id && (ROLE_RANK[w.role] ?? 0) >= ROLE_RANK.approver,
  )

  const sharesQuery = useQuery({
    queryKey: ['batch-shares', batchId],
    queryFn: () => api.listBatchShares(batchId),
    enabled: open,
    retry: false,
  })
  const shares = sharesQuery.data ?? []

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['batch-shares', batchId] })

  const createMutation = useMutation({
    mutationFn: () =>
      mode === 'email'
        ? api.createBatchShare(batchId, { email: email.trim() })
        : api.createBatchShare(batchId, { workspace_id: workspaceId }),
    onMutate: () => setError(null),
    onSuccess: () => {
      setEmail('')
      setWorkspaceId('')
      refresh()
    },
    onError: (e) => setError(errText(e)),
  })
  const revokeMutation = useMutation({
    mutationFn: (shareId: string) => api.revokeBatchShare(batchId, shareId),
    onMutate: () => setError(null),
    onSuccess: refresh,
    onError: (e) => setError(errText(e)),
  })

  const revoke = async (s: BatchShare) => {
    const ok = await askConfirm({
      title: 'Revoke share',
      message: `Stop sharing "${batchName}" with ${recipientLabel(s)}? They lose access to the batch and its files straight away. Files they already copied into their own documents stay there.`,
      confirmLabel: 'Revoke',
      destructive: true,
    })
    if (ok) revokeMutation.mutate(s.id)
  }

  const formValid = mode === 'email' ? /\S+@\S+/.test(email.trim()) : workspaceId !== ''

  return (
    <Modal open={open} onClose={onClose} title="Share this batch" subtitle={batchName} width={720}>
      <div className="stack" style={{ gap: 14 }}>
        <div className="fine-print">
          The people you share with see the rows, their status and scores, what was reported for
          each row, and the files written for each row. They do not see costs, models, run
          transcripts, settings or the batch record files. Files they copy into their own
          workspace are marked as shared. The share is read-only, and it follows the batch as it
          changes. You can revoke it at any time.
        </div>

        {canManage ? (
          <form
            className="stack"
            style={{ gap: 10 }}
            onSubmit={(e) => {
              e.preventDefault()
              if (formValid) createMutation.mutate()
            }}
          >
            {otherWorkspaces.length > 0 && (
              <div className="row" style={{ gap: 14 }} role="radiogroup" aria-label="Share with">
                <label className="row" style={{ gap: 6 }}>
                  <input
                    type="radio"
                    name={`share-mode-${batchId}`}
                    checked={mode === 'email'}
                    onChange={() => setMode('email')}
                  />
                  <span>A person by email</span>
                </label>
                <label className="row" style={{ gap: 6 }}>
                  <input
                    type="radio"
                    name={`share-mode-${batchId}`}
                    checked={mode === 'workspace'}
                    onChange={() => setMode('workspace')}
                  />
                  <span>One of my other workspaces</span>
                </label>
              </div>
            )}
            {mode === 'email' || otherWorkspaces.length === 0 ? (
              <div className="field">
                <label className="mono-label" htmlFor={emailId}>
                  Email address
                </label>
                <input
                  id={emailId}
                  type="email"
                  autoComplete="off"
                  placeholder="name@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
                <div className="fine-print">
                  The share stays pending until they accept it in a workspace of theirs.
                </div>
              </div>
            ) : (
              <div className="field">
                <label className="mono-label" htmlFor={wsId}>
                  Workspace
                </label>
                <select id={wsId} value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)}>
                  <option value="">Choose a workspace</option>
                  {otherWorkspaces.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <div>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={!formValid || createMutation.isPending}
              >
                {createMutation.isPending ? 'Sharing…' : 'Share'}
              </button>
            </div>
          </form>
        ) : (
          <div className="fine-print">Only an approver or above can share or revoke.</div>
        )}

        {error && (
          <div className="error-text" role="alert">
            {error}
          </div>
        )}

        <div>
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Shared with
          </div>
          {sharesQuery.isLoading ? (
            <div className="fine-print pulse">Loading…</div>
          ) : sharesQuery.error ? (
            <div className="error-text">{errText(sharesQuery.error)}</div>
          ) : shares.length === 0 ? (
            <div className="fine-print">Not shared with anyone yet.</div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="mono-table">
                <thead>
                  <tr>
                    <th>Recipient</th>
                    <th>Status</th>
                    <th>Shared</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {shares.map((s) => (
                    <tr key={s.id}>
                      <td style={{ overflowWrap: 'anywhere' }}>
                        {recipientLabel(s)}
                        {s.recipient_workspace && s.recipient_email && (
                          <div className="fine-print">{s.recipient_email}</div>
                        )}
                      </td>
                      <td>
                        <span className={`badge ${SHARE_STATUS_COLOR[s.status] ?? 'badge-gray'}`}>
                          {s.status}
                        </span>
                      </td>
                      <td>{formatDateTime(s.created_at)}</td>
                      <td>
                        {canManage && (s.status === 'pending' || s.status === 'active') && (
                          <button
                            type="button"
                            className="btn btn-sm btn-reject"
                            disabled={revokeMutation.isPending}
                            aria-label={`Revoke share with ${recipientLabel(s)}`}
                            onClick={() => void revoke(s)}
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </Modal>
  )
}

