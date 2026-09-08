import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import {
  api,
  ApiError,
  gateRefusalDetail,
  type ConnectionActivityItem,
  type ConnectionProvider,
  type ConnectionProviderInfo,
  type M365ReadEntry,
  type WorkspaceConnection,
  type WriteTarget,
} from '../api/client'
import { M365ReadAccessPicker } from '../components/connections/M365ReadAccessPicker'
import { M365WriteTargetPicker } from '../components/connections/M365WriteTargetPicker'
import { formatDateTime } from '../components/shared/format'
import { Modal } from '../components/shared/Modal'
import { QueryError } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'

const PROVIDER_LABELS: Record<ConnectionProvider, string> = {
  gdrive: 'Google Drive',
  m365: 'Microsoft 365 (SharePoint & OneDrive)',
}

const PROVIDER_ENV_HINTS: Record<ConnectionProvider, string> = {
  gdrive: 'TRET_GDRIVE_CLIENT_ID / TRET_GDRIVE_CLIENT_SECRET',
  m365: 'TRET_M365_CLIENT_ID / TRET_M365_CLIENT_SECRET',
}

// Order matches the contract's provider roster.
const PROVIDER_ORDER: ConnectionProvider[] = ['gdrive', 'm365']

export function ConnectionsView() {
  return (
    <div className="stack" style={{ gap: 20, maxWidth: 780 }}>
      <div>
        <Link to="/settings" className="mono-label" style={{ display: 'inline-block', marginBottom: 10 }}>
          ← Settings
        </Link>
        <h1 className="view-title">Connections</h1>
        <div className="view-sub">
          Connect Google Drive or Microsoft 365 so runs and imports can read files from them.
        </div>
      </div>
      <CallbackNotice />
      <ProviderCards />
    </div>
  )
}

// ── ?connected=/?error= from the OAuth callback redirect ────────────────────
// The backend's callback route lands back here as
// /settings/connections?connected={provider} or ?error={short_code} — shown
// once, then the params are cleared so a refresh or a bookmark doesn't
// re-show a stale notice.

