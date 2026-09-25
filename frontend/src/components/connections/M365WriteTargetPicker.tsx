import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api, ApiError, type M365BrowseItem, type WriteTarget } from '../../api/client'
import { Modal } from '../shared/Modal'
import { QueryError } from '../shared/MonoTable'

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{1,39}$/

/** Turns a folder name into a starting-point slug: lowercase, non
 *  `[a-z0-9-]` runs collapsed to a single `-`, leading/trailing `-` trimmed,
 *  and clamped to the 40-char limit `SLUG_PATTERN` enforces. Never
 *  guaranteed unique or non-empty on its own — `slugValid` below is what
 *  actually gates Save. */
function slugify(name: string): string {
  const s = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
  return s
}

/** One step in the breadcrumb trail, same shape as `M365ImportModal`'s —
 *  plus `siteId`, carried forward from whichever site crumb (if any) an item
 *  was reached through, since a `WriteTarget` needs the owning site even
 *  once browsing has moved past the site/drive level into folders (where
 *  `M365BrowseItem` itself no longer carries `site_id`). */
interface Crumb {
  label: string
  params: { scope?: 'sites' | 'drive_children'; site_id?: string; drive_id?: string; item_id?: string }
  siteId: string | null
}

const ROOT_CRUMB: Crumb = { label: 'Sites & OneDrive', params: {}, siteId: null }

function crumbFor(item: M365BrowseItem, parentSiteId: string | null): Crumb | null {
  if (item.kind === 'site') {
    return { label: item.name, params: { scope: 'drive_children', site_id: item.id }, siteId: item.id }
  }
  if (item.kind === 'drive') {
    return {
      label: item.name,
      params: { scope: 'drive_children', drive_id: item.id },
      siteId: parentSiteId,
    }
  }
  if (item.kind === 'folder' && item.drive_id) {
    return {
      label: item.name,
      params: { scope: 'drive_children', drive_id: item.drive_id, item_id: item.id },
      siteId: parentSiteId,
    }
  }
  return null
}

const KIND_ICON: Record<M365BrowseItem['kind'], string> = {
  site: '◆',
  drive: '▣',
  folder: '▸',
  file: '·',
}

/** The folder about to be saved as a write target: everything a `WriteTarget`
 *  needs except `slug`/`label`, which the confirm form collects. */
interface PendingFolder {
  item: M365BrowseItem
  siteId: string | null
  path: string
}

/** Modal folder picker for the m365 write-back allowlist: sites → drives →
 *  folders, drilling in with a breadcrumb (same tree `M365ImportModal`
 *  browses). Unlike the import modal and the read-access picker, this never
 *  multi-selects — there is no "write anywhere" mode, so each save adds
 *  exactly one folder. Picking a folder ("Use this folder" on its row) opens
 *  a small confirm step (label + slug) rather than saving immediately, since
 *  the slug has to be validated and can collide with an existing target.
 *
 *  Save PUTs the *complete* write list (`existingTargets` plus the new one)
 *  — same "replace the whole array" contract as the read allowlist's Save,
 *  just never emptied to mean "allow everything" (there is no such mode for
 *  writes). */
