import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, type ReactNode, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  api,
  ApiError,
  type EgressMode,
  type LocalProviderTest,
  takesApiKey,
  type User,
  type WorkspaceInviteCreated,
  type WorkspaceMember,
} from '../api/client'
import { BillingSection } from '../components/shared/BillingSection'
import { formatDateTime } from '../components/shared/format'
import { type Column, MonoTable, QueryError } from '../components/shared/MonoTable'
import { ProviderKeyForm } from '../components/shared/ProviderKeyForm'
import { StatusBadge } from '../components/shared/StatusBadge'

export function SettingsView() {
  return (
    <div className="stack" style={{ gap: 28, maxWidth: 780 }}>
      <div>
        <h1 className="view-title">Settings</h1>
        <div className="view-sub">
          Team, provider keys, router configuration, network access, and open data
          requests.
        </div>
      </div>
      <WorkspaceMembersSection />
      <InstanceUsersSection />
      <ProviderKeys />
      <ConnectionsTeaser />
      <BillingSection />
      <RouterInfo />
      <NetworkAccess />
      <DataRequests />
    </div>
  )
}

// ── Connections teaser ───────────────────────────────────────────────────
// A one-line summary of what Google Drive / Microsoft 365 look like right
// now, linking through to the full page at /settings/connections. A separate
// route (rather than another inline section here, unlike everything else on
// this page) because the OAuth round trip has to land somewhere after the
// provider redirects back — see Connections.tsx's own CallbackNotice.

function ConnectionsTeaser() {
  const connectionsQuery = useQuery({ queryKey: ['connections'], queryFn: api.listConnections })
  const connections = connectionsQuery.data?.connections ?? []
  const activeCount = connections.filter((c) => c.status === 'active').length

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Connections
      </div>
      <Link to="/settings/connections" className="panel" style={{ display: 'block' }}>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div className="mono-body">
            {connectionsQuery.isLoading
              ? 'Loading…'
              : activeCount === 0
                ? 'Google Drive and Microsoft 365 are not connected.'
                : `${activeCount} provider${activeCount === 1 ? '' : 's'} connected.`}
          </div>
          <span className="mono-label">Manage →</span>
        </div>
      </Link>
    </div>
  )
}

// ── Workspace members ─────────────────────────────────────────────────────
// The current workspace's own team: members with role controls, pending
// invites, and the invite form. A personal workspace is just the one person
// it belongs to, so this renders a create-team nudge instead — there is
// nothing to manage yet.

const WORKSPACE_ROLES = [
  { value: 'owner', hint: 'full control, incl. billing' },
  { value: 'admin', hint: 'manage members, invites and provider keys' },
  { value: 'approver', hint: 'can approve/reject findings' },
  { value: 'analyst', hint: 'runs work, cannot approve' },
]

const INVITE_ROLES = WORKSPACE_ROLES.filter((r) => r.value !== 'owner')

function memberRoleBadge(role: string): string {
  if (role === 'owner' || role === 'admin') return 'badge-violet'
  if (role === 'approver') return 'badge-green'
  return 'badge-blue'
}

