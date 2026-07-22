import { useQuery, useQueryClient } from '@tanstack/react-query'
import { NavLink, useLocation } from 'react-router-dom'

import { api, type User } from './api/client'
import { AppRoutes } from './router'
import { Login } from './views/Login'

const NAV = [
  { to: '/', label: 'Chat' },
  { to: '/workbench', label: 'Workbench' },
  { to: '/runs', label: 'Runs' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/documents', label: 'Documents' },
  { to: '/packs', label: 'Packs' },
  { to: '/harnesses', label: 'Harnesses' },
  { to: '/settings', label: 'Settings' },
]

export default function App() {
  const queryClient = useQueryClient()
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
        onLogout={async () => {
          await api.logout()
          queryClient.clear()
        }}
      />
      <Main />
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

function Sidebar({ user, onLogout }: { user: User; onLogout: () => void }) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        bench<span>_</span>
      </div>
      <nav>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.to === '/'}>
            {n.label}
          </NavLink>
        ))}
      </nav>
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
