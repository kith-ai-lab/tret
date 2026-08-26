import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'

/** Landing page for an invite link, `/invite/{token}`.
 *
 *  This view only ever mounts once `me` has resolved successfully — App.tsx
 *  shows the login screen in its place, at this same URL, for anyone not yet
 *  signed in, and Login.tsx carries the token through as `?next=` so a fresh
 *  sign-in (password or SSO) lands back here without the token ever needing
 *  to be stored anywhere.
 *
 *  Accepting is a deliberate, explicit step, not something that fires on
 *  mount: it joins a workspace *and* switches this session's current one, so
 *  a stray click or a link opened out of curiosity should not be enough to
 *  do either. There is no endpoint to preview what the token names before
 *  accepting it, so the confirmation copy stays generic rather than
 *  pretending to know. */
export function InviteAccept() {
  const { token } = useParams<{ token: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const acceptMutation = useMutation({
    mutationFn: () => api.acceptInvite(token!),
    onSuccess: () => {
      // Same rigor as switching workspaces or logging out: the invite put
      // this session in a new workspace, so nothing cached from before it
      // may survive — cleared immediately, before the joined state ever
      // renders, so no stale prior-workspace data shows even for an instant.
      queryClient.clear()
      // `clear()` above wipes `me` along with everything else, but any
      // still-mounted observer of it (the sidebar's workspace switcher)
      // needs telling to refetch — otherwise it just keeps showing nothing
      // until something else happens to touch that query. Invalidating
      // (rather than a full remount) lets it pick up the new workspace on
      // its own.
      queryClient.invalidateQueries({ queryKey: ['me'] })
    },
  })

  const error = acceptMutation.error as ApiError | null
  const joined = acceptMutation.data
  const workspaceName = joined?.workspaces.find((w) => w.id === joined.current_workspace_id)?.name

  const decline = () => navigate('/', { replace: true })

  return (
    <div className="stack" style={{ gap: 20, maxWidth: 480 }}>
      <div>
        <h1 className="view-title">Workspace invite</h1>
        <div className="view-sub">
          {acceptMutation.isIdle
            ? 'Review the invite before joining.'
            : acceptMutation.isSuccess
              ? 'You are in.'
              : acceptMutation.isError
                ? 'Could not accept this invite.'
                : 'Accepting your invite…'}
        </div>
      </div>

      <div className="panel">
        {acceptMutation.isIdle ? (
          <div className="stack" style={{ gap: 14 }}>
            <div className="mono-body">Accept invitation to join this workspace?</div>
            <div className="row" style={{ gap: 10 }}>
              <button
                type="button"
                className="btn btn-primary"
                disabled={acceptMutation.isPending}
                onClick={() => acceptMutation.mutate()}
              >
                Accept
              </button>
              <button type="button" className="btn" onClick={decline}>
                Decline
              </button>
            </div>
          </div>
        ) : acceptMutation.isPending ? (
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
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => navigate('/', { replace: true })}
            >
              Go to workspace
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
