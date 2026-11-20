import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react'

import {
  type ConversationDetail,
  type ConversationSummary,
  type SessionGroup,
  sessionApi,
} from '../../api/client'
import type { ChatWording } from '../../edition'

// Session rail (R20-R22): groups with inline-editable titles, per-group
// "New session here", a row menu (Rename / Move to group / Archive) and a
// lazily loaded Archived section with Restore.

const COLLAPSED_KEY = 'tret.rail.collapsedGroups'
const ARCHIVED_OPEN_KEY = 'tret.rail.archivedOpen'

function readCollapsed(): string[] {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

function writeCollapsed(ids: string[]) {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(ids))
  } catch {
    /* storage unavailable: collapsed state just does not persist */
  }
}

function readArchivedOpen(): boolean {
  try {
    return localStorage.getItem(ARCHIVED_OPEN_KEY) === '1'
  } catch {
    return false
  }
}

function relTime(iso: string | null): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'now'
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
}

export interface SessionRailProps {
  conversations: ConversationSummary[]
  loading: boolean
  error: Error | null
  selectedId: string | null
  onSelect: (id: string) => void
  onNewChat: () => void
  /** A session was created inside a group; the parent selects it. */
  onCreatedInGroup: (conversation: ConversationDetail) => void
  wording: ChatWording
}

