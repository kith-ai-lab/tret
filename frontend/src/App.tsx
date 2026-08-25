import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Wordmark } from './components/shared/Wordmark'
import { useEffect, useRef, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'

import { api, ApiError, type User } from './api/client'
import { FirstRunSetup } from './components/shared/FirstRunSetup'
import { AppRoutes } from './router'
import { Login } from './views/Login'

type Theme = 'light' | 'dark'
const THEME_KEY = 'tret-theme'

// Mirrors the inline script in index.html <head>, which already applied the
// stored preference before first paint — this just brings React's state in
// sync with whatever's on <html> so the toggle reflects reality on mount.
function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() =>
    document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light',
  )

  useEffect(() => {
    if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark')
    } else {
      document.documentElement.removeAttribute('data-theme')
    }
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))]
}

const NAV = [
  { to: '/', label: 'Chat' },
  { to: '/workbench', label: 'Workbench' },
  { to: '/runs', label: 'Runs' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/analytics', label: 'Analytics' },
  { to: '/emissions', label: 'Emissions' },
  { to: '/deliverables', label: 'Deliverables' },
  { to: '/documents', label: 'Documents' },
  { to: '/packs', label: 'Packs' },
  { to: '/harnesses', label: 'Harnesses' },
  { to: '/settings', label: 'Settings' },
]

export default function App() {
  const queryClient = useQueryClient()
  const [theme, toggleTheme] = useTheme()
  const meQuery = useQuery({
    queryKey: ['me'],
    queryFn: api.me,
    retry: false,
    staleTime: Infinity,
  })

  if (meQuery.isLoading) {
    return (
      <div className="login-wrap">
        <div className="mono-label pulse">loading…</div>
      </div>
    )
  }

  if (meQuery.isError || !meQuery.data) {
    return <Login onLogin={() => queryClient.invalidateQueries({ queryKey: ['me'] })} />
  }

  return (
    <div className="app-shell">
      <Sidebar
        user={meQuery.data}
        theme={theme}
        onToggleTheme={toggleTheme}
        onLogout={async () => {
          // Local session first (the cookie always needs clearing), then hand
          // off to the IdP's own end-session endpoint when the backend names
          // one — `both` mode has no single "next place" to send a user after
          // an IdP logout, so it never sets this and the redirect is skipped.
          const result = await api.logout()
          queryClient.clear()
          if (result.oidc_logout_url) {
            window.location.href = result.oidc_logout_url
          }
        }}
      />
      {/* Column, not just <Main/>: the first-run setup banner sits above every
          view (chat's full-bleed layout included) without living inside any of
          them. It renders nothing on a configured install. */}
      <div className="main-col">
        <FirstRunSetup />
        <Main />
      </div>
    </div>
  )
}

function Main() {
  // The chat route owns its full viewport (its own scroll + pinned composer),
  // so drop the standard content padding there.
  const location = useLocation()
  const isChat = location.pathname === '/'
  return (
    <main className={`main${isChat ? ' main--chat' : ''}`}>
      <AppRoutes />
    </main>
  )
}

function Sidebar({
  user,
  theme,
  onToggleTheme,
  onLogout,
}: {
  user: User
  theme: 'light' | 'dark'
  onToggleTheme: () => void
  onLogout: () => void
}) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <Wordmark size={20} />
      </div>
      {user.workspaces.length >= 1 && <WorkspaceSwitcher user={user} />}
      <nav>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.to === '/'}>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <button
        type="button"
        className="theme-toggle"
        onClick={onToggleTheme}
        title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
      >
        <span aria-hidden="true">{theme === 'dark' ? '☾' : '☀'}</span>
        {theme === 'dark' ? 'Dark' : 'Light'}
      </button>
      <div className="sidebar-footer">
        <div className="who" title={user.email}>
          {user.display_name}
        </div>
        <div className="role">{user.role}</div>
        <button className="btn btn-sm" onClick={onLogout}>
          Log out
        </button>
      </div>
    </aside>
  )
}

// ── Workspace switcher ───────────────────────────────────────────────────
// Sits above the nav: the current workspace (with a personal/team hint),
// opening onto every workspace this user belongs to plus a "create team"
// action. Switching re-mints the session cookie server-side, so the cache is
// dropped wholesale afterward — every workspace-scoped query in the app
// (runs, findings, documents, …) is stale the instant the active workspace
// changes, and a full drop is the only way to guarantee none of it survives
// into the new context. Matches the rigor `onLogout` already uses.

function WorkspaceSwitcher({ user }: { user: User }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const switchMutation = useMutation({
    mutationFn: (workspaceId: string) => api.switchWorkspace(workspaceId),
    onSuccess: () => queryClient.clear(),
  })

  const createMutation = useMutation({
    mutationFn: (name: string) => api.createWorkspace(name),
    onSuccess: () => queryClient.clear(),
  })

  const current = user.workspaces.find((w) => w.id === user.current_workspace_id)
  const switchError = switchMutation.error as ApiError | null
  const createError = createMutation.error as ApiError | null
  const pending = switchMutation.isPending || createMutation.isPending

  const createTeam = () => {
    const name = window.prompt('Name your team workspace')
    if (name && name.trim()) {
      setOpen(false)
      createMutation.mutate(name.trim())
    }
  }

  return (
    <div className="workspace-switcher" ref={wrapRef}>
      <button
        type="button"
        className="workspace-switcher-trigger"
        aria-expanded={open}
        aria-haspopup="true"
        disabled={pending}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="workspace-switcher-name">
          {pending ? 'switching…' : (current?.name ?? 'select workspace')}
        </span>
        {current && <span className="workspace-switcher-kind">{current.kind}</span>}
      </button>

      {open && (
        <div className="workspace-switcher-pop" role="menu">
          {user.workspaces.map((w) => (
            <button
              key={w.id}
              type="button"
              role="menuitemradio"
              aria-checked={w.id === user.current_workspace_id}
              className={`list-item${w.id === user.current_workspace_id ? ' active' : ''}`}
              disabled={pending}
              onClick={() => {
                setOpen(false)
                if (w.id !== user.current_workspace_id) switchMutation.mutate(w.id)
              }}
            >
              {w.name}
              <span className="sub">{w.kind}</span>
            </button>
          ))}
          <button
            type="button"
            className="list-item workspace-switcher-create"
            disabled={pending}
            onClick={createTeam}
          >
            {createMutation.isPending ? 'Creating…' : '+ Create team workspace'}
          </button>
        </div>
      )}

      {(switchError || createError) && (
        <div className="error-text" style={{ marginTop: 4, fontSize: 10.5 }}>
          {(switchError ?? createError)?.message}
        </div>
      )}
    </div>
  )
}
