import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Wordmark } from './components/shared/Wordmark'
import { useEffect, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'

import { api, type User } from './api/client'
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
          await api.logout()
          queryClient.clear()
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
