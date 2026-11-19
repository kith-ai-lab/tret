import { useMutation, useQuery } from '@tanstack/react-query'
import { type KeyboardEvent, type ReactNode, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import {
  api,
  ApiError,
  MAX_SESSION_FOLDERS,
  type M365BrowseItem,
  type SessionFolder,
  type SessionFolderRef,
} from '../../api/client'
import { rememberReturnPath } from '../connections/connectReturn'
import { Modal } from '../shared/Modal'
import './session-folders.css'

/** Stable identity of a folder: a drive id plus an item id. */
export function folderKey(f: SessionFolderRef): string {
  return `${f.drive_id}\u0000${f.item_id}`
}

export function toRef(f: SessionFolderRef): SessionFolderRef {
  return { drive_id: f.drive_id, item_id: f.item_id }
}

/** Append `extra` to `current`, dropping duplicates and capping at the limit. */
export function mergeFolders(current: SessionFolder[], extra: SessionFolder[]): SessionFolder[] {
  const seen = new Set(current.map(folderKey))
  const out = [...current]
  for (const f of extra) {
    if (seen.has(folderKey(f)) || out.length >= MAX_SESSION_FOLDERS) continue
    seen.add(folderKey(f))
    out.push(f)
  }
  return out
}

function FolderIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </svg>
  )
}

function PlusGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

// ── "+" menu ────────────────────────────────────────────────────────────────

/** The composer's "+" button and its menu: upload files, or add a OneDrive
 *  folder (or connect OneDrive first when the workspace has no Microsoft 365
 *  connection). */
export function ComposerPlusMenu({
  disabled,
  folderDisabled = false,
  onUploadFiles,
  onAddFolder,
}: {
  disabled: boolean
  /** Only the folder item waits (conversation still loading, or a folder PUT
   *  pending); "Upload files" follows `disabled` alone. */
  folderDisabled?: boolean
  onUploadFiles: () => void
  onAddFolder: () => void
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const navigate = useNavigate()
  const location = useLocation()

  // "Connect OneDrive…" starts the Microsoft sign-in from here and comes back
  // to this page afterwards (see connectReturn.ts). Any refusal (Microsoft 365
  // not configured, no credits yet, not an admin of this workspace) goes to
  // Settings > Connections, whose card explains it and offers the same button.
  const connectMutation = useMutation({
    mutationFn: () => api.authorizeConnection('m365'),
    onSuccess: (res) => {
      rememberReturnPath(location.pathname + location.search)
      window.location.assign(res.authorize_url)
    },
    onError: () => navigate('/settings/connections'),
  })

  // Same key and fn as the Connections view, so this is usually a cache read.
  const connectionsQuery = useQuery({ queryKey: ['connections'], queryFn: api.listConnections })
  // Unknown (loading) shows the normal item; a failed list means we cannot
  // tell, so also keep the normal item, whose picker surfaces the real error.
  const connected =
    connectionsQuery.data === undefined
      ? true
      : connectionsQuery.data.connections.some((c) => c.provider === 'm365')

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  // Focus the first item on open.
  useEffect(() => {
    if (open) wrapRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
  }, [open])

  useEffect(() => {
    if (disabled) setOpen(false)
  }, [disabled])

  const close = (restoreFocus: boolean) => {
    setOpen(false)
    if (restoreFocus) triggerRef.current?.focus()
  }

  const onMenuKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]'))
    const index = items.indexOf(document.activeElement as HTMLElement)
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close(true)
    } else if (e.key === 'Tab') {
      setOpen(false)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      items[(index + 1) % items.length]?.focus()
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      items[(index - 1 + items.length) % items.length]?.focus()
    } else if (e.key === 'Home') {
      e.preventDefault()
      items[0]?.focus()
    } else if (e.key === 'End') {
      e.preventDefault()
      items[items.length - 1]?.focus()
    }
  }

  return (
    <div className="chat-plus-wrap" ref={wrapRef}>
      <button
        type="button"
        ref={triggerRef}
        className="chat-attach"
        aria-label="Add to message"
        aria-haspopup="menu"
        aria-expanded={open}
        title="Upload files or add a OneDrive folder"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault()
            setOpen(true)
          }
        }}
      >
        <PlusGlyph />
      </button>
      {open && (
        <div className="chat-plus-menu" role="menu" aria-label="Add to message" onKeyDown={onMenuKeyDown}>
          <button
            type="button"
            role="menuitem"
            className="chat-plus-item"
            title="Upload files (25 MB max each)"
            onClick={() => {
              close(false)
              onUploadFiles()
            }}
          >
            Upload files
            <span className="desc">25 MB max each</span>
          </button>
          <button
            type="button"
            role="menuitem"
            className="chat-plus-item"
            disabled={connected ? folderDisabled : connectMutation.isPending}
            onClick={() => {
              close(false)
              if (connected) onAddFolder()
              else connectMutation.mutate()
            }}
          >
            {connected ? 'Add OneDrive folder' : connectMutation.isPending ? 'Redirecting…' : 'Connect OneDrive…'}
            <span className="desc">
              {connected ? 'Let the session read a folder' : 'Sign in to Microsoft 365 to add folders'}
            </span>
          </button>
        </div>
      )}
    </div>
  )
}