function WorkspaceMembersSection() {
  const queryClient = useQueryClient()
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const me = meQuery.data
  const currentWorkspace = me?.workspaces.find((w) => w.id === me.current_workspace_id)
  const workspaceId = currentWorkspace?.id

  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState('analyst')
  const [lastInvite, setLastInvite] = useState<WorkspaceInviteCreated | null>(null)
  const [copiedLink, setCopiedLink] = useState<string | null>(null)

  const membersQuery = useQuery({
    queryKey: ['workspace-members', workspaceId],
    queryFn: () => api.workspaceMembers(workspaceId!),
    enabled: !!workspaceId,
  })
  const invitesQuery = useQuery({
    queryKey: ['workspace-invites', workspaceId],
    queryFn: () => api.listInvites(workspaceId!),
    enabled: !!workspaceId,
  })
  // The list endpoint returns every invite this workspace has ever issued —
  // pending, accepted, and revoked alike — so this section (titled "Pending
  // invites") filters to the ones still open.
  const pendingInvites = (invitesQuery.data ?? []).filter((inv) => inv.status === 'pending')

  const inviteMutation = useMutation({
    mutationFn: () => api.createInvite(workspaceId!, inviteEmail.trim(), inviteRole),
    onSuccess: (invite) => {
      setLastInvite(invite)
      setInviteEmail('')
      setInviteRole('analyst')
      queryClient.invalidateQueries({ queryKey: ['workspace-invites', workspaceId] })
    },
  })
  const revokeMutation = useMutation({
    mutationFn: (inviteId: string) => api.revokeInvite(workspaceId!, inviteId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workspace-invites', workspaceId] }),
  })
  const roleMutation = useMutation({
    mutationFn: ({ userId, newRole }: { userId: string; newRole: string }) =>
      api.updateMemberRole(workspaceId!, userId, newRole),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workspace-members', workspaceId] }),
  })
  const removeMutation = useMutation({
    mutationFn: (userId: string) => api.removeMember(workspaceId!, userId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workspace-members', workspaceId] }),
  })
  const createWorkspaceMutation = useMutation({
    mutationFn: (name: string) => api.createWorkspace(name),
    // A brand-new workspace means the whole active context changed — same
    // full-drop rigor as switching workspaces or logging out.
    onSuccess: () => queryClient.clear(),
  })

  if (!me || !currentWorkspace) return null

  const canManage = ['owner', 'admin'].includes(me.role)

  const copyLink = async (link: string) => {
    try {
      await navigator.clipboard.writeText(link)
      setCopiedLink(link)
      window.setTimeout(() => setCopiedLink((c) => (c === link ? null : c)), 1500)
    } catch {
      /* clipboard unavailable — the link stays selectable by hand */
    }
  }

  if (currentWorkspace.kind === 'personal') {
    return (
      <div>
        <div className="mono-label" style={{ marginBottom: 8 }}>
          Team
        </div>
        <div className="panel">
          <div className="mono-body" style={{ marginBottom: 12 }}>
            Personal workspaces are just you — create a team to collaborate.
          </div>
          <button
            type="button"
            className="btn btn-primary"
            disabled={createWorkspaceMutation.isPending}
            onClick={() => {
              const name = window.prompt('Name your team workspace')
              if (name && name.trim()) createWorkspaceMutation.mutate(name.trim())
            }}
          >
            {createWorkspaceMutation.isPending ? 'Creating…' : 'Create team workspace'}
          </button>
          {createWorkspaceMutation.isError && (
            <div className="error-text" style={{ marginTop: 10 }}>
              {(createWorkspaceMutation.error as Error).message}
            </div>
          )}
        </div>
      </div>
    )
  }

  const memberColumns: Column<WorkspaceMember>[] = [
    {
      key: 'member',
      header: 'Member',
      render: (m) => (
        <div>
          <div>{m.display_name}</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>{m.email}</div>
        </div>
      ),
    },
    {
      key: 'role',
      header: 'Role',
      render: (m) => {
        // Only an owner may hand out or take away the owner role; anyone
        // else managing the team can move members between the other three.
        const canEditThisRow = canManage && (m.role !== 'owner' || me.role === 'owner')
        if (!canEditThisRow) {
          return <span className={`badge ${memberRoleBadge(m.role)}`}>{m.role}</span>
        }
        return (
          <select
            value={m.role}
            disabled={roleMutation.isPending}
            onChange={(e) => roleMutation.mutate({ userId: m.user_id, newRole: e.target.value })}
          >
            {WORKSPACE_ROLES.filter((r) => r.value !== 'owner' || me.role === 'owner').map((r) => (
              <option key={r.value} value={r.value}>
                {r.value}
              </option>
            ))}
          </select>
        )
      },
    },
    {
      key: 'actions',
      header: '',
      render: (m) =>
        canManage && m.user_id !== me.id && (m.role !== 'owner' || me.role === 'owner') ? (
          <button
            type="button"
            className="btn btn-sm"
            disabled={removeMutation.isPending}
            onClick={() => removeMutation.mutate(m.user_id)}
          >
            Remove
          </button>
        ) : null,
    },
  ]

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Team — {currentWorkspace.name}
      </div>

      {membersQuery.isLoading ? (
        <div className="empty pulse">Loading members…</div>
      ) : membersQuery.isError ? (
        <QueryError error={membersQuery.error} what="the workspace member list" />
      ) : (
        <div style={{ marginBottom: 14 }}>
          <MonoTable
            columns={memberColumns}
            rows={membersQuery.data ?? []}
            rowKey={(m) => m.user_id}
            empty="No members."
          />
        </div>
      )}
      {(roleMutation.isError || removeMutation.isError) && (
        <div className="error-text" style={{ marginBottom: 14 }}>
          {((roleMutation.error ?? removeMutation.error) as Error).message}
        </div>
      )}

      <div className="mono-label" style={{ marginBottom: 8 }}>
        Pending invites
      </div>
      {invitesQuery.isLoading ? (
        <div className="empty pulse">Loading invites…</div>
      ) : invitesQuery.isError ? (
        <QueryError error={invitesQuery.error} what="pending invites" />
      ) : pendingInvites.length === 0 ? (
        <div className="empty" style={{ marginBottom: 14 }}>
          No pending invites.
        </div>
      ) : (
        <table className="mono-table" style={{ marginBottom: 14 }}>
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>When</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {pendingInvites.map((inv) => (
              <tr key={inv.id}>
                <td>{inv.email}</td>
                <td>
                  <span className={`badge ${memberRoleBadge(inv.role)}`}>{inv.role}</span>
                </td>
                <td>{formatDateTime(inv.created_at)}</td>
                <td>
                  {canManage && (
                    // No copy-link button here: the list endpoint deliberately
                    // never repeats the invite's token/link (see
                    // WorkspaceInvite's doc comment in api/client.ts) — a lost
                    // link means revoke-and-reinvite, not "look it up again".
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      disabled={revokeMutation.isPending}
                      onClick={() => revokeMutation.mutate(inv.id)}
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canManage && (
        <form
          className="panel"
          onSubmit={(e) => {
            e.preventDefault()
            setLastInvite(null)
            inviteMutation.mutate()
          }}
        >
          <div className="mono-label" style={{ marginBottom: 10 }}>
            Invite a member
          </div>
          <div className="row" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label className="mono-label">Email</label>
              <input
                type="email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                autoComplete="off"
                required
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 130 }}>
              <label className="mono-label">Role</label>
              <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
                {INVITE_ROLES.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.value}
                  </option>
                ))}
              </select>
            </div>
            <button
              className="btn btn-primary"
              type="submit"
              disabled={inviteMutation.isPending || !inviteEmail.trim()}
            >
              {inviteMutation.isPending ? 'Sending…' : 'Send invite'}
            </button>
          </div>
          <div
            style={{
              marginTop: 6,
              fontFamily: 'var(--mono)',
              fontSize: 10.5,
              color: 'var(--text-muted)',
            }}
          >
            {INVITE_ROLES.map((r) => `${r.value}: ${r.hint}`).join(' · ')}
          </div>
          {inviteMutation.isError && (
            <div className="error-text" style={{ marginTop: 10 }}>
              {(inviteMutation.error as Error).message}
            </div>
          )}
          {lastInvite &&
            (() => {
              // invite_url is a path (`/invite/{token}`) — the backend leaves
              // prefixing it with an origin to the caller.
              const inviteLink = `${window.location.origin}${lastInvite.invite_url}`
              return (
                <div
                  className="panel"
                  style={{ marginTop: 12, borderColor: 'var(--amber)', background: 'var(--amber-dim)' }}
                >
                  <div className="mono-label" style={{ marginBottom: 6, color: 'var(--amber)' }}>
                    {lastInvite.email_sent
                      ? 'Invite sent — share this link too'
                      : 'Email delivery is not set up — share this link'}
                  </div>
                  <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                    <code
                      style={{
                        userSelect: 'all',
                        background: 'var(--bg-input)',
                        border: '1px solid var(--border)',
                        borderRadius: 4,
                        padding: '2px 6px',
                        flex: 1,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                      }}
                    >
                      {inviteLink}
                    </code>
                    <button type="button" className="btn btn-sm" onClick={() => void copyLink(inviteLink)}>
                      {copiedLink === inviteLink ? 'copied' : 'copy'}
                    </button>
                  </div>
                </div>
              )
            })()}
        </form>
      )}
    </div>
  )
}

// ── Instance users ────────────────────────────────────────────────────────
// Self-host's instance-wide user management — POST/GET /api/auth/users*,
// gated on the legacy global role rather than the current workspace's role,
// so a self-hosted single-workspace deployment keeps working exactly as it
// always has. Hidden entirely for anyone who isn't a global admin: a 403 here
// is the ordinary case for every workspace-scoped role, so it reads as "this
// does not apply to you" rather than as an error.

const ROLES = [
  { value: 'admin', hint: 'everything, incl. keys and users' },
  { value: 'approver', hint: 'can approve/reject findings' },
  { value: 'analyst', hint: 'runs work, cannot approve' },
]

function generatePassword(): string {
  const charset = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789-_!'
  const bytes = new Uint32Array(16)
  crypto.getRandomValues(bytes)
  return [...bytes].map((b) => charset[b % charset.length]).join('')
}

function InstanceUsersSection() {
  const queryClient = useQueryClient()
  const usersQuery = useQuery({ queryKey: ['users'], queryFn: api.listUsers, retry: false })
  // The signed-in user's own global role, from the cache App already
  // populated. Admin status is a property of the user, not of whether a list
  // request happened to succeed: `isAdmin` used to be inferred as "the user
  // list did not 403", so any *other* failure — a 500, a dropped connection —
  // showed the admin-only user creation form (and its role picker and
  // generated password) to a non-admin.
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })

  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [role, setRole] = useState('analyst')
  const [password, setPassword] = useState('')
  const [passwordVisible, setPasswordVisible] = useState(false)
  const [created, setCreated] = useState<{ email: string; password: string } | null>(null)

  const createMutation = useMutation({
    mutationFn: () =>
      api.createUser({ email: email.trim(), display_name: displayName.trim(), password, role }),
    onSuccess: (user) => {
      setCreated({ email: user.email, password })
      setEmail('')
      setDisplayName('')
      setRole('analyst')
      setPassword('')
      setPasswordVisible(false)
      queryClient.invalidateQueries({ queryKey: ['users'] })
    },
  })

  const listError = usersQuery.error as ApiError | null
  const createError = createMutation.error as ApiError | null
  const isAdmin = meQuery.data?.global_role === 'admin'

  // Not a global admin: this section does not apply to this account at all,
  // so it disappears rather than showing an "admin only" placeholder.
  if (usersQuery.isError && listError?.status === 403) return null

  const columns: Column<User>[] = [
    { key: 'email', header: 'Email', render: (u) => u.email },
    { key: 'name', header: 'Display name', render: (u) => u.display_name },
    {
      key: 'role',
      header: 'Role',
      render: (u) => (
        <span
          className={`badge ${u.global_role === 'admin' ? 'badge-violet' : u.global_role === 'approver' ? 'badge-green' : 'badge-blue'}`}
        >
          {u.global_role}
        </span>
      ),
    },
  ]

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setCreated(null)
    createMutation.mutate()
  }

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Instance users
      </div>

      {usersQuery.isLoading ? (
        <div className="empty pulse">Loading users…</div>
      ) : usersQuery.isError ? (
        <QueryError error={usersQuery.error} what="the instance user list" />
      ) : (
        <div style={{ marginBottom: 14 }}>
          <MonoTable
            columns={columns}
            rows={usersQuery.data ?? []}
            rowKey={(u) => u.id}
            empty="No users."
          />
        </div>
      )}

      {isAdmin && (
        <form onSubmit={submit} className="panel">
          <div className="mono-label" style={{ marginBottom: 10 }}>
            Add user (admin)
          </div>
          <div className="row" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
            <div className="field" style={{ marginBottom: 0, width: 200 }}>
              <label className="mono-label">Email</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="off"
                required
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 160 }}>
              <label className="mono-label">Display name</label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                required
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 130 }}>
              <label className="mono-label">Role</label>
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                {ROLES.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.value}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div
            style={{
              marginTop: 6,
              fontFamily: 'var(--mono)',
              fontSize: 10.5,
              color: 'var(--text-muted)',
            }}
          >
            {ROLES.map((r) => `${r.value}: ${r.hint}`).join(' · ')}
          </div>
          <div className="row" style={{ alignItems: 'flex-end', marginTop: 12, flexWrap: 'wrap' }}>
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 220 }}>
              <label className="mono-label">Password (min 8 chars)</label>
              <input
                type={passwordVisible ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
                style={passwordVisible ? { fontFamily: 'var(--mono)' } : undefined}
                required
              />
            </div>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setPassword(generatePassword())
                setPasswordVisible(true)
              }}
            >
              Generate
            </button>
            <button
              className="btn btn-primary"
              type="submit"
              disabled={createMutation.isPending || !email.trim() || !displayName.trim() || password.length < 8}
            >
              {createMutation.isPending ? 'Creating…' : 'Add user'}
            </button>
          </div>
          {passwordVisible && password && !created && (
            <div
              style={{
                marginTop: 8,
                fontFamily: 'var(--mono)',
                fontSize: 11,
                color: 'var(--text-muted)',
              }}
            >
              Copy this password before saving — it is stored only as a hash.
            </div>
          )}
          {createError && (
            <div className="error-text" style={{ marginTop: 10 }}>
              {createError.status === 403 ? 'Admin only.' : createError.message}
            </div>
          )}
          {created && (
            <div
              className="panel"
              style={{ marginTop: 12, borderColor: 'var(--amber)', background: 'var(--amber-dim)' }}
            >
              <div className="mono-label" style={{ marginBottom: 6, color: 'var(--amber)' }}>
                One-time credentials — copy now, this will not be shown again
              </div>
              <div className="mono-body">
                {created.email} ·{' '}
                <code
                  style={{
                    userSelect: 'all',
                    background: 'var(--bg-input)',
                    border: '1px solid var(--border)',
                    borderRadius: 4,
                    padding: '2px 6px',
                  }}
                >
                  {created.password}
                </code>
              </div>
            </div>
          )}
        </form>
      )}
    </div>
  )
}

