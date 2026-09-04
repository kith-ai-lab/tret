import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { api, type ConnectionProvider, type ImportItem } from '../../api/client'
import { openGooglePicker } from './googleDrivePicker'
import { M365ImportModal } from './M365ImportModal'
import { Modal } from './Modal'

const PROVIDER_MENU_LABELS: Record<ConnectionProvider, string> = {
  gdrive: 'Google Drive',
  m365: 'SharePoint / OneDrive',
}

/** "Import from…" — shown on the Documents view once at least one connection
 *  is active. Two flows share one import mutation: Google Drive opens the
 *  Google Picker directly (imperative, no modal of its own); SharePoint /
 *  OneDrive opens `M365ImportModal`'s tree browser. Both end up POSTing the
 *  same `{provider, items}` shape to the import endpoint and show the same
 *  per-item results, including errors — the two source pickers are the only
 *  thing that differs between the flows. */
export function ImportFromMenu({ projectId }: { projectId: string | null }) {
  const queryClient = useQueryClient()
  const connectionsQuery = useQuery({ queryKey: ['connections'], queryFn: api.listConnections })
  const providersQuery = useQuery({ queryKey: ['connection-providers'], queryFn: api.connectionProviders })

  const [menuOpen, setMenuOpen] = useState(false)
  const [m365Open, setM365Open] = useState(false)
  const [pickerError, setPickerError] = useState<string | null>(null)
  const [pickerBusy, setPickerBusy] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const importMutation = useMutation({
    mutationFn: ({ provider, items }: { provider: ConnectionProvider; items: ImportItem[] }) => {
      if (!projectId) {
        throw new Error('Could not determine the current project — reload the page and try again.')
      }
      return api.importDocuments(projectId, provider, items)
    },
    onSuccess: (res) => {
      if (res.documents.length > 0) {
        queryClient.invalidateQueries({ queryKey: ['documents'] })
      }
    },
  })

  // The results modal (below) takes over as soon as the import settles —
  // close the m365 tree browser out from under it rather than stacking two
  // dialogs.
  useEffect(() => {
    if (importMutation.isSuccess || importMutation.isError) setM365Open(false)
  }, [importMutation.isSuccess, importMutation.isError])

  const activeProviders = new Set(
    (connectionsQuery.data?.connections ?? []).filter((c) => c.status === 'active').map((c) => c.provider),
  )
  const providerInfo = new Map((providersQuery.data?.providers ?? []).map((p) => [p.provider, p]))

  if (activeProviders.size === 0) return null

  const runGoogleDrivePicker = async () => {
    setMenuOpen(false)
    setPickerError(null)
    const picker = providerInfo.get('gdrive')?.picker
    if (!picker) {
      setPickerError(
        'Google Picker is not configured on this deployment — set TRET_GDRIVE_PICKER_API_KEY and TRET_GDRIVE_APP_ID.',
      )
      return
    }
    setPickerBusy(true)
    try {
      const items = await openGooglePicker(picker, async () => {
        const token = await api.gdriveToken()
        return token.access_token
      })
      if (items.length > 0) {
        importMutation.mutate({ provider: 'gdrive', items })
      }
    } catch (err) {
      setPickerError(err instanceof Error ? err.message : 'Could not open the Google Picker.')
    } finally {
      setPickerBusy(false)
    }
  }

  const openM365Browser = () => {
    setMenuOpen(false)
    setPickerError(null)
    setM365Open(true)
  }

  return (
    <div className="routing-wrap" ref={wrapRef}>
      <button
        type="button"
        className="btn"
        disabled={pickerBusy}
        onClick={() => setMenuOpen((o) => !o)}
        aria-expanded={menuOpen}
        aria-haspopup="true"
      >
        {pickerBusy ? 'Opening picker…' : 'Import from…'}
      </button>

      {menuOpen && (
        <div
          role="menu"
          style={{
            position: 'absolute',
            zIndex: 60,
            top: 'calc(100% + 4px)',
            right: 0,
            minWidth: 200,
            background: 'var(--bg-panel)',
            border: '1px solid var(--border)',
            borderRadius: 6,
            padding: 4,
            boxShadow: '0 8px 30px rgba(0, 0, 0, 0.3)',
            display: 'flex',
            flexDirection: 'column',
            gap: 2,
          }}
        >
          {activeProviders.has('gdrive') && (
            <button type="button" className="list-item" role="menuitem" onClick={() => void runGoogleDrivePicker()}>
              {PROVIDER_MENU_LABELS.gdrive}
            </button>
          )}
          {activeProviders.has('m365') && (
            <button type="button" className="list-item" role="menuitem" onClick={openM365Browser}>
              {PROVIDER_MENU_LABELS.m365}
            </button>
          )}
        </div>
      )}

      {pickerError && (
        <div className="error-text" style={{ marginTop: 8, maxWidth: 320 }}>
          {pickerError}
        </div>
      )}

      <M365ImportModal
        open={m365Open}
        onClose={() => setM365Open(false)}
        importing={importMutation.isPending}
        onImport={(items) => importMutation.mutate({ provider: 'm365', items })}
      />

      <Modal
        open={importMutation.isSuccess || importMutation.isError}
        onClose={() => importMutation.reset()}
        title="Import results"
        footer={
          <button type="button" className="btn btn-sm" onClick={() => importMutation.reset()}>
            Close
          </button>
        }
      >
        {importMutation.isError ? (
          <div className="error-text">
            {importMutation.error instanceof Error ? importMutation.error.message : 'Import failed.'}
          </div>
        ) : importMutation.data ? (
          <div className="stack" style={{ gap: 12 }}>
            {importMutation.data.documents.length > 0 && (
              <div>
                <div className="mono-label" style={{ marginBottom: 6, color: 'var(--green)' }}>
                  Imported {importMutation.data.documents.length}
                </div>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {importMutation.data.documents.map((d) => (
                    <li key={d.id} className="mono-body">
                      {d.filename}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {importMutation.data.errors.length > 0 && (
              <div>
                <div className="mono-label" style={{ marginBottom: 6, color: 'var(--red)' }}>
                  Could not import {importMutation.data.errors.length}
                </div>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {importMutation.data.errors.map((e) => (
                    <li key={e.id} className="error-text">
                      {e.name} — {e.detail}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {importMutation.data.documents.length === 0 && importMutation.data.errors.length === 0 && (
              <div className="empty">Nothing was imported.</div>
            )}
          </div>
        ) : null}
      </Modal>
    </div>
  )
}
