import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'

import { api, ApiError, type User } from '../api/client'
import { type Column, MonoTable } from '../components/shared/MonoTable'
import { StatusBadge } from '../components/shared/StatusBadge'
import { formatDateTime } from './Runs'

export function SettingsView() {
  return (
    <div className="stack" style={{ gap: 28, maxWidth: 780 }}>
      <div>
        <h1 className="view-title">Settings</h1>
        <div className="view-sub">
          Team, provider keys, router configuration, and open data requests.
        </div>
      </div>
      <TeamSection />
      <ProviderKeys />
      <RouterInfo />
      <DataRequests />
    </div>
  )
}

// ── Team ──────────────────────────────────────────────────────────────────

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

function TeamSection() {
  const queryClient = useQueryClient()
  const usersQuery = useQuery({ queryKey: ['users'], queryFn: api.listUsers, retry: false })

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
  const isAdmin = !(usersQuery.isError && listError?.status === 403)

  const columns: Column<User>[] = [
    { key: 'email', header: 'Email', render: (u) => u.email },
    { key: 'name', header: 'Display name', render: (u) => u.display_name },
    {
      key: 'role',
      header: 'Role',
      render: (u) => (
        <span
          className={`badge ${u.role === 'admin' ? 'badge-violet' : u.role === 'approver' ? 'badge-green' : 'badge-blue'}`}
        >
          {u.role}
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
        Team
      </div>

      {usersQuery.isLoading ? (
        <div className="empty pulse">Loading users…</div>
      ) : usersQuery.isError ? (
        <div className="empty">
          {listError?.status === 403 ? 'Admin only.' : listError?.message}
        </div>
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
  const queryClient = useQueryClient()
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providerStatus })

  const [provider, setProvider] = useState('anthropic')
  const [apiKey, setApiKey] = useState('')

  const setKeyMutation = useMutation({
    mutationFn: () => api.setProviderKey(provider, apiKey),
    onSuccess: () => {
      setApiKey('')
      queryClient.invalidateQueries({ queryKey: ['providers'] })
      queryClient.invalidateQueries({ queryKey: ['models'] })
    },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (apiKey.trim()) setKeyMutation.mutate()
  }

  const setKeyError = setKeyMutation.error as ApiError | null

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
        <table className="mono-table" style={{ marginBottom: 14 }}>
          <thead>
            <tr>
              <th>Provider</th>
              <th>Configured</th>
              <th>Source</th>
              <th>Key</th>
            </tr>
          </thead>
          <tbody>
            {(providersQuery.data ?? []).map((p) => (
              <tr key={p.provider}>
                <td>{p.provider}</td>
                <td>
                  <span
                    style={{
                      display: 'inline-block',
                      width: 8,
                      height: 8,
                      borderRadius: 4,
                      background: p.configured ? 'var(--green)' : 'var(--red)',
                      marginRight: 6,
                    }}
                  />
                  {p.configured ? 'yes' : 'no'}
                </td>
                <td>{p.source ?? '—'}</td>
                <td>{p.last4 ? `••••${p.last4}` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <form onSubmit={submit} className="panel">
        <div className="mono-label" style={{ marginBottom: 10 }}>
          Set provider key (admin, write-only)
        </div>
        <div className="row" style={{ alignItems: 'flex-end' }}>
          <div className="field" style={{ marginBottom: 0, width: 160 }}>
            <label className="mono-label">Provider</label>
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              <option value="anthropic">anthropic</option>
              <option value="kimi">kimi</option>
              <option value="openrouter">openrouter</option>
            </select>
          </div>
          <div className="field" style={{ marginBottom: 0, flex: 1 }}>
            <label className="mono-label">API key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-…"
              autoComplete="off"
            />
          </div>
          <button className="btn btn-primary" type="submit" disabled={setKeyMutation.isPending || !apiKey.trim()}>
            {setKeyMutation.isPending ? 'Saving…' : 'Save key'}
          </button>
        </div>
        {setKeyError && (
          <div className="error-text" style={{ marginTop: 10 }}>
            {setKeyError.status === 403 ? 'Requires admin role.' : setKeyError.message}
          </div>
        )}
        {setKeyMutation.isSuccess && (
          <div className="mono-label" style={{ marginTop: 10, color: 'var(--green)' }}>
            key stored (encrypted at rest)
          </div>
        )}
      </form>
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
