import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api, ApiError, type M365BrowseItem, type M365ReadEntry } from '../../api/client'
import { Modal } from '../shared/Modal'
import { QueryError } from '../shared/MonoTable'

/** Modal allowlist picker over the m365 browse tree: sites and OneDrive at
 *  the root (same `browseM365({})` call `M365ImportModal` makes), each site
 *  expanding in place to its document-library drives. Unlike the import
 *  modal this never drills into folders — a read-access entry names a whole
 *  drive, never a subfolder — so the tree is two levels deep and flat, an
 *  accordion rather than a breadcrumb trail.
 *
 *  `initialEntries` seeds the checked set from the connection's current
 *  `selected_resources.read`; Save always PUTs the *complete* set, replacing
 *  whatever the backend had, which is what lets an empty save mean "allow
 *  everything" elsewhere in the card without a separate code path here. */
export function M365ReadAccessPicker({
  open,
  onClose,
  initialEntries,
  onSaved,
}: {
  open: boolean
  onClose: () => void
  initialEntries: M365ReadEntry[]
  onSaved: () => void
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Map<string, M365ReadEntry>>(new Map())

  // Reset to the connection's current allowlist on every open — the modal
  // stays mounted between visits, so without this a prior session's edits
  // (or a stale selection from before the last save) would carry over.
  useEffect(() => {
    if (open) {
      setSelected(new Map(initialEntries.map((e) => [e.drive_id, e])))
      setExpanded(new Set())
    }
    // initialEntries is a fresh array/object each render (derived from the
    // connections query); keying off `open` alone is what makes this "reset
    // on open", not "reset whenever the parent re-renders while open".
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const rootQuery = useQuery({
    queryKey: ['m365-read-access-root'],
    queryFn: () => api.browseM365({}),
    enabled: open,
  })

  const saveMutation = useMutation({
    mutationFn: () => api.setM365ReadAccess(Array.from(selected.values())),
    onSuccess: () => {
      onSaved()
      onClose()
    },
  })
  const saveError = saveMutation.error as ApiError | null

  const toggle = (entry: M365ReadEntry) => {
    setSelected((prev) => {
      const next = new Map(prev)
      if (next.has(entry.drive_id)) {
        next.delete(entry.drive_id)
      } else {
        next.set(entry.drive_id, entry)
      }
      return next
    })
  }

  const toggleExpanded = (siteId: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(siteId)) {
        next.delete(siteId)
      } else {
        next.add(siteId)
      }
      return next
    })
  }

  const close = () => {
    if (saveMutation.isPending) return
    onClose()
  }

  const items = rootQuery.data?.items ?? []
  const rootDrives = items.filter((i) => i.kind === 'drive')
  const sites = items.filter((i) => i.kind === 'site')

  return (
    <Modal
      open={open}
      onClose={close}
      title="Choose read-access locations"
      subtitle="Sites and OneDrive the connected account can see. Runs and chats will only search and read from what's checked here."
      width={640}
      footer={
        <>
          <button type="button" className="btn btn-sm" onClick={close} disabled={saveMutation.isPending}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            {saveMutation.isPending
              ? 'Saving…'
              : selected.size === 0
                ? 'Save (allow everything)'
                : `Save (${selected.size} location${selected.size === 1 ? '' : 's'})`}
          </button>
        </>
      }
    >
      {rootQuery.isLoading ? (
        <div className="empty pulse">Loading…</div>
      ) : rootQuery.isError ? (
        <QueryError error={rootQuery.error} what="sites and drives" />
      ) : items.length === 0 ? (
        <div className="empty">Nothing here.</div>
      ) : (
        <div style={{ maxHeight: 400, overflowY: 'auto' }}>
          {rootDrives.map((drive) => (
            <label
              key={drive.id}
              className="check-row"
              style={{ padding: '6px 2px', borderBottom: '1px solid var(--border-subtle)' }}
            >
              <input
                type="checkbox"
                checked={selected.has(drive.id)}
                onChange={() =>
                  toggle({
                    site_id: null,
                    drive_id: drive.id,
                    label: drive.name,
                    kind: 'onedrive',
                    web_url: null,
                  })
                }
              />
              <span>{drive.name}</span>
              <span className="desc">OneDrive</span>
            </label>
          ))}
          {sites.map((site) => (
            <SiteDrives
              key={site.id}
              site={site}
              expanded={expanded.has(site.id)}
              onToggleExpand={() => toggleExpanded(site.id)}
              selected={selected}
              onToggleDrive={toggle}
              enabled={open}
            />
          ))}
        </div>
      )}
      {saveError && (
        <div className="error-text" style={{ marginTop: 10 }}>
          {saveError.message}
        </div>
      )}
    </Modal>
  )
}

/** One site's row plus, once expanded, its document-library drives — each a
 *  checkbox building the `kind: 'site_drive'` entry the parent's `selected`
 *  map keys by drive id. Its own `browseM365({ scope: 'drive_children', ... })`
 *  query so the parent only ever fetches the drives of sites actually opened. */
function SiteDrives({
  site,
  expanded,
  onToggleExpand,
  selected,
  onToggleDrive,
  enabled,
}: {
  site: M365BrowseItem
  expanded: boolean
  onToggleExpand: () => void
  selected: Map<string, M365ReadEntry>
  onToggleDrive: (entry: M365ReadEntry) => void
  enabled: boolean
}) {
  const drivesQuery = useQuery({
    queryKey: ['m365-read-access-site-drives', site.id],
    queryFn: () => api.browseM365({ scope: 'drive_children', site_id: site.id }),
    enabled: enabled && expanded,
  })
  const drives = (drivesQuery.data?.items ?? []).filter((i) => i.kind === 'drive')

  return (
    <div style={{ borderBottom: '1px solid var(--border-subtle)' }}>
      <button
        type="button"
        className="btn btn-sm"
        style={{ width: '100%', textAlign: 'left', border: 'none', background: 'none', padding: '6px 2px' }}
        onClick={onToggleExpand}
        aria-expanded={expanded}
      >
        <span style={{ color: 'var(--text-muted)', marginRight: 6 }}>{expanded ? '▾' : '▸'}</span>
        {site.name}
      </button>
      {expanded &&
        (drivesQuery.isLoading ? (
          <div className="empty pulse" style={{ paddingLeft: 20 }}>
            Loading…
          </div>
        ) : drivesQuery.isError ? (
          <div style={{ paddingLeft: 20 }}>
            <QueryError error={drivesQuery.error} what={`${site.name}'s drives`} />
          </div>
        ) : drives.length === 0 ? (
          <div className="empty" style={{ paddingLeft: 20 }}>
            No document libraries.
          </div>
        ) : (
          drives.map((drive) => (
            <label key={drive.id} className="check-row" style={{ paddingLeft: 20 }}>
              <input
                type="checkbox"
                checked={selected.has(drive.id)}
                onChange={() =>
                  onToggleDrive({
                    site_id: site.id,
                    drive_id: drive.id,
                    label: `${site.name} — ${drive.name}`,
                    kind: 'site_drive',
                    web_url: site.web_url ?? null,
                  })
                }
              />
              <span>{drive.name}</span>
            </label>
          ))
        ))}
    </div>
  )
}
