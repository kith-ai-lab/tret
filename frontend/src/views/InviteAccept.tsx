import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'

/** Landing page for an invite link, `/invite/{token}`.
 *
 *  This view only ever mounts once `me` has resolved successfully — App.tsx
 *  shows the login screen in its place, at this same URL, for anyone not yet
 *  signed in, and Login.tsx carries the token through as `?next=` so a fresh
 *  sign-in (password or SSO) lands back here without the token ever needing
 *  to be stored anywhere. */
export function InviteAccept() {
  const { token } = useParams<{ token: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const acceptMutation = useMutation({
    mutationFn: () => api.acceptInvite(token!),
  })

  useEffect(() => {
    if (token) acceptMutation.mutate()
    // Fire once per token; the mutation object is stable across renders and
    // does not belong in this dependency list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  const error = acceptMutation.error as ApiError | null
  const joined = acceptMutation.data
  const workspaceName = joined?.workspaces.find((w) => w.id === joined.current_workspace_id)?.name

  const goToWorkspace = () => {
    // Same rigor as switching workspaces or logging out: the invite put this
    // session in a new workspace, so nothing cached from before it may survive.
    queryClient.clear()
    navigate('/', { replace: true })
  }

  return (
    <div className="stack" style={{ gap: 20, maxWidth: 480 }}>
      <div>
        <h1 className="view-title">Workspace invite</h1>
        <div className="view-sub">
          {acceptMutation.isSuccess ? 'You are in.' : 'Accepting your invite…'}
        </div>
      </div>

      <div className="panel">
        {acceptMutation.isPending || acceptMutation.isIdle ? (
          <div className="empty pulse">Joining the workspace…</div>
        ) : acceptMutation.isError ? (
          <div className="stack" style={{ gap: 14 }}>
            <div className="error-text">
              {error?.message ?? 'Could not accept this invite — it may be expired or already used.'}
            </div>
            <Link className="btn" to="/">
              Back to tret
            </Link>
          </div>
        ) : (
          <div className="stack" style={{ gap: 14 }}>
            <div className="mono-body">
              {workspaceName ? (
                <>
                  You've joined <strong>{workspaceName}</strong>.
                </>
              ) : (
                "You've joined the workspace."
              )}
            </div>
            <button type="button" className="btn btn-primary" onClick={goToWorkspace}>
              Go to workspace
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