export function SessionRail({
  conversations,
  loading,
  error,
  selectedId,
  onSelect,
  onNewChat,
  onCreatedInGroup,
  wording,
}: SessionRailProps) {
  const queryClient = useQueryClient()
  const [collapsed, setCollapsed] = useState<string[]>(readCollapsed)
  const [archivedOpen, setArchivedOpen] = useState<boolean>(readArchivedOpen)
  const [renamingGroup, setRenamingGroup] = useState<string | null>(null)
  const [renamingSession, setRenamingSession] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [note, setNote] = useState<{ id: string; title: string } | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const groupsQuery = useQuery({
    queryKey: ['conversation-groups'],
    queryFn: sessionApi.listGroups,
    retry: false,
  })
  // An older server without the groups route just shows a flat list.
  const groups: SessionGroup[] = groupsQuery.data ?? []

  const archivedQuery = useQuery({
    queryKey: ['conversations', 'archived'],
    queryFn: sessionApi.listArchivedConversations,
    enabled: archivedOpen,
    retry: false,
  })

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['conversations'] })
    queryClient.invalidateQueries({ queryKey: ['conversation-groups'] })
  }, [queryClient])

  const onFail = (err: unknown) => setActionError(err instanceof Error ? err.message : 'Something went wrong')

  const createGroup = useMutation({
    mutationFn: () => sessionApi.createGroup(),
    onSuccess: (g) => {
      // Put the new group in the cache first so its title input renders (and
      // takes focus) now, not after the refetch lands.
      queryClient.setQueryData<SessionGroup[]>(['conversation-groups'], (old) =>
        old?.some((x) => x.id === g.id) ? old : [...(old ?? []), g],
      )
      setRenamingGroup(g.id)
      refresh()
    },
    onError: onFail,
  })
  const renameGroup = useMutation({
    mutationFn: (v: { id: string; title: string }) => sessionApi.renameGroup(v.id, v.title),
    onSuccess: refresh,
    onError: onFail,
  })
  const deleteGroup = useMutation({
    mutationFn: (id: string) => sessionApi.deleteGroup(id),
    onSuccess: refresh,
    onError: onFail,
  })
  const newInGroup = useMutation({
    mutationFn: (id: string) => sessionApi.createConversationInGroup(id),
    onSuccess: (conv) => {
      refresh()
      onCreatedInGroup(conv)
    },
    onError: onFail,
  })
  const renameSession = useMutation({
    mutationFn: (v: { id: string; title: string }) => sessionApi.renameConversation(v.id, v.title),
    onSuccess: refresh,
    onError: onFail,
  })
  const moveSession = useMutation({
    mutationFn: (v: { id: string; groupId: string | null }) => sessionApi.moveConversation(v.id, v.groupId),
    onSuccess: refresh,
    onError: onFail,
  })
  const archive = useMutation({
    mutationFn: (c: ConversationSummary) => sessionApi.archiveConversation(c.id),
    onSuccess: (_r, c) => {
      refresh()
      setNote({ id: c.id, title: c.title ?? wording.untitled })
    },
    onError: onFail,
  })
  const restore = useMutation({
    mutationFn: (id: string) => sessionApi.unarchiveConversation(id),
    onSuccess: (_r, id) => {
      refresh()
      setNote((n) => (n && n.id === id ? null : n))
    },
    onError: onFail,
  })

  // The "Archived — Undo" note clears itself.
  useEffect(() => {
    if (!note) return
    const t = window.setTimeout(() => setNote(null), 8000)
    return () => window.clearTimeout(t)
  }, [note])

  const toggleGroup = (id: string) => {
    setCollapsed((prev) => {
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
      writeCollapsed(next)
      return next
    })
  }

  const toggleArchived = () => {
    setArchivedOpen((open) => {
      try {
        localStorage.setItem(ARCHIVED_OPEN_KEY, open ? '0' : '1')
      } catch {
        /* ignore */
      }
      return !open
    })
  }

  const groupIds = new Set(groups.map((g) => g.id))
  const ungrouped = conversations.filter((c) => !c.group_id || !groupIds.has(c.group_id))
  const sessionsIn = (gid: string) => conversations.filter((c) => c.group_id === gid)

  const rowProps = {
    groups,
    selectedId,
    wording,
    renamingId: renamingSession,
    onSelect,
    onStartRename: (id: string) => setRenamingSession(id),
    onFinishRename: (id: string, title: string | null) => {
      setRenamingSession(null)
      if (title !== null) renameSession.mutate({ id, title })
    },
    onMove: (id: string, groupId: string | null) => moveSession.mutate({ id, groupId }),
    onArchive: (c: ConversationSummary) => archive.mutate(c),
  }

  return (
    <aside className="chat-rail">
      <button className="chat-newchat" onClick={onNewChat}>
        <PlusGlyph />
        <span>{wording.newChat}</span>
      </button>
      <div className="chat-rail-list">
        {loading ? (
          <div className="empty pulse" style={{ padding: '8px 4px' }}>
            Loading…
          </div>
        ) : error ? (
          // "No conversations yet" over a failed fetch reads as "your history is
          // gone", which would send a user off to retype work they still have.
          <div className="error-text" style={{ padding: '8px 4px', fontSize: 'var(--fs-xs)' }}>
            {wording.loadFailed} — {error.message}
          </div>
        ) : (
          <>
            {groups.map((g) => {
              const isCollapsed = collapsed.includes(g.id)
              const members = sessionsIn(g.id)
              return (
                <div className="session-group" key={g.id}>
                  <div className="session-group-head">
                    <button
                      className="session-group-toggle"
                      onClick={() => toggleGroup(g.id)}
                      aria-expanded={!isCollapsed}
                      aria-label={`${isCollapsed ? 'Expand' : 'Collapse'} group ${g.title}`}
                    >
                      <span className={`session-chevron${isCollapsed ? '' : ' open'}`} aria-hidden>
                        ▸
                      </span>
                    </button>
                    {renamingGroup === g.id ? (
                      <InlineEdit
                        initial={g.title}
                        label="Group name"
                        onDone={(t) => {
                          setRenamingGroup(null)
                          if (t !== null && t !== g.title) renameGroup.mutate({ id: g.id, title: t })
                        }}
                      />
                    ) : (
                      <button
                        className="session-group-title"
                        onClick={() => setRenamingGroup(g.id)}
                        title="Click to rename"
                      >
                        {g.title}
                      </button>
                    )}
                    <span className="session-group-count">{members.length}</span>
                    <button
                      className="session-mini-btn"
                      onClick={() => newInGroup.mutate(g.id)}
                      disabled={newInGroup.isPending}
                      title={`New ${wording.noun} here`}
                      aria-label={`New ${wording.noun} in ${g.title}`}
                    >
                      +
                    </button>
                    <button
                      className="session-mini-btn"
                      onClick={() => setConfirmDelete(g.id)}
                      title="Delete group"
                      aria-label={`Delete group ${g.title}`}
                    >
                      ×
                    </button>
                  </div>
                  {confirmDelete === g.id && (
                    <div className="session-note" role="alert">
                      <span>Delete this group? Its {wording.nouns} stay, ungrouped.</span>
                      <button
                        className="session-link-btn danger"
                        onClick={() => {
                          setConfirmDelete(null)
                          deleteGroup.mutate(g.id)
                        }}
                      >
                        Delete
                      </button>
                      <button className="session-link-btn" onClick={() => setConfirmDelete(null)}>
                        Cancel
                      </button>
                    </div>
                  )}
                  {!isCollapsed && (
                    <div className="session-group-body">
                      {members.length === 0 ? (
                        <div className="session-empty">No {wording.nouns} in this group.</div>
                      ) : (
                        members.map((c) => <SessionRow key={c.id} c={c} {...rowProps} />)
                      )}
                    </div>
                  )}
                </div>
              )
            })}
            {conversations.length === 0 && groups.length === 0 ? (
              <div className="empty" style={{ padding: '8px 4px', fontSize: 'var(--fs-xs)' }}>
                {wording.empty}
              </div>
            ) : (
              ungrouped.length > 0 && (
                <div className="session-ungrouped">
                  {groups.length > 0 && <div className="session-section-label">Ungrouped</div>}
                  {ungrouped.map((c) => (
                    <SessionRow key={c.id} c={c} {...rowProps} />
                  ))}
                </div>
              )
            )}
          </>
        )}
      </div>

      {actionError && (
        <div className="session-note error-text" role="alert">
          <span>{actionError}</span>
          <button className="session-link-btn" onClick={() => setActionError(null)}>
            Dismiss
          </button>
        </div>
      )}
      {note && (
        <div className="session-note" role="status">
          <span className="session-note-text">Archived “{note.title}”.</span>
          <button className="session-link-btn" onClick={() => restore.mutate(note.id)}>
            Undo
          </button>
        </div>
      )}

      <div className="session-rail-foot">
        <button className="session-foot-btn" onClick={() => createGroup.mutate()} disabled={createGroup.isPending}>
          + New group
        </button>
        <button className="session-foot-btn" onClick={toggleArchived} aria-expanded={archivedOpen}>
          <span className={`session-chevron${archivedOpen ? ' open' : ''}`} aria-hidden>
            ▸
          </span>{' '}
          Archived
        </button>
        {archivedOpen && (
          <div className="session-archived-list">
            {archivedQuery.isLoading ? (
              <div className="session-empty pulse">Loading…</div>
            ) : archivedQuery.error ? (
              <div className="session-empty error-text">Could not load archived {wording.nouns}.</div>
            ) : (archivedQuery.data ?? []).length === 0 ? (
              <div className="session-empty">No archived {wording.nouns}.</div>
            ) : (
              (archivedQuery.data ?? []).map((c) => (
                <div className="session-archived-row" key={c.id}>
                  <button
                    className="chat-rail-item"
                    onClick={() => onSelect(c.id)}
                    title={c.title ?? wording.untitled}
                  >
                    <span className="chat-rail-title">{c.title ?? wording.untitled}</span>
                  </button>
                  <button
                    className="session-link-btn"
                    onClick={() => restore.mutate(c.id)}
                    aria-label={`Restore ${c.title ?? wording.untitled}`}
                  >
                    Restore
                  </button>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </aside>
  )
}

// ── Rows ──────────────────────────────────────────────────────────────────

interface RowProps {
  c: ConversationSummary
  groups: SessionGroup[]
  selectedId: string | null
  wording: ChatWording
  renamingId: string | null
  onSelect: (id: string) => void
  onStartRename: (id: string) => void
  onFinishRename: (id: string, title: string | null) => void
  onMove: (id: string, groupId: string | null) => void
  onArchive: (c: ConversationSummary) => void
}

function SessionRow({
  c,
  groups,
  selectedId,
  wording,
  renamingId,
  onSelect,
  onStartRename,
  onFinishRename,
  onMove,
  onArchive,
}: RowProps) {
  const [menu, setMenu] = useState<'closed' | 'main' | 'move'>('closed')
  const wrapRef = useRef<HTMLDivElement>(null)
  const menuBtnRef = useRef<HTMLButtonElement>(null)
  const title = c.title ?? wording.untitled

  useEffect(() => {
    if (menu === 'closed') return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setMenu('closed')
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [menu])

  // Focus the first entry whenever the menu (or its Move view) opens.
  useEffect(() => {
    if (menu === 'closed') return
    wrapRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
  }, [menu])

  const close = (restoreFocus: boolean) => {
    setMenu('closed')
    if (restoreFocus) menuBtnRef.current?.focus()
  }

  const onMenuKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      if (menu === 'move') setMenu('main')
      else close(true)
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const items = Array.from(wrapRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
    const i = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = e.key === 'ArrowDown' ? (i + 1) % items.length : (i - 1 + items.length) % items.length
    items[next]?.focus()
  }

  if (renamingId === c.id) {
    return (
      <div className="session-row">
        <InlineEdit
          initial={c.title ?? ''}
          label={`Rename ${wording.noun}`}
          onDone={(t) => onFinishRename(c.id, t !== null && t !== '' && t !== c.title ? t : null)}
        />
      </div>
    )
  }

  return (
    <div className="session-row" ref={wrapRef}>
      <button
        className={`chat-rail-item${c.id === selectedId ? ' active' : ''}`}
        onClick={() => onSelect(c.id)}
        title={title}
      >
        <span className="chat-rail-title">{title}</span>
        <span className="chat-rail-time">{relTime(c.updated_at)}</span>
      </button>
      <button
        ref={menuBtnRef}
        className="session-row-menu-btn"
        aria-haspopup="menu"
        aria-expanded={menu !== 'closed'}
        aria-label={`Options for ${title}`}
        onClick={() => setMenu((m) => (m === 'closed' ? 'main' : 'closed'))}
      >
        ⋯
      </button>
      {menu !== 'closed' && (
        <div className="session-menu" role="menu" onKeyDown={onMenuKey}>
          {menu === 'main' ? (
            <>
              <button
                role="menuitem"
                onClick={() => {
                  close(false)
                  onStartRename(c.id)
                }}
              >
                Rename
              </button>
              <button role="menuitem" aria-haspopup="menu" onClick={() => setMenu('move')}>
                Move to group ▸
              </button>
              <button
                role="menuitem"
                onClick={() => {
                  close(false)
                  onArchive(c)
                }}
              >
                Archive
              </button>
            </>
          ) : (
            <>
              <button role="menuitem" onClick={() => setMenu('main')}>
                ◂ Back
              </button>
              {groups.map((g) => (
                <button
                  key={g.id}
                  role="menuitem"
                  aria-current={c.group_id === g.id ? 'true' : undefined}
                  onClick={() => {
                    close(false)
                    if (c.group_id !== g.id) onMove(c.id, g.id)
                  }}
                >
                  {c.group_id === g.id ? '✓ ' : ''}
                  {g.title}
                </button>
              ))}
              <button
                role="menuitem"
                onClick={() => {
                  close(false)
                  if (c.group_id) onMove(c.id, null)
                }}
              >
                {!c.group_id ? '✓ ' : ''}No group
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// Enter saves, Esc cancels, blur saves. `onDone(null)` means cancelled.
function InlineEdit({
  initial,
  label,
  onDone,
}: {
  initial: string
  label: string
  onDone: (value: string | null) => void
}) {
  const [value, setValue] = useState(initial)
  const doneRef = useRef(false)
  const finish = (v: string | null) => {
    if (doneRef.current) return
    doneRef.current = true
    onDone(v === null ? null : v.trim())
  }
  return (
    <input
      className="session-inline-input"
      aria-label={label}
      value={value}
      maxLength={200}
      autoFocus
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          finish(value)
        } else if (e.key === 'Escape') {
          e.preventDefault()
          finish(null)
        }
      }}
      onBlur={() => finish(value)}
    />
  )
}

function PlusGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <path d="M8 3v10M3 8h10" strokeLinecap="round" />
    </svg>
  )
}

export type { ConversationDetail }
