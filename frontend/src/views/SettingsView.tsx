import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'

import { api, ApiError } from '../api/client'
import { StatusBadge } from '../components/shared/StatusBadge'
import { formatDateTime } from './Runs'

export function SettingsView() {
  return (
    <div className="stack" style={{ gap: 28, maxWidth: 780 }}>
      <div>
        <h1 className="view-title">Settings</h1>
        <div className="view-sub">Provider keys, router configuration, and open data requests.</div>
      </div>
      <ProviderKeys />
      <RouterInfo />
      <DataRequests />
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