export function M365WriteTargetPicker({
  open,
  onClose,
  existingTargets,
  onSaved,
}: {
  open: boolean
  onClose: () => void
  existingTargets: WriteTarget[]
  onSaved: () => void
}) {
  const [trail, setTrail] = useState<Crumb[]>([ROOT_CRUMB])
  const [pending, setPending] = useState<PendingFolder | null>(null)
  const [label, setLabel] = useState('')
  const [slug, setSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
  const current = trail[trail.length - 1]

  const browseQuery = useQuery({
    queryKey: ['m365-write-target-browse', current.params],
    queryFn: () => api.browseM365(current.params),
    enabled: open && !pending,
  })

  const saveMutation = useMutation({
    mutationFn: (target: WriteTarget) => api.setM365WriteTargets([...existingTargets, target]),
    onSuccess: () => {
      onSaved()
      onClose()
    },
  })
  const saveError = saveMutation.error as ApiError | null

  // Reset everything on close, however it happens — the modal stays mounted
  // between visits, so without this a prior session's trail/confirm step
  // would carry over into the next open.
  useEffect(() => {
    if (!open) {
      setTrail([ROOT_CRUMB])
      setPending(null)
      setLabel('')
      setSlug('')
      setSlugTouched(false)
    }
  }, [open])

  const items = browseQuery.data?.items ?? []

  const drillInto = (item: M365BrowseItem) => {
    const crumb = crumbFor(item, current.siteId)
    if (!crumb) return
    setTrail((prev) => [...prev, crumb])
  }

  const jumpTo = (index: number) => setTrail((prev) => prev.slice(0, index + 1))

  const pathFor = (name: string) => [...trail.slice(1).map((c) => c.label), name].join(' / ')

  const useFolder = (item: M365BrowseItem) => {
    const path = pathFor(item.name)
    setPending({ item, siteId: current.siteId, path })
    setLabel(item.name)
    setSlug(slugify(item.name))
    setSlugTouched(false)
  }

  const backToBrowse = () => {
    if (saveMutation.isPending) return
    setPending(null)
  }

  const onLabelChange = (value: string) => {
    setLabel(value)
    if (!slugTouched) setSlug(slugify(value))
  }

  const onSlugChange = (value: string) => {
    setSlugTouched(true)
    setSlug(value)
  }

  const slugFormatValid = SLUG_PATTERN.test(slug)
  const slugTaken = existingTargets.some((t) => t.slug === slug)
  const slugValid = slugFormatValid && !slugTaken
  const labelValid = label.trim() !== ''
  const canSave = pending !== null && labelValid && slugValid && !saveMutation.isPending

  const save = () => {
    if (!pending || !canSave) return
    const item = pending.item
    if (!item.drive_id) return
    const target: WriteTarget = {
      slug,
      label: label.trim(),
      path: pending.path,
      site_id: pending.siteId,
      drive_id: item.drive_id,
      item_id: item.id,
      web_url: item.web_url ?? null,
    }
    saveMutation.mutate(target)
  }

  const close = () => {
    if (saveMutation.isPending) return
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title={pending ? 'Add output folder' : 'Choose an output folder'}
      subtitle={
        pending
          ? undefined
          : 'Browse sites and drives with the connected account, then pick a folder to write into.'
      }
      width={640}
      footer={
        pending ? (
          <>
            <button type="button" className="btn btn-sm" onClick={backToBrowse} disabled={saveMutation.isPending}>
              Back
            </button>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={!canSave}
              onClick={save}
            >
              {saveMutation.isPending ? 'Saving…' : 'Save'}
            </button>
          </>
        ) : (
          <button type="button" className="btn btn-sm" onClick={close}>
            Close
          </button>
        )
      }
    >
      {pending ? (
        <div className="stack" style={{ gap: 12 }}>
          <div>
            <div className="mono-label" style={{ marginBottom: 4 }}>
              Folder
            </div>
            <div className="mono-body" style={{ fontFamily: 'var(--mono)', fontSize: 'var(--fs-xs)' }}>
              {pending.path}
            </div>
          </div>
          <div className="field">
            <label className="mono-label">Label</label>
            <input type="text" value={label} onChange={(e) => onLabelChange(e.target.value)} />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label className="mono-label">Slug</label>
            <input type="text" value={slug} onChange={(e) => onSlugChange(e.target.value)} />
            {!slugFormatValid && slug !== '' && (
              <div className="fine-print" style={{ marginTop: 4 }}>
                Lowercase letters, digits and hyphens, starting with a letter or digit — 2 to 40
                characters.
              </div>
            )}
            {slugFormatValid && slugTaken && (
              <div className="fine-print" style={{ marginTop: 4 }}>
                Already used by another output folder.
              </div>
            )}
          </div>
          {saveError && <div className="error-text">{saveError.message}</div>}
        </div>
      ) : (
        <>
          <div className="row" style={{ flexWrap: 'wrap', gap: 4, marginBottom: 12 }}>
            {trail.map((crumb, i) => (
              <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                {i > 0 && <span style={{ color: 'var(--text-muted)' }}>/</span>}
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={i === trail.length - 1}
                  onClick={() => jumpTo(i)}
                  style={
                    i === trail.length - 1
                      ? { opacity: 1, cursor: 'default', borderColor: 'transparent' }
                      : undefined
                  }
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
                    <span
                      className="row"
                      style={{ flex: 1, color: 'var(--text-muted)', gap: 6 }}
                      aria-hidden="true"
                    >
                      <span>{KIND_ICON.file}</span>
                      <span>{item.name}</span>
                    </span>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="btn btn-sm"
                        style={{ flex: 1, textAlign: 'left', border: 'none', background: 'none' }}
                        onClick={() => drillInto(item)}
                      >
                        <span style={{ color: 'var(--text-muted)', marginRight: 6 }}>
                          {KIND_ICON[item.kind]}
                        </span>
                        {item.name}
                      </button>
                      {item.kind === 'folder' && (
                        <button type="button" className="btn btn-sm" onClick={() => useFolder(item)}>
                          Use this folder
                        </button>
                      )}
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Modal>
  )
}