// ── Provider keys ─────────────────────────────────────────────────────────

function ProviderKeys() {
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providerStatus })

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Provider keys
      </div>
      {providersQuery.isLoading ? (
        <div className="empty pulse">Loading providers…</div>
      ) : providersQuery.isError ? (
        <div className="error-text">{(providersQuery.error as Error).message}</div>
      ) : (
        <>
          <table className="mono-table" style={{ marginBottom: 8 }}>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Configured</th>
                <th>Source</th>
                <th>Credential</th>
              </tr>
            </thead>
            <tbody>
              {(providersQuery.data ?? []).map((p) => {
                // "local" has no API key: a configured base URL is the credential,
                // and an unset one is a normal state, not a misconfiguration.
                const isLocal = !takesApiKey(p.provider)
                return (
                  <tr key={p.provider}>
                    <td>{p.provider}</td>
                    <td>
                      <span
                        style={{
                          display: 'inline-block',
                          width: 8,
                          height: 8,
                          borderRadius: 4,
                          background: p.configured
                            ? 'var(--green)'
                            : isLocal
                              ? 'var(--gray)'
                              : 'var(--red)',
                          marginRight: 6,
                        }}
                      />
                      {p.configured ? 'yes' : 'no'}
                    </td>
                    <td>{p.source ?? '—'}</td>
                    <td>
                      {isLocal ? (
                        <span style={{ color: 'var(--text-muted)' }}>
                          {p.configured ? 'base URL (no key)' : 'not set'}
                        </span>
                      ) : p.last4 ? (
                        `••••${p.last4}`
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div
            style={{
              marginBottom: 14,
              fontFamily: 'var(--mono)',
              fontSize: 10.5,
              color: 'var(--text-muted)',
            }}
          >
            local is enabled with the <code>TRET_LOCAL_BASE_URL</code> env var (an OpenAI-compatible
            server, e.g. <code>http://localhost:11434/v1</code>) — it takes no API key. Its models
            then appear as <code>local/*</code> in the pickers, free in dollars but never in watts.
          </div>
          <LocalModels
            configured={
              (providersQuery.data ?? []).find((p) => p.provider === 'local')?.configured ?? false
            }
          />
        </>
      )}

      {/* The write-only key form itself lives in components/shared/ProviderKeyForm
          — shared with the first-run setup surface, which offers the same flow. */}
      <ProviderKeyForm />
    </div>
  )
}

// ── Local models: setup guide + connection test ───────────────────────────
// The full write-up lives in docs/local-models.md; this is the in-app version of
// it, kept deliberately short. Commands are shown, never run — tret cannot
// install anything on the machine hosting the model server.

const OLLAMA_MODEL = 'qwen2.5:14b-instruct'
const OLLAMA_MODEL_SMALL = 'qwen2.5:7b-instruct'
const OLLAMA_BASE_URL = 'http://localhost:11434/v1'
const DOCKER_BASE_URL = 'http://host.docker.internal:11434/v1'

/** A copy-able command line. Selection still works if the clipboard is blocked
 *  (no HTTPS, no permission), which is why the text stays user-selectable. */
function CommandSnippet({ command }: { command: string }) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard unavailable — the snippet is selectable by hand */
    }
  }

  return (
    <div className="row" style={{ gap: 8, alignItems: 'flex-start', marginTop: 6 }}>
      <pre className="code-block" style={{ flex: 1, userSelect: 'all' }}>
        {command}
      </pre>
      <button type="button" className="btn btn-sm" onClick={() => void copy()}>
        {copied ? 'copied' : 'copy'}
      </button>
    </div>
  )
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <div style={{ marginTop: 14 }}>
      <div className="mono-body">
        <strong>{n}.</strong> {title}
      </div>
      <div
        style={{
          fontFamily: 'var(--mono)',
          fontSize: 11,
          color: 'var(--text-muted)',
          marginTop: 4,
        }}
      >
        {children}
      </div>
    </div>
  )
}

function SetupGuide() {
  const [open, setOpen] = useState(false)

  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <div className="mono-label">Set up local models</div>
          <div
            style={{
              fontFamily: 'var(--mono)',
              fontSize: 11,
              color: 'var(--text-muted)',
              marginTop: 4,
            }}
          >
            Three steps, ~10 minutes plus a download. No cloud key, no egress.
          </div>
        </div>
        <button type="button" className="btn btn-sm" onClick={() => setOpen(!open)}>
          {open ? 'hide' : 'show me how'}
        </button>
      </div>

      {open && (
        <>
          <Step n={1} title="Install Ollama — a local model server">
            macOS (or install the app from ollama.com/download, which also starts the server):
            <CommandSnippet command="brew install ollama && ollama serve" />
            Linux:
            <CommandSnippet command="curl -fsSL https://ollama.com/install.sh | sh" />
            Windows: run the installer from ollama.com/download — it starts the server for you.
          </Step>

          <Step n={2} title="Download a model that can call tools">
            tret drives everything through tool calls, so the model must support them. This one is
            about 9 GB and wants ~16 GB of RAM:
            <CommandSnippet command={`ollama pull ${OLLAMA_MODEL}`} />
            On a smaller machine use <code>{OLLAMA_MODEL_SMALL}</code> instead (~4.7 GB, ~8 GB RAM).
          </Step>

          <Step n={3} title="Point tret at it and restart">
            Add this to your <code>.env</code>, then restart tret:
            <CommandSnippet command={`TRET_LOCAL_BASE_URL=${OLLAMA_BASE_URL}`} />
            Running the docker compose stack? The container's <code>localhost</code> is not your
            machine — use <code>{DOCKER_BASE_URL}</code>. Or skip the install entirely and run{' '}
            <code>docker compose --profile local up</code> for a bundled Ollama (slower: CPU-only in
            a container).
          </Step>

          <div
            style={{
              marginTop: 14,
              fontFamily: 'var(--mono)',
              fontSize: 11,
              color: 'var(--text-muted)',
            }}
          >
            Full guide, other servers (LM Studio, vLLM, llama.cpp), recommended models, and
            troubleshooting: <code>docs/local-models.md</code>.
          </div>
        </>
      )}
    </div>
  )
}

/** True when the failure is "nothing is listening there", as opposed to an HTTP
 *  error from a server that did answer. */
function isConnectFailure(error: string): boolean {
  const e = error.toLowerCase()
  return (
    e.includes('connecterror') ||
    e.includes('connecttimeout') ||
    e.includes('connection refused') ||
    e.includes('all connection attempts failed') ||
    e.includes('timeout')
  )
}

function TestResult({ result }: { result: LocalProviderTest }) {
  if (!result.configured) {
    return (
      <div className="mono-body" style={{ marginTop: 10, color: 'var(--text-muted)' }}>
        No <code>TRET_LOCAL_BASE_URL</code> is set, so there is nothing to test.
      </div>
    )
  }

  if (!result.reachable) {
    const dockerHint =
      !!result.base_url &&
      result.base_url.includes('localhost') &&
      !!result.error &&
      isConnectFailure(result.error)
    return (
      <div style={{ marginTop: 10 }}>
        <div className="error-text">not reachable at {result.base_url}</div>
        {result.error && (
          <pre className="code-block" style={{ marginTop: 6, color: 'var(--text-muted)' }}>
            {result.error}
          </pre>
        )}
        <div
          style={{
            marginTop: 6,
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--text-muted)',
          }}
        >
          Check that the server is running (<code>ollama serve</code>, or the Ollama app) and that
          the port matches.
          {dockerHint && (
            <>
              {' '}
              If tret runs in docker, <code>localhost</code> is the container, not your machine —
              set <code>{DOCKER_BASE_URL}</code> instead.
            </>
          )}
        </div>
      </div>
    )
  }

  if (result.models.length === 0) {
    return (
      <div style={{ marginTop: 10 }}>
        <div className="mono-body" style={{ color: 'var(--amber)' }}>
          reachable at {result.base_url} — but the server has no models
        </div>
        <div
          style={{
            marginTop: 4,
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--text-muted)',
          }}
        >
          Download one (~9 GB), then test again:
        </div>
        <CommandSnippet command={`ollama pull ${OLLAMA_MODEL}`} />
      </div>
    )
  }

  return (
    <div style={{ marginTop: 10 }}>
      <div className="mono-body" style={{ color: 'var(--green)' }}>
        reachable at {result.base_url} — {result.counts.models} model
        {result.counts.models === 1 ? '' : 's'}, {result.counts.tool_capable} usable by tret
      </div>
      <table className="mono-table" style={{ marginTop: 8 }}>
        <thead>
          <tr>
            <th>Model</th>
            <th>Tool calling</th>
            <th className="num">Context</th>
          </tr>
        </thead>
        <tbody>
          {result.models.map((m) => (
            <tr key={m.id}>
              <td>{m.display_name}</td>
              <td style={{ color: m.supports_tools ? 'var(--green)' : 'var(--red)' }}>
                {m.supports_tools ? '✓' : '✗'}
              </td>
              <td className="num">
                {m.context_window > 0 ? m.context_window.toLocaleString() : 'not reported'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {result.counts.no_tools > 0 && (
        <div
          style={{
            marginTop: 6,
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--text-muted)',
          }}
        >
          A ✗ means the model failed tret's forced-tool-call probe and is excluded from routing —
          pick a tool-calling build (e.g. <code>{OLLAMA_MODEL}</code>) rather than working around
          it. See <code>docs/local-models.md</code>.
        </div>
      )}
    </div>
  )
}

function LocalModels({ configured }: { configured: boolean }) {
  const testMutation = useMutation({ mutationFn: api.testLocalProvider })
  const testError = testMutation.error as ApiError | null

  if (!configured) return <SetupGuide />

  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <div className="mono-label">Local model server</div>
          <div
            style={{
              fontFamily: 'var(--mono)',
              fontSize: 11,
              color: 'var(--text-muted)',
              marginTop: 4,
            }}
          >
            Re-reads the configured server now, ignoring the 5-minute discovery cache, and re-probes
            each model's tool calling.
          </div>
        </div>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={() => testMutation.mutate()}
          disabled={testMutation.isPending}
        >
          {testMutation.isPending ? 'testing…' : 'Test connection'}
        </button>
      </div>

      {testMutation.isPending && (
        <div className="empty pulse" style={{ padding: '10px 0' }}>
          Contacting the server and probing tool calling — up to ~45s with several models…
        </div>
      )}
      {testError && (
        <div className="error-text" style={{ marginTop: 10 }}>
          {testError.status === 403 ? 'Requires admin role.' : testError.message}
        </div>
      )}
      {testMutation.data && !testMutation.isPending && <TestResult result={testMutation.data} />}
    </div>
  )
}

// ── Router info ───────────────────────────────────────────────────────────

function RouterInfo() {
  const routerQuery = useQuery({ queryKey: ['router-settings'], queryFn: api.routerSettings })

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        LLM router
      </div>
      {routerQuery.isLoading ? (
        <div className="empty pulse">Loading router settings…</div>
      ) : routerQuery.isError ? (
        <div className="error-text">{(routerQuery.error as Error).message}</div>
      ) : routerQuery.data ? (
        <div className="panel config-stats">
          <div className="config-stat">
            <div className="mono-label">Router model</div>
            <div className="mono-body">{routerQuery.data.router_model}</div>
          </div>
          <div className="config-stat">
            <div className="mono-label">Prompt version</div>
            <div className="mono-body">{routerQuery.data.routing_prompt_version}</div>
          </div>
          <div className="config-stat">
            <div className="mono-label">Timeout</div>
            <div className="mono-body">{routerQuery.data.timeout_seconds}s</div>
          </div>
        </div>
      ) : null}
    </div>
  )
}