// ── Chips ───────────────────────────────────────────────────────────────────

export function FolderChips({
  folders,
  busy,
  error,
  onRemove,
}: {
  folders: SessionFolder[]
  busy: boolean
  error: Error | null
  onRemove: (folder: SessionFolder) => void
}) {
  if (folders.length === 0 && !error) return null
  const needsReconnect = error instanceof ApiError && error.status === 409
  return (
    <div className="chat-attachments">
      {folders.map((f) => (
        <span key={folderKey(f)} className="chat-attachment-chip chat-folder-chip" title={f.path || f.name}>
          <FolderIcon />
          {f.web_url ? (
            <a className="chat-attachment-name" href={f.web_url} target="_blank" rel="noopener noreferrer">
              {f.name}
            </a>
          ) : (
            <span className="chat-attachment-name">{f.name}</span>
          )}
          <button
            type="button"
            className="chat-attachment-remove"
            aria-label={`Remove folder ${f.name}`}
            disabled={busy}
            onClick={() => onRemove(f)}
          >
            ×
          </button>
        </span>
      ))}
      {error && (
        <div className="error-text chat-folder-error">
          {error.message}
          {needsReconnect && (
            <>
              {' '}
              <Link to="/settings/connections">Reconnect in Settings</Link>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── Picker modal ────────────────────────────────────────────────────────────

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
    return { label: item.name, params: { scope: 'drive_children', drive_id: item.drive_id ?? item.id } }
  }
  if (item.kind === 'folder' && item.drive_id) {
    return { label: item.name, params: { scope: 'drive_children', drive_id: item.drive_id, item_id: item.id } }
  }
  return null
}

const KIND_ICON: Record<M365BrowseItem['kind'], ReactNode> = {
  site: <span aria-hidden="true">◆</span>,
  drive: <span aria-hidden="true">▣</span>,
  folder: <FolderIcon />,
  file: <span aria-hidden="true">·</span>,
}

/** Modal folder browser over GET /api/connections/m365/browse. Selection is
 *  kept across navigation (each pick carries its own ids), folders only, and
 *  the total including `attached` is capped at MAX_SESSION_FOLDERS. Calls
 *  `onConfirm` with the newly picked folders (display fields filled from the
 *  breadcrumb trail; the server returns the canonical path once attached). */
export function FolderPickerModal({
  open,
  onClose,
  attached,
  onConfirm,
}: {
  open: boolean
  onClose: () => void
  attached: SessionFolder[]
  onConfirm: (picked: SessionFolder[]) => void
}) {
  const [trail, setTrail] = useState<Crumb[]>([ROOT_CRUMB])
  const [selected, setSelected] = useState<Map<string, SessionFolder>>(new Map())
  const current = trail[trail.length - 1]

  const browseQuery = useQuery({
    queryKey: ['m365-browse', current.params],
    queryFn: () => api.browseM365(current.params),
    enabled: open,
    retry: false,
  })

  useEffect(() => {
    if (!open) {
      setTrail([ROOT_CRUMB])
      setSelected(new Map())
    }
  }, [open])

  const attachedKeys = new Set(attached.map(folderKey))
  const remaining = MAX_SESSION_FOLDERS - attached.length - selected.size
  const atLimit = remaining <= 0

  const raw = browseQuery.data?.items ?? []
  // The user's own OneDrive (a drive with no site) first, then the rest.
  const items = [...raw.filter((i) => i.kind === 'drive' && !i.site_id), ...raw.filter((i) => !(i.kind === 'drive' && !i.site_id))]
  const hasFolderOrContainer = items.some((i) => i.kind !== 'file')

  const pathOf = (name: string) => [...trail.slice(1).map((c) => c.label), name].join(' / ')

  const toggle = (item: M365BrowseItem) => {
    if (!item.drive_id) return
    const ref: SessionFolderRef = { drive_id: item.drive_id, item_id: item.id }
    const key = folderKey(ref)
    setSelected((prev) => {
      const next = new Map(prev)
      if (next.has(key)) next.delete(key)
      else next.set(key, { ...ref, name: item.name, path: pathOf(item.name), web_url: null })
      return next
    })
  }

  const drillInto = (item: M365BrowseItem) => {
    const crumb = crumbFor(item)
    if (crumb) setTrail((prev) => [...prev, crumb])
  }

  const err = browseQuery.error
  const status = err instanceof ApiError ? err.status : null
  const picked = Array.from(selected.values())

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add OneDrive folders"
      subtitle="Pick the folders this session can read. You can choose folders at different levels."
      width={640}
      footer={
        <>
          <button type="button" className="btn btn-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={picked.length === 0}
            onClick={() => onConfirm(picked)}
          >
            {picked.length === 0 ? 'Add folders' : `Add ${picked.length} folder${picked.length === 1 ? '' : 's'}`}
          </button>
        </>
      }
    >
      <div className="folder-picker-crumbs">
        {trail.length > 1 && (
          <button type="button" className="btn btn-sm" onClick={() => setTrail((p) => p.slice(0, -1))}>
            ← Back
          </button>
        )}
        {trail.map((crumb, i) => (
          <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            {i > 0 && <span style={{ color: 'var(--text-muted)' }}>/</span>}
            <button
              type="button"
              className="btn btn-sm"
              disabled={i === trail.length - 1}
              onClick={() => setTrail((p) => p.slice(0, i + 1))}
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
        <div>
          <div className="error-text">{err instanceof Error ? err.message : 'Could not load this folder'}</div>
          <div className="row" style={{ gap: 8, marginTop: 8 }}>
            {status === 409 && (
              <Link to="/settings/connections" className="btn btn-sm" onClick={onClose}>
                Reconnect in Settings
              </Link>
            )}
            {status !== 403 && status !== 409 && (
              <button type="button" className="btn btn-sm" onClick={() => void browseQuery.refetch()}>
                Retry
              </button>
            )}
          </div>
        </div>
      ) : items.length === 0 || !hasFolderOrContainer ? (
        <div className="empty">{items.length === 0 ? 'Nothing here.' : 'No folders here.'}</div>
      ) : (
        <div className="folder-picker-list">
          {items.map((item) => {
            if (item.kind === 'file') {
              return (
                <div key={item.id} className="folder-picker-row muted" aria-disabled="true" title="Files cannot be added, only folders">
                  <span className="folder-picker-name">{item.name}</span>
                </div>
              )
            }
            const isFolder = item.kind === 'folder'
            const key = item.drive_id ? folderKey({ drive_id: item.drive_id, item_id: item.id }) : ''
            const already = isFolder && attachedKeys.has(key)
            const checked = already || selected.has(key)
            const blocked = !checked && atLimit
            return (
              <div key={item.id} className="folder-picker-row">
                {isFolder && (
                  <input
                    type="checkbox"
                    aria-label={`Select ${item.name}`}
                    checked={checked}
                    disabled={already || blocked}
                    title={
                      already
                        ? 'Already attached'
                        : blocked
                          ? `A session can have at most ${MAX_SESSION_FOLDERS} folders`
                          : undefined
                    }
                    onChange={() => toggle(item)}
                  />
                )}
                <button type="button" className="folder-picker-open" onClick={() => drillInto(item)}>
                  {KIND_ICON[item.kind]}
                  <span className="folder-picker-name">{item.name}</span>
                </button>
              </div>
            )
          })}
        </div>
      )}
      <div className="folder-picker-note">
        {atLimit
          ? `Limit reached: a session can have at most ${MAX_SESSION_FOLDERS} folders.`
          : `${attached.length + selected.size} of ${MAX_SESSION_FOLDERS} folders used.`}
      </div>
    </Modal>
  )
}
