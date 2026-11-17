import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api, ApiError, gateRefusalDetail, type DatabaseConnection } from '../../api/client'
import { formatDateTime } from '../shared/format'
import { useConfirm } from '../shared/ConfirmDialog'
import { QueryError } from '../shared/MonoTable'

function errorText(error: unknown): string {
  const e = error as ApiError
  if (e.status === 403) return gateRefusalDetail(e) ?? 'Requires the owner or admin role.'
  return e.message
}

/** "Databases" section of the connections page: read-only Postgres / Supabase. */
export function DatabaseConnections() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity })
  const listQuery = useQuery({
    queryKey: ['database-connections'],
    queryFn: api.listDatabaseConnections,
    retry: false,
  })
  const isAdmin = ['owner', 'admin'].includes(meQuery.data?.role ?? '')
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['database-connections'] })

  const [label, setLabel] = useState('')
  const [dsn, setDsn] = useState('')
  const addMutation = useMutation({
    mutationFn: () => api.addDatabaseConnection({ label: label.trim(), dsn: dsn.trim() }),
    onSuccess: () => {
      setLabel('')
      setDsn('')
      refresh()
    },
  })
  const testMutation = useMutation({
    mutationFn: (slug: string) => api.testDatabaseConnection(slug),
    onSettled: refresh,
  })
  const removeMutation = useMutation({
    mutationFn: (slug: string) => api.removeDatabaseConnection(slug),
    onSuccess: refresh,
  })

  const listError = listQuery.error as ApiError | null
  if (listError && listError.status === 404) return null // feature off on this deployment

  const remove = async (c: DatabaseConnection) => {
    const ok = await confirm({
      title: 'Remove database?',
      message: `Remove "${c.label}"? The stored connection string is deleted and runs can no longer query it.`,
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (ok) removeMutation.mutate(c.slug)
  }

  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="mono-label" style={{ marginBottom: 8 }}>
        Databases (read-only Postgres / Supabase)
      </div>
      {listQuery.isLoading ? (
        <div className="empty pulse">Loading…</div>
      ) : listError ? (
        <QueryError error={listError} what="database connections" />
      ) : (listQuery.data ?? []).length === 0 ? (
        <div className="mono-body" style={{ color: 'var(--text-muted)' }}>
          No databases connected.
        </div>
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          {(listQuery.data ?? []).map((c) => (
            <div key={c.slug} style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: 10 }}>
              <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <div>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="mono-body">{c.label}</span>
                    <span className="chip">{c.status}</span>
                  </div>
                  <div className="fine-print">
                    {c.host}:{c.port} / {c.database} as {c.user}
                    {c.last_tested_at ? ` · tested ${formatDateTime(c.last_tested_at)}` : ''}
                  </div>
                </div>
                {isAdmin && (
                  <div className="row" style={{ gap: 6 }}>
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={testMutation.isPending || addMutation.isPending}
                      onClick={() => testMutation.mutate(c.slug)}
                    >
                      Test
                    </button>
                    <button type="button" className="btn btn-sm btn-danger" onClick={() => remove(c)}>
                      Remove
                    </button>
                  </div>
                )}
              </div>
              {c.status !== 'active' && c.last_error && (
                <div className="error-text" style={{ marginTop: 6 }}>
                  {c.last_error}
                </div>
              )}
              {c.status === 'error' && (
                <div className="fine-print" style={{ marginTop: 4 }}>
                  Runs cannot use this database until an admin re-tests it successfully.
                </div>
              )}
              {c.warnings.map((w) => (
                <div key={w} className="callout callout-note" style={{ marginTop: 6 }}>
                  {w}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      {testMutation.error != null && (
        <div className="error-text" style={{ marginTop: 8 }}>
          {errorText(testMutation.error)}
        </div>
      )}
      {removeMutation.error != null && (
        <div className="error-text" style={{ marginTop: 8 }}>
          {errorText(removeMutation.error)}
        </div>
      )}

      {isAdmin ? (
        <form
          style={{ marginTop: 14 }}
          autoComplete="off"
          onSubmit={(e) => {
            e.preventDefault()
            addMutation.mutate()
          }}
        >
          <div className="mono-label" style={{ marginBottom: 6 }}>
            Add database
          </div>
          <div className="field">
            <label className="mono-label" htmlFor="db-label">
              Label
            </label>
            <input
              id="db-label"
              className="input"
              value={label}
              maxLength={80}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Warehouse"
            />
          </div>
          <div className="field">
            <label className="mono-label" htmlFor="db-dsn">
              Connection string
            </label>
            <input
              id="db-dsn"
              className="input"
              type="password"
              autoComplete="off"
              value={dsn}
              onChange={(e) => setDsn(e.target.value)}
              placeholder="postgresql://user:password@db.<project>.supabase.co:5432/postgres"
            />
          </div>
          <div className="fine-print">
            Use a dedicated read-only database role. The connection string is stored encrypted and is never
            shown again. Only Supabase hosts are allowed unless the operator configured otherwise.
          </div>
          <button
            type="submit"
            className="btn btn-primary"
            style={{ marginTop: 10 }}
            disabled={addMutation.isPending || !label.trim() || !dsn.trim()}
          >
            {addMutation.isPending ? 'Connecting…' : 'Add database'}
          </button>
          {addMutation.error != null && (
            <div className="error-text" style={{ marginTop: 8 }}>
              {errorText(addMutation.error)}
            </div>
          )}
        </form>
      ) : (
        <div className="fine-print" style={{ marginTop: 10 }}>
          Adding or removing databases requires the owner or admin role.
        </div>
      )}
    </div>
  )
}