// ── Network access ────────────────────────────────────────────────────────
// The kill switch, and an honest picture of what this deployment can reach.
//
// The one rule this UI has to respect: modes narrow, never widen. The API
// accepts a wider request and returns the mode actually in force, so every
// control here renders the *response* rather than what it asked for — an
// operator must never be shown egress they did not actually get back.

const CLASS_LABELS: Record<string, string> = {
  provider: 'Cloud model providers',
  catalog: 'Model catalog',
  local: 'Local model server',
  research: 'Web search & page fetch',
  search: 'Web search backend',
}

const CLASS_HINTS: Record<string, string> = {
  provider: 'Anthropic, Kimi, OpenRouter',
  catalog: 'the OpenRouter model list and key checks',
  local: 'your own server — not covered by the master switch',
  research: 'the only class an agent points at a URL it chose',
  search: 'the configured SearXNG/Brave endpoint — no switch of its own, follows research',
}

function NetworkAccess() {
  const queryClient = useQueryClient()
  const egressQuery = useQuery({ queryKey: ['egress-settings'], queryFn: api.egressSettings })
  const [pending, setPending] = useState<string | null>(null)

  const narrow = useMutation({
    mutationFn: ({ cls, mode }: { cls: string; mode: EgressMode }) => api.setEgress(cls, mode),
    onSettled: () => {
      setPending(null)
      queryClient.invalidateQueries({ queryKey: ['egress-settings'] })
      // Availability of the web tools follows the research class.
      queryClient.invalidateQueries({ queryKey: ['tools'] })
    },
  })

  const restore = useMutation({
    mutationFn: (cls: string) => api.clearEgressOverride(cls),
    onSettled: () => {
      setPending(null)
      queryClient.invalidateQueries({ queryKey: ['egress-settings'] })
      queryClient.invalidateQueries({ queryKey: ['tools'] })
    },
  })

  const data = egressQuery.data

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Network access
      </div>
      {egressQuery.isLoading ? (
        <div className="empty pulse">Loading network settings…</div>
      ) : egressQuery.isError ? (
        <QueryError error={egressQuery.error} what="this deployment's network settings" />
      ) : data ? (
        <div className="panel stack" style={{ gap: 14 }}>
          <div className="mono-body" style={{ opacity: 0.75 }}>
            {data.note}
          </div>
          {Object.entries(data.classes).map(([cls, status]) => (
            <div key={cls} className="stack" style={{ gap: 4 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <StatusBadge status={status.mode} />
                <span className="mono-body">{CLASS_LABELS[cls] ?? cls}</span>
                <span className="mono-body" style={{ opacity: 0.6 }}>
                  {CLASS_HINTS[cls] ?? ''}
                </span>
                <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                  {status.mode !== 'off' ? (
                    <button
                      className="btn btn-sm btn-danger"
                      disabled={pending === cls}
                      onClick={() => {
                        setPending(cls)
                        narrow.mutate({ cls, mode: 'off' })
                      }}
                    >
                      Cut
                    </button>
                  ) : null}
                  {status.runtime_override ? (
                    <button
                      className="btn btn-sm"
                      disabled={pending === cls}
                      onClick={() => {
                        setPending(cls)
                        restore.mutate(cls)
                      }}
                    >
                      Restore
                    </button>
                  ) : null}
                </span>
              </div>
              <div className="mono-body" style={{ opacity: 0.6, paddingLeft: 4 }}>
                {status.runtime_override
                  ? `narrowed here to ${status.runtime_override}; environment allows ${status.configured}`
                  : `set by the environment (${status.configured})`}
                {status.allow_hosts.length > 0
                  ? ` · hosts: ${status.allow_hosts.join(', ')}`
                  : cls === 'research' && status.mode !== 'off'
                    ? ' · no allowlist: any public host'
                    : ''}
              </div>
            </div>
          ))}
          <div className="mono-body" style={{ opacity: 0.6 }}>
            Search backend: {data.search_backend}
            {data.proxy ? ' · all traffic routed through the configured egress proxy' : ''}
          </div>
          {narrow.isError || restore.isError ? (
            <div className="error-text">
              {((narrow.error ?? restore.error) as Error).message}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

// ── Data requests ─────────────────────────────────────────────────────────

function DataRequests() {
  const queryClient = useQueryClient()
  const requestsQuery = useQuery({
    queryKey: ['data-requests'],
    queryFn: () => api.listDataRequests(),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: 'fulfilled' | 'dismissed' }) =>
      api.updateDataRequest(id, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['data-requests'] }),
  })

  const requests = requestsQuery.data ?? []

  return (
    <div>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Data requests
      </div>
      {requestsQuery.isLoading ? (
        <div className="empty pulse">Loading data requests…</div>
      ) : requestsQuery.isError ? (
        <QueryError error={requestsQuery.error} what="the open data requests" />
      ) : requests.length === 0 ? (
        <div className="empty">No data requests — models file these when required data is missing.</div>
      ) : (
        <table className="mono-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Subject</th>
              <th>Missing</th>
              <th>Why needed</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {requests.map((r) => (
              <tr key={r.id}>
                <td>{r.created_at ? formatDateTime(r.created_at) : '—'}</td>
                <td>{JSON.stringify(r.subject)}</td>
                <td style={{ maxWidth: 220 }}>{r.what_is_missing}</td>
                <td style={{ maxWidth: 220, color: 'var(--text-muted)' }}>{r.why_needed}</td>
                <td>
                  <StatusBadge status={r.status} />
                </td>
                <td>
                  {r.status === 'open' && (
                    <span className="row" style={{ gap: 6 }}>
                      <button
                        className="btn btn-sm btn-approve"
                        onClick={() => updateMutation.mutate({ id: r.id, status: 'fulfilled' })}
                        disabled={updateMutation.isPending}
                      >
                        Fulfill
                      </button>
                      <button
                        className="btn btn-sm"
                        onClick={() => updateMutation.mutate({ id: r.id, status: 'dismissed' })}
                        disabled={updateMutation.isPending}
                      >
                        Dismiss
                      </button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {updateMutation.isError && (
        <div className="error-text" style={{ marginTop: 8 }}>
          {(updateMutation.error as Error).message}
        </div>
      )}
    </div>
  )
}