function CallbackNotice() {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [notice, setNotice] = useState<{ kind: 'connected' | 'error'; value: string } | null>(null)

  useEffect(() => {
    const connected = searchParams.get('connected')
    const error = searchParams.get('error')
    if (connected) {
      setNotice({ kind: 'connected', value: connected })
      queryClient.invalidateQueries({ queryKey: ['connections'] })
    } else if (error) {
      setNotice({ kind: 'error', value: error })
    }
    if (connected || error) {
      setSearchParams({}, { replace: true })
    }
    // Runs once on arrival — searchParams/setSearchParams identity changes on
    // every navigation, and re-running this on the params this effect itself
    // just cleared would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!notice) return null

  const label = PROVIDER_LABELS[notice.value as ConnectionProvider] ?? notice.value

  return (
    <div
      className="panel"
      style={{
        borderColor: notice.kind === 'connected' ? 'var(--green)' : 'var(--red)',
        background: notice.kind === 'connected' ? 'var(--green-dim)' : 'var(--red-dim)',
      }}
    >
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <span
          className="mono-body"
          style={{ color: notice.kind === 'connected' ? 'var(--green)' : 'var(--red)' }}
        >
          {notice.kind === 'connected'
            ? `Connected ${label}.`
            : `Could not connect — ${notice.value}. Check the deployment's logs for details.`}
        </span>
        <button type="button" className="btn btn-sm" onClick={() => setNotice(null)}>
          dismiss
        </button>
      </div>
    </div>
  )
}

// ── Provider cards ───────────────────────────────────────────────────────

function ProviderCards() {
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const providersQuery = useQuery({ queryKey: ['connection-providers'], queryFn: api.connectionProviders })
  const connectionsQuery = useQuery({ queryKey: ['connections'], queryFn: api.listConnections })

  const isAdmin = ['owner', 'admin'].includes(meQuery.data?.role ?? '')

  if (providersQuery.isLoading || connectionsQuery.isLoading) {
    return <div className="empty pulse">Loading connections…</div>
  }
  if (providersQuery.isError) {
    return <QueryError error={providersQuery.error} what="the connection providers" />
  }
  if (connectionsQuery.isError) {
    return <QueryError error={connectionsQuery.error} what="this workspace's connections" />
  }

  const providers = providersQuery.data?.providers ?? []
  const connections = connectionsQuery.data?.connections ?? []
  const byProvider = new Map(connections.map((c) => [c.provider, c]))

  const ordered = [...providers].sort(
    (a, b) => PROVIDER_ORDER.indexOf(a.provider) - PROVIDER_ORDER.indexOf(b.provider),
  )

  return (
    <div className="stack" style={{ gap: 14 }}>
      {ordered.map((p) => (
        <ProviderCard key={p.provider} info={p} connection={byProvider.get(p.provider) ?? null} isAdmin={isAdmin} />
      ))}
      {ordered.length === 0 && <div className="empty">No connection providers registered.</div>}
    </div>
  )
}

function ProviderCard({
  info,
  connection,
  isAdmin,
}: {
  info: ConnectionProviderInfo
  connection: WorkspaceConnection | null
  isAdmin: boolean
}) {
  const queryClient = useQueryClient()

  const authorizeMutation = useMutation({
    mutationFn: () => api.authorizeConnection(info.provider),
    onSuccess: (res) => {
      window.location.assign(res.authorize_url)
    },
  })
  const authorizeError = authorizeMutation.error as ApiError | null

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['connections'] })
    queryClient.invalidateQueries({ queryKey: ['connection-providers'] })
  }

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div className="mono-label" style={{ marginBottom: 4 }}>
            {PROVIDER_LABELS[info.provider]}
          </div>
          {connection ? (
            <div className="row" style={{ gap: 8 }}>
              <StatusBadge status={connection.status} />
              <span className="mono-body">{connection.account_label ?? '(no account label)'}</span>
            </div>
          ) : (
            <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
              Not connected
            </div>
          )}
        </div>
        {connection && <DisconnectButton provider={info.provider} isAdmin={isAdmin} onDone={invalidate} />}
      </div>

      {connection && (
        <div className="config-stats" style={{ marginTop: 12 }}>
          <div className="config-stat">
            <div className="mono-label">Scopes</div>
            <div className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
              {connection.granted_scopes.length === 0
                ? '—'
                : connection.granted_scopes.map((s) => (
                    <span key={s} className="chip">
                      {s}
                    </span>
                  ))}
            </div>
          </div>
          <div className="config-stat">
            <div className="mono-label">Connected</div>
            <div className="mono-body">
              {connection.connected_at ? formatDateTime(connection.connected_at) : '—'}
            </div>
          </div>
          <div className="config-stat">
            <div className="mono-label">Last refreshed</div>
            <div className="mono-body">
              {connection.refreshed_at ? formatDateTime(connection.refreshed_at) : '—'}
            </div>
          </div>
        </div>
      )}

      {connection?.status === 'error' && (
        <div className="error-text" style={{ marginTop: 10 }}>
          {connection.error_detail ?? 'This connection needs to be reconnected.'}
        </div>
      )}

      {connection?.status === 'active' && info.provider === 'm365' && (
        <>
          <M365ReadAccessSection connection={connection} isAdmin={isAdmin} onChanged={invalidate} />
          <M365WriteBackSection connection={connection} isAdmin={isAdmin} onChanged={invalidate} />
          {isAdmin && <M365ActivitySection />}
        </>
      )}

      {!connection && (
        <div style={{ marginTop: 12 }}>
          {!info.configured ? (
            <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
              Not available on this deployment — set <code>{PROVIDER_ENV_HINTS[info.provider]}</code> to
              enable it.
            </div>
          ) : isAdmin ? (
            <>
              <button
                type="button"
                className="btn btn-primary"
                disabled={authorizeMutation.isPending}
                onClick={() => authorizeMutation.mutate()}
              >
                {authorizeMutation.isPending ? 'Redirecting…' : `Connect ${PROVIDER_LABELS[info.provider]}`}
              </button>
              <div className="callout callout-note" style={{ marginTop: 10 }}>
                Runs and imports by anyone in this workspace will access files as the connected account.
              </div>
              {authorizeError && (
                <div className="error-text" style={{ marginTop: 8 }}>
                  {authorizeError.status === 403
                    ? (gateRefusalDetail(authorizeError) ?? 'Requires admin role.')
                    : authorizeError.message}
                </div>
              )}
            </>
          ) : (
            <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
              Connecting requires the owner or admin role in this workspace.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function DisconnectButton({
  provider,
  isAdmin,
  onDone,
}: {
  provider: ConnectionProvider
  isAdmin: boolean
  onDone: () => void
}) {
  const [open, setOpen] = useState(false)
  const disconnectMutation = useMutation({
    mutationFn: () => api.disconnectConnection(provider),
    onSuccess: () => {
      setOpen(false)
      onDone()
    },
  })
  const disconnectError = disconnectMutation.error as ApiError | null

  if (!isAdmin) return null

  return (
    <>
      <button type="button" className="btn btn-sm btn-danger" onClick={() => setOpen(true)}>
        Disconnect
      </button>
      <Modal
        open={open}
        onClose={() => (disconnectMutation.isPending ? undefined : setOpen(false))}
        title={`Disconnect ${PROVIDER_LABELS[provider]}?`}
        footer={
          <>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              disabled={disconnectMutation.isPending}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-sm btn-danger"
              disabled={disconnectMutation.isPending}
              onClick={() => disconnectMutation.mutate()}
            >
              {disconnectMutation.isPending ? 'Disconnecting…' : 'Disconnect'}
            </button>
          </>
        }
      >
        <div className="mono-body">
          Revokes this workspace's access and removes the stored connection. Runs and imports will no
          longer be able to reach {PROVIDER_LABELS[provider]} until it is reconnected.
        </div>
        {disconnectError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {disconnectError.message}
          </div>
        )}
      </Modal>
    </>
  )
}

// ── m365 read-access allowlist ───────────────────────────────────────────
// `connection.selected_resources.read` is the source of truth for both
// whether the connection is restricted and what it's restricted to — no
// separate fetch needed to render the card itself. The picker (which does
// call the m365 browse endpoint) only opens once someone chooses to change
// the allowlist.

function M365ReadAccessSection({
  connection,
  isAdmin,
  onChanged,
}: {
  connection: WorkspaceConnection
  isAdmin: boolean
  onChanged: () => void
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const entries = connection.selected_resources?.read ?? []
  const restricted = entries.length > 0

  const removeMutation = useMutation({
    mutationFn: (entry: M365ReadEntry) =>
      api.setM365ReadAccess(entries.filter((e) => e.drive_id !== entry.drive_id)),
    onSuccess: onChanged,
  })
  const removeError = removeMutation.error as ApiError | null

  return (
    <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--border-subtle)' }}>
      <div className="mono-label" style={{ marginBottom: 4 }}>
        Read access in runs
      </div>
      <div className="mono-body" style={{ color: 'var(--text-muted)', marginBottom: 10 }}>
        Runs and chats can search and read files from these locations. With no locations chosen,
        everything the connected account can see is searchable.
      </div>

      {!restricted ? (
        <>
          <div className="mono-body" style={{ color: 'var(--amber)', marginBottom: 10 }}>
            Everything the connected account can see is searchable by every workspace member.
          </div>
          {isAdmin && (
            <button type="button" className="btn btn-sm" onClick={() => setPickerOpen(true)}>
              Choose locations
            </button>
          )}
        </>
      ) : (
        <>
          <div className="stack" style={{ gap: 6, marginBottom: 10 }}>
            {entries.map((entry) => (
              <div
                key={entry.drive_id}
                className="row"
                style={{ justifyContent: 'space-between', gap: 8 }}
              >
                <div className="row" style={{ gap: 8, minWidth: 0 }}>
                  <span className="mono-body">{entry.label}</span>
                  <span className="chip">{entry.kind === 'onedrive' ? 'OneDrive' : 'Site'}</span>
                </div>
                {isAdmin && (
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={removeMutation.isPending}
                    onClick={() => removeMutation.mutate(entry)}
                  >
                    Remove
                  </button>
                )}
              </div>
            ))}
          </div>
          {isAdmin && (
            <div className="row" style={{ gap: 8 }}>
              <button type="button" className="btn btn-sm" onClick={() => setPickerOpen(true)}>
                Add location
              </button>
              <AllowEverythingButton onDone={onChanged} />
            </div>
          )}
        </>
      )}

      {removeError && (
        <div className="error-text" style={{ marginTop: 8 }}>
          {removeError.message}
        </div>
      )}

      {isAdmin && (
        <M365ReadAccessPicker
          open={pickerOpen}
          onClose={() => setPickerOpen(false)}
          initialEntries={entries}
          onSaved={onChanged}
        />
      )}
    </div>
  )
}

function AllowEverythingButton({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false)
  const allowMutation = useMutation({
    mutationFn: () => api.setM365ReadAccess([]),
    onSuccess: () => {
      setOpen(false)
      onDone()
    },
  })
  const allowError = allowMutation.error as ApiError | null

  return (
    <>
      <button type="button" className="btn btn-sm" onClick={() => setOpen(true)}>
        Allow everything
      </button>
      <Modal
        open={open}
        onClose={() => (allowMutation.isPending ? undefined : setOpen(false))}
        title="Allow everything?"
        footer={
          <>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              disabled={allowMutation.isPending}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-sm btn-danger"
              disabled={allowMutation.isPending}
              onClick={() => allowMutation.mutate()}
            >
              {allowMutation.isPending ? 'Clearing…' : 'Allow everything'}
            </button>
          </>
        }
      >
        <div className="mono-body">
          Clears the read-access allowlist. Runs and chats will be able to search and read
          everything the connected account can see, workspace-wide.
        </div>
        {allowError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {allowError.message}
          </div>
        )}
      </Modal>
    </>
  )
}

// ── m365 write-back allowlist ────────────────────────────────────────────
// Unlike read access, write-back has no "everything" mode — a connection
// carries no write scopes at all until an admin explicitly re-authorizes for
// them (`write_enabled`), and even then only names folders admins add one at
// a time. `connection.selected_resources.write` is the source of truth for
// the folder list, same as `.read` above; the picker only calls the m365
// browse endpoint once someone opens it.

function M365WriteBackSection({
  connection,
  isAdmin,
  onChanged,
}: {
  connection: WorkspaceConnection
  isAdmin: boolean
  onChanged: () => void
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const targets = connection.selected_resources?.write ?? []
  const writeEnabled = connection.write_enabled ?? false

  const enableMutation = useMutation({
    mutationFn: () => api.authorizeConnection('m365', 'write'),
    onSuccess: (res) => {
      window.location.assign(res.authorize_url)
    },
  })
  const enableError = enableMutation.error as ApiError | null

  const removeMutation = useMutation({
    mutationFn: (target: WriteTarget) =>
      api.setM365WriteTargets(targets.filter((t) => t.slug !== target.slug)),
    onSuccess: onChanged,
  })
  const removeError = removeMutation.error as ApiError | null

  return (
    <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--border-subtle)' }}>
      <div className="row" style={{ gap: 8, marginBottom: 4 }}>
        <div className="mono-label">Write-back</div>
        {writeEnabled && <span className="chip">write-back on</span>}
      </div>
      <div className="mono-body" style={{ color: 'var(--text-muted)', marginBottom: 10 }}>
        Approved runs and published deliverables can be written into these folders. tret creates a{' '}
        <code>tret/</code> subfolder inside each and never overwrites or deletes files.
      </div>

      {!writeEnabled ? (
        <>
          <div className="mono-body" style={{ color: 'var(--text-muted)', marginBottom: 10 }}>
            Write-back is off. Enabling it re-opens Microsoft sign-in to grant edit access
            (Files.ReadWrite.All, Sites.ReadWrite.All).
          </div>
          {isAdmin && (
            <button
              type="button"
              className="btn btn-sm"
              disabled={enableMutation.isPending}
              onClick={() => enableMutation.mutate()}
            >
              {enableMutation.isPending ? 'Redirecting…' : 'Enable write-back'}
            </button>
          )}
          {enableError && (
            <div className="error-text" style={{ marginTop: 8 }}>
              {enableError.status === 403
                ? (gateRefusalDetail(enableError) ?? 'Requires admin role.')
                : enableError.message}
            </div>
          )}
        </>
      ) : (
        <>
          {targets.length === 0 ? (
            <div className="mono-body" style={{ color: 'var(--text-muted)', marginBottom: 10 }}>
              No output folders yet. Runs cannot propose writes until an admin adds one.
            </div>
          ) : (
            <div className="stack" style={{ gap: 6, marginBottom: 10 }}>
              {targets.map((target) => (
                <div key={target.slug} className="row" style={{ justifyContent: 'space-between', gap: 8 }}>
                  <div className="row" style={{ gap: 8, minWidth: 0 }}>
                    <span className="mono-body">{target.label}</span>
                    <span className="desc" style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
                      {target.path}
                    </span>
                    <span className="chip" style={{ fontFamily: 'var(--mono)' }}>
                      {target.slug}
                    </span>
                  </div>
                  {isAdmin && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={removeMutation.isPending}
                      onClick={() => removeMutation.mutate(target)}
                    >
                      Remove
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
          {isAdmin && (
            <button type="button" className="btn btn-sm" onClick={() => setPickerOpen(true)}>
              Add output folder
            </button>
          )}
        </>
      )}

      {removeError && (
        <div className="error-text" style={{ marginTop: 8 }}>
          {removeError.message}
        </div>
      )}

      {isAdmin && (
        <M365WriteTargetPicker
          open={pickerOpen}
          onClose={() => setPickerOpen(false)}
          existingTargets={targets}
          onSaved={onChanged}
        />
      )}
    </div>
  )
}

// ── connection activity (admin only) ─────────────────────────────────────
// A collapsed disclosure at the bottom of the m365 card — same shape as
// EmissionsHistory's "Change history": say nothing while loading, hide
// entirely on a 404 (endpoint not deployed on this backend yet) since that
// is "this UI does not exist here" rather than an error, and only fetch once
// someone actually opens it.

function M365ActivitySection() {
  const [open, setOpen] = useState(false)
  const activityQuery = useQuery({
    queryKey: ['connection-activity'],
    queryFn: () => api.connectionActivity(50),
    enabled: open,
    retry: false,
  })
  const error = activityQuery.error as ApiError | null

  if (error && error.status === 404) return null

  return (
    <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--border-subtle)' }}>
      <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
        <summary className="mono-label" style={{ cursor: 'pointer' }}>
          Activity
        </summary>
        <div style={{ marginTop: 10 }}>
          {error && error.status === 403 ? (
            <div className="fine-print">Admin only — ask an admin or owner to view this.</div>
          ) : activityQuery.isLoading ? (
            <div className="empty pulse">Loading…</div>
          ) : error ? (
            <QueryError error={error} what="connection activity" />
          ) : (activityQuery.data?.items.length ?? 0) === 0 ? (
            <div className="fine-print">No activity recorded yet.</div>
          ) : (
            <div style={{ maxHeight: 320, overflowY: 'auto' }}>
              <table className="mono-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Bytes</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {(activityQuery.data?.items ?? []).map((row: ConnectionActivityItem) => (
                    <tr key={row.id}>
                      <td>{row.created_at ? formatDateTime(row.created_at) : '—'}</td>
                      <td>{row.action}</td>
                      <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{row.target ?? '—'}</td>
                      <td>{row.bytes ?? '—'}</td>
                      <td>{row.detail ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </details>
    </div>
  )
}
