import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'

import { api, ApiError, takesApiKey } from '../../api/client'

/** The write-only "set provider key" form (admin), extracted from SettingsView so
 *  the first-run setup surface can offer the same flow without forking it.
 *
 *  The roster comes from the backend, which derives it from
 *  providers/catalog.py::PROVIDER_SPECS — this form keeps no provider list of
 *  its own, so adding a provider stays a one-place change. Key-optional
 *  providers (local: a base URL, not a key) are excluded: there is nothing to
 *  submit for them here.
 *
 *  A successful save invalidates ['providers'] and ['models']. That is also
 *  what dismisses the first-run surface — it watches the same ['providers']
 *  query, so no extra wiring is needed for it to disappear.
 *
 *  Gated client-side on owner/admin in the current workspace, mirroring
 *  `POST /settings/providers`'s own admin requirement — the request would
 *  403 either way, but a form that hides itself for a role that cannot use it
 *  reads better than one that lets you fill it in and fail at submit. */
export function ProviderKeyForm({
  heading = 'Set provider key (admin, write-only)',
  onSaved,
}: {
  heading?: string
  /** Called after a successful save, in addition to the query invalidation. */
  onSaved?: () => void
}) {
  const queryClient = useQueryClient()
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providerStatus })
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })

  const keyProviders = (providersQuery.data ?? []).map((p) => p.provider).filter(takesApiKey)

  const [provider, setProvider] = useState('')
  const [apiKey, setApiKey] = useState('')

  // Select the first provider once the roster loads; never overwrite a choice
  // the user made, and never leave a stale name selected if the roster changes.
  useEffect(() => {
    if (keyProviders.length === 0) return
    setProvider((curr) => (curr && keyProviders.includes(curr) ? curr : keyProviders[0]))
  }, [keyProviders.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  const setKeyMutation = useMutation({
    mutationFn: () => api.setProviderKey(provider, apiKey),
    onSuccess: () => {
      setApiKey('')
      queryClient.invalidateQueries({ queryKey: ['providers'] })
      queryClient.invalidateQueries({ queryKey: ['models'] })
      onSaved?.()
    },
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (provider && apiKey.trim()) setKeyMutation.mutate()
  }

  const setKeyError = setKeyMutation.error as ApiError | null

  // Wait for `me` to settle before deciding — App.tsx already resolves it
  // before this component can mount, so this is normally instant, but a
  // brief flash of "requires admin" ahead of the real answer would be worse
  // than the wait.
  if (meQuery.isLoading) return null
  if (!['owner', 'admin'].includes(meQuery.data?.role ?? '')) {
    return (
      <div className="panel mono-body" style={{ color: 'var(--text-muted)' }}>
        Setting provider keys requires the owner or admin role in this workspace.
      </div>
    )
  }

  return (
    <form onSubmit={submit} className="panel">
      <div className="mono-label" style={{ marginBottom: 10 }}>
        {heading}
      </div>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <div className="field" style={{ marginBottom: 0, width: 160 }}>
          <label className="mono-label">Provider</label>
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            {keyProviders.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
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
        <button
          className="btn btn-primary"
          type="submit"
          disabled={setKeyMutation.isPending || !provider || !apiKey.trim()}
        >
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
  )
}
