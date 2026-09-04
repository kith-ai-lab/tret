import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api, type ImportItem, type M365BrowseItem } from '../../api/client'
import { Modal } from './Modal'
import { QueryError } from './MonoTable'

/** One step in the breadcrumb trail: a label for the crumb button, and the
 *  `browseM365` params that reproduce that step. The root crumb passes no
 *  params at all — the backend defaults `scope=sites` and folds in the
 *  pseudo "OneDrive" entry itself. */
interface Crumb {
  label: string
  params: { scope?: 'sites' | 'drive_children'; site_id?: string; drive_id?: string; item_id?: string }
}

const ROOT_CRUMB: Crumb = { label: 'Sites & OneDrive', params: {} }

function crumbFor(item: M365BrowseItem): Crumb | null {
  if (item.kind === 'site') {
    return { label: item.name, params: { scope: 'drive_children', site_id: item.id } }
  }
  if (item.kind === 'drive') {
    return { label: item.name, params: { scope: 'drive_children', drive_id: item.id } }
  }
  if (item.kind === 'folder' && item.drive_id) {
    return {
      label: item.name,
      params: { scope: 'drive_children', drive_id: item.drive_id, item_id: item.id },
    }
  }
  return null
}

function formatSize(bytes: number | null | undefined): string {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const KIND_ICON: Record<M365BrowseItem['kind'], string> = {
  site: '◆',
  drive: '▣',
  folder: '▸',
  file: '·',
}

/** Modal tree browser over GET /api/connections/m365/browse: sites and
 *  OneDrive at the root, drilling into drives/folders, multi-select of files
 *  across as many folders as the user visits — selection survives
 *  navigation, since each picked file already carries its own id/name/drive_id
 *  independent of whatever crumb is showing. */
export function M365ImportModal({
  open,
  onClose,
  onImport,
  importing,
}: {
  open: boolean
  onClose: () => void
  onImport: (items: ImportItem[]) => void
  importing: boolean
}) {
  const [trail, setTrail] = useState<Crumb[]>([ROOT_CRUMB])
  const [selected, setSelected] = useState<Map<string, ImportItem>>(new Map())
  const current = trail[trail.length - 1]

  const browseQuery = useQuery({
    queryKey: ['m365-browse', current.params],
    queryFn: () => api.browseM365(current.params),
    enabled: open,
  })

  const items = browseQuery.data?.items ?? []

  // Reset on every close, however it happens — Cancel/Escape/backdrop, or the
  // parent closing this out from under itself once an import settles (see
  // ImportFromMenu). The component itself never unmounts between opens, so
  // without this the trail and selection would carry over into the next visit.
  useEffect(() => {
    if (!open) {
      setTrail([ROOT_CRUMB])
      setSelected(new Map())
    }
  }, [open])

  const toggle = (item: M365BrowseItem) => {
    setSelected((prev) => {
      const next = new Map(prev)
      if (next.has(item.id)) {
        next.delete(item.id)
      } else {
        next.set(item.id, { id: item.id, name: item.name, drive_id: item.drive_id ?? null })
      }
      return next
    })
  }

  const drillInto = (item: M365BrowseItem) => {
    const crumb = crumbFor(item)
    if (!crumb) return
    setTrail((prev) => [...prev, crumb])
  }

  const jumpTo = (index: number) => setTrail((prev) => prev.slice(0, index + 1))

  const selectedItems = Array.from(selected.values())

  const close = () => {
    if (importing) return
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Import from SharePoint / OneDrive"
      subtitle="Browse sites and drives with the connected account, then pick files to import."
      width={640}
      footer={
        <>
          <button type="button" className="btn btn-sm" onClick={close} disabled={importing}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={selectedItems.length === 0 || importing}
            onClick={() => onImport(selectedItems)}
          >
            {importing
              ? 'Importing…'
              : selectedItems.length === 0
                ? 'Import'
                : `Import ${selectedItems.length} file${selectedItems.length === 1 ? '' : 's'}`}
          </button>
        </>
      }
    >
      <div className="row" style={{ flexWrap: 'wrap', gap: 4, marginBottom: 12 }}>
        {trail.map((crumb, i) => (
          <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            {i > 0 && <span style={{ color: 'var(--text-muted)' }}>/</span>}
            <button
              type="button"
              className="btn btn-sm"
              disabled={i === trail.length - 1}
              onClick={() => jumpTo(i)}
              style={i === trail.length - 1 ? { opacity: 1, cursor: 'default', borderColor: 'transparent' } : undefined}
            >
              {crumb.label}
            </button>
          </span>
        ))}
      </div>

      {browseQuery.isLoading ? (
        <div className="empty pulse">Loading…</div>
      ) : browseQuery.isError ? (
        <QueryError error={browseQuery.error} what="this folder" />
      ) : items.length === 0 ? (
        <div className="empty">Nothing here.</div>
      ) : (
        <div style={{ maxHeight: 360, overflowY: 'auto' }}>
          {items.map((item) => (
            <div
              key={item.id}
              className="row"
              style={{
                justifyContent: 'space-between',
                padding: '6px 2px',
                borderBottom: '1px solid var(--border-subtle)',
              }}
            >
              {item.kind === 'file' ? (
                <label className="check-row" style={{ flex: 1 }}>
                  <input
                    type="checkbox"
                    checked={selected.has(item.id)}
                    onChange={() => toggle(item)}
                  />
                  <span>{item.name}</span>
                  {item.size != null && <span className="desc">{formatSize(item.size)}</span>}
                </label>
              ) : (
                <button
                  type="button"
                  className="btn btn-sm"
                  style={{ flex: 1, textAlign: 'left', border: 'none', background: 'none' }}
                  onClick={() => drillInto(item)}
                >
                  <span style={{ color: 'var(--text-muted)', marginRight: 6 }}>{KIND_ICON[item.kind]}</span>
                  {item.name}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {selectedItems.length > 0 && (
        <div className="fine-print" style={{ marginTop: 10 }}>
          {selectedItems.length} file{selectedItems.length === 1 ? '' : 's'} selected across this
          session — selections are kept while you browse into other folders.
        </div>
      )}
    </Modal>
  )
}
